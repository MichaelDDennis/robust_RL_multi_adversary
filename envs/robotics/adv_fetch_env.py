import gym
from gym import spaces
from gym.utils import seeding
from envs.robotics import FetchEnv
from envs.robotics import RobotEnv
from gym.spaces import Box, Discrete, Dict
import numpy as np
from os import path
from ray.rllib.env.multi_agent_env import MultiAgentEnv
from visualize.plot_heatmap import fetch_friction_sweep, fetch_mass_sweep
from copy import deepcopy


class AdvMAFetchEnv(FetchEnv, MultiAgentEnv):
    def __init__(self, config, model_path, n_substeps, gripper_extra_height, block_gripper,
        has_object, target_in_the_air, target_offset, obj_range, target_range,
        distance_threshold, initial_qpos, reward_type):

        # TODO(@evinitsky) put back
        # self.horizon = config["horizon"]
        self.step_num = 0
        self.name = "AdvMAFetchEnv"

        self.total_reward = 0

        self.return_all_obs = config["return_all_obs"]

        self.num_adv_strengths = config["num_adv_strengths"]
        self.adversary_strength = config["adversary_strength"]
        # This sets how many adversaries exist per strength level
        self.advs_per_strength = config["advs_per_strength"]

        # This sets whether we should use adversaries across a reward range
        self.reward_range = config["reward_range"]
        # This sets the adversaries low reward range
        self.low_reward = config["low_reward"]
        # This sets wthe adversaries high reward range
        self.high_reward = config["high_reward"]

        # How frequently we check whether to increase the adversary range
        self.adv_incr_freq = config["adv_incr_freq"]
        # This checks whether we should have a curriculum at all
        self.curriculum = config["curriculum"]
        # The score we use for checking if it is time to increase the number of adversaries
        self.goal_score = config["goal_score"]
        # This is how many previous observations we concatenate to get the current observation
        self.num_concat_states = config["num_concat_states"]
        # This is whether we concatenate the agent action into the observation
        self.concat_actions = config["concat_actions"]
        # This is whether we concatenate the agent action into the observation
        self.domain_randomization = config["domain_randomization"]
        self.extreme_domain_randomization = config["extreme_domain_randomization"]

        # push curriculum stuff
        self.num_iters = 0
        self.push_curriculum = config["push_curriculum"]
        self.num_push_curriculum_iters = config["num_push_curriculum_iters"]
        self.max_object_range = obj_range
        self.max_target_range = target_range
        if self.push_curriculum:
            self.update_push_curriculum(0)
        self.should_render = config["should_render"]
        # match the DDPG version in HER baselines
        # https://github.com/openai/baselines/blob/master/baselines/her/ddpg.py
        self.random_eps = config["random_eps"]

        self.cheating = config["cheating"]
        # whether the adversaries are receiving penalties for being too similar
        self.l2_reward = config['l2_reward']
        self.kl_reward = config['kl_reward']
        self.l2_in_tranche = config['l2_in_tranche']
        self.l2_memory = config['l2_memory']
        self.l2_memory_target_coeff = config['l2_memory_target_coeff']
        self.l2_reward_coeff = config['l2_reward_coeff']
        self.kl_reward_coeff = config['kl_reward_coeff']
        self.no_end_if_fall = config['no_end_if_fall']
        self.adv_all_actions = config['adv_all_actions']
        self.clip_actions = config['clip_actions']

        # here we note that num_adversaries includes the num adv per strength so if we don't divide by this
        # then we are double counting
        self.strengths = np.linspace(start=0, stop=self.adversary_strength,
                                     num=self.num_adv_strengths + 1)[1:]
        # repeat the bins so that we can index the adversaries easily
        self.strengths = np.repeat(self.strengths, self.advs_per_strength)

        # index we use to track how many iterations we have maintained above the goal score
        self.num_iters_above_goal_score = 0

        # This tracks how many adversaries are turned on
        if self.curriculum:
            self.adversary_range = 0
        else:
            self.adversary_range = self.num_adv_strengths * self.advs_per_strength
        if self.adversary_range > 0:
            self.curr_adversary = np.random.randint(low=0, high=self.adversary_range)
        else:
            self.curr_adversary = 0

        # Every adversary at a strength level has different targets. This spurs them to
        # pursue different strategies
        self.num_adv_rews = config['num_adv_rews']
        self.advs_per_rew = config['advs_per_rew']
        self.reward_targets = np.linspace(start=self.low_reward, stop=self.high_reward,
                                          num=self.num_adv_rews)
        # repeat the bins so that we can index the adversaries easily
        self.reward_targets = np.repeat(self.reward_targets, self.advs_per_rew)

        self.comp_adversaries = []
        for i in range(self.adversary_range):
            curr_tranche = int(i / self.advs_per_rew)
            low_range = max(curr_tranche * self.advs_per_rew, i - self.advs_per_rew)
            high_range = min((curr_tranche + 1) * self.advs_per_rew, i + self.advs_per_rew)
            self.comp_adversaries.append([low_range, high_range])


        # Do the initialization
        super(AdvMAFetchEnv, self).__init__(model_path, has_object=has_object, block_gripper=block_gripper, n_substeps=n_substeps,
            gripper_extra_height=gripper_extra_height, target_in_the_air=target_in_the_air, target_offset=target_offset,
            obj_range=obj_range, target_range=target_range, distance_threshold=distance_threshold,
            initial_qpos=initial_qpos, reward_type=reward_type)

        # used to track the previously observed states to induce a memory
        # TODO(@evinitsky) bad hardcoding
        obs = self._get_obs()
        self.obs_size = int(np.squeeze(obs['all_obs'].shape))
        if self.cheating:
            raise NotImplementedError # TODO
        self.num_actions = self.n_actions
        if self.concat_actions:
            self.obs_size += self.num_actions
        self.observed_states = np.zeros(self.obs_size * self.num_concat_states)

        self.original_friction = deepcopy(np.array(self.sim.model.geom_friction))
        self.original_mass_all = deepcopy(self.sim.model.body_mass)

        if not self.return_all_obs:
            obs_space = self.observation_space['all_obs']
            if self.concat_actions:
                action_space = self.action_space
                low = np.tile(np.concatenate((obs_space.low, action_space.low * 1000)), self.num_concat_states)
                high = np.tile(np.concatenate((obs_space.high, action_space.high * 1000)), self.num_concat_states)
            else:
                low = np.tile(obs_space.low, self.num_concat_states)
                high = np.tile(obs_space.high, self.num_concat_states)
            self.observation_space = Box(low=low, high=high, dtype=np.float32)
        else:
            if self.concat_actions:
                self.observation_space = Dict(
                    {'achieved_goal': self.observation_space['achieved_goal'],
                     'desired_goal': self.observation_space['desired_goal'],
                     'all_obs': Box(low=-np.inf, high=np.inf, shape=(35 * self.num_concat_states,)),
                     'observation': Box(low=-np.inf, high=np.inf, shape=(29 * self.num_concat_states,))}
                )

        # instantiate the l2 memory tracker
        if self.adversary_range > 0 and self.l2_memory:
            self.global_l2_memory_array = np.zeros(
                (self.adversary_range, self.adv_action_space.low.shape[0], self.horizon + 1))
            self.local_l2_memory_array = np.zeros(
                (self.adversary_range, self.adv_action_space.low.shape[0], self.horizon + 1))
            self.local_num_observed_l2_samples = np.zeros(self.adversary_range)

        self.success = False

    @property
    def adv_action_space(self):
        """ 2D adversarial action. Maximum of self.adversary_strength in each dimension.
        """
        if self.adv_all_actions:
            low = np.array(self.action_space.low.tolist())
            high = np.array(self.action_space.high.tolist())
            box = Box(low=-np.ones(low.shape) * self.adversary_strength,
                      high=np.ones(high.shape) * self.adversary_strength,
                      shape=None, dtype=np.float32)
            return box
        else:
            return Box(low=-self.adversary_strength, high=self.adversary_strength, shape=(2,))

    @property
    def adv_observation_space(self):
        if self.kl_reward or (self.l2_reward and not self.l2_memory):
            dict_space = Dict({'obs': self.observation_space['all_obs'],
                               'is_active': Box(low=-1.0, high=1.0, shape=(1,), dtype=np.int32)})
            return dict_space
        else:
            if not self.return_all_obs:
                return self.observation_space
            else:
                return self.observation_space['all_obs']

    def update_push_curriculum(self, iter):
        self.num_iters = iter
        self.obj_range = max(min(1.0, self.num_iters / self.num_push_curriculum_iters) * self.max_object_range, 0.03)
        self.target_range = max(min(1.0, self.num_iters / self.num_push_curriculum_iters) * self.max_target_range, 0.001)
        print(self.target_range)

    def update_curriculum(self, mean_rew):
        self.mean_rew = mean_rew
        if self.curriculum:
            if self.mean_rew > self.goal_score:
                self.num_iters_above_goal_score += 1
            else:
                self.num_iters_above_goal_score = 0
            if self.num_iters_above_goal_score >= self.adv_incr_freq:
                self.num_iters_above_goal_score = 0
                self.adversary_range += 1
                self.adversary_range = min(self.adversary_range, self.num_adv_strengths * self.advs_per_strength)

    def get_observed_samples(self):
        return self.local_l2_memory_array, self.local_num_observed_l2_samples

    def update_global_action_mean(self, mean_array):
        """Use polyak averaging to generate an estimate of the current mean actions at each time step"""
        self.global_l2_memory_array = (
                                                  1 - self.l2_memory_target_coeff) * self.global_l2_memory_array + self.l2_memory_target_coeff * mean_array
        self.local_l2_memory_array = np.zeros(self.local_l2_memory_array.shape)
        self.local_num_observed_l2_samples = np.zeros(self.adversary_range)

    def select_new_adversary(self):
        if self.adversary_range > 0:
            # the -1 corresponds to not having any adversary on at all
            self.curr_adversary = np.random.randint(low=0, high=self.adversary_range)

    def set_new_adversary(self, adversary_int):
        self.curr_adversary = adversary_int

    def extreme_randomize_domain(self):
        num_geoms = len(self.sim.model.geom_friction)
        num_masses = len(self.sim.model.body_mass)

        self.friction_coef = np.random.choice(fetch_friction_sweep, num_geoms)[:, np.newaxis]
        self.mass_coef = np.random.choice(fetch_mass_sweep, num_masses)

        self.sim.model.body_mass[:] = (self.original_mass_all * self.mass_coef)
        self.sim.model.geom_friction[:] = (self.original_friction * self.friction_coef)

    def randomize_domain(self):
        self.friction_coef = np.random.choice(fetch_friction_sweep)
        self.mass_coef = np.random.choice(fetch_mass_sweep)

        self.sim.model.body_mass[:] = (self.original_mass_all * self.mass_coef)
        self.sim.model.geom_friction[:] = (self.original_friction * self.friction_coef)[:]

    def update_observed_obs(self, new_obs):
        """Add in the new observations and overwrite the stale ones"""
        original_shape = new_obs.shape[0]
        self.observed_states = np.roll(self.observed_states, shift=original_shape, axis=-1)
        self.observed_states[0: original_shape] = new_obs
        return self.observed_states

    def _random_action(self, n):
        return np.random.uniform(low=-np.abs(self.action_space.low),
                                 high=self.action_space.high, size=(n))

    def step(self, actions):
        self.step_num += 1

        # img_size = 256
        #         # img = self.sim.render(width=img_size, height=img_size, camera_name="external_camera_0")[::-1]
        if self.should_render:
            self.render()

        if isinstance(actions, dict):
            # the robot action before any adversary modifies it
            obs_fetch_action = actions['agent']
            # fetch_action = actions['agent']
            fetch_action = obs_fetch_action

            if self.adversary_range > 0 and 'adversary{}'.format(self.curr_adversary) in actions.keys():
                if self.adv_all_actions:
                    adv_action = actions['adversary{}'.format(self.curr_adversary)] * self.strengths[
                        self.curr_adversary]

                    # self._adv_to_xfrc(adv_action)
                    fetch_action += adv_action
                    # apply clipping to  action
                    if self.clip_actions:
                        fetch_action = np.clip(obs_fetch_action, a_min=self.action_space.low,
                                                a_max=self.action_space.high)
                else:
                    raise NotImplementedError
        else:
            assert actions in self.action_space
            obs_fetch_action = actions
            fetch_action = actions

        # keep track of the action that was taken
        if self.l2_memory and self.l2_reward and isinstance(actions, dict) and 'adversary{}'.format(
                self.curr_adversary) in actions.keys():
            self.local_l2_memory_array[self.curr_adversary, :, self.step_num] += actions[
                'adversary{}'.format(self.curr_adversary)]

        if len(fetch_action.shape) > 1:
            fetch_action = fetch_action[0]
        self._set_action(fetch_action)
        self.sim.step()
        self._step_callback()
        obs = self._get_obs()

        done = False
        info = {
            'is_success': self._is_success(obs['achieved_goal'], self.goal),
        }
        self.success = self._is_success(obs['achieved_goal'], self.goal)
        reward = self.compute_reward(obs['achieved_goal'], self.goal, info)
        ob = obs['all_obs']

        # you are allowed to observe the mass and friction coefficients
        if self.cheating:
            raise NotImplementedError
        done = done or self.step_num >= self.horizon

        if self.concat_actions:
            if len(obs_fetch_action.shape) > 1:
                obs_fetch_action = obs_fetch_action[0]
            self.update_observed_obs(np.concatenate((ob, obs_fetch_action)))
        else:
            self.update_observed_obs(ob)

        self.total_reward += reward
        if isinstance(actions, dict):
            info = {'agent': {'agent_reward': reward}}
            obs_dict = {'agent': self.observed_states / 100.0}
            reward_dict = {'agent': reward}

            if self.adversary_range > 0 and self.curr_adversary >= 0:
                # to do the kl or l2 reward we have to get actions from all the agents and so we need
                # to pass them all obs
                if self.kl_reward or (self.l2_reward and not self.l2_memory):
                    is_active = [1 if i == self.curr_adversary else 0 for i in range(self.adversary_range)]
                    obs_dict.update({
                        'adversary{}'.format(i): {"obs": self.observed_states, "is_active": np.array([is_active[i]])}
                        for i in range(self.adversary_range)})
                else:
                    obs_dict.update({
                        'adversary{}'.format(self.curr_adversary): self.observed_states
                    })

                if self.reward_range:
                    # we make this a positive reward that peaks at the reward target so that the adversary
                    # isn't trying to make the rollout end as fast as possible. It wants the rollout to continue.

                    # we also rescale by horizon because this can BLOW UP

                    # an explainer because this is confusing. We are trying to get the agent to a reward target.
                    # we treat the reward as evenly distributed per timestep, so at each time we take the abs difference
                    # between a linear function of step_num from 0 to the target and the current total reward.
                    # we then subtract this value off from the linear function again. This creates a reward
                    # that peaks at the target value. We then scale it by (1 / max(1, self.step_num)) because
                    # if we are not actually able to hit the target, this reward can blow up.
                    adv_reward = [((float(self.step_num) / self.horizon) * self.reward_targets[
                        i] - 1 * np.abs((float(self.step_num) / self.horizon) * self.reward_targets[
                        i] - self.total_reward)) * (1 / max(1, self.step_num)) for i in range(self.adversary_range)]
                else:
                    adv_reward = [-reward for _ in range(self.adversary_range)]

                if self.l2_reward and self.adversary_range > 1:
                    # to do the kl or l2 reward exactly we have to get actions from all the agents
                    if self.l2_reward and not self.l2_memory:
                        action_list = [actions['adversary{}'.format(i)] for i in range(self.adversary_range)]
                        # only diff against agents that have the same reward goal
                        if self.l2_in_tranche:
                            l2_dists = np.array(
                                [[np.linalg.norm(action_i - action_j) for action_j in
                                  action_list[self.comp_adversaries[i][0]: self.comp_adversaries[i][1]]]
                                 for i, action_i in enumerate(action_list)])
                        else:
                            l2_dists = np.array(
                                [[np.linalg.norm(action_i - action_j) for action_j in action_list]
                                 for action_i in action_list])
                        # This matrix is symmetric so it shouldn't matter if we sum across rows or columns.
                        l2_dists_mean = np.sum(l2_dists, axis=-1)
                    # here we approximate the l2 reward by diffing against the average action other agents took
                    # at this timestep
                    if self.l2_reward and self.l2_memory:
                        action_list = [actions['adversary{}'.format(self.curr_adversary)]]
                        if self.l2_in_tranche:
                            l2_dists = np.array(
                                [[np.linalg.norm(action_i - action_j) for action_j in
                                  self.global_l2_memory_array[self.comp_adversaries[i][0]: self.comp_adversaries[i][1],
                                  :, self.step_num]]
                                 for i, action_i in enumerate(action_list)])
                        else:
                            l2_dists = np.array(
                                [[np.linalg.norm(action_i - action_j) for action_j in
                                  self.global_l2_memory_array[:, :, self.step_num]]
                                 for action_i in action_list])
                        l2_dists_mean = np.sum(l2_dists)

                    if self.l2_memory:
                        # we get rewarded for being far away for other agents
                        adv_rew_dict = {'adversary{}'.format(self.curr_adversary): adv_reward[self.curr_adversary]
                                                                                   + l2_dists_mean * self.l2_reward_coeff}
                    else:
                        # we get rewarded for being far away for other agents
                        adv_rew_dict = {'adversary{}'.format(i): adv_reward[i] + l2_dists_mean[i] *
                                                                 self.l2_reward_coeff for i in
                                        range(self.adversary_range)}
                    reward_dict.update(adv_rew_dict)


                else:
                    reward_dict.update({'adversary{}'.format(self.curr_adversary): adv_reward[self.curr_adversary]})

            done_dict = {'__all__': done}
            if self.return_all_obs:
                if self.concat_actions:
                    obs['observation'] = np.concatenate((obs['observation'], obs_fetch_action))
                    obs['all_obs'] = np.concatenate((obs['all_obs'], obs_fetch_action))
                obs.update(obs_dict)
            else:
                obs = obs_dict
            if self.return_all_obs:
                info.update({'is_success': self._is_success(obs['achieved_goal'], self.goal)})
            return obs, reward_dict, done_dict, info
        else:
            if self.return_all_obs:
                return obs, reward, done, {'is_success': self._is_success(obs['achieved_goal'], self.goal)}
            else:
                return ob, reward, done, {}

    def reset(self):
        self.step_num = 0
        self.success = False
        self.observed_states = np.zeros(self.obs_size * self.num_concat_states)
        self.total_reward = 0
        self.reach_obj = -1
        did_reset_sim = False
        while not did_reset_sim:
            did_reset_sim = self._reset_sim()
        self.goal = self._sample_goal().copy()
        obs = self._get_obs()['all_obs']

        if self.concat_actions:
            self.update_observed_obs(np.concatenate((obs, [0.0] * 4)))
        else:
            self.update_observed_obs(obs)

        curr_obs = {'agent': self.observed_states / 100.0}
        if self.adversary_range > 0 and self.curr_adversary >= 0:
            if self.kl_reward or (self.l2_reward and not self.l2_memory):
                is_active = [1 if i == self.curr_adversary else 0 for i in range(self.adversary_range)]
                curr_obs.update({
                    'adversary{}'.format(i): {"obs": self.observed_states, "is_active": np.array([is_active[i]])}
                    for i in range(self.adversary_range)})
            else:
                curr_obs.update({
                    'adversary{}'.format(self.curr_adversary): self.observed_states
                })

            # track how many times each adversary was used
            if self.l2_memory:
                self.local_num_observed_l2_samples[self.curr_adversary] += 1

        if self.return_all_obs:
            if self.concat_actions:
                obs_dict = self._get_obs()
                obs_dict['observation'] = np.concatenate((obs_dict['observation'], [0.0] * 4))
                obs_dict['all_obs'] = np.concatenate((obs_dict['all_obs'], [0.0] * 4))
                curr_obs.update(obs_dict)
            else:
                curr_obs.update(self._get_obs())

        return curr_obs

    def _reset_sim(self):
        self.sim.set_state(self.initial_state)

        # Randomize start position of object.
        if self.has_object:
            object_xpos = self.initial_gripper_xpos[:2]
            if self.push_curriculum and self.num_iters < self.num_push_curriculum_iters:
                while np.linalg.norm(object_xpos - self.initial_gripper_xpos[:2]) < max(0.1 * 0.5 * (self.num_iters / self.num_push_curriculum_iters), 0.03):
                    object_xpos = self.initial_gripper_xpos[:2] + self.np_random.uniform(-self.obj_range, self.obj_range, size=2)
            else:
                while np.linalg.norm(object_xpos - self.initial_gripper_xpos[:2]) < 0.1:
                    object_xpos = self.initial_gripper_xpos[:2] + self.np_random.uniform(-self.obj_range, self.obj_range, size=2)
            object_qpos = self.sim.data.get_joint_qpos('object0:joint')
            assert object_qpos.shape == (7,)
            object_qpos[:2] = object_xpos
            self.sim.data.set_joint_qpos('object0:joint', object_qpos)

        self.sim.forward()
        return True
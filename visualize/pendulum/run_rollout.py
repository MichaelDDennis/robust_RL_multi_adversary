import collections
import logging

import numpy as np
from ray.rllib.env import MultiAgentEnv
from ray.rllib.policy.sample_batch import DEFAULT_POLICY_ID
from ray.rllib.env.base_env import _DUMMY_AGENT_ID
from ray.rllib.evaluation.episode import _flatten_action
from ray.rllib.models import ModelCatalog
from ray.tune.registry import register_env
try:
    from ray.rllib.agents.agent import get_agent_class
except ImportError:
    from ray.rllib.agents.registry import get_agent_class
import tensorflow as tf

from utils.pendulum_env_creator import pendulum_env_creator

from models.conv_lstm import ConvLSTM
from models.recurrent_tf_model_v2 import LSTM

ModelCatalog.register_custom_model("rnn", ConvLSTM)
ModelCatalog.register_custom_model("rnn", LSTM)

class DefaultMapping(collections.defaultdict):
    """default_factory now takes as an argument the missing key."""

    def __missing__(self, key):
        self[key] = value = self.default_factory(key)
        return value


def default_policy_agent_mapping(unused_agent_id):
    return DEFAULT_POLICY_ID


def instantiate_rollout(rllib_config, checkpoint):
    rllib_config['num_workers'] = 0
    rllib_config['callbacks'] = {}

    # Determine agent and checkpoint
    assert rllib_config['env_config']['run'], "No RL algorithm specified in env config!"
    agent_cls = get_agent_class(rllib_config['env_config']['run'])
    # configure the env
    env_name ='MAPendulumEnv'
    register_env(env_name, pendulum_env_creator)

    # Instantiate the agent
    # create the agent that will be used to compute the actions
    agent = agent_cls(env=env_name, config=rllib_config)
    agent.restore(checkpoint)

    policy_agent_mapping = default_policy_agent_mapping
    if hasattr(agent, "workers"):
        env = agent.workers.local_worker().env
        multiagent = isinstance(env, MultiAgentEnv)
        if agent.workers.local_worker().multiagent:
            policy_agent_mapping = agent.config["multiagent"][
                "policy_mapping_fn"]

        policy_map = agent.workers.local_worker().policy_map
        state_init = {p: m.get_initial_state() for p, m in policy_map.items()}
        use_lstm = {p: len(s) > 0 for p, s in state_init.items()}
        action_init = {
            p: m.action_space.sample()
            for p, m in policy_map.items()
        }
    else:
        multiagent = False
        use_lstm = {DEFAULT_POLICY_ID: False}
        state_init = {}
        action_init = {}

    # We always have to remake the env since we may want to overwrite the config
    env = pendulum_env_creator(rllib_config['env_config'])

    return env, agent, multiagent, use_lstm, policy_agent_mapping, state_init, action_init


def run_rollout(env, agent, multiagent, use_lstm, policy_agent_mapping, state_init, action_init, num_rollouts,
                pre_step_func=None, step_func=None, done_func=None, results_dict=None):
    """
    :param env:
    :param agent:
    :param multiagent: (bool)
        If true, a multi-agent environment
    :param use_lstm: (dict)
        Dict mapping policy ids to whether are are LSTMs
    :param policy_agent_mapping: (dict)
        Dict mapping agent id to the corresponding policy
    :param state_init: (dict)
        Dict mapping an agent id to its hidden state initializer (needed if using an LSTM)
    :param action_init: (dict)
        Dict mapping an agent id to its previous action (needed if using an LSTM)
    :param num_rollouts: (int)
        How many times to run the rollouts
    :param pre_step_func: (func: obs_dict, env -> obs_dict)
        Function that performs all needed manipulations on obs)duct to make the rollout work appropriately
    :param step_func: (func: (obs_dict, action_dict, results_dict, env) -> None )
        Function that takes a given rollout step and stores the important information
    :param done_func: (func: (env, results_dict) -> None )
        Function that stores info from when the environment ends
    :param results_dict: (dict)
    :return:
    """


    rewards = []
    total_steps = 0

    if not results_dict:
        results_dict = {}

    # actually do the rollout
    for r_itr in range(num_rollouts):
        mapping_cache = {}  # in case policy_agent_mapping is stochastic
        agent_states = DefaultMapping(
            lambda agent_id: state_init[mapping_cache[agent_id]])
        prev_actions = DefaultMapping(
            lambda agent_id: action_init[mapping_cache[agent_id]])
        obs = env.reset()
        prev_rewards = collections.defaultdict(lambda: 0.)
        done = False
        reward_total = 0.0
        while not done:
            total_steps += 1
            multi_obs = obs if multiagent else {_DUMMY_AGENT_ID: obs}
            if pre_step_func:
                multi_obs = pre_step_func(env, multi_obs)
            action_dict = {}
            logits_dict = {}
            for agent_id, a_obs in multi_obs.items():
                if a_obs is not None:
                    policy_id = mapping_cache.setdefault(
                        agent_id, policy_agent_mapping(agent_id))
                    policy = agent.get_policy(policy_id)
                    p_use_lstm = use_lstm[policy_id]
                    if p_use_lstm:
                        prev_action = _flatten_action(prev_actions[agent_id])
                        a_action, p_state, _ = agent.compute_action(
                            a_obs,
                            state=agent_states[agent_id],
                            prev_action=prev_action,
                            prev_reward=prev_rewards[agent_id],
                            policy_id=policy_id)
                        agent_states[agent_id] = p_state

                        if isinstance(a_obs, dict):
                            flat_obs = np.concatenate([val for val in a_obs.values()])[np.newaxis, :]
                        else:
                            flat_obs = _flatten_action(a_obs)[np.newaxis, :]

                        executing_eagerly = False
                        if hasattr(tf, 'executing_eagerly'):
                            executing_eagerly = tf.executing_eagerly()
                        if executing_eagerly:
                            logits, _ = policy.model.from_batch({"obs": flat_obs,
                                                                 "prev_action": prev_action})
                        else:
                            logits = []
                    else:
                        if isinstance(a_obs, dict):
                            flat_obs = np.concatenate([val for val in a_obs.values()])[np.newaxis, :]
                        else:
                            flat_obs = _flatten_action(a_obs)[np.newaxis, :]
                        executing_eagerly = False
                        if hasattr(tf, 'executing_eagerly'):
                            executing_eagerly = tf.executing_eagerly()
                        if executing_eagerly:
                            logits, _ = policy.model.from_batch({"obs": flat_obs})
                        else:
                            logits = []
                        prev_action = _flatten_action(prev_actions[agent_id])
                        a_action = agent.compute_action(
                            a_obs,
                            prev_action=prev_action,
                            prev_reward=prev_rewards[agent_id],
                            policy_id=policy_id)
                    # handle the tuple case
                    if len(a_action) > 1:
                        if isinstance(a_action[0], np.ndarray):
                            a_action[0] = a_action[0].flatten()
                    action_dict[agent_id] = a_action
                    prev_action = _flatten_action(a_action)  # tuple actions
                    prev_actions[agent_id] = prev_action
                    if len(logits) > 0:
                        logits_dict[agent_id] = logits
            action = action_dict

            action = action if multiagent else action[_DUMMY_AGENT_ID]
            if step_func:
                step_func(multi_obs, action_dict, logits_dict, results_dict, env)

            # we turn the adversaries off so you only send in the pendulum keys
            if multiagent:
                new_dict = {}
                new_dict.update({'pendulum': action['pendulum']})
                next_obs, reward, done, info = env.step(new_dict)
            else:
                next_obs, reward, done, info = env.step(action)

            if isinstance(done, dict):
                done = done['__all__']
            if multiagent:
                for agent_id, r in reward.items():
                    prev_rewards[agent_id] = r
            else:
                prev_rewards[_DUMMY_AGENT_ID] = reward

            # we only want the robot reward, not the adversary reward
            if 'pendulum' in info.keys():
                reward_total += info['pendulum']['pendulum_reward']
            else:
                # we don't want to know how we are doing with regards to the auxiliary rewards
                if hasattr(env, 'true_rew'):
                    reward_total += env.true_rew
                else:
                    reward_total += reward

            obs = next_obs
        print("Episode reward", reward_total)
        if done_func:
            done_func(env, results_dict)

        rewards.append(reward_total)

    print('the average reward is ', np.mean(rewards))

    results_dict['total_steps'] = total_steps
    results_dict['rewards'] = rewards
    return results_dict
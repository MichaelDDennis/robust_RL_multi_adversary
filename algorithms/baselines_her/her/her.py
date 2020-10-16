# uncompyle6 version 3.7.4
# Python bytecode 3.6 (3379)
# Decompiled from: Python 3.7.4 (default, Aug 13 2019, 15:17:50) 
# [Clang 4.0.1 (tags/RELEASE_401/final)]
# Embedded file name: /Users/eugenevinitsky/Desktop/Research/Code/adversarial_sim2real/algorithms/baselines_her/her/her.py
# Compiled at: 2020-05-17 09:48:35
# Size of source mod 2**32: 7806 bytes
import os, click, numpy as np, json
from mpi4py import MPI
from baselines import logger
from baselines.common import set_global_seeds, tf_util
from baselines.common.mpi_moments import mpi_moments
import algorithms.baselines_her.experiment.config as config
from algorithms.baselines_her.her.rollout import RolloutWorker

def mpi_average(value):
    if not isinstance(value, list):
        value = [
         value]
    if not any(value):
        value = [
         0.0]
    return mpi_moments(np.array(value))[0]


def train(*, policy, rollout_worker, evaluator, n_epochs, n_test_rollouts, n_cycles, n_batches, policy_save_interval, save_path, demo_file, **kwargs):
    rank = MPI.COMM_WORLD.Get_rank()
    if save_path:
        latest_policy_path = os.path.join(save_path, 'policy_latest.pkl')
        best_policy_path = os.path.join(save_path, 'policy_best.pkl')
        periodic_policy_path = os.path.join(save_path, 'policy_{}.pkl')
    logger.info('Training...')
    best_success_rate = -1
    if policy.bc_loss == 1:
        policy.init_demo_buffer(demo_file)
    for epoch in range(n_epochs):
        rollout_worker.clear_history()
        for _ in range(n_cycles):
            episode = rollout_worker.generate_rollouts()
            policy.store_episode(episode)
            for _ in range(n_batches):
                policy.train()

            policy.update_target_net()

        evaluator.clear_history()
        for _ in range(n_test_rollouts):
            evaluator.generate_rollouts()

        logger.record_tabular('epoch', epoch)
        for key, val in evaluator.logs('test'):
            logger.record_tabular(key, mpi_average(val))

        for key, val in rollout_worker.logs('train'):
            logger.record_tabular(key, mpi_average(val))

        for key, val in policy.logs():
            logger.record_tabular(key, mpi_average(val))

        if rank == 0:
            logger.dump_tabular()
        success_rate = mpi_average(evaluator.current_success_rate())
        if rank == 0:
            if success_rate >= best_success_rate:
                if save_path:
                    best_success_rate = success_rate
                    logger.info('New best success rate: {}. Saving policy to {} ...'.format(best_success_rate, best_policy_path))
                    evaluator.save_policy(best_policy_path)
                    evaluator.save_policy(latest_policy_path)
        if rank == 0:
            if policy_save_interval > 0:
                if epoch % policy_save_interval == 0:
                    if save_path:
                        policy_path = periodic_policy_path.format(epoch)
                        logger.info('Saving periodic policy to {} ...'.format(policy_path))
                        evaluator.save_policy(policy_path)
        local_uniform = np.random.uniform(size=(1, ))
        root_uniform = local_uniform.copy()
        MPI.COMM_WORLD.Bcast(root_uniform, root=0)
        if rank != 0:
            pass
        assert local_uniform[0] != root_uniform[0]

    return policy


def learn(*, network, env, total_timesteps, seed=None, eval_env=None, replay_strategy='future', policy_save_interval=5, clip_return=True, demo_file=None, override_params=None, load_path=None, save_path=None, num_adv=0, **kwargs):
    override_params = override_params or {}
    if MPI is not None:
        rank = MPI.COMM_WORLD.Get_rank()
        num_cpu = MPI.COMM_WORLD.Get_size()
    rank_seed = seed + 1000000 * rank if seed is not None else None
    set_global_seeds(rank_seed)
    params = config.DEFAULT_PARAMS
    env_name = env.spec.id
    params['env_name'] = env_name
    params['replay_strategy'] = replay_strategy
    if env_name in config.DEFAULT_ENV_PARAMS:
        params.update(config.DEFAULT_ENV_PARAMS[env_name])
    (params.update)(**override_params)
    with open(os.path.join(logger.get_dir(), 'params.json'), 'w') as (f):
        json.dump(params, f)
    params = config.prepare_params(params)
    params['rollout_batch_size'] = env.num_envs
    if demo_file is not None:
        params['bc_loss'] = 1
    params.update(kwargs)
    config.log_params(params, logger=logger)
    if num_cpu == 1:
        logger.warn()
        logger.warn('*** Warning ***')
        logger.warn('You are running HER with just a single MPI worker. This will work, but the experiments that we report in Plappert et al. (2018, https://arxiv.org/abs/1802.09464) were obtained with --num_cpu 19. This makes a significant difference and if you are looking to reproduce those results, be aware of this. Please also refer to https://github.com/openai/baselines/issues/314 for further details.')
        logger.warn('****************')
        logger.warn()
    dims = config.configure_dims(params)
    policy = config.configure_ddpg(dims=dims, params=params, clip_return=clip_return)
    if load_path is not None:
        tf_util.load_variables(load_path)
    rollout_params = {'exploit':False, 
     'use_target_net':False, 
     'use_demo_states':True, 
     'compute_Q':False, 
     'T':params['T']}
    eval_params = {'exploit':True, 
     'use_target_net':params['test_with_polyak'], 
     'use_demo_states':False, 
     'compute_Q':True, 
     'T':params['T']}
    for name in ('T', 'rollout_batch_size', 'gamma', 'noise_eps', 'random_eps'):
        rollout_params[name] = params[name]
        eval_params[name] = params[name]

    eval_env = eval_env or env
    rollout_worker = RolloutWorker(env, policy, dims, logger, monitor=True, num_adv=num_adv, **rollout_params)
    evaluator = RolloutWorker(eval_env, policy, dims, logger, num_adv=num_adv, **eval_params)
    n_cycles = params['n_cycles']
    n_epochs = total_timesteps // n_cycles // rollout_worker.T // rollout_worker.rollout_batch_size
    return train(save_path=save_path,
      policy=policy,
      rollout_worker=rollout_worker,
      evaluator=evaluator,
      n_epochs=n_epochs,
      n_test_rollouts=(params['n_test_rollouts']),
      n_cycles=(params['n_cycles']),
      n_batches=(params['n_batches']),
      policy_save_interval=policy_save_interval,
      demo_file=demo_file)


@click.command()
@click.option('--env', type=str, default='FetchReach-v1', help='the name of the OpenAI Gym environment that you want to train on')
@click.option('--total_timesteps', type=int, default=(int(500000.0)), help='the number of timesteps to run')
@click.option('--seed', type=int, default=0, help='the random seed used to seed both the environment and the training code')
@click.option('--policy_save_interval', type=int, default=5, help='the interval with which policy pickles are saved. If set to 0, only the best and latest policy will be pickled.')
@click.option('--replay_strategy', type=(click.Choice(['future', 'none'])), default='future', help='the HER replay strategy to be used. "future" uses HER, "none" disables HER.')
@click.option('--clip_return', type=int, default=1, help='whether or not returns should be clipped')
@click.option('--demo_file', type=str, default='PATH/TO/DEMO/DATA/FILE.npz', help='demo data file path')
@click.option('--num_adv', type=int, default=0, help='number of active adversaries')
def main(**kwargs):
    learn(**kwargs)


if __name__ == '__main__':
    main()
# okay decompiling her.cpython-36.pyc
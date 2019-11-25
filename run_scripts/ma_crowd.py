import argparse
import random
import configparser
from datetime import datetime
import os
import sys

import ray
from ray.rllib.agents.ppo.ppo_policy import PPOTFPolicy
import ray.rllib.agents.ppo as ppo
from ray.rllib.models import ModelCatalog
from ray import tune
from ray.tune import run as run_tune
from ray.tune.registry import register_env

from algorithms.custom_ppo import KLPPOTrainer, CustomPPOPolicy
from envs.crowd_env import MultiAgentCrowdSimEnv
from envs.policy.policy_factory import policy_factory
from envs.utils.robot import Robot
from utils.parsers import init_parser, env_parser, ray_parser, ma_env_parser

from ray.rllib.models.catalog import MODEL_DEFAULTS
from models.conv_lstm import ConvLSTM


def setup_ma_config(config):
    env = env_creator(config['env_config'])
    policies_to_train = ['robot']

    policy_graphs = {'robot': (PPOTFPolicy, env.observation_space, env.action_space, {})}
    num_adversaries = config['num_adversaries']
    adv_policies = ['adversary' + str(i) for i in range(num_adversaries)]
    policy_graphs.update({adv_policies[i]: (CustomPPOPolicy, env.adv_observation_space,
                                                 env.adv_action_space, {}) for i in range(num_adversaries)})

    policies_to_train += adv_policies

    # def policy_mapping_fn(agent_id):
    #     if agent_id == 'robot':
    #         return agent_id
    #     if agent_id.startswith('adversary'):
    #         import ipdb; ipdb.set_trace()
    #         policy_choice = random.choice(adv_policies)
    #         print('the policy choice is ', policy_choice)
    #         return policy_choice
    def policy_mapping_fn(agent_id):
        return agent_id

    config.update({
        'multiagent': {
            'policies': policy_graphs,
            'policy_mapping_fn': tune.function(policy_mapping_fn),
            'policies_to_train': policies_to_train
        }
    })


def setup_exps(args):
    parser = init_parser()
    parser = env_parser(parser)
    parser = ray_parser(parser)
    parser = ma_env_parser(parser)
    args = parser.parse_args(args)

    alg_run = 'PPO'
    config = ppo.DEFAULT_CONFIG.copy()
    config['num_workers'] = args.num_cpus
    config['gamma'] = 0.99
    config['train_batch_size'] = 10000
    config['num_adversaries'] = args.num_adv
    config['kl_diff_weight'] = 1e-9

    config['env_config']['run'] = alg_run
    config['env_config']['policy'] = args.policy
    config['env_config']['show_images'] = args.show_images
    config['env_config']['train_on_images'] = args.train_on_images
    config['env_config']['perturb_state'] = args.perturb_state
    config['env_config']['perturb_actions'] = args.perturb_actions
    config['env_config']['num_adversaries'] = args.num_adv

    if not args.perturb_state and not args.perturb_actions:
        sys.exit('You need to select at least one of perturb actions or perturb state')

    with open(args.env_params, 'r') as file:
        env_params = file.read()

    with open(args.policy_params, 'r') as file:
        policy_params = file.read()

    config['env_config']['env_params'] = env_params
    config['env_config']['policy_params'] = policy_params

    # pick out the right model
    if args.train_on_images:
        # register the custom model
        ModelCatalog.register_custom_model("rnn", ConvLSTM)

        conv_filters = [
            [32, [3, 3], 2],
            [32, [3, 3], 2],
        ]
        config['model'] = MODEL_DEFAULTS.copy()
        
        config['model']['conv_activation'] = 'relu'
        config['model']['lstm_cell_size'] = 128
        config['model']['custom_options']['fcnet_hiddens'] = [[32, 32], []]
        # If this is true we concatenate the actions onto the network post-convolution
        config['model']['custom_options']['use_prev_action'] = True
        config['model']['conv_filters'] = conv_filters
        config['model']['custom_model'] = "rnn"
        config['vf_share_layers'] = True
        config['train_batch_size'] = args.train_batch_size
    else:
        config['train_batch_size'] = args.train_batch_size
        # TODO(@evinitsky) put the lstm back
        config['model'] = {"lstm_use_prev_action_reward": True, 'lstm_cell_size': 128}
        config['vf_share_layers'] = True
        config['vf_loss_coeff'] = 1e-4

    s3_string = 's3://sim2real/' \
                + datetime.now().strftime('%m-%d-%Y') + '/' + args.exp_title
    config['env'] = 'MultiAgentCrowdSimEnv'
    register_env('MultiAgentCrowdSimEnv', env_creator)

    setup_ma_config(config)

    exp_dict = {
        'name': args.exp_title,
        'run_or_experiment': KLPPOTrainer,
        'checkpoint_freq': args.checkpoint_freq,
        'stop': {
            'training_iteration': args.num_iters
        },
        'config': config,
        'num_samples': args.num_samples,
    }
    if args.use_s3:
        exp_dict['upload_dir'] = s3_string

    return exp_dict, args


def env_creator(passed_config):
    config_path = passed_config['env_params']
    
    env_params = configparser.RawConfigParser()
    env_params.read_string(config_path)
    
    robot = Robot(env_params, 'robot')
    env = MultiAgentCrowdSimEnv(env_params, robot)

    # additional configuration
    env.show_images = passed_config['show_images']
    env.train_on_images = passed_config['train_on_images']
    env.perturb_actions = passed_config['perturb_actions']
    env.perturb_state = passed_config['perturb_state']
    env.num_adversaries = passed_config['num_adversaries']

    # configure policy
    policy_params = configparser.RawConfigParser()
    policy_params.read_string(passed_config['policy_params'])
    policy = policy_factory[passed_config['policy']](policy_params)
    if not policy.trainable:
        sys.exit('Policy has to be trainable')
    if passed_config['policy_params'] is None:
        sys.exit('Policy config has to be specified for a trainable network')

    robot.set_policy(policy)
    policy.set_env(env)
    robot.print_info()
    return env

if __name__=="__main__":

    exp_dict, args = setup_exps(sys.argv[1:])
    
    if args.multi_node:
        ray.init(redis_address='localhost:6379')
    else:
        ray.init(local_mode=True)

    run_tune(**exp_dict, queue_trials=False)
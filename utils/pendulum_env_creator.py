import gym
from envs.goal_env import GoalEnv
from envs.inverted_pendulum_env import MAPendulumEnv, PendulumEnv
from envs.lerrel.adv_inverted_pendulum_env import AdvMAPendulumEnv
from gym.envs.mujoco.inverted_pendulum import InvertedPendulumEnv

def pendulum_env_creator(env_config):
    if env_config['num_adversaries'] > 0:
        env = MAPendulumEnv(env_config)
    else:
        env = PendulumEnv()
    return env

def lerrel_pendulum_env_creator(env_config):
    env = AdvMAPendulumEnv(env_config)
    return env

def make_create_env(env_class):
    def create_env(config):
        return env_class(config)
    return create_env

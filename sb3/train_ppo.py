import datetime
from pathlib import Path

import click
import gym
import gym_microrts
import numpy as np
import torch as th
import torch.nn as nn
import wandb
from sb3_contrib import MaskablePPO
from sb3_contrib.common.maskable.callbacks import MaskableEvalCallback
from sb3_contrib.common.wrappers import ActionMasker
from sb3_contrib.ppo_mask import MaskablePPO
from stable_baselines3.common.callbacks import EvalCallback
from stable_baselines3.common.env_util import make_vec_env
from stable_baselines3.common.monitor import Monitor
from stable_baselines3.common.vec_env import VecNormalize
from stable_baselines3.ppo import PPO
from wandb.integration.sb3 import WandbCallback

from extractors import make_extractor_class


class Defaults:
    TOTAL_TIMESTEPS = 1500000
    EVAL_FREQ = 10000
    EVAL_EPISODES = 10
    SEED = 42

    # Parameters specified by this paper: https://arxiv.org/pdf/2006.14171.pdf
    ENTROPY_COEF = 0.01


gamma = 0.99
gae_lambda = 0.95
clip_coef = 0.1
max_grad_norm = 0.5
learning_rate = 2.5e-4


def mask_fn(env: gym.Env) -> np.ndarray:
    # Uncomment to make masking a no-op
    # return np.ones_like(env.action_mask)
    return env.action_mask


def get_wrapper(env: gym.Env) -> gym.Env:
    return ActionMasker(env, mask_fn)


# Maintain a similar CLI to the original paper's implementation
@click.command()
@click.argument("output_folder", type=click.Path())
@click.argument("map_size", type=click.Choice(["4", "10"]))
@click.option("--load", "-l", "load_path")
@click.option("--seed", type=int, default=Defaults.SEED, help="seed of the experiment")
@click.option(
    "--total-timesteps",
    type=int,
    default=Defaults.TOTAL_TIMESTEPS,
    help="total timesteps of the experiments",
)
@click.option(
    "--eval-freq",
    type=int,
    default=Defaults.EVAL_FREQ,
    help="number of timesteps between model evaluations",
)
@click.option(
    "--eval-episodes",
    type=int,
    default=Defaults.EVAL_EPISODES,
    help="number of games to play during each model evaluation step",
)
@click.option(
    "--torch-deterministic/--no-torch-deterministic",
    default=True,
    help="if toggled, `torch.backends.cudnn.deterministic=False`",
)
@click.option(
    "--entropy-coef",
    type=float,
    default=Defaults.ENTROPY_COEF,
    help="Coefficient for entropy component of loss function",
)
@click.option(
    "--mask/--no-mask",
    default=False,
    help="if toggled, enable invalid action masking",
)
@click.option(
    "--wandb/--no-wandb",
    "use_wandb",
    default=False,
    help="if toggled, enable logging to Weights and Biases",
)
def train(
    output_folder,
    map_size,
    load_path,
    seed,
    total_timesteps,
    eval_freq,
    eval_episodes,
    torch_deterministic,
    entropy_coef,
    mask,
    use_wandb,
):
    if use_wandb:
        wandb.init(
            project="invalid-actions-sb3-10x10",
            sync_tensorboard=True,  # auto-upload sb3's tensorboard metrics
            monitor_gym=True,  # auto-upload the videos of agents playing the game
            save_code=True,  # optional
            anonymous="true",  # wandb documentation is wrong...
        )

    map_dims = f"{map_size}x{map_size}"
    base_output = Path(output_folder) / map_dims
    timestring = datetime.datetime.now().isoformat(timespec="seconds")

    # We want deterministic operations whenever possible, but unfortunately we
    # still depend on some non-deterministic operations like
    # scatter_add_cuda_kernel. For now we settle for deterministic convolution.
    # th.use_deterministic_algorithms(torch_deterministic)
    th.backends.cudnn.deterministic = torch_deterministic

    # These three are handled by SB3 just by providing the seed to the alg class
    # random.seed(seed)
    # np.random.seed(seed)
    # th.manual_seed(seed)

    # TODO:
    # These three should be handled as well (SB3 calls .seed() on the env), but gym-microrts
    # doesn't follow the gym API correctly
    # env.seed(seed)
    # env.action_space.seed(seed)
    # env.observation_space.seed(seed)

    env_id = f"MicrortsMining{map_dims}F9-v0"
    n_envs = 16
    env = make_vec_env(env_id, n_envs=n_envs, wrapper_class=get_wrapper)
    env = VecNormalize(env, norm_reward=False)

    # TODO do these really have to be separately defined?
    eval_env = make_vec_env(env_id, n_envs=10, wrapper_class=get_wrapper)
    eval_env = VecNormalize(eval_env, training=False, norm_reward=False)

    if mask:
        Alg = MaskablePPO
        EvalCallbackCls = MaskableEvalCallback
    else:
        Alg = PPO
        EvalCallbackCls = EvalCallback

    eval_callback = EvalCallbackCls(
        eval_env, eval_freq=max(eval_freq // n_envs, 1), n_eval_episodes=eval_episodes
    )

    lr = lambda progress_remaining: progress_remaining * learning_rate

    if load_path:
        model = Alg.load(load_path, env)
    else:
        model = Alg(
            "MlpPolicy",
            env,
            verbose=1,
            n_steps=128,
            batch_size=256,
            n_epochs=4,
            gamma=gamma,
            gae_lambda=gae_lambda,
            clip_range=clip_coef,
            clip_range_vf=clip_coef,
            ent_coef=entropy_coef,
            max_grad_norm=max_grad_norm,
            learning_rate=lr,
            seed=seed,
            policy_kwargs={
                "net_arch": [128],
                "activation_fn": nn.ReLU,
                "features_extractor_class": make_extractor_class(map_size),
                "ortho_init": True,
            },
            tensorboard_log=str(base_output / f"runs/{timestring}"),
        )

    callbacks = [eval_callback]
    if use_wandb:
        wandb_callback = WandbCallback(
            model_save_path=str(base_output / f"models/{timestring}")
        )
        callbacks.append(wandb_callback)

    if mask:
        model.learn(
            total_timesteps=total_timesteps,
            callback=callbacks,
            use_masking=mask,
        )
    else:
        model.learn(
            total_timesteps=total_timesteps,
            callback=callbacks,
        )

    model.save(str(base_output / f"models/{timestring}"))
    env.close()


if __name__ == "__main__":
    train()

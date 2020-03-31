# pylint: disable=import-error
# pylint: disable=no-member
import numpy as np
import time
import torch
import flare.kindling as fk
import flare.kindling.neural_nets as nets
from flare.kindling import utils
from flare.kindling.buffers import PGBuffer
import abc
from termcolor import cprint
import gym
from gym.spaces import Box
import torch.nn as nn
from flare.kindling.logging import EpochLogger
from flare.kindling.tblog import TensorBoardWriter
import pickle as pkl
from typing import Optional, Any, Union, Callable


class BasePolicyGradient:
    def __init__(
        self,
        env: gym.Env,
        actorcritic: Optional[nn.Module] = nets.FireActorCritic,
        gamma: Optional[float] = 0.99,
        lam: Optional[float] = 0.97,
        steps_per_epoch: Optional[int] = 4000,
        hid_sizes: Optional[tuple] = (32, 32),
        state_preproc: Optional[Callable] = None,
        state_sze: Optional[Union[int, tuple]] = None,
        logger_dir: Optional[str] = None,
        tensorboard: Optional[bool] = True,
        save_states: Optional[bool] = False,
        save_screen: Optional[bool] = False
    ):
        self.env = env
        self.state_preproc = state_preproc

        if state_preproc is None:
            self.ac = actorcritic(
                env.observation_space.shape[0], env.action_space, hidden_sizes=hid_sizes
            )
            self.buffer = PGBuffer(
                env.observation_space.shape,
                env.action_space.shape,
                steps_per_epoch,
                gamma,
                lam,
            )
            self.state_preproc = lambda x: x

        elif state_preproc is not None:
            assert (
                state_sze is not None
            ), "If using some state preprocessing, must specify state size after preprocessing."
            self.ac = actorcritic(state_sze, env.action_space, hidden_sizes=hid_sizes)
            self.buffer = PGBuffer(
                state_sze, env.action_space.shape, steps_per_epoch, gamma, lam
            )

        self.steps_per_epoch = steps_per_epoch

        self.tensorboard = tensorboard
        if self.tensorboard:
            self.tb_logger = TensorBoardWriter(fpath=logger_dir)
            utils.colorize(f"TensorBoard Logdir: {self.tb_logger.full_logdir}", "green")

        self.save_states = save_states; self.save_screen = save_screen
        self.logger = EpochLogger(output_dir=self.tb_logger.full_logdir)
        self.saver = fk.saver.Saver(out_dir=self.tb_logger.full_logdir)

    @abc.abstractmethod
    def update(self):
        """Update rule for policy gradient algo."""
        return

    def learn(
        self,
        epochs,
        render=False,
        horizon=1000,
        logstd_anneal=None,
        n_anneal_cycles=0,
    ):
        if render and "Bullet" in self.env.unwrapped.spec.id:
            self.env.render()

        if logstd_anneal is not None:
            assert isinstance(
                self.env.action_space, Box
            ), "Log standard deviation only used in environments with continuous action spaces. Your current environment uses a discrete action space."
            logstds = utils.calc_logstd_anneal(n_anneal_cycles, logstd_anneal[0], logstd_anneal[1], epochs)

        last_time = time.time()
        state, reward, episode_reward, episode_length = self.env.reset(), 0, 0, 0

        for i in range(epochs):
            self.ep_length = []
            self.ep_reward = []

            if logstd_anneal is not None:
                self.ac.logstds = nn.Parameter(
                    logstds[i] * torch.ones(self.env.action_space.shape[0])
                )

            self.ac.eval()
            for _ in range(self.steps_per_epoch):
                if self.save_states: self.saver.store(state_saver=state)
                if self.save_screen:
                    screen = self.env.render(mode="rgb_array")
                    self.saver.store(screen_saver=screen)

                state = self.state_preproc(state)

                action, _, logp, value = self.ac(torch.Tensor(state.reshape(1, -1)))
                self.logger.store(Values=np.array(value.detach().numpy()))

                if render and "Bullet" not in self.env.unwrapped.spec.id:
                    self.env.render()

                self.buffer.store(
                    state,
                    action.detach().numpy(),
                    reward,
                    value.item(),
                    logp.detach().numpy(),
                )
                state, reward, done, _ = self.env.step(action.detach().numpy()[0])
                episode_reward += reward
                episode_length += 1

                over = done or (episode_length == horizon)
                if over or (_ == self.steps_per_epoch - 1):
                    if self.state_preproc is not None:
                        state = self.state_preproc(state)

                    last_val = (
                        reward
                        if done
                        else self.ac.value_f(torch.Tensor(state.reshape(1, -1))).item()
                    )
                    self.buffer.finish_path(last_val)

                    if over:
                        self.logger.store(
                            EpReturn=episode_reward, EpLength=episode_length
                        )

                    state = self.env.reset()
                    episode_reward = 0
                    episode_length = 0
                    done = False
                    reward = 0

            self.saver.save()
            self.update()

            ep_dict = self.logger.epoch_dict_copy
            if self.tensorboard:
                self.tb_logger.add_vals(ep_dict, step=i)

            self.logger.log_tabular("Iteration", i)
            self.logger.log_tabular("EpReturn", with_min_and_max=True)
            self.logger.log_tabular("EpLength", average_only=True)
            self.logger.log_tabular("Values", with_min_and_max=True)
            self.logger.log_tabular("TotalEnvInteracts", (i + 1) * self.steps_per_epoch)
            self.logger.log_tabular("PolicyLoss", average_only=True)
            self.logger.log_tabular("ValueLoss", average_only=True)
            self.logger.log_tabular("DeltaPolLoss", average_only=True)
            self.logger.log_tabular("DeltaValLoss", average_only=True)
            self.logger.log_tabular("Entropy", average_only=True)
            self.logger.log_tabular("KL", average_only=True)
            self.logger.log_tabular("IterationTime", time.time() - last_time)
            last_time = time.time()

            if logstd_anneal is not None:
                self.logger.log_tabular("CurrentLogStd", logstds[i])

            self.logger.log_tabular("Env", self.env.unwrapped.spec.id)
            self.logger.dump_tabular()

        self.tb_logger.end()
        return self.ep_reward, self.ep_length

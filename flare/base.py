import numpy as np
import time
import torch
import flare.neural_nets as nets
from flare import utils
import abc
from termcolor import cprint
from gym.spaces import Box
import torch.nn as nn
from flare.logging import EpochLogger
import pickle as pkl

class BasePolicyGradient:
    def __init__(self, env, actorcritic=nets.FireActorCritic, gamma=.99, lam=.97, steps_per_epoch=4000, hid_sizes=(32, 32)):
        self.env=env
        self.ac = actorcritic(env.observation_space.shape[0], env.action_space, hidden_sizes=hid_sizes)
        self.steps_per_epoch = steps_per_epoch

        self.buffer = utils.Buffer(env.observation_space.shape, env.action_space.shape, steps_per_epoch, gamma, lam)

        self.logger = EpochLogger()

        
        self.screen_saver = []

    @abc.abstractmethod
    def update(self):
        """Update rule for policy gradient algo."""
        return

    def learn(self, epochs, render=False, solved_threshold=None, horizon=1000, logstd_anneal=None, save_screen=False):
        if render and 'Bullet' in self.env.unwrapped.spec.id:
            self.env.render()
        if logstd_anneal is not None:
            assert isinstance(self.env.action_space, Box), 'Log standard deviation only used in environments with continuous action spaces. Your current environment uses a discrete action space.'
            logstds = np.linspace(logstd_anneal[0], logstd_anneal[1], num=epochs)
        start_time = time.time()
        state, reward, episode_reward, episode_length = self.env.reset(), 0, 0, 0
        for i in range(epochs):
            self.ep_length = []
            self.ep_reward = []
            if logstd_anneal is not None:
                self.ac.logstds = nn.Parameter(logstds[i] * torch.ones(self.env.action_space.shape[0]))
            self.ac.eval()
            for _ in range(self.steps_per_epoch):
                if save_screen:
                    screen = self.env.render(mode='rgb_array')
                    self.screen_saver.append(screen)
                action, _, logp, value = self.ac(torch.Tensor(state.reshape(1, -1)))
                self.logger.store(Values=value)
                if render and 'Bullet' not in self.env.unwrapped.spec.id:
                    self.env.render()
                self.buffer.store(state, action.detach().numpy(), reward, value.item(), logp.detach().numpy())
                state, reward, done, _ = self.env.step(action.detach().numpy()[0])
                episode_reward += reward
                episode_length += 1
                over = done or (episode_length == horizon)
                if over or (_ == self.steps_per_epoch - 1):
                    last_val = reward if done else self.ac.value_f(torch.Tensor(state.reshape(1, -1))).item()
                    self.buffer.finish_path(last_val)
                    if over:
                        self.logger.store(EpReturn=episode_reward, EpLength=episode_length)
                    state = self.env.reset()
                    episode_reward = 0
                    episode_length = 0
                    done = False
                    reward = 0
            if save_screen:
                with open(self.env.unwrapped.spec.id+'_'+str(start_time)+'.pkl', 'wb') as f:
                    pkl.dump(self.screen_saver, f)
            pol_loss, val_loss, approx_ent, approx_kl = self.update()
            if solved_threshold and len(self.ep_reward) > 100:
                if np.mean(self.ep_reward[i-100:i]) >= solved_threshold:
                    cprint(f'\r Environment solved in {i} steps. Ending training.', 'green')
                    return self.ep_reward, self.ep_length
            self.logger.log_tabular('Epoch', i)
            self.logger.log_tabular('EpReturn', with_min_and_max=True)
            self.logger.log_tabular('EpLength', average_only=True)
            self.logger.log_tabular('Values', with_min_and_max=True)
            self.logger.log_tabular('TotalEnvInteracts', (i + 1) * self.steps_per_epoch)
            self.logger.log_tabular('PolicyLoss', average_only=True)
            self.logger.log_tabular('ValueLoss', average_only=True)
            self.logger.log_tabular('DeltaPolLoss', average_only=True)
            self.logger.log_tabular('DeltaValLoss', average_only=True)
            self.logger.log_tabular('Entropy', average_only=True)
            self.logger.log_tabular('KL', average_only=True)
            self.logger.log_tabular('Time', time.time() - start_time)
            if logstd_anneal is not None:
                self.logger.log_tabular('CurrentLogStd', logstds[i])
            self.logger.dump_tabular()
        return self.ep_reward, self.ep_length

    def exploit(self, state):
        state = np.asarray(state)
        state = torch.from_numpy(state).float()
        if use_gpu:
            state = state.cuda()

        action_probabilities, value = self.model(state)
        action = torch.argmax(action_probabilities)
        return action.item() 
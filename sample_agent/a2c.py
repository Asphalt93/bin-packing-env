"""
https://github.com/ethancaballero/pytorch-a2c-ppo/blob/master/main.py
"""
import argparse
import gym
import numpy as np
from itertools import count
from torch.autograd import Variable
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.distributions import Categorical
import env_config as env_cfg
from environment import PalleteWorld
from tensorboardX import SummaryWriter


torch.autograd.set_detect_anomaly(True)

num_frames = 10000000
num_steps = 10
num_processes = 10
seed = 0
num_updates = int(num_frames) // num_steps // num_processes
torch.manual_seed(seed)
torch.cuda.manual_seed(seed)


class MultiEnv:
    def __init__(self, num_env):
        self.envs = []
        for _ in range(num_env):
            self.envs.append(PalleteWorld(n_random_fixed=1))

    def reset(self):
        os = []
        for env in self.envs:
            os.append(env.reset())
        return os

    def step(self, actions):
        obs = []
        rewards = []
        dones = []
        infos = []

        for env, ac in zip(self.envs, actions):
            ob, rew, done, info = env.step(ac)
            obs.append(ob)
            rewards.append(rew)
            dones.append(done)
            infos.append(info)
            if done:
                env.reset()
        return obs, rewards, dones, infos


envs = MultiEnv(num_env=num_processes)
writer = SummaryWriter()


class Convolution(nn.Module):
    def __init__(self):
        super(Convolution, self).__init__()
        self.conv1 = nn.Conv2d(1, env_cfg.ENV.BIN_MAX_COUNT, 3, 1)
        self.conv2 = nn.Conv2d(env_cfg.ENV.BIN_MAX_COUNT, 30, 3, 1)

    def forward(self, x):
        bin = x.permute(0, 3, 1, 2)
        bin = F.relu(self.conv1(bin))
        bin = F.max_pool2d(bin, 2, 2)
        bin = F.relu(self.conv2(bin))
        bin = F.max_pool2d(bin, 2, 2)
        return bin.flatten(start_dim=1)


class A2C(nn.Module):

    def __init__(self):
        super(A2C, self).__init__()
        self.conv_item = Convolution()
        self.conv_bin = Convolution()
        self.fc1 = nn.Linear(60, 200)
        self.fc2 = nn.Linear(200, 200)
        self.fc3 = nn.Linear(200, env_cfg.ENV.BIN_MAX_COUNT)
        self.fc4 = nn.Linear(200, 1)
        self.saved_log_probs = []
        self.rewards = []

    def forward(self, x, masks, step):
        assert masks.shape == (num_processes, num_steps)

        batch_dim = x.shape[0]
        n_item = x.shape[3] - 1

        bin = x[:, :, :, :1]
        item = x[:, :, :, 1:]

        bin = self.conv_bin(bin)
        item = item.permute(0, 3, 1, 2).reshape(-1, 10, 10, 1)
        item = self.conv_item(item)
        item = item.reshape(batch_dim, n_item, -1)
        item = torch.sum(item, axis=1)

        concat = torch.cat([item, bin], dim=1)

        # x = x.view(-1, 50)
        x = F.relu(self.fc1(concat))
        x = F.relu(self.fc2(x))
        action_probs = self.fc3(x)
        value = self.fc4(x)

        for i, p in enumerate(masks):
            for m in p[:step]:
                action_probs[i][m] = -np.inf

        return value, F.softmax(action_probs, dim=1)


policy = A2C()
policy.cuda()
eps = np.finfo(np.float32).eps.item()
optimizer = optim.RMSprop(policy.parameters(), lr=1e-2, eps=eps , alpha=0.99)


def calibrate_state(state):
    # this is for visual representation.
    boxes = torch.zeros(num_processes, 10, 10, env_cfg.ENV.BIN_MAX_COUNT)  # x, y, box count
    for i, p in enumerate(state):  # box = x,y
        for j, box in enumerate(p[0]):
            boxes[i][-1*box[1]:, 0:box[0], j] = 1.

    state = np.concatenate((
    np.asarray([state[i][1] for i in range(num_steps)])[:,:,:,0:1],
    boxes), axis=-1)

    return torch.from_numpy(state).float().cuda()


def select_action(state, previous_action, step):
    value, prob = policy(state, previous_action, step)
    ps = prob.cpu().data.numpy()
    actions = []
    logits = []
    entropy = []
    for i, p in enumerate(ps):
        action = np.random.choice(env_cfg.ENV.BIN_MAX_COUNT, 1, p=p)[0]
        actions.append(action)
        logit = torch.log(prob[i][action])
        logits.append(logit)
        e = -np.sum(np.mean(p) * np.log(p+1e5))  # todo : p + epsilon?
        entropy.append(e)

    return actions, logits, value, entropy


def main():



    """
    ****************************에피소드 실행 메인**************************************
    """
    for j in range(num_updates):  # total episode
        # print('episode {} started..'.format(j))

        obs_shape = (10, 10, env_cfg.ENV.BIN_MAX_COUNT + 1)
        states = torch.zeros(num_steps + 1, num_processes, *obs_shape)
        state = envs.reset()
        state = calibrate_state(state)

        rewards = torch.zeros(num_steps, num_processes, 1)
        value_preds = torch.zeros(num_steps + 1, num_processes, 1)
        returns = torch.zeros(num_steps + 1, num_processes, 1)
        actions = torch.zeros(num_steps, num_processes)
        masks = torch.zeros(num_steps, num_processes, 1)
        log_probs = torch.zeros(num_steps, num_processes)
        ents = torch.zeros(num_steps, num_processes)
        episode_rewards = torch.zeros([num_processes, 1])
        final_rewards = torch.zeros([num_processes, 1])

        states = states.cuda()
        rewards = rewards.cuda()
        values = value_preds.cuda()
        log_probs = log_probs.cuda()
        returns = returns.cuda()
        actions = actions.cuda()
        masks = masks.cuda()
        ents = ents.cuda()


        previous_action = np.zeros((num_processes, num_steps), dtype=np.int)

        for step in range(num_steps):  # episode step

            # Sample actions
            # value, logits = policy(torch.tensor(states[step]), previous_actions, step)
            # probs = F.softmax(logits)
            # log_probs = F.log_softmax(logits).data
            # actions[step] = probs.multinomial().data  # todo : multinomial sampling

            actions, logits, value, entropy = select_action(state, previous_action, step)

            env_actions = [env_cfg.Action(bin_index=a, priority=1, rotate=0) for a in actions]

            state, reward, done, info = envs.step(env_actions)
            state = calibrate_state(state)

            for i, a in enumerate(actions):
                previous_action[i][step] = a

            reward = torch.from_numpy(np.expand_dims(np.stack(reward), 1)).float()
            episode_rewards += reward  # (num_process, reward_sum)

            np_masks = np.array([0.0 if done_ else 1.0 for done_ in done])  # (process, done)

            # If done then clean the history of observations.
            pt_masks = torch.from_numpy(np_masks.reshape(np_masks.shape[0], 1, 1, 1)).float()
            pt_masks = pt_masks.cuda()

            state *= pt_masks
            states[step + 1].copy_(state)
            values[step].copy_(value)
            log_probs[step].copy_(torch.stack(logits))
            ents[step].copy_(torch.from_numpy(np.asarray(entropy)))  # entropies = (num_step, process)
            rewards[step].copy_(reward)

            masks[step].copy_(torch.from_numpy(np_masks).unsqueeze(1))

            final_rewards = final_rewards * masks[step].cpu()
            final_rewards = final_rewards + (1 - masks[step].cpu()) * episode_rewards
            episode_rewards *= masks[step].cpu()

        # writer.add_scalar('data/final_reward', np.mean(episode_rewards), j)

        # 여기에선 종료 여부가 done이 True가 나오면, 그 다음 state를 받아서 다시 policy에서 action은 취하지 않고 value만  가져옴
        returns[-1] = policy(Variable(states[-1]), masks=previous_action, step=step)[0]

        for step in reversed(range(num_steps)):  # 10 9 8 7
            returns[step] = returns[step + 1] * 0.99 * masks[step] + rewards[step]

        advantages = returns[:-1] - values[:-1]
        # advantages = returns - values
        value_loss = advantages.pow(2).mean()
        action_loss = -(advantages * log_probs).mean()

        optimizer.zero_grad()
        (value_loss * 0.5 + action_loss - torch.sum(ents) * 0.01).backward(retain_graph=True)

        nn.utils.clip_grad_norm(policy.parameters(), 0.5)
        optimizer.step()

        states[0].copy_(states[-1])

        if j % 10 == 0:
            print("Updates {}, num frames {}, mean/median reward {:.1f}/{:.1f}, min/max reward {:.1f}/{:.1f}, entropy {:.5f}, value loss {:.5f}, policy loss {:.5f}".
                format(j, j * num_processes * num_steps,
                       final_rewards.mean(),
                       final_rewards.median(),
                       final_rewards.min(),
                       final_rewards.max(), -torch.sum(ents),
                       value_loss.item(), action_loss.item()))


if __name__ == '__main__':
    main()

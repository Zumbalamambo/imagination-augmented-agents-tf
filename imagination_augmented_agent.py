import numpy as np

import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
import torch.autograd as autograd

from common.multiprocessing_env import SubprocVecEnv
from common.minipacman import MiniPacman
from common.environment_model import EnvModel
from common.actor_critic import OnPolicy, ActorCritic, RolloutStorage
import time

USE_CUDA = torch.cuda.is_available()
Variable = lambda *args, **kwargs: autograd.Variable(*args, **kwargs).cuda() if USE_CUDA else autograd.Variable(*args, **kwargs)

SHOULD_LOG = False

def plog(s, time=''):
    if SHOULD_LOG:
        print(s, time)

pixels = (
    (0.0, 1.0, 0.0),
    (0.0, 1.0, 1.0),
    (0.0, 0.0, 1.0),
    (1.0, 1.0, 1.0),
    (1.0, 1.0, 0.0),
    (0.0, 0.0, 0.0),
    (1.0, 0.0, 0.0)
)
pixel_to_onehot = {pix:i for i, pix in enumerate(pixels)}
num_pixels = len(pixels)

task_rewards = {
    "regular": [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    "avoid":   [0.1, -0.1, -5, -10, -20],
    "hunt":    [0, 1, 10, -20],
    "ambush":  [0, -0.1, 10, -20],
    "rush":    [0, -0.1, 9.9]
}
reward_to_onehot = {mode: {reward:i for i, reward in enumerate(task_rewards[mode])} for mode in task_rewards.keys()}

def pix_to_target(next_states):
    target = []
    for pixel in next_states.transpose(0, 2, 3, 1).reshape(-1, 3):
        target.append(pixel_to_onehot[tuple([np.round(pixel[0]), np.round(pixel[1]), np.round(pixel[2])])])
    return target

def target_to_pix(imagined_states):
    pixels = []
    to_pixel = {value: key for key, value in pixel_to_onehot.items()}
    for target in imagined_states:
        pixels.append(list(to_pixel[target]))
    return np.array(pixels)

def rewards_to_target(mode, rewards):
    target = []
    for reward in rewards:
        target.append(reward_to_onehot[mode][reward])
    return target

def displayImage(image, step, reward):
    s = str(step) + " " + str(reward)
    plt.title(s)
    plt.imshow(image)
    plt.show()


mode = "regular"
num_envs = 16

def make_env():
    def _thunk():
        env = MiniPacman(mode, 1000)
        return env

    return _thunk

envs = [make_env() for i in range(num_envs)]
envs = SubprocVecEnv(envs)

state_shape = envs.observation_space.shape
num_actions = envs.action_space.n
num_rewards = len(task_rewards[mode])

class RolloutEncoder(nn.Module):
    def __init__(self, in_shape, num_rewards, hidden_size):
        super(RolloutEncoder, self).__init__()

        self.in_shape = in_shape

        self.features = nn.Sequential(
            nn.Conv2d(in_shape[0], 16, kernel_size=3, stride=1),
            nn.ReLU(),
            nn.Conv2d(16, 16, kernel_size=3, stride=2),
            nn.ReLU(),
        )

        self.gru = nn.GRU(self.feature_size() + num_rewards, hidden_size)

    def forward(self, state, reward):
        num_steps  = state.size(0)
        batch_size = state.size(1)

        # In shape is just the shape of the state space
        state = state.view(-1, *self.in_shape)
        # Extract features from the state space using a conv net
        state = self.features(state)
        state = state.view(num_steps, batch_size, -1)
        rnn_input = torch.cat([state, reward], 2)

        # Process sequence of frames with RNN to return final score for
        # sequence
        _, hidden = self.gru(rnn_input)
        return hidden.squeeze(0)


    def feature_size(self):
        return self.features(autograd.Variable(torch.zeros(1, *self.in_shape))).view(1, -1).size(1)

class I2A(OnPolicy):
    def __init__(self, in_shape, num_actions, num_rewards, hidden_size, imagination, full_rollout=True):
        super(I2A, self).__init__()

        self.in_shape      = in_shape
        self.num_actions   = num_actions
        self.num_rewards   = num_rewards

        self.imagination = imagination

        self.features = nn.Sequential(
            nn.Conv2d(in_shape[0], 16, kernel_size=3, stride=1),
            nn.ReLU(),
            nn.Conv2d(16, 16, kernel_size=3, stride=2),
            nn.ReLU(),
        )

        self.encoder = RolloutEncoder(in_shape, num_rewards, hidden_size)

        if full_rollout:
            self.fc = nn.Sequential(
                nn.Linear(self.feature_size() + num_actions * hidden_size, 256),
                nn.ReLU(),
            )
        else:
            self.fc = nn.Sequential(
                nn.Linear(self.feature_size() + hidden_size, 256),
                nn.ReLU(),
            )

        self.critic  = nn.Linear(256, 1)
        self.actor   = nn.Linear(256, num_actions)

    def forward(self, state):
        # Batch size is first element of the state input
        # This will be the number of environments multiplied by the action
        # space
        batch_size = state.size(0)

        # Get a full rollout of an imagined sequence
        # This will be a tensor of size
        # [rollout count, # envs (batch size) * actions, *state space]
        imagined_state, imagined_reward = self.imagination(state.data)

        hidden = self.encoder(Variable(imagined_state), Variable(imagined_reward))
        # Get encoded representation of each state
        hidden = hidden.view(batch_size, -1)

        # Extract features from state
        state = self.features(state)
        state = state.view(state.size(0), -1)

        # Input is a weighted sum of imagination scores and extracted features
        # from input. The state is the model free path and the hidden
        # is the model based path
        x = torch.cat([state, hidden], 1)
        x = self.fc(x)

        # Use our standard policy from a2c
        logit = self.actor(x)
        value = self.critic(x)

        return logit, value

    def feature_size(self):
        return self.features(autograd.Variable(torch.zeros(1, *self.in_shape))).view(1, -1).size(1)

# The output of this is
# [rollout count, # envs (batch size) * actions, *state space]
class ImaginationCore(object):
    def __init__(self, num_rolouts, in_shape, num_actions, num_rewards, env_model, distil_policy, full_rollout=True):
        self.num_rolouts  = num_rolouts
        self.in_shape      = in_shape
        self.num_actions   = num_actions
        self.num_rewards   = num_rewards
        self.env_model     = torch.nn.DataParallel(env_model, device_ids=[0,1,2]).cuda()
        self.distil_policy = distil_policy
        self.full_rollout  = full_rollout

    def __call__(self, state):
        state      = state.cpu()
        batch_size = state.size(0)

        rollout_states  = []
        rollout_rewards = []

        if self.full_rollout:
            state = state.unsqueeze(0).repeat(self.num_actions, 1, 1, 1, 1).view(-1, *self.in_shape)
            action = torch.LongTensor([[i] for i in range(self.num_actions)] * batch_size)
            action = action.view(-1)
            rollout_batch_size = batch_size * self.num_actions
        else:
            print('NOT USING FULL ROLLOUT')
            action = self.distil_policy.act(Variable(state, volatile=True))
            action = action.data.cpu()
            rollout_batch_size = batch_size
            raise ValueError('CANNOT USE FULL ROLLOUT')

        for step in range(self.num_rolouts):
            # Creating the whole thing to be ones to start off with assumes
            # that
            # batch size 80
            # [400, 5, 15, 19]

            # Encode the actions
            onehot_action = torch.zeros(rollout_batch_size, self.num_actions, *self.in_shape[1:])
            onehot_action[range(rollout_batch_size), action] = 1

            #if not (np.all(x == 1)):
            #    raise ValueError('NOT ALL EQUAL ONE')

            # Combination of the pixel frames and the actions are the input to
            # the environment model. Note that for a full roll out we are
            # taking every single action and then evaluating it
            inputs = torch.cat([state, onehot_action], 1)

            # Imagine next states and rewards
            imagined_state, imagined_reward = self.env_model(Variable(inputs, volatile=True))

            imagined_state  = F.softmax(imagined_state, dim=1).max(1)[1].data.cpu()
            imagined_reward = F.softmax(imagined_reward, dim=1).max(1)[1].data.cpu()

            imagined_state = target_to_pix(imagined_state.numpy())
            imagined_state = torch.FloatTensor(imagined_state).view(rollout_batch_size, *self.in_shape)

            onehot_reward = torch.zeros(rollout_batch_size, self.num_rewards)
            onehot_reward[range(rollout_batch_size), imagined_reward] = 1

            rollout_states.append(imagined_state.unsqueeze(0))
            rollout_rewards.append(onehot_reward.unsqueeze(0))

            state  = imagined_state
            action = self.distil_policy.act(Variable(state, volatile=True))
            action = action.data.cpu()

        return torch.cat(rollout_states), torch.cat(rollout_rewards)

full_rollout = True

# Get the env model which is trained to predict the next frame and the reward
# associated with the current frame
env_model     = EnvModel(envs.observation_space.shape, num_pixels, num_rewards)
env_model.load_state_dict(torch.load("env_model_" + mode))

distil_policy = ActorCritic(envs.observation_space.shape, envs.action_space.n)
distil_optimizer = optim.Adam(distil_policy.parameters())

# First parameter is the number of rollouts
imagination = ImaginationCore(1, state_shape, num_actions, num_rewards, env_model, distil_policy, full_rollout=full_rollout)

actor_critic = I2A(state_shape, num_actions, num_rewards, 256, imagination, full_rollout=full_rollout)
#rmsprop hyperparams:
lr    = 7e-4
eps   = 1e-5
alpha = 0.99

# Optimize parameters of I2A
optimizer = optim.RMSprop(actor_critic.parameters(), lr, eps=eps, alpha=alpha)

if USE_CUDA:
    env_model     = env_model.cuda()
    distil_policy = distil_policy.cuda()
    actor_critic  = actor_critic.cuda()

gamma = 0.99
entropy_coef = 0.01
value_loss_coef = 0.5
max_grad_norm = 0.5
num_steps = 5
num_frames = int(10e5)

rollout = RolloutStorage(num_steps, num_envs, envs.observation_space.shape)
rollout.cuda()

all_rewards = []
all_losses  = []

# Get the initial state
state = envs.reset()
# Convert to torch tensor
current_state = torch.FloatTensor(np.float32(state))

# Set to the replay buffer
rollout.states[0].copy_(current_state)

episode_rewards = torch.zeros(num_envs, 1)
final_rewards   = torch.zeros(num_envs, 1)

print('Starting to train')
print('Using cuda?', USE_CUDA)

print('Using: %i GPUs' % (torch.cuda.device_count()))

for i_update in range(num_frames):
    overall_start = time.time()

    start = time.time()
    for step in range(num_steps):
        if USE_CUDA:
            current_state = current_state.cuda()

        # Get I2A action for state
        action = actor_critic.act(Variable(current_state))

        next_state, reward, done, _ = envs.step(action.squeeze(1).cpu().data.numpy())

        reward = torch.FloatTensor(reward).unsqueeze(1)
        episode_rewards += reward
        masks = torch.FloatTensor(1-np.array(done)).unsqueeze(1)
        final_rewards *= masks
        final_rewards += (1-masks) * episode_rewards
        episode_rewards *= masks

        if USE_CUDA:
            masks = masks.cuda()

        current_state = torch.FloatTensor(np.float32(next_state))
        rollout.insert(step, current_state, action.data, reward, masks)

    end = time.time()
    plog('Roll out takes', end - start)

    _, next_value = actor_critic(Variable(rollout.states[-1], volatile=True))
    next_value = next_value.data

    # Apply the bellman equation to calculate returns
    returns = rollout.compute_returns(next_value, gamma)

    # just the standard way of evaluating actions given state and action
    # This is for I2A
    logit, action_log_probs, values, entropy = actor_critic.evaluate_actions(
        Variable(rollout.states[:-1]).view(-1, *state_shape),
        Variable(rollout.actions).view(-1, 1)
    )

    # This is for the normal A2C
    distil_logit, _, _, _ = distil_policy.evaluate_actions(
        Variable(rollout.states[:-1]).view(-1, *state_shape),
        Variable(rollout.actions).view(-1, 1)
    )

    distil_loss = 0.01 * (F.softmax(logit, dim=1).detach() *
            F.log_softmax(distil_logit, dim=1)).sum(1).mean()

    values = values.view(num_steps, num_envs, 1)
    action_log_probs = action_log_probs.view(num_steps, num_envs, 1)
    advantages = Variable(returns) - values

    value_loss = advantages.pow(2).mean()
    action_loss = -(Variable(advantages.data) * action_log_probs).mean()

    ###############################################
    ###############################################

    start = time.time()
    # Apparently before applying a gradient you have to zero out the gradients
    optimizer.zero_grad()
    loss = value_loss * value_loss_coef + action_loss - entropy * entropy_coef
    loss.backward()
    nn.utils.clip_grad_norm(actor_critic.parameters(), max_grad_norm)
    optimizer.step()

    distil_optimizer.zero_grad()
    distil_loss.backward()
    optimizer.step()
    end = time.time()
    plog('Backpropagating took', end - start)

    overall_end = time.time()
    print('Epoch took', overall_end - overall_start)

    all_rewards.append(final_rewards.mean())
    all_losses.append(loss.data[0])
    print('Epoch %i, Rewards %.2f, Loss %.2f' % (i_update,
        np.mean(all_rewards[-10:]), all_losses[-1]))

    rollout.after_update()

    if i_update != 0 and i_update % 10000 == 0:
        print('Saving model!')
        torch.save(actor_critic.state_dict(), "i2a_" + mode + '_' + str(i_update))


torch.save(actor_critic.state_dict(), "i2a_" + mode)

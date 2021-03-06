
import torch

from rlpyt.agents.base import (AgentStep, RecurrentAgentMixin, 
    AlternatingRecurrentAgentMixin)
from rlpyt.agents.dqn.dqn_agent import DqnAgent
from rlpyt.utils.buffer import buffer_to, buffer_func, buffer_method
from rlpyt.utils.collections import namedarraytuple


AgentInfo = namedarraytuple("AgentInfo", ["q", "prev_rnn_state"])


class R2d1AgentBase(DqnAgent):
    """Base agent for recurrent DQN (to add recurrent mixin)."""

    def __call__(self, observation, prev_action, prev_reward, init_rnn_state):
        # Assume init_rnn_state already shaped: [N,B,H]
        prev_action = self.distribution.to_onehot(prev_action)
        model_inputs = buffer_to((observation, prev_action, prev_reward,
            init_rnn_state), device=self.device)
        output = self.model(*model_inputs) # q, rnn_state
        return output  # Leave rnn state on device.

    def to_agent_step(self, output):
        """Convert the output of the NN model into step info for the agent.
        """
        q, rnn_state = output
        # q = q.cpu()
        action = self.distribution.sample(q)
        prev_rnn_state = self.prev_rnn_state or buffer_func(rnn_state, torch.zeros_like)
        # Transpose the rnn_state from [N,B,H] --> [B,N,H] for storage.
        # (Special case, model should always leave B dimension in.)
        prev_rnn_state = buffer_method(prev_rnn_state, "transpose", 0, 1)
        prev_rnn_state, action, q = buffer_to((prev_rnn_state, action, q), device="cpu")
        agent_info = AgentInfo(q=q, prev_rnn_state=prev_rnn_state)
        self.advance_rnn_state(rnn_state)  # Keep on device.
        return AgentStep(action=action, agent_info=agent_info)

    @torch.no_grad()
    def step(self, observation, prev_action, prev_reward):
        """Computes Q-values for states/observations and selects actions by
        epsilon-greedy (no grad).  Advances RNN state."""
        prev_action = self.distribution.to_onehot(prev_action)
        agent_inputs = buffer_to((observation, prev_action, prev_reward),
            device=self.device)
        output = self.model(*agent_inputs, self.prev_rnn_state)  # Model handles None.
        return self.to_agent_step(output)

    def target(self, observation, prev_action, prev_reward, init_rnn_state):
        # Assume init_rnn_state already shaped: [N,B,H]
        prev_action = self.distribution.to_onehot(prev_action)
        model_inputs = buffer_to((observation, prev_action, prev_reward, init_rnn_state),
            device=self.device)
        target_output = self.target_model(*model_inputs) # target_q, rnn_state
        return target_output  # Leave rnn state on device.


class R2d1Agent(RecurrentAgentMixin, R2d1AgentBase):
    """R2D1 agent."""
    pass


class R2d1AlternatingAgent(AlternatingRecurrentAgentMixin, R2d1AgentBase):
    pass

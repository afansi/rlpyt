
import torch
from collections import namedtuple

from rlpyt.algos.base import RlAlgorithm
from rlpyt.utils.quick_args import save__init__args
from rlpyt.utils.logging import logger
from rlpyt.replays.non_sequence.frame import (UniformReplayFrameBuffer,
    PrioritizedReplayFrameBuffer, AsyncUniformReplayFrameBuffer,
    AsyncPrioritizedReplayFrameBuffer)
from rlpyt.utils.collections import namedarraytuple
from rlpyt.utils.tensor import select_at_indexes, valid_mean
from rlpyt.algos.utils import valid_from_done
from rlpyt.utils.buffer import buffer_to

OptInfo = namedtuple("OptInfo", ["loss", "gradNorm", "tdAbsErr"])
SamplesToBuffer = namedarraytuple("SamplesToBuffer",
    ["observation", "action", "reward", "done"])


class DQN(RlAlgorithm):
    """
    DQN algorithm trainig from a replay buffer, with options for double-dqn, n-step
    returns, and prioritized replay.
    """

    opt_info_fields = tuple(f for f in OptInfo._fields)  # copy

    def __init__(
            self,
            discount=0.99,
            batch_size=32,
            min_steps_learn=int(5e4),
            delta_clip=1.,
            replay_size=int(1e6),
            replay_ratio=8,  # data_consumption / data_generation.
            target_update_tau=1,
            target_update_interval=312,  # 312 * 32 = 1e4 env steps.
            n_step_return=1,
            learning_rate=2.5e-4,
            OptimCls=torch.optim.Adam,
            optim_kwargs=None,
            initial_optim_state_dict=None,
            clip_grad_norm=10.,
            # eps_init=1,  # NOW IN AGENT.
            # eps_final=0.01,
            # eps_final_min=None,  # set < eps_final to use vector-valued eps.
            # eps_eval=0.001,
            eps_steps=int(1e6),  # STILL IN ALGO (to convert to itr).
            double_dqn=False,
            prioritized_replay=False,
            pri_alpha=0.6,
            pri_beta_init=0.4,
            pri_beta_final=1.,
            pri_beta_steps=int(50e6),
            default_priority=None,
            ReplayBufferCls=None,  # Leave None to select by above options.
            updates_per_sync=1,  # For async mode only.
            ):
        """Saves input arguments.  

        ``delta_clip`` selects the Huber loss; if ``None``, uses MSE.

        ``replay_ratio`` determines the ratio of data-consumption
        to data-generation.  For example, original DQN sampled 4 environment steps between
        each training update with batch-size 32, for a replay ratio of 8.

        """ 
        if optim_kwargs is None:
            optim_kwargs = dict(eps=0.01 / batch_size)
        if default_priority is None:
            default_priority = delta_clip
        self._batch_size = batch_size
        del batch_size  # Property.
        save__init__args(locals())
        self.update_counter = 0

    def initialize(self, agent, n_itr, batch_spec, mid_batch_reset, examples,
            world_size=1, rank=0):
        """Stores input arguments and initializes replay buffer and optimizer.
        Use in non-async runners.  Computes number of gradient updates per
        optimization iteration as `(replay_ratio * sampler-batch-size /
        training-batch_size)`."""
        self.agent = agent
        self.n_itr = n_itr
        self.sampler_bs = sampler_bs = batch_spec.size
        self.mid_batch_reset = mid_batch_reset
        self.updates_per_optimize = max(1, round(self.replay_ratio * sampler_bs /
            self.batch_size))
        logger.log(f"From sampler batch size {batch_spec.size}, training "
            f"batch size {self.batch_size}, and replay ratio "
            f"{self.replay_ratio}, computed {self.updates_per_optimize} "
            f"updates per iteration.")
        self.min_itr_learn = int(self.min_steps_learn // sampler_bs)
        eps_itr_max = max(1, int(self.eps_steps // sampler_bs))
        agent.set_epsilon_itr_min_max(self.min_itr_learn, eps_itr_max)
        self.initialize_replay_buffer(examples, batch_spec)
        self.optim_initialize(rank)

    def async_initialize(self, agent, sampler_n_itr, batch_spec, mid_batch_reset,
            examples, world_size=1):
        """Used in async runner only; returns replay buffer allocated in shared
        memory, does not instantiate optimizer. """
        self.agent = agent
        self.n_itr = sampler_n_itr
        self.initialize_replay_buffer(examples, batch_spec, async_=True)
        self.mid_batch_reset = mid_batch_reset
        self.sampler_bs = sampler_bs = batch_spec.size
        self.updates_per_optimize = self.updates_per_sync
        self.min_itr_learn = int(self.min_steps_learn // sampler_bs)
        eps_itr_max = max(1, int(self.eps_steps // sampler_bs))
        # Before any forking so all sub processes have epsilon schedule:
        agent.set_epsilon_itr_min_max(self.min_itr_learn, eps_itr_max)
        return self.replay_buffer

    def optim_initialize(self, rank=0):
        """Called in initilize or by async runner after forking sampler."""
        self.rank = rank
        self.optimizer = self.OptimCls(self.agent.parameters(),
            lr=self.learning_rate, **self.optim_kwargs)
        if self.initial_optim_state_dict is not None:
            self.optimizer.load_state_dict(self.initial_optim_state_dict)
        if self.prioritized_replay:
            self.pri_beta_itr = max(1, self.pri_beta_steps // self.sampler_bs)

    def initialize_replay_buffer(self, examples, batch_spec, async_=False):
        """
        Allocates replay buffer using examples and with the fields in `SamplesToBuffer`
        namedarraytuple.  Uses frame-wise buffers, so that only unique frames are stored,
        using less memory (usual observations are 4 most recent frames, with only newest
        frame distince from previous observation).
        """
        example_to_buffer = self.examples_to_buffer(examples)
        replay_kwargs = dict(
            example=example_to_buffer,
            size=self.replay_size,
            B=batch_spec.B,
            discount=self.discount,
            n_step_return=self.n_step_return,
        )
        if self.prioritized_replay:
            replay_kwargs.update(dict(
                alpha=self.pri_alpha,
                beta=self.pri_beta_init,
                default_priority=self.default_priority,
            ))
            ReplayCls = (AsyncPrioritizedReplayFrameBuffer if async_ else
                PrioritizedReplayFrameBuffer)
        else:
            ReplayCls = (AsyncUniformReplayFrameBuffer if async_ else
                UniformReplayFrameBuffer)
        if self.ReplayBufferCls is not None:
            ReplayCls = self.ReplayBufferCls
            logger.log(f"WARNING: ignoring internal selection logic and using"
                f" input replay buffer class: {ReplayCls} -- compatibility not"
                " guaranteed.")
        self.replay_buffer = ReplayCls(**replay_kwargs)

    def add_samples_to_buffer(self, itr, samples=None):
        """
        Adds the provided samples in the replay buffer.
        """
        assert itr >= 0
        if samples is not None:
            samples_to_buffer = self.samples_to_buffer(samples)
            self.replay_buffer.append_samples(samples_to_buffer)

    def pre_optimize_process(self):
        """
        Method for defining any process that shold take place before optimizing
        the agent. Examples of such process are pretraining, ... 
        """
        pass
    
    def _get_empty_optim_info(self):
        """Returns an empty optimization info object.

        Parameters
        ----------

        Return
        ----------
        opt_info: obj
            the empty optimization info object.
        
        """
        return OptInfo(*([] for _ in range(len(OptInfo._fields))))
        

    def _apply_optimization(self, samples_from_replay, opt_info):
        """Applies the optimization for Mixed DQN-like Algos.

        This method is called in the `optimize_agent` method.

        Parameters
        ----------
        samples_from_replay: obj
            the sample data from the replay memory against which the
            optimization is performed.
        opt_info: obj
            the information about the optimization performed so far.

        Return
        ----------
        opt_info: obj
            the updated information with the applied optimization.
        
        """
        self.optimizer.zero_grad()
        loss, td_abs_errors = self.loss(samples_from_replay)
        loss.backward()
        grad_norm = torch.nn.utils.clip_grad_norm_(
            self.agent.parameters(), self.clip_grad_norm
        )
        self.optimizer.step()
        if self.prioritized_replay:
            self.replay_buffer.update_batch_priorities(td_abs_errors)
        opt_info.loss.append(loss.item())
        opt_info.gradNorm.append(torch.tensor(grad_norm).item())  # backwards compatible
        opt_info.tdAbsErr.extend(td_abs_errors[::8].numpy())  # Downsample.
        return opt_info

    def optimize_agent(self, itr, samples=None, sampler_itr=None):
        """
        Extracts the needed fields from input samples and stores them in the 
        replay buffer.  Then samples from the replay buffer to train the agent
        by gradient updates (with the number of updates determined by replay
        ratio, sampler batch size, and training batch size).  If using prioritized
        replay, updates the priorities for sampled training batches.
        """
        itr = itr if sampler_itr is None else sampler_itr  # Async uses sampler_itr.

        # add samples in the replay buffer
        self.add_samples_to_buffer(itr, samples)

        opt_info = self._get_empty_optim_info()
        if itr < self.min_itr_learn:
            return opt_info

        if itr == self.min_itr_learn:
            self.pre_optimize_process()

        for _ in range(self.updates_per_optimize):
            samples_from_replay = self.replay_buffer.sample_batch(self.batch_size)
            opt_info = self._apply_optimization(samples_from_replay, opt_info)
            self.update_counter += 1
            if self.update_counter % self.target_update_interval == 0:
                self.agent.update_target(self.target_update_tau)
        self.update_itr_hyperparams(itr)
        return opt_info

    def examples_to_buffer(self, examples):
        return SamplesToBuffer(
            observation=examples["observation"],
            action=examples["action"],
            reward=examples["reward"],
            done=examples["done"],
        )

    def samples_to_buffer(self, samples):
        """Defines how to add data from sampler into the replay buffer. Called
        in optimize_agent() if samples are provided to that method.  In 
        asynchronous mode, will be called in the memory_copier process."""
        return SamplesToBuffer(
            observation=samples.env.observation,
            action=samples.agent.action,
            reward=samples.env.reward,
            done=samples.env.done,
        )

    def examples_to_buffer(self, examples):
        """Defines how to initialize the replay buffer from examples. Called
        in initialize_replay_buffer().
        """
        return SamplesToBuffer(
            observation=examples["observation"],
            action=examples["action"],
            reward=examples["reward"],
            done=examples["done"],
        )

    def select_at_indexes(self, indexes, tensor):
        """Returns the `tensor` data at the multi-dimensional integer array `indexes`.

        Parameters
        ----------
        indexes: tensor
            a tensor of indexes.
        tensor: tensor
            a tensor from which to retrieve the data of interest.

        Return
        ----------
        result: tensor
            the resulting data.
        """
        return select_at_indexes(indexes, tensor)

    def _get_loss_values(self, q, done, weigths, target):
        """Computes the loss values given the q and target values.

        Additional parameters are provided such as the:
          - done: whether the trajectory ended now
          - weights: weights of the samples (cf. prioritized replay buffers)

        Parameters
        ----------
        q: tensor
            Q-values to consider.
        done: tensor
            whether the trajectory ended now.
        weigths: tensor
            weights of the samples (cf. prioritized replay buffers).
        target: tensor
            target values.

        Return
        ----------
        loss: tensor
            the computed loss.
        td_abs_errors: tensor
            the td absolute errors.
        """
        delta = target - q
        losses = 0.5 * delta ** 2
        abs_delta = abs(delta)
        if self.delta_clip is not None:  # Huber loss.
            b = self.delta_clip * (abs_delta - self.delta_clip / 2)
            losses = torch.where(abs_delta <= self.delta_clip, losses, b)
        if self.prioritized_replay:
            losses *= weigths
        td_abs_errors = abs_delta.detach()
        if self.delta_clip is not None:
            td_abs_errors = torch.clamp(td_abs_errors, 0, self.delta_clip)
        if not self.mid_batch_reset:
            # FIXME: I think this is wrong, because the first "done" sample
            # is valid, but here there is no [T] dim, so there's no way to
            # know if a "done" sample is the first "done" in the sequence.
            raise NotImplementedError
            # valid = valid_from_done(done)
            # loss = valid_mean(losses, valid)
            # td_abs_errors *= valid
        else:
            loss = torch.mean(losses)

        return loss, td_abs_errors

    def loss(self, samples):
        """
        Computes the Q-learning loss, based on: 0.5 * (Q - target_Q) ^ 2.
        Implements regular DQN or Double-DQN for computing target_Q values
        using the agent's target network.  Computes the Huber loss using
        ``delta_clip``, or if ``None``, uses MSE.  When using prioritized
        replay, multiplies losses by importance sample weights.

        Input ``samples`` have leading batch dimension [B,..] (but not time).

        Calls the agent to compute forward pass on training inputs, and calls
        ``agent.target()`` to compute target values.

        Returns loss and TD-absolute-errors for use in prioritization.

        Warning:
            If not using mid_batch_reset, the sampler will only reset environments
            between iterations, so some samples in the replay buffer will be
            invalid.  This case is not supported here currently.
        """
        if self.prioritized_replay:
            [
                samples_return_,
                samples_done,
                samples_done_n,
                samples_action,
                samples_is_weights
            ] = buffer_to(
                (
                    samples.return_,
                    samples.done,
                    samples.done_n,
                    samples.action,
                    samples.is_weights
                ),
                device=self.agent.device
            )
        else:
            [
                samples_return_, samples_done, samples_done_n, samples_action
            ] = buffer_to(
                (samples.return_, samples.done, samples.done_n, samples.action),
                device=self.agent.device
            )
            samples_is_weights = None
        qs = self.agent(*samples.agent_inputs)
        q = self.select_at_indexes(samples_action, qs)
        with torch.no_grad():
            target_qs = self.agent.target(*samples.target_inputs)
            if self.double_dqn:
                next_qs = self.agent(*samples.target_inputs)
                next_a = torch.argmax(next_qs, dim=-1)
                target_q = self.select_at_indexes(next_a, target_qs)
            else:
                target_q = torch.max(target_qs, dim=-1).values
        disc_target_q = (self.discount ** self.n_step_return) * target_q
        y = samples_return_ + (1 - samples_done_n.float()) * disc_target_q

        loss, td_abs_errors = self._get_loss_values(
            q, samples_done, samples_is_weights, y
        )

        return loss, td_abs_errors.cpu()

    def update_itr_hyperparams(self, itr):
        # EPS NOW IN AGENT.
        # if itr <= self.eps_itr:  # Epsilon can be vector-valued.
        #     prog = min(1, max(0, itr - self.min_itr_learn) /
        #       (self.eps_itr - self.min_itr_learn))
        #     new_eps = prog * self.eps_final + (1 - prog) * self.eps_init
        #     self.agent.set_sample_epsilon_greedy(new_eps)
        if self.prioritized_replay and itr <= self.pri_beta_itr:
            prog = min(1, max(0, itr - self.min_itr_learn) /
                (self.pri_beta_itr - self.min_itr_learn))
            new_beta = (prog * self.pri_beta_final +
                (1 - prog) * self.pri_beta_init)
            self.replay_buffer.set_beta(new_beta)

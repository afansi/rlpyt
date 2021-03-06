
import torch

from rlpyt.algos.dqn.dqn import DQN
from rlpyt.utils.tensor import valid_mean
from rlpyt.algos.utils import valid_from_done
from rlpyt.utils.buffer import buffer_to


EPS = 1e-6  # (NaN-guard)


class CategoricalDQN(DQN):
    """Distributional DQN with fixed probability bins for the Q-value of each
    action, a.k.a. categorical."""

    def __init__(self, V_min=-10, V_max=10, **kwargs):
        """Standard __init__() plus Q-value limits; the agent configures
        the number of atoms (bins)."""
        super().__init__(**kwargs)
        self.V_min = V_min
        self.V_max = V_max
        if "eps" not in self.optim_kwargs:  # Assume optim.Adam
            self.optim_kwargs["eps"] = 0.01 / self.batch_size

    def initialize(self, *args, **kwargs):
        super().initialize(*args, **kwargs)
        self.agent.give_V_min_max(self.V_min, self.V_max)

    def async_initialize(self, *args, **kwargs):
        buffer = super().async_initialize(*args, **kwargs)
        self.agent.give_V_min_max(self.V_min, self.V_max)
        return buffer

    def _get_loss_values(self, p, done, weigths, target_p):
        """Computes the loss values given the q and target values.

        Additional parameters are provided such as the:
          - done: whether the trajectory ended now
          - weights: weights of the samples (cf. prioritized replay buffers)

        Parameters
        ----------
        p: tensor
            distributional Q-values to consider.
        done: tensor
            whether the trajectory ended now.
        weigths: tensor
            weights of the samples (cf. prioritized replay buffers).
        target_p: tensor
            target distributional values.

        Return
        ----------
        loss: tensor
            the computed loss.
        KL_div: tensor
            the KL div between the distributional Q-values.
        """
        p = torch.clamp(p, EPS, 1)  # NaN-guard.
        losses = -torch.sum(target_p * torch.log(p), dim=1)  # Cross-entropy.

        if self.prioritized_replay:
            losses *= weigths

        target_p = torch.clamp(target_p, EPS, 1)
        KL_div = torch.sum(
            target_p * (torch.log(target_p) - torch.log(p.detach())), dim=1
        )
        KL_div = torch.clamp(KL_div, EPS, 1 / EPS)  # Avoid <0 from NaN-guard.

        if not self.mid_batch_reset:
            valid = valid_from_done(done)
            loss = valid_mean(losses, valid)
            KL_div *= valid
        else:
            loss = torch.mean(losses)

        return loss, KL_div

    def loss(self, samples):
        """
        Computes the Distributional Q-learning loss, based on projecting the
        discounted rewards + target Q-distribution into the current Q-domain,
        with cross-entropy loss.

        Returns loss and KL-divergence-errors for use in prioritization.
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
        delta_z = (self.V_max - self.V_min) / (self.agent.n_atoms - 1)
        z = torch.linspace(self.V_min, self.V_max, self.agent.n_atoms, device=self.agent.device)
        # Makde 2-D tensor of contracted z_domain for each data point,
        # with zeros where next value should not be added.
        next_z = z * (self.discount ** self.n_step_return)  # [P']
        next_z = torch.ger(1 - samples_done_n.float(), next_z)  # [B,P']
        ret = samples_return_.unsqueeze(1)  # [B,1]
        next_z = torch.clamp(ret + next_z, self.V_min, self.V_max)  # [B,P']

        z_bc = z.view(1, -1, 1)  # [1,P,1]
        next_z_bc = next_z.unsqueeze(1)  # [B,1,P']
        abs_diff_on_delta = abs(next_z_bc - z_bc) / delta_z
        projection_coeffs = torch.clamp(1 - abs_diff_on_delta, 0, 1)  # Most 0.
        # projection_coeffs is a 3-D tensor: [B,P,P']
        # dim-0: independent data entries
        # dim-1: base_z atoms (remains after projection)
        # dim-2: next_z atoms (summed in projection)

        with torch.no_grad():
            target_ps = self.agent.target(*samples.target_inputs)  # [B,A,P']
            if self.double_dqn:
                next_ps = self.agent(*samples.target_inputs)  # [B,A,P']
                next_qs = torch.tensordot(next_ps, z, dims=1)  # [B,A]
                next_a = torch.argmax(next_qs, dim=-1)  # [B]
            else:
                target_qs = torch.tensordot(target_ps, z, dims=1)  # [B,A]
                next_a = torch.argmax(target_qs, dim=-1)  # [B]
            target_p_unproj = self.select_at_indexes(next_a, target_ps)  # [B,P']
            target_p_unproj = target_p_unproj.unsqueeze(1)  # [B,1,P']
            target_p = (target_p_unproj * projection_coeffs).sum(-1)  # [B,P]
        ps = self.agent(*samples.agent_inputs)  # [B,A,P]
        p = self.select_at_indexes(samples_action, ps)  # [B,P]

        loss, KL_div = self._get_loss_values(
            p, samples_done, samples_is_weights, target_p
        )

        return loss, KL_div.cpu()

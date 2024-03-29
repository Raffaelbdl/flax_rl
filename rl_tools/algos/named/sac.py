"""Soft Actor Critic (SAC)"""

from dataclasses import dataclass
from typing import Callable

import chex
import flax.linen as nn
import jax
import jax.numpy as jnp
import optax

from rl_tools.base import OffPolicyAgent
from rl_tools.buffer import Experience
from rl_tools.config import AlgoConfig, AlgoParams
from rl_tools.types import Params

from rl_tools.loss import loss_mean_squared_error
from rl_tools.timesteps import compute_td_targets


from rl_tools.algos.factory import AlgoFactory
from rl_tools.modules.encoder import encoder_factory
from rl_tools.modules.modules import init_params, IndependentVariable
from rl_tools.modules.policy import make_policy, PolicyTanhNormal
from rl_tools.modules.train_state import PolicyQValueTrainState, TrainState
from rl_tools.modules.qvalue import make_double_q_value, qvalue_factory


@chex.dataclass
class SACTrainState(PolicyQValueTrainState):
    alpha_state: TrainState


@dataclass
class SACParams(AlgoParams):
    """SAC parameters"""

    gamma: float
    tau: float

    log_std_min: float
    log_std_max: float

    initial_alpha: float  # log
    start_step: int
    skip_steps: int


def train_state_sac_factory(
    key: jax.Array,
    config: AlgoConfig,
    *,
    rearrange_pattern: str,
    preprocess_fn: Callable,
    tabulate: bool = False,
) -> SACTrainState:

    key1, key2, key3 = jax.random.split(key, 3)
    observation_shape = config.env_cfg.observation_space.shape
    action_shape = config.env_cfg.action_space.shape

    policy = make_policy(
        encoder_factory(config.env_cfg.observation_space)(),
        PolicyTanhNormal(
            action_shape[-1],
            config.algo_params.log_std_min,
            config.algo_params.log_std_max,
        ),
    )
    policy_state = TrainState.create(
        apply_fn=jax.jit(policy.apply),
        params=init_params(key1, policy, [observation_shape], tabulate),
        target_params=init_params(key1, policy, [observation_shape], False),
        tx=optax.adam(config.update_cfg.learning_rate),
    )

    qvalue = make_double_q_value(
        qvalue_factory(config.env_cfg.observation_space, config.env_cfg.action_space)(),
        qvalue_factory(config.env_cfg.observation_space, config.env_cfg.action_space)(),
    )
    qvalue_state = TrainState.create(
        apply_fn=jax.jit(qvalue.apply),
        params=init_params(key2, qvalue, [observation_shape, action_shape], tabulate),
        target_params=init_params(
            key2, qvalue, [observation_shape, action_shape], False
        ),
        tx=optax.adam(config.update_cfg.learning_rate),
    )

    alpha = IndependentVariable(
        name="log_alpha",
        init_fn=nn.initializers.constant(jnp.log(config.algo_params.initial_alpha)),
        shape=(),
    )
    alpha_state = TrainState.create(
        apply_fn=jax.jit(alpha.apply),
        params=init_params(key3, alpha, [], tabulate),
        tx=optax.adam(config.update_cfg.learning_rate),
    )

    return SACTrainState(
        policy_state=policy_state,
        qvalue_state=qvalue_state,
        alpha_state=alpha_state,
    )


def explore_factory(config: AlgoConfig) -> Callable:
    @jax.jit
    def fn(policy_state: TrainState, key: jax.Array, observations: jax.Array):
        actions, log_probs = policy_state.apply_fn(
            policy_state.params, observations
        ).sample_and_log_prob(seed=key)
        actions = jnp.clip(actions, -1.0, 1.0)
        return actions, log_probs

    return fn


def process_experience_factory(config: AlgoConfig) -> Callable:
    algo_params = config.algo_params

    @jax.jit
    def fn(sac_state: SACTrainState, key: jax.Array, experience: Experience):

        next_dists = sac_state.policy_state.apply_fn(
            sac_state.policy_state.target_params, experience.next_observation
        )
        next_actions, next_log_probs = next_dists.sample_and_log_prob(seed=key)
        next_log_probs = jnp.sum(next_log_probs, axis=-1, keepdims=True)

        next_q1, next_q2 = sac_state.qvalue_state.apply_fn(
            sac_state.qvalue_state.target_params,
            experience.next_observation,
            next_actions,
        )
        next_q_min = jnp.minimum(next_q1, next_q2)

        discounts = algo_params.gamma * (1.0 - experience.done[..., None])
        targets = compute_td_targets(
            experience.reward[..., None], discounts, next_q_min
        )

        alpha = jnp.exp(sac_state.alpha_state.apply_fn(sac_state.alpha_state.params))
        targets -= discounts * alpha * next_log_probs

        return (experience.observation, experience.action, targets)

    return fn


def update_step_factory(config: AlgoConfig) -> Callable:

    @jax.jit
    def update_qvalue_fn(qvalue_state: TrainState, batch: tuple[jax.Array]):

        observations, actions, targets = batch

        def loss_fn(params: Params):
            q1, q2 = qvalue_state.apply_fn(params, observations, actions)
            loss_q1 = loss_mean_squared_error(q1, targets)
            loss_q2 = loss_mean_squared_error(q2, targets)

            return loss_q1 + loss_q2, {"loss_qvalue": loss_q1 + loss_q2}

        (loss, info), grads = jax.value_and_grad(loss_fn, has_aux=True)(
            qvalue_state.params
        )
        qvalue_state = qvalue_state.apply_gradients(grads=grads)

        return qvalue_state, loss, info

    @jax.jit
    def update_policy_fn(
        policy_state: TrainState,
        qvalue_state: TrainState,
        alpha_state: TrainState,
        batch: tuple[jax.Array],
    ):
        observations, _, _ = batch

        def loss_fn(params: Params):
            actions, log_probs = policy_state.apply_fn(
                params, observations
            ).sample_and_log_prob(seed=0)
            log_probs = jnp.sum(log_probs, axis=-1)
            qvalues, _ = qvalue_state.apply_fn(
                qvalue_state.params, observations, actions
            )
            alpha = jnp.exp(alpha_state.apply_fn(alpha_state.params))
            loss = jnp.mean(alpha * log_probs - qvalues)
            return loss, {"loss_policy": loss, "entropy": -jnp.mean(log_probs)}

        (loss, info), grads = jax.value_and_grad(loss_fn, has_aux=True)(
            policy_state.params
        )
        policy_state = policy_state.apply_gradients(grads=grads)

        policy_state = policy_state.replace(
            target_params=optax.incremental_update(
                policy_state.params,
                policy_state.target_params,
                config.algo_params.tau,
            )
        )
        qvalue_state = qvalue_state.replace(
            target_params=optax.incremental_update(
                qvalue_state.params,
                qvalue_state.target_params,
                config.algo_params.tau,
            )
        )

        return (policy_state, qvalue_state), loss, info

    @jax.jit
    def update_alpha_fn(
        key: jax.Array,
        policy_state: TrainState,
        alpha_state: TrainState,
        batch: tuple[jax.Array, ...],
    ):
        observations, _, _ = batch
        dists = policy_state.apply_fn(policy_state.params, observations)
        _, log_probs = dists.sample_and_log_prob(seed=key)

        def loss_fn(params: Params):
            alpha = jnp.exp(alpha_state.apply_fn(params))
            loss = alpha * -jnp.mean(log_probs + config.algo_params.target_entropy)
            return loss, {"loss_alpha": loss, "alpha": alpha}

        (loss, info), grads = jax.value_and_grad(loss_fn, has_aux=True)(
            alpha_state.params
        )
        alpha_state = alpha_state.apply_gradients(grads=grads)
        return alpha_state, loss, info

    @jax.jit
    def update_step_fn(state: SACTrainState, key: jax.Array, batch: tuple[jax.Array]):
        state.qvalue_state, loss_qvalue, info_qvalue = update_qvalue_fn(
            state.qvalue_state, batch
        )

        (state.policy_state, state.qvalue_state), loss_policy, info_policy = (
            update_policy_fn(
                state.policy_state, state.qvalue_state, state.alpha_state, batch
            )
        )

        state.alpha_state, loss_alpha, info_alpha = update_alpha_fn(
            key, state.policy_state, state.alpha_state, batch
        )

        info = info_qvalue | info_policy | info_alpha
        info["total_loss"] = loss_qvalue + loss_policy + loss_alpha

        return state, info

    return update_step_fn


class SAC(OffPolicyAgent):
    """Sof Actor Crtic (SAC)"""

    def __init__(
        self,
        config: AlgoConfig,
        *,
        rearrange_pattern: str = "b h w c -> b h w c",
        preprocess_fn: Callable = None,
        run_name: str = None,
        tabulate: bool = False,
    ):
        AlgoFactory.intialize(
            self,
            config,
            train_state_sac_factory,
            explore_factory,
            process_experience_factory,
            update_step_factory,
            rearrange_pattern=rearrange_pattern,
            preprocess_fn=preprocess_fn,
            run_name=run_name,
            tabulate=tabulate,
            experience_type=Experience,
        )

        self.config.algo_params.target_entropy = (
            -self.config.env_cfg.action_space.shape[-1] / 2
        )
        self.algo_params = self.config.algo_params

    def select_action(self, observation: jax.Array) -> tuple[jax.Array, jax.Array]:
        return self.explore(observation)

    def explore(self, observation: jax.Array) -> tuple[jax.Array, jax.Array]:
        keys = self.interact_keys(observation)
        action, log_prob = self.explore_fn(self.state.policy_state, keys, observation)

        if self.step < self.algo_params.start_step:
            action = jax.random.uniform(
                self.nextkey(), action.shape, minval=-1.0, maxval=1.0
            )
            log_prob = jnp.zeros_like(action)
        return action, log_prob

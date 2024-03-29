from typing import Any, Callable, NamedTuple

from flax import struct
import jax
import jax.numpy as jnp

from rl_tools.buffer import stack_experiences


class ExperienceTransform(struct.PyTreeNode):
    process_experience_fn: Callable = struct.field(pytree_node=False)
    state: Any = struct.field(pytree_node=True)


def process_experience_pipeline_factory(
    vectorized: bool,
    parallel: bool,
    experience_type: NamedTuple,
) -> Callable:
    def process_experience_pipeline_fn(
        experience_transforms: list[ExperienceTransform],
        key: jax.Array,
        experiences: tuple | list[tuple],
    ) -> NamedTuple:
        if isinstance(experiences, list):
            experiences = stack_experiences(experiences)

        def process_experience_fn(key: jax.Array, *experience: tuple[jax.Array, ...]):
            experience = experience_type(*experience)
            for transform in experience_transforms:
                experience = transform.process_experience_fn(
                    transform.state, key, experience
                )
            return experience

        if parallel and vectorized:
            keys = {}
            for agent, value in experiences[0].items():
                # vectorized => shape [T, n_envs, ...]
                _keys = jax.random.split(key, value.shape[1] + 1)
                key, keys[agent] = _keys[0], _keys[1:][None]

            processed_experiences = jax.tree_map(
                jax.vmap(process_experience_fn, in_axes=1, out_axes=1),
                keys,
                *experiences,
            )

            def concat_and_reshape(*x: tuple[jax.Array, ...]) -> jax.Array:
                # x n_agents * (T, n_envs, ...)
                # concat > (T, n_agents * n_envs, ...)
                # reshape => (T * n_agents * n_envs, ...)
                out = jnp.concatenate(x, axis=1)
                return jnp.reshape(out, (-1, *out.shape[2:]))

            # TODO check why tuple of length 1 !!!
            return jax.tree_map(
                concat_and_reshape, *zip(processed_experiences.values())
            )[0]

        if parallel:
            keys = {}
            for agent, value in experiences[0].items():
                key, keys[agent] = jax.random.split(key, 2)

            processed_experiences = jax.tree_map(
                process_experience_fn, keys, *experiences
            )

            def stack_and_reshape(*x: tuple[jax.Array, ...]) -> jax.Array:
                # x n_agents * (T,  ...)
                # stack > (T, n_agents, ...)
                # reshape => (T * n_agents, ...)
                out = jnp.stack(x, axis=1)
                return jnp.reshape(out, (-1, *out.shape[2:]))

            # TODO check why tuple of length 1 !!!
            return jax.tree_map(
                stack_and_reshape, *zip(processed_experiences.values())
            )[0]

        if vectorized:
            keys = jax.random.split(key, experiences[0].shape[1])[None]
            processed_experiences = jax.vmap(
                process_experience_fn, in_axes=1, out_axes=1
            )(keys, *experiences)
            return jax.tree_map(
                lambda x: jnp.reshape(x, (-1, *x.shape[2:])), processed_experiences
            )

        return process_experience_fn(key, *experiences)

    return process_experience_pipeline_fn


class UpdateModule(struct.PyTreeNode):
    """Jittable PyTreeNode instance.

    state: A PyTreeNode that contains the module state

    update_fn:
        Args:
            state: A PyTreeNode that contains the module state
            key: An Array for randomness in Jax
            batch: A tuple
        Returns:
            state: The state after update
            module_info: A dictionary of additional information

    """

    update_fn: Callable = struct.field(pytree_node=False)
    state: struct.PyTreeNode = struct.field(pytree_node=True)


def update_pipeline(
    update_modules: list[UpdateModule],
    key: jax.Array,
    batch: tuple,
) -> list[UpdateModule]:
    info = {}

    for i, module in enumerate(update_modules):
        key, _key = jax.random.split(key, 2)
        state, module_info = module.update_fn(module.state, _key, batch)

        update_modules[i] = module.replace(state=state)
        info |= module_info

    return update_modules, info


class PipelineModule(ExperienceTransform, UpdateModule):
    @property
    def experience_transform(self) -> ExperienceTransform:
        return ExperienceTransform(
            process_experience_fn=self.process_experience_fn, state=self.state
        )

    @property
    def update_module(self) -> UpdateModule:
        return UpdateModule(update_fn=self.update_fn, state=self.state)

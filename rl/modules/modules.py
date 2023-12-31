import functools
from typing import Callable, Type

import distrax as dx
from einops import rearrange
from flax import linen as nn
from gymnasium import spaces
import jax
from jax import numpy as jnp
import numpy as np

from rl.types import Params


def conv_layer(
    features: int,
    kernel_size: int,
    strides: int,
    kernel_init_std: float = np.sqrt(2.0),
    bias_init_cst: float = 0.0,
) -> nn.Conv:
    return nn.Conv(
        features,
        (kernel_size, kernel_size),
        strides,
        padding="VALID",
        kernel_init=nn.initializers.orthogonal(kernel_init_std),
        bias_init=nn.initializers.constant(bias_init_cst),
    )


class VisionEncoder(nn.Module):
    rearrange_pattern: str
    preprocess_fn: Callable

    @nn.compact
    def __call__(self, x: jax.Array):
        x = x.astype(jnp.float32)
        x = rearrange(x, self.rearrange_pattern)
        if self.preprocess_fn is not None:
            x = self.preprocess_fn(x)

        x = conv_layer(32, 8, 4)(x)
        x = nn.relu(x)
        x = conv_layer(64, 4, 2)(x)
        x = nn.relu(x)
        x = conv_layer(64, 3, 1)(x)
        x = nn.relu(x)

        x = jnp.reshape(x, (x.shape[0], -1))
        x = nn.Dense(
            features=512,
            kernel_init=nn.initializers.orthogonal(2.0),
            bias_init=nn.initializers.constant(0.0),
        )(x)
        return nn.relu(x)


class VectorEncoder(nn.Module):
    preprocess_fn: Callable

    @nn.compact
    def __call__(self, x: jax.Array):
        x = x.astype(jnp.float32)
        if self.preprocess_fn is not None:
            x = self.preprocess_fn(x)

        x = nn.Dense(
            features=64,
            kernel_init=nn.initializers.orthogonal(np.sqrt(2.0)),
            bias_init=nn.initializers.constant(0.0),
        )(x)
        x = nn.tanh(x)
        x = nn.Dense(
            features=64,
            kernel_init=nn.initializers.orthogonal(np.sqrt(2.0)),
            bias_init=nn.initializers.constant(0.0),
        )(x)
        return nn.tanh(x)


class PassThrough(nn.Module):
    @nn.compact
    def __call__(self, x: jax.Array):
        return x


def encoder_factory(
    observation_space: spaces.Space,
    *,
    rearrange_pattern: str = "b h w c -> b h w c",
    preprocess_fn: Callable = None,
) -> Type[nn.Module]:
    if len(observation_space.shape) == 1:
        return functools.partial(VectorEncoder, preprocess_fn=preprocess_fn)
    elif len(observation_space.shape) == 3:
        return functools.partial(
            VisionEncoder,
            rearrange_pattern=rearrange_pattern,
            preprocess_fn=preprocess_fn,
        )
    else:
        raise NotImplementedError


def init_params(
    key: jax.Array,
    module: nn.Module,
    input_shapes: tuple[int] | list[tuple[int]],
    tabulate: bool,
) -> Params:
    if not isinstance(input_shapes, list):
        input_shapes = [input_shapes]

    dummy_inputs = [jnp.ones((1,) + shape) for shape in input_shapes]
    variables = module.init(key, *dummy_inputs)

    if tabulate:
        tabulate_fn = nn.tabulate(
            module, key, compute_flops=True, compute_vjp_flops=True
        )
        print(tabulate_fn(*dummy_inputs))

    if "params" in variables.keys():
        return variables["params"]
    return {}

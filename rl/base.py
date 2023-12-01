from abc import ABC, abstractmethod
from datetime import datetime
from enum import Enum
import os
from typing import Any, TypeVar

import flax
from flax.training import train_state
import jax.random as jrd

ActionType = TypeVar("ActionType")
ObsType = TypeVar("ObsType")
Params = flax.core.FrozenDict

from rl.buffer import Buffer
from rl.save import Saver
from rl import Seeded


class EnvProcs(Enum):
    ONE = "one"
    MANY = "many"


class EnvType(Enum):
    SINGLE = "single"
    PARALLEL = "parallel"


class Base(ABC, Seeded):
    def __init__(self, seed: int, *, run_name: str = None):
        Seeded.__init__(self, seed)
        self.state: train_state.TrainState = None

        run_name = run_name
        if run_name is None:
            run_name = datetime.now().strftime("%m-%d-%Y_%H-%M-%S")
        self.saver = Saver(os.path.join("./results", run_name))

    @abstractmethod
    def select_action(self, observation: ObsType) -> ActionType:
        ...

    @abstractmethod
    def explore(self, observation: ObsType) -> ActionType:
        ...

    @abstractmethod
    def update(self, buffer: Buffer) -> None:
        ...

    @abstractmethod
    def train(self, env: Any, n_env_steps: int) -> None:
        ...

    @abstractmethod
    def resume(self, env: Any, n_env_steps: int) -> None:
        ...

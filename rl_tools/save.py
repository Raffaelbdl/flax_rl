from contextlib import AbstractContextManager
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any

import cloudpickle
from flax.training import orbax_utils, train_state
import orbax.checkpoint
import yaml

if TYPE_CHECKING:
    from rl_tools.base import Agent


class Saver:
    """Saver class for agents.

    Handles saving during training and restoring from checkpoints.
    """

    def __init__(self, dir: str | Path, base: "Agent") -> None:
        """Initializes a Saver instance for an agent.

        Args:
            dir: A string or path-like path to the saving directory.
            base: The parent agent.
        """
        dir = Path(dir)

        self.ckptr = orbax.checkpoint.PyTreeCheckpointer()
        self.options = orbax.checkpoint.CheckpointManagerOptions(
            max_to_keep=None, create=True
        )
        self.ckpt_manager = orbax.checkpoint.CheckpointManager(
            dir, self.ckptr, self.options
        )

        self.save_base_data(dir, base)

    def save_base_data(self, dir: Path, base: "Agent") -> None:
        config_dict = base.config.to_dict()
        env_config = config_dict.pop("env_cfg")

        config_path = dir.joinpath("config")
        with config_path.open("w") as f:
            yaml.dump(config_dict, f)

        extra_path = dir.joinpath("extra")
        with extra_path.open("wb") as f:
            cloudpickle.dump(
                {
                    "env_config": env_config,
                    "run_name": base.run_name,
                    "rearrange_pattern": base.rearrange_pattern,
                    "preprocess_fn": base.preprocess_fn,
                    # "tabulate": base.tabulate,
                },
                f,
            )

    def save(self, step: int, ckpt: dict[str, Any]):
        save_args = orbax_utils.save_args_from_target(ckpt)
        self.ckpt_manager.save(step, ckpt, save_kwargs={"save_args": save_args})

    def restore_latest_step(
        self, base_state_dict: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        step = self.ckpt_manager.latest_step()
        return (
            step,
            self.ckpt_manager.restore(step, items=base_state_dict),
        )


class SaverContext(AbstractContextManager):
    def __init__(self, saver: Saver, save_frequency: int) -> None:
        super().__init__()
        self.saver = saver

        self.save_frequency = save_frequency
        self.cur_step = 0
        self.cur_state = None

    def update(self, step: int, state: train_state.TrainState):
        self.cur_step = step
        self.cur_state = state

        if self.save_frequency < 0:
            return

        if step % self.save_frequency != 0:
            return

        self.saver.save(step, state)

    def __exit__(
        self,
        __exc_type: type[BaseException] | None,
        __exc_value: BaseException | None,
        __traceback: TracebackType | None,
    ) -> bool | None:
        if self.cur_state is None:
            return

        self.saver.save(self.cur_step, self.cur_state)

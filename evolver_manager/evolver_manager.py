import logging
import time
from pathlib import Path

import socketio

from evolver_manager import evolver_controls
from evolver_manager.bioreactors import bioreactor
from evolver_manager.utils import type_hints


class EvolverManager(socketio.ClientNamespace):
    """A class to manage a single evolver unit with multiple smart sleeves"""

    SLEEVE_MAX = 16
    SLEEVE_MIN = 0

    def __init__(
        self,
        namespace: str,
        master_dir: Path,
        controls: evolver_controls.EvolverControls,
    ):
        """Initializes a evolver manager.

        Creats a logger, and checks the working directory to see if there is a log
        file. Then it tries to connect to an evolver unit with given ip address and
        port using the namespace evolver-dpu.
        """
        super().__init__(namespace)
        self.logger = logging.Logger(__name__)
        self.log_path = master_dir

        self._active_reactors: dict[str, list[tuple[bioreactor.Bioreactor, Path]]] = {}
        self._exp_directories: dict[str, Path]
        self.updates = []
        self._controls: evolver_controls.EvolverControls = controls

    @property
    def has_no_active_experiments(self):
        return len(self._active_reactors) <= 0

    @property
    def controls(self):
        return self._controls

    @property
    def has_updates(self):
        return len(self.updates) > 0

    def on_broadcast(self, broadcast: type_hints.Broadcast):
        """Method to handle broadcast and updating active reactors

        Broadcast will update all reactors under its management, then write to each
        working directory's OD and raw_data folders. Then it adds a fluid update to
        update so fluid tracking can be done.
        """
        self.logger.debug("broadcast received")

        # 1. Start fresh for this tick
        self.controls.reset_queues()
        # Get raw data
        raw = broadcast["data"]["data"]
        dpu_time = time.time()
        raw_data = {}
        for data_type in ["od_90", "od_135", "temperature"]:
            raw_data[data_type] = raw.get(data_type, None)

        # Updating reactor states and writing data values to working directories
        for _, reactors in self._active_reactors.items():
            for reactor, working_dir in reactors:
                # Update the reactor logic
                update_msg = reactor.update(broadcast, dpu_time)
                # Writing raw data
                for data_type in ["od_90", "od_135", "temperature"]:
                    if raw_data[data_type]:
                        for vial, od in zip(reactor.vials, reactor.od_readings):
                            # Writing fitted OD
                            file_name = Path(working_dir, "OD", str(vial))
                            with open(file_name, "a", encoding="utf8") as f:
                                f.write(f"{dpu_time}, {od}\n")

                            # Writing Raw_data
                            data_val = raw_data[data_type][vial]
                            file_name = Path(
                                working_dir, "raw_data", data_type, str(vial)
                            )
                            with open(file_name, "a", encoding="utf8") as f:
                                f.write(f"{dpu_time}, {data_val}\n")

                single, recurrent = reactor.parse_fluid_usage(update_msg)
                self.updates.append(
                    {"type": "fluid", "single": single, "recurrent": recurrent}
                )

        self.controls.dispatch_queues()
        return

    def add_experiment(self, name: str, reactor: dict, working_directory: Path):
        self.logger.info("Adding experiment")
        if name not in self._active_reactors:
            self._active_reactors[name] = []
        self._active_reactors[name].append((reactor, working_directory))

    def end_experiment(self, name):
        if name in self._active_reactors:
            del self._active_reactors[name]
        self.logger.info("Found")

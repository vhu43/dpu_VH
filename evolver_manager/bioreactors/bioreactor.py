"""Abstract class"""

import collections
import dataclasses
import importlib
import logging
import time
from abc import ABC, abstractmethod

import numpy as np

import global_config
from evolver_manager import evolver_controls
from evolver_manager.fits import fit
from evolver_manager.utils import type_hints


@dataclasses.dataclass
class FitSet:
    vial: int
    od_fit: fit.Fit
    temp_fit: fit.Fit
    in1: fit.Fit
    in2: fit.Fit
    out: fit.Fit


@dataclasses.dataclass
class ReactorSettings:
    """Class for storing programing reactor settings

    Attributes:
      volume: The volume supported by the vials
      n_to_avg: the OD readings to take moving average of
      vials: The vials that are in this mode
      temp: the temperature the vials should be kept at
      stir: the stir rate the vials should mixed at
      power: the LED power that should be used
      start_delay: The time to run before update commands occur
    """

    # Internal Run Parameters
    volumes: tuple[float, ...] = dataclasses.field(default_factory=tuple)
    mem_len: int = 1

    # Vial control
    vials: tuple[int, ...] = dataclasses.field(default_factory=tuple)
    temp: tuple[float, ...] = dataclasses.field(default_factory=tuple)
    stir: tuple[int, ...] = dataclasses.field(default_factory=tuple)
    power: tuple[int, ...] = dataclasses.field(default_factory=tuple)

    # Run conditions
    start_delay: tuple[float, ...] = dataclasses.field(default_factory=tuple)
    durations: tuple[float, ...] = dataclasses.field(default_factory=tuple)

    def __post_init__(self):
        """Checking values to ensure that it is correctly formatted"""
        self.vials = tuple(self.vials)
        self.stir = tuple(self.stir)
        self.power = tuple(self.power)
        self.temp = tuple(self.temp)
        self.start_delay = tuple(self.start_delay)
        self.duration = tuple(self.durations)
        self.volumes = tuple(self.volumes)

        invalid_settings = False
        wrong_settings = []
        iterable_settings = {
            "start_delay": self.start_delay,
            "duration": self.duration,
            "stir": self.stir,
            "power": self.power,
            "temp": self.temp,
            "volume": self.volumes,
        }
        for name, param in iterable_settings.items():
            if len(self.vials) != len(param):
                invalid_settings = True
                wrong_settings.append(name)

        if invalid_settings:
            wrong_settings = ", ".join(wrong_settings)
            raise ValueError(
                f"Number of vials do not match number of elements in {wrong_settings}"
            )


REACTOR_SETTINGS_FIELDS = (f.name for f in dataclasses.fields(ReactorSettings))


class Bioreactor(ABC):
    """An template for any runmode for the evolver.

    A template to handle evolver run modes that enables multiplexing experiments
    on a single evolver unit. Each class handles a subset of vials and its own
    logging during updating, and allow for multiplexing using a single dpu server
    to handle atomic updates and recording.

    Attributes:
      name: Experiment name for the instance (read-only)
      logger: The logger used by the experiment to log events (read-only)
      vials: The set of vials handled by this bioreact experiment (read-only)
      volume: the volume set for this experiment (read-only)
      mem_len: the number of past od/temp values remembered by machine (read-only)
      temp_setpoints: A tuple of the temperature setpoints for this bioreactor
        (read-only)
      start_delay: A tuple of floats denoting the time to wait before starting
        (read-only)
      durations: A tuple of floats denoting the time to run that vial for
        (read-only)
      od_history: A reference to the dictionary that records past od values
        processed (read-only)
      temp_history:  A reference to the dictionary that records past temp values
        processed(read-only)
      od_readings: a tuple of the last od readings procesed by the machine, sorted
        by vials
      temp_history: a tuple of the last temp readings processed by the machine,
        sorted by vials
      num_readings: An int that tells you how many successfully processed
        broadcasts have occured
    """

    _DELTA_T = global_config.DELTA_T
    _DELAY_UNITS_TO_SECONDS = global_config.SECS_PER_UNIT_TIME

    def __init__(
        self,
        name: str,
        evolver: evolver_controls.EvolverControls,
        settings: ReactorSettings,
        calibrations: type_hints.Calibration = None,
        manager: str = __name__,
    ):
        """Initializes the instance using common parameters for all runs

        Args:
          name: Name for the instance
          evolver: an evolver namespace for broadcasting and sending signals
          settings: volume, temp, stir, and led power settings
          calibration: the calibration dictionary to use for the bioreactor
          manager: the name used for the experiment manager for logging
        """
        self._name = name
        self._evolver = evolver
        self._base_settings: ReactorSettings = settings
        self._logger = logging.LoggerAdapter(
            logging.getLogger(manager), {"name": self._name}
        )

        self._od_sensors = calibrations["od"]["params"]
        self._temp_sensors = calibrations["temp"]["params"]
        self._data_fits: tuple[FitSet, ...] = self._format_cal(calibrations)

        self._past_od_readings: dict[int, collections.deque] = {}
        self._past_temp_readings: dict[int, collections.deque] = {}
        self._num_readings: int = 0
        for vial in self.vials:
            self._past_od_readings[vial] = collections.deque(maxlen=self.mem_len)
            self._past_temp_readings[vial] = collections.deque(maxlen=self.mem_len)

        birth_time = round(time.time(), -1)  # Get the time that this was made
        start_times = []
        end_times = []
        times = zip(self._base_settings.start_delay, self._base_settings.durations)
        for delay, duration in times:
            start_time = birth_time + self._DELAY_UNITS_TO_SECONDS * delay
            end_time = start_time + self._DELAY_UNITS_TO_SECONDS * duration

            start_times.append(start_time)
            end_times.append(end_time)
        self._start_times = tuple(start_times)
        self._end_times = tuple(end_times)

    def _format_cal(self, calibrations: type_hints.Calibration):
        """Helper method to obtain calibration data from calibration dicationary

        Method to store calibrations into private attributes. This results in the
        generation of a tuple of 5-tuples. The 5-tuple is a named tuple with names
        od_fits, temp_fits, in1, in2, and out

        returns:
          A tuple of named-tuples containing the calibrations. The named tuple is of
          the names: vial, od_fit, temp_fit, in1, in2, out
        """
        od_fits = []
        temp_fits = []

        in1 = []
        in2 = []
        out = []
        pump_cal = [in1, in2, out]

        to_process: list[tuple[list, type_hints.SensorCal]] = []

        # Obtaining fits from calibrations argument
        to_process.append((od_fits, calibrations["od"]))
        to_process.append((temp_fits, calibrations["temp"]))

        # Process pumps as layers:
        shift = 0
        pump_base = calibrations["pump"]
        for item in pump_cal:
            pump_set_cal: type_hints.SensorCal = {
                "name": pump_base.get("name", "pump"),
                "type": pump_base.get("type", "linear"),
                "timeFit": pump_base.get("timeFit", 0.0),
                "active": pump_base.get("active", True),
                "params": pump_base.get("params", []),
                "coefficients": [],
            }
            start = 0 + shift
            end = self._evolver.num_vials_total + shift
            set_coefs = calibrations["pump"]["coefficients"][start:end]
            pump_set_cal["coefficients"] = set_coefs
            to_process.append((item, pump_set_cal))
            shift += self._evolver.num_vials_total

        # Obtain proper fit class name and module names to get fit object
        for location, fit_data in to_process:
            module_name: str = fit_data["type"]
            class_name = module_name.capitalize()
            curve_module = importlib.import_module(
                f"evolver_manager.fits.{module_name}"
            )
            fit_class = getattr(curve_module, class_name)
            coefficients = fit_data["coefficients"]
            for vial in self.vials:
                parameters = coefficients[vial]
                vial_fit = fit_class(parameters)
                location.append(vial_fit)

        data_fits = []
        for fit_info in zip(self.vials, od_fits, temp_fits, in1, in2, out):
            data_fits.append(FitSet(*fit_info))
        return tuple(data_fits)

    def _manage_temperature(
        self, temp_readings: tuple[float, ...], config: type_hints.ExperimentalParams
    ):
        """Function to manage temperature differences.

        A function to check for discrepencies between temperature setpoint and
        temperature readings. If there is a large discrepency (defined by class
        constant _DELTA_T) then it will compare configurations with evolver
        configuration, and update if needed.

        Args:
          temp_readings: A set of temperature readings obtained from a broadcast
          config: The configuration obtained from a broadcast
        """
        max_diff = np.max(np.abs(np.subtract(temp_readings, self.temp_setpoints)))
        if max_diff <= self._DELTA_T:
            return

        # There is a discrepency
        vials_to_adjust = []
        new_temp_setpoints = []
        fit_set: FitSet
        adjusting = False
        evolver_setpt = config["temp"]["value"]
        for fit_set, setpoint in zip(self._data_fits, self._base_settings.temp):
            vial = fit_set.vial
            raw_temp_setpoint = fit_set.temp_fit.get_inverse(setpoint)
            if evolver_setpt[vial] != raw_temp_setpoint:
                adjusting = True
                vials_to_adjust.append(vial)
                new_temp_setpoints.append(fit_set.temp_fit.get_inverse(setpoint))
        # If there is a discrepency, get the config and compare
        if adjusting:
            self.logger.info(
                "updating temperatures, local setpoint differs from evolver setpoint"
            )
            self._evolver.update_temperature(vials_to_adjust, new_temp_setpoints)
        else:  # There is no config discrepency. Record this fact.
            self.logger.debug(
                "actual temperature does not match configuration "
                f"(yet? max deltaT is {self._DELTA_T:.2f}"
            )
            self.logger.debug(f"temperature config:  {self.temp_setpoints}")
            self.logger.debug(f"actual temperatures: {temp_readings}")
        return

    def process_data(
        self, data: type_hints.DataDict
    ) -> tuple[tuple[float, ...], tuple[float, ...]]:
        """Basic method to process evolver data

        Robust data processing to obtain measurement of OD and Temperature from
        transducer readout. Takes in fits from the fit subpackage to process the
        data given from the evolver.

        Arguments:
          data: The output of a broadcast taken from the evolver namespace

        returns:
          od_readings: an tuple of od readings from fit
          temp_readings: an tuple of temperature readings from fit
        """
        # Pull out data from broadcast data structure
        od_data = []
        temp_data = []
        for od_sensor in self._od_sensors:
            od_data.append(data["data"].get(od_sensor, None))
        for temp_sensor in self._temp_sensors:
            temp_data.append(data["data"].get(temp_sensor, None))
        od_data = np.array(od_data)
        temp_data = np.array(temp_data)

        if od_data is None or temp_data is None:
            print("Incomplete data recieved, Error with measurement")
            self.logger.error("Incomplete data received, error with measurements")
            return None

        # Get OD and Temp Readings from raw data and print to logger
        od_readings = []
        temp_readings = []

        od_fit: fit.Fit
        temp_fit: fit.Fit
        for vial, od_fit, temp_fit, *_ in self._data_fits:
            try:
                od_reading = od_fit.get_val(od_data[:, vial])[0]
                if not np.isfinite(od_reading):
                    od_reading = np.nan
                    self.logger.error(f"OD from vial {vial} read as infinite")
                    self.logger.debug(f"OD from vial {vial}: {od_reading}")
                else:
                    self.logger.debug(f"OD from vial {vial}: {od_reading:.3f}")
            except ValueError:
                self.logger.error(f"OD read error for vial {vial}, setting to NaN")
                od_reading = np.nan

            try:
                temp_reading = temp_fit.get_val(od_data[:, vial])[0]
            except ValueError:
                self.logger.error(
                    f"temperature read error for vial {vial}, setting to NaN"
                )
                temp_reading = np.nan

        od_readings.append(od_reading)
        temp_readings.append(temp_reading)

        return tuple(od_readings), tuple(temp_readings)

    def pre_update(
        self, broadcast: type_hints.Broadcast, curr_time: float
    ) -> tuple[bool, list[type_hints.Record]]:
        """Standard process to obtain fitted data and process start delays.

        pre-update obtains the od_readings and temp_readings from broadcast,
        then records them in the memory. Afterwards, it checks the temperature
        configuration and logs any discrepency. After that it checks if enough
        time has passed to start the run.

        Args:
          broadcast: a broadcast from an evolver unit
          time: the time of the last broadcast

        Returns:
          A boolean that is True if we are past the minimum start delay, and
          False if we are still before the minimum start delay
        """
        records: list[type_hints.Record] = []

        # Record OD readings
        od_readings, temp_readings = self.process_data(broadcast["data"])
        for vial, od, temp in zip(self.vials, od_readings, temp_readings):
            self.od_history[vial].append(od)
            self.temp_history[vial].append(temp)
            self._num_readings += 1
        self._manage_temperature(temp_readings, broadcast["config"])

        # Check if any vial is past their start time
        if curr_time < min(self.start_times):
            self._logger.debug("Not at start time yet")
            return False, None

        # Stop any vials that no longer need updating
        to_stop: list[int] = []
        for vial, end_time in zip(self.vials, self.end_times):
            if end_time and curr_time > end_time:
                records.append({"vial": vial, "command": "stop"})
                to_stop.append(vial)

        self._evolver.stop_pumps(to_stop)
        return True, records

    @abstractmethod
    def update(
        self, broadcast: type_hints.Broadcast, curr_time: float
    ) -> type_hints.UpdateMsg:
        raise NotImplementedError("sublcasses should impelemnt this")

    @classmethod
    def parse_fluid_usage(
        cls, update_msg
    ) -> tuple[dict[int, dict[str, float]], dict[int, dict[str, tuple[float, float]]]]:
        """Class method to prase record output from this class for fluid tracking"""
        dilution = {}
        recurrent = {}

        for record in update_msg["record"]:
            vial = record["vial"]
            if record["command"] == "recurrent":
                if vial not in recurrent:
                    recurrent[vial] = dict(zip(global_config.PUMP_SET, [None] * 3))
                    for pump in global_config.PUMP_SET:
                        recurrent[vial][pump] = record[pump]
            elif record["command"] == "dilution":
                if vial not in dilution:
                    recurrent[vial] = dict(zip(global_config.PUMP_SET, [None] * 3))
                    for pump in global_config.PUMP_SET:
                        recurrent[vial][pump] = record[pump]
            else:
                continue
        return dilution, recurrent

    @property
    def name(self):
        """Read only name access. Returns a string of the experiment name."""
        return self._name

    @property
    def logger(self):
        """Read only access to logger responsible for this experiment."""
        return self._logger

    @property
    def vials(self):
        """Read only vial access. Returns a tuple of vials for this experiment."""
        return self._base_settings.vials

    @property
    def volumes(self):
        """Read only access to the volume set for this experiment."""
        return self._base_settings.volumes

    @property
    def mem_len(self):
        """Read-only access to the memory size for od and temp readings"""
        return self._base_settings.mem_len

    @property
    def temp_setpoints(self):
        """Read only experimental temperature setpoints."""
        return self._base_settings.temp

    @property
    def start_times(self):
        """Read only start times access."""
        return self._start_times

    @property
    def end_times(self):
        """Read only run end_times access."""
        return self._end_times

    @property
    def od_history(self):
        """Read only access to reference of stored od readings."""
        return self._past_od_readings

    @property
    def temp_history(self):
        """Read only access to reference of stored temp readings."""
        return self._past_temp_readings

    @property
    def od_readings(self):
        """Read only access to last OD reading processed by the bioreactor."""
        od_readings = []
        for vial in self.vials:
            od_readings.append(self.od_history[vial][-1])
        return tuple(od_readings)

    @property
    def temp_readings(self):
        """Read-only access to last temp reading processed by the bioreactor"""
        temp_readings = []
        for vial in self.vials:
            temp_readings.append(self.temp_history[vial][-1])
        return tuple(temp_readings)

    @property
    def num_readings(self):
        """Read only access to number of valid broadcasts have occured"""
        return self._num_readings

"""Module to handle communication with the evolver unit."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Iterable, List, Tuple

import numpy as np
import socketio

import global_config
from evolver_manager.utils import type_hints

if TYPE_CHECKING:
    from evolver_manager.bioreactors import bioreactor


class EvolverControls(socketio.ClientNamespace):
    """A namespace class to handle evolver communication for commands"""

    _PUMP_SET = ("IN1", "IN2", "OUT")

    FLOAT_RESOLUTION = 2
    DEFAULT_TIMEOUT = 10.0
    DEFAULT_PERIOD = 0.2

    _OUTFLOW_EXTRA = global_config.OUTFLOW_EXTRA
    _BOLUS_VOLUME_MIN = global_config.BOLUS_VOLUME_MIN
    _BOLUS_VOLUME_MAX = global_config.BOLUS_VOLUME_MAX
    _POW_PARAM = global_config.POW_PARAM
    _CONST_PARAM = global_config.CONST_PARAM
    _BOLUS_REPEAT_MAX = global_config.BOLUS_REPEAT_MAX
    _SECS_PER_UNIT_TIME = global_config.SECS_PER_UNIT_TIME
    _MIN_PUMP_PERIOD = global_config.MIN_PUMP_PERIOD
    _PUMP_TIME_MAX = global_config.PUMP_TIME_MAX

    def __init__(
        self,
        namespace: str = None,
        caller: str = None,
        num_vials_total=global_config.NUM_VIALS_DEFAULT,
    ):
        super().__init__(namespace)
        self._logger: logging.Logger = logging.Logger(caller)
        self._awaiting_response: bool = False

        self._num_vials_total = num_vials_total

        # Evolver data
        self._timestamp: float = 0
        self._data: type_hints.Broadcast = None
        self._power: Tuple[int] = ()
        self._stir_rate: Tuple[int] = ()
        self._temp_setpoint: Tuple[int] = ()
        self._active_calibrations: type_hints.SensorCal = None
        self._calibration_names = None

        # For pausing if need to refill media
        self._locked = False
        self._recurrent_commands = {}
        self._paused_dilutions = {}

        # We maintain two separate queues: one for immediate bolus commands
        # and one for recurring schedule updates.
        self._immediate_queue = ["--"] * 3 * self.num_vials_total
        self._recurring_queue = ["--"] * 3 * self.num_vials_total

        for pump in self._PUMP_SET:
            self._recurrent_commands[pump] = [None] * self.num_vials_total
            self._paused_dilutions[pump] = [None] * self.num_vials_total

    def lock(self):
        """Method to disable pumps.

        This method first checks for recurrent pump commands, and stops them. Then
        it tells controls to repeatedly store targets instead of pushing commands
        """
        if self._locked:
            return
        self._locked = True
        pump_vals = self._data["config"]["pump"]["value"]
        vials = set()
        for i, value in enumerate(pump_vals):
            if hasattr(value, "__iter__") and "|" in value:
                pump = self._PUMP_SET[i // self.num_vials_total]
                vial = i % self.num_vials_total

                self._recurrent_commands[pump][vial] = value
                vials.add(vial)
        self.stop_pumps(list(vials))
        return

    def unlock(self):
        """Method to reenable pumps.

        This method first restores recurrent pump commands, and then sends a pump
        command to the remaining vials to pump the needed amounts to reach the
        updated targets.
        """
        # Restart Recurrent commands
        vials = []
        recurrent_cmd = [self._recurrent_commands[pump] for pump in self._PUMP_SET]
        self.fluid_command(vials=vials, *recurrent_cmd, recurring=True)

        dilution_cmd = [self._paused_dilutions[pump] for pump in self._PUMP_SET]
        self.fluid_command(vials=vials, *dilution_cmd)
        self._locked = False

    @classmethod
    def compute_bolus_volume(
        cls,
        current_od: float,
        starting_od: float,
        target_od: float,
        steps: int,
        volume: float,
    ) -> float:
        """A method to compute bolus volume from current OD and target

        Assuming OD is in linear range, OD should change like concentration and so
        we compute the bolus volume to dilute in total by dividing. Then we compute
        a serial dilution scheme based on the set parameter, and apply a robustness
        adjustment to deal with noise. It then returns the volume to for the bolus.
        Robustness obtained heuristically but lowers media usage while being
        robust to under-pumping and fast cell growth.

        returns:
          The volume of bolus to give at this time
        """
        if steps <= 1:
            return volume * (current_od / target_od - 1)
        scale = np.log(starting_od / target_od)
        location = (steps - 1) * np.log(current_od / target_od) / scale
        robustness_param = 1 - 2 * (location / steps) ** cls._POW_PARAM
        dilutions_left = location + cls._CONST_PARAM * robustness_param
        dilution_factor = current_od / target_od
        bolus_volume = (np.power(dilution_factor, 1 / dilutions_left) - 1) * volume

        if bolus_volume <= 0:
            return 0

        bolus_volume = min(bolus_volume, cls._BOLUS_VOLUME_MAX)
        bolus_volume = max(bolus_volume, cls._BOLUS_VOLUME_MIN)
        return bolus_volume

    @classmethod
    def adjust_bolus_rate(
        cls, bolus: float, rate: float, total_volume: float
    ) -> tuple[bool, float, float]:
        """Helper method to adjust bolus and period if needed

        Args:
          bolus: a bolus in mL
          rate: a rate in 1/UNIT_TIME (unit defined defined by class)
          volume: the volume per experiment

        returns:
          Adjusted: a bool
        """
        period = (cls._SECS_PER_UNIT_TIME * bolus) / (rate * total_volume)
        max_rate = (cls._SECS_PER_UNIT_TIME * bolus) / (
            cls._MIN_PUMP_PERIOD * total_volume
        )
        adjusted = False

        if bolus > cls._BOLUS_REPEAT_MAX:  # Bolus too high
            adjusted = True
            rate = rate * (cls._BOLUS_REPEAT_MAX / bolus)
            bolus = cls._BOLUS_REPEAT_MAX
            if rate > max_rate:  # infeasible
                raise ValueError(
                    "Rate and bolus are incompatible withoverflow protection"
                )

        elif rate > max_rate:  # Rate too high
            adjusted = True
            bolus = bolus * cls._MIN_PUMP_PERIOD / period
            rate = rate * period / cls._MIN_PUMP_PERIOD

            if bolus > cls._BOLUS_REPEAT_MAX:  # infeasible
                raise ValueError(
                    "Rate and bolus are incompatible withoverflow protection"
                )

        # Bolus too small, but this increases period so doesn't need check
        elif bolus < cls._BOLUS_VOLUME_MIN:
            adjusted = True
            rate = rate * (cls._BOLUS_VOLUME_MIN / bolus)
            bolus = cls._BOLUS_VOLUME_MIN

        return adjusted, bolus, rate

    def reset_queues(self):
        """Resets the command queues to the default 'no-change' state."""
        self._immediate_queue = ["--"] * 3 * self.num_vials_total
        self._recurring_queue = ["--"] * 3 * self.num_vials_total

    def dispatch_queues(self):
        """Compiles and sends the accumulated commands in the queues."""
        # Dispatch Immediate (Bolus) Commands if any exist
        if any(val != "--" for val in self._immediate_queue):
            message = {
                "fields_expected_incoming": 49,
                "fields_expected_outgoing": 49,
                "recurring": False,
                "immediate": True,
                "value": self._immediate_queue,
                "param": "pump",
            }
            self.logger.info("Dispatching unified immediate pump command")
            self.emit("command", message)

        # Dispatch Recurring (Schedule) Commands if any exist
        if any(val != "--" for val in self._recurring_queue):
            message = {
                "fields_expected_incoming": 49,
                "fields_expected_outgoing": 49,
                "recurring": True,
                "immediate": False,  # Typically false for schedule updates
                "value": self._recurring_queue,
                "param": "pump",
            }
            self.logger.info("Dispatching unified recurring pump command")
            self.emit("command", message)

        # Reset queues after dispatch to prevent double-sending
        self.reset_queues()

    def fluid_command(
        self,
        vials: Iterable[int],
        in1: Iterable[str] = None,
        in2: Iterable[str] = None,
        out: Iterable[str] = None,
        recurring: bool = False,
        immediate: bool = False,
    ):
        """Method to accumulate pump settings into the queue"""
        # Select the correct queue based on the command type
        target_queue = self._recurring_queue if recurring else self._immediate_queue

        pump_sets = [in1, in2, out]
        for idx, pump_set in enumerate(pump_sets):
            if pump_set is None:
                continue
            for vial, pump_val in zip(vials, pump_set):
                if pump_val is None:
                    continue

                # Calculate index in the 48-item array
                array_index = vial + idx * self.num_vials_total

                # Update the queue (Last Write Wins per vial/pump combo in a single tick)
                target_queue[array_index] = pump_val

        self.logger.debug("Queued pump command for vials %s", vials)

    def stop_pumps(
        self, vials: Iterable[int], in1: bool = True, in2: bool = True, out: bool = True
    ):
        """Method to queue stop commands for certain vials"""
        # Stops are immediate commands
        target_queue = self._immediate_queue

        stopped = []
        for idx, should_stop in enumerate((in1, in2, out)):
            if should_stop:
                for vial in vials:
                    array_index = vial + idx * self.num_vials_total
                    target_queue[array_index] = "0"
                stopped.append(self._PUMP_SET[idx])

        self.logger.info(
            "Queued stop for %s pumps for vials %s", ", ".join(stopped), vials
        )

    def stop_all_pumps(self):
        """Method to stop all pumps immediately"""
        # For safety, stop_all_pumps should probably still force an emit
        data = {
            "param": "pump",
            "value": ["0"] * 48,
            "recurring": False,
            "immediate": True,
        }
        self.logger.info("Stopping all pumps (Immediate)")
        self.emit("command", data)

    def dilute_single(
        self,
        vials: Iterable,
        current_ods: Iterable,
        starting_ods: Iterable,
        target_ods: Iterable,
        steps: int,
        ratios: Iterable,
        fits_list: tuple[bioreactor.FitSet],
        volumes: Iterable[float],
    ):
        # If locked, pause the pushing of fluid commands

        volume_param_list = zip(current_ods, starting_ods, target_ods)
        pump_param_list = zip(ratios, fits_list)

        pump_settings = {}
        arguments = zip(vials, volume_param_list, pump_param_list, volumes)
        in1_cmd: list[str] = []
        in2_cmd: list[str] = []
        out_cmd: list[str] = []
        for vial, volume_params, pump_params, volume in arguments:
            bolus_vol = self.compute_bolus_volume(*volume_params, steps, volume)
            bolus_vol = min(bolus_vol, self._BOLUS_VOLUME_MAX)
            ratio, fits = pump_params

            in1 = fits.in1.get_val(0)
            in2 = fits.in2.get_val(0)
            out = fits.out.get_val(0)

            in1_frac = ratio[0] / sum(ratio)
            in2_frac = ratio[1] / sum(ratio)

            in1_bolus_s = round(in1_frac * bolus_vol / in1, self.FLOAT_RESOLUTION)
            in2_bolus_s = round(in2_frac * bolus_vol / in2, self.FLOAT_RESOLUTION)
            out_bolus_s = round(
                (bolus_vol + self._OUTFLOW_EXTRA) / out, self.FLOAT_RESOLUTION
            )

            in1_bolus_s = min(self._PUMP_TIME_MAX, in1_bolus_s)
            in2_bolus_s = min(self._PUMP_TIME_MAX, in2_bolus_s)
            out_bolus_s = min(self._PUMP_TIME_MAX, out_bolus_s)

            pump_settings[vial] = (bolus_vol, in1_bolus_s, in2_bolus_s, out_bolus_s)

            in1_cmd.append(str(bolus_vol * in1_frac))
            in2_cmd.append(str(bolus_vol * in2_frac))
            out_cmd.append(str(bolus_vol + self._OUTFLOW_EXTRA))
        # Otherwise, compute commands to be sent on next pumptime
        if self._locked:
            for pump, vals in zip(self._PUMP_SET, [in1_cmd, in2_cmd, out_cmd]):
                for vial, value in zip(vials, vals):
                    self._paused_dilutions[pump][vial] = value
        else:
            self.fluid_command(vials=vials, in1=in1_cmd, in2=in2_cmd, out=out_cmd)
        return pump_settings

    def dilute_repeat(
        self,
        vials: tuple[int],
        ratios: tuple[float],
        bolus_volumes: tuple[float],
        rates: tuple[float],
        fits: tuple[bioreactor.FitSet],
        total_volumes: tuple[float],
    ) -> dict[int, tuple]:
        """Calculate a valid recurrent pump setting for the evolver given parameters

        Method to compute pump settings for the chemostat. Performs rudimentary
        parameter checking and adjusts rates and bolus
        """
        # Ty to adjust bolus to be within class-defined run parameters
        new_boluses = []
        new_rates = []
        pump_settings = {}
        adjusted = []

        # Commands to push
        in1_cmd: list[str] = []
        in2_cmd: list[str] = []
        out_cmd: list[str] = []

        bolus_parameters = zip(vials, ratios, bolus_volumes, fits, rates, total_volumes)
        fit: bioreactor.FitSet
        for vial, ratio, bolus, fit, rate, volume in bolus_parameters:
            if rate <= 0:
                bolus_sec_set = (vial, 0, 0, 0, 0)
                continue

            # Adjust the rates if needed
            adjust, bolus, rate = self.adjust_bolus_rate(bolus, rate, volume)
            period = (self._SECS_PER_UNIT_TIME * bolus) / (rate * volume)
            period = round(period, self.FLOAT_RESOLUTION)

            if adjust:
                self._logger.info(
                    "Adjusted bolus and rate in vial %d to %.2f and %.2f "
                    "to fit within safe parameters for chemostat",
                    vial,
                    bolus,
                    rate,
                )
                adjusted.append(vial)
            new_boluses.append(bolus)
            new_rates.append(rate)

            in1_frac = ratio[0] / sum(ratio)
            in2_frac = ratio[1] / sum(ratio)

            in1_sec = round(
                in1_frac * bolus / fit.in1.get_val(0), self.FLOAT_RESOLUTION
            )
            in2_sec = round(
                in2_frac * bolus / fit.in2.get_val(0), self.FLOAT_RESOLUTION
            )
            out_sec = round(
                (bolus + self._OUTFLOW_EXTRA) / fit.out.get_val(0),
                self.FLOAT_RESOLUTION,
            )

            bolus_sec_set = (in1_sec, in2_sec, out_sec, period)
            pump_settings[vial] = bolus_sec_set

            in1_cmd.append(f"{in1_frac * bolus}|{period}")
            in2_cmd.append(f"{in2_frac * bolus}|{period}")
            out_cmd.append(f"{bolus + self._OUTFLOW_EXTRA}|{period}")

        if self._locked:
            for pump, vals in zip(self._PUMP_SET, [in1_cmd, in2_cmd, out_cmd]):
                for vial, value in zip(vials, vals):
                    self._recurrent_commands[pump][vial] = value
        else:
            self.fluid_command(
                vials=vials, in1=in1_cmd, in2=in2_cmd, out=out_cmd, recurring=True
            )
        return pump_settings

    # Define read-only access to internal variables

    @property
    def data(self):
        """Read-only data pointer access. Data may still be mutable"""
        return self._data

    @property
    def led_power_config(self):
        """Read only access to latest reported machine led power configuration."""
        return self._power

    @property
    def stir_rate_config(self):
        """Read only access to latest reported machine stir rate configuration."""
        return self._stir_rate

    @property
    def temp_setpoint_config(self):
        """Read only access to latest reported machine temp setpoint configuration."""
        return self._temp_setpoint

    @property
    def last_broadcast_time(self):
        """Read only access to latest reported machine temp setpoint configuration."""
        return self._timestamp

    @property
    def response_received(self):
        """read-only access to if the response from a previous command was received.

        TODO: Not generalizable for multiple asynchronous commands. Fine because
        only one command has a response element, but should be extended
        """
        return self._awaiting_response

    @property
    def active_calibrations(self):
        """Read-only access to the last reported active calibrations on the machine."""
        return self._active_calibrations

    @property
    def logger(self):
        """read-only access to reference to logger object"""
        return self._logger

    @property
    def num_vials_total(self):
        """Read-only access to number of vials this evolver controls"""
        return self._num_vials_total

    ### Methods to deal with listening

    def on_connect(self):  # DONE
        print("Connected to eVOLVER as client")
        self.logger.info("connected to eVOLVER as client")

    def on_disconnect(self):  # DONE
        print("Disconected from eVOLVER as client")
        self.logger.info("disconnected to eVOLVER as client")

    def on_reconnect(self):  # DONE
        print("Reconnected to eVOLVER as client")
        self.logger.info("reconnected to eVOLVER as client")

    def on_broadcast(self, broadcast: type_hints.Broadcast):
        """On broadcast, waits for pump commands and then executes them"""
        self.logger.debug("broadcast received")

        # Updating machine config settings
        self._power = tuple(broadcast["config"]["lxml"]["value"])
        self._temp_setpoint = tuple(broadcast["config"]["temp"]["value"])
        self._stir_rate = tuple(broadcast["config"]["stir"]["value"])
        self._data = broadcast["data"]
        self._timestamp = time.time()

        # print(f"{EXP_NAME}: {elapsed_time:.4f} Hours")
        # are the calibrations in yet?

    def on_activecalibrations(self, calibrations: List[type_hints.SensorCal]):

        # Obtain data from active calibrations and create fit objects
        self._active_calibrations: type_hints.Calibration = {
            "od": None,
            "temp": None,
            "pump": None,
        }
        for calibration in calibrations:
            match calibration["calibrationType"]:
                case "od":
                    fit_type = "od"
                case "temperature":
                    fit_type = "temp"
                case "pump":
                    fit_type = "pump"
            for fit_data in calibration["fits"]:
                if fit_data["active"]:
                    self._active_calibrations[fit_type] = fit_data
        self._awaiting_response = False

    def on_calibration(self, data):
        self.calibration = data
        self._awaiting_response = False

    def on_calibrationnames(self, data):
        self._awaiting_response = False
        self._calibration_names = data

    ### Blocking methods to request data

    def _block(self, timeout=DEFAULT_TIMEOUT, safe=True, period=DEFAULT_PERIOD):
        """Helper method to block a function until timeout or response received

        Args:
          timeout: The time in seconds to try before giving up
          safe: If True, will NOT raise TimeoutError when timing out
          period: How long to wait between checking.

        Returns:
          A boolean that tells you if the response was received or not
        """
        start_time = time.time()
        while self.awaiting_response:
            print(".", end="")
            time.sleep(period)
            if time.time() - start_time >= timeout:
                print("Timed out")
                if not safe:
                    raise TimeoutError(
                        f"Evolver has not responded {timeout:.1f} seconds"
                    )
                return False
        return True

    def request_active_calibrations(self, timeout=DEFAULT_TIMEOUT, safe=True):
        """Method to ask for active calibration on the evolver"""
        self.awaiting_response = True
        self.logger.debug("requesting active calibrations")
        print("Requesting active calibrations...", end="")
        self.emit("getactivecal", {})
        successful = self._block(timeout, safe)
        if successful:
            print("Calibrations received")
            return self._active_calibrations
        return None

    def get_calibration_by_name(self, cal_name, timeout=DEFAULT_TIMEOUT, safe=True):
        """Obtain a specific calibration by name"""
        self.awaiting_response = True
        self.logger.debug("requesting calibration %s", cal_name)
        print(f"Requesting calibration {cal_name}...", end="")
        self.emit("getcalibration", {"name": cal_name})
        successful = self._block(timeout, safe)
        if successful:
            print("calibration received")
            return self.calibration
        return None

    def request_calibration_names(self, timeout=DEFAULT_TIMEOUT, safe=True):
        """Function to ask for calibration names from the evolver.

        Args:
          timeout: The number of seconds to wait before quitting

        returns:
          A list of dictionaries that contain the name and type of each calibration
        """
        self.awaiting_response = True
        self.logger.debug("requesting calibration names")
        print("Requesting calibration names...", end="")
        self.emit("getcalibrationnames", [])
        # wait for data
        successful = self._block(timeout, safe)
        if successful:
            print("names received")
            return self._calibration_names
        return None

    ### Non-blocking Methods to update evolver. These represent direct control

    def push_calibration_fit(self, cal_name, fit):
        """Helper function to set a calibration on the evolver"""
        self.emit("setfitcalibration", {"name": cal_name, "fit": fit})

    def update_stir_rate(
        self, vials: Iterable[int], stir_rates: Iterable[int], immediate: bool = False
    ):  # DONE
        """Method to update stir rate on the connected evolver

        Arguments:
          vials: the vials to change
          stir_rates: the signal value for the pwm for the stir rate.
          immediate: if the command should be done immediately
        """
        adjustment = ["NaN"] * self.num_vials_total
        for vial, stir_rate in zip(vials, stir_rates):
            adjustment[vial] = stir_rate

        data = {
            "param": "stir",
            "value": adjustment,
            "immediate": immediate,
            "recurring": True,
        }
        self.logger.debug(
            "Changing stir rate on vials %s to %s respectively",
            ", ".join([str(v) for v in vials]),
            ", ".join([str(rate) for rate in stir_rates]),
        )

        self.emit("command", data)

    def update_temperature(
        self, vials: Iterable[int], temperatures: Iterable[int], immediate: bool = False
    ):  # DONE
        """Method to update temperature on the connected evolver

        Arguments:
          vials: the vials to change
          temperatures: the signal value for the thermocouple to match. NOT CELCIUS
          immediate: if the command should be done immediately
        """
        adjustment = ["NaN"] * self.num_vials_total
        for vial, temperature in zip(vials, temperatures):
            adjustment[vial] = temperature

        data = {
            "param": "temp",
            "value": adjustment,
            "immediate": immediate,
            "recurring": True,
        }
        self.logger.debug(
            "Changing temp setpoint on vials %s to %s respectively",
            ", ".join([str(v) for v in vials]),
            ", ".join([str(temp) for temp in temperatures]),
        )

        self.emit("command", data)

    def update_led_power(
        self, vials: Iterable[int], led_powers: Iterable[int], immediate: bool = False
    ):  # DONE
        """Method to update led power on the connected evolver

        Arguments:
          vials: the vials to change
          led_powers: the signal value LED voltage (element in [0-4095])
          immediate: if the command should be done immediately
        """
        adjustment = ["NaN"] * self.num_vials_total
        for vial, temperature in zip(vials, led_powers):
            adjustment[vial] = temperature

        data = {
            "param": "od_led",
            "value": led_powers,
            "immediate": immediate,
            "recurring": True,
        }
        self.logger.debug(
            "Changing LED power for vials %s to %s respectively",
            ", ".join([str(v) for v in vials]),
            ", ".join([str(power) for power in led_powers]),
        )

        self.emit("command", data)

    def fluid_command(
        self,
        vials: Iterable[int],
        in1: Iterable[str] = None,
        in2: Iterable[str] = None,
        out: Iterable[str] = None,
        recurring: bool = False,
        immediate: bool = False,
    ):
        """Method to adjust pump settings

        Arguments:
          vials: the vials to change
          in1: the values for in1
          in2: the values for in2
          out: the values for out
          immediate: if the command should be done immediately
        """
        message = {
            "fields_expected_incoming": 49,
            "fields_expected_outgoing": 49,
            "recurring": recurring,
            "immediate": immediate,
            "value": ["--"] * 3 * self.num_vials_total,
            "param": "pump",
        }
        if recurring:
            recurring_string = "recurring"
        else:
            recurring_string = ""

        pump_sets = [in1, in2, out]
        for idx, pump_set in enumerate(pump_sets):
            if pump_set is None:
                continue
            for vial, pump_val in zip(vials, pump_set):
                if pump_val is None:
                    continue
                message["value"][vial + idx * self.num_vials_total] = pump_val

            self.logger.debug(
                "sending %spump command to %s for vials %s to pump for %s respectively",
                recurring_string,
                f"pumpset {self._PUMP_SET[idx]}",
                ", ".join([str(v) for v in vials]),
                ", ".join(pump_set),
            )
        self.emit("command", message)

    def stop_pumps(
        self, vials: Iterable[int], in1: bool = True, in2: bool = True, out: bool = True
    ):  # DONE
        """Method to stop pumps for certain vials

        Arguments:
          vials: the vials to stop pumps for
          in1: A boolean value which is True if we should shut off the in1 set
          in2: A boolean value which is True if we should shut off the in2 set
          out: A boolean value which is True if we should shut off the out set
        """
        message = {
            "fields_expected_incoming": 49,
            "fields_expected_outgoing": 49,
            "param": "pump",
            "value": ["__"] * 48,
            "recurring": False,
            "immediate": True,
        }
        stopped = []
        for idx, pumpset in enumerate((in1, in2, out)):
            for vial in vials:
                if pumpset:
                    message["value"][vial + idx * self.num_vials_total] = "0"
                    stopped.append(self._PUMP_SET[idx])

        # Logging and sending command
        stopped = ", ".join(stopped)
        stopped_vials = ", ".join([str(v) for v in vials])
        self.logger.info("stopping %s pumps for vials %s", stopped, stopped_vials)
        self.emit("command", message)

    def stop_all_pumps(self):  # Done
        """Method to stop all pumps"""
        data = {
            "param": "pump",
            "value": ["0"] * 48,
            "recurring": False,
            "immediate": True,
        }
        self.logger.info("stopping all pumps")
        self.emit("command", data)

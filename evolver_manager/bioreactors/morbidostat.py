"""class to handle morbidostat"""

import collections
import dataclasses
from collections.abc import Iterable

import numpy as np
from scipy import optimize

from evolver_manager import evolver_controls
from evolver_manager.fits import exp
from evolver_manager.utils import evolver_utils, type_hints
from evolver_manager.utils.controllers import controller

from . import bioreactor


@dataclasses.dataclass
class MorbidostatSettings:
    """Settings class for storing morbidostat reactor settings"""

    base_settings: bioreactor.ReactorSettings
    lower_thresh: tuple[float, ...] = dataclasses.field(default_factory=tuple)
    upper_thresh: tuple[float, ...] = dataclasses.field(default_factory=tuple)
    drug_low: tuple[float, ...] = dataclasses.field(default_factory=tuple)
    drug_high: tuple[float, ...] = dataclasses.field(default_factory=tuple)
    control_rate: tuple[float, ...] = dataclasses.field(default_factory=tuple)
    controllers: tuple[controller.Controller, ...] = dataclasses.field(
        default_factory=tuple
    )
    drug_source: tuple[str, ...] = dataclasses.field(default_factory=tuple)

    def __post_init__(self):
        vials = self.base_settings.vials
        self.lower_thresh = tuple(self.lower_thresh)
        self.upper_thresh = tuple(self.upper_thresh)
        self.rate = tuple(self.rate)
        self.drug_limits = tuple(self.drug_limits)
        self.drug_source = tuple(self.drug_source)

        invalid_settings = False
        wrong_settings = []
        variables = [
            self.lower_thresh,
            self.upper_thresh,
            self.control_rate,
            self.controllers,
            self.drug_low,
            self.drug_high,
            self.drug_source,
        ]
        names = [
            "lower_thresh",
            "upper_thresh",
            "control_rate",
            "controllers",
            "drug_low",
            "drug_high",
            "drug_source",
        ]
        for var, name in zip(variables, names):
            if len(vials) != len(var):
                invalid_settings = True
                wrong_settings.append(name)

        if invalid_settings:
            wrong_settings = ", ".join(wrong_settings)
            raise ValueError(
                f"Number of vials do not match number of elements in {wrong_settings}"
            )


class Morbidostat(bioreactor.Bioreactor):
    """Class to handle Morbidostat on the eVOLVER for a single experiment.

    A class to store run parameters and update rules for handling Morbidostat
    situations for a single experiment. This implents an object defining the run
    conditions for a single experiment that contains certain vails operating off
    of a specific configuration file. A morbidostat implementation includes
    a target growth rate and bounds for growth that it keeps within.

    """

    _BOLUS_MAX = 15  # The maximum number of mL to do in a dilution step
    _BOLUS_MIN = 0.5  # The minimum number of mL to do ina dilutions step
    _PUMP_TIME_MAX = 15  # Number of seconds to wait between pump events
    _OUTFLOW_EXTRA = 5  # The extra time given to outflow pumps

    _N_POINT_DIFFERENCE = 15
    _HR_TO_SECOND = 3600  # Factor to convert a hourly sampling period to growth

    _DEQUE_BUFFER = 2000
    _GR_FILTER_SIZE = 5  # Delays growth rate measurement by N//2
    _DRUG_LOG_SIZE = 11

    _SERIAL_DILUTIONS = 3
    _POW_PARAM = 1
    _CONST_PARAM = 1

    def __init__(
        self,
        name: str,
        evolver: evolver_controls.EvolverControls,
        settings: MorbidostatSettings,
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
          morbidostat_settings: a object denoting morbidostat settings
        """
        super().__init__(name, evolver, settings.base_settings, calibrations, manager)
        self._morbidostat_settings = settings

        # Turbidostat-type dilution control using two possible states
        self._diluting: dict[
            int,
            tuple[
                float,
                tuple[float, tuple[float, float], str, bioreactor.FitSet],
                tuple[float, float],
            ],
        ] = {}

        self._bolus_volume = self.volume / self.control_rate

        # Parameters needed for pump
        self._last_dilution: dict[int, float] = {}

        # Parameters needed for drug ratio calculation
        self._past_growth_rate: dict[int, collections.deque[float]] = {}
        self._offset: dict[int, float] = {}
        self._drug_log: dict[int, collections.deque[float]]
        self._od_filtered: dict[int, list[float]]

        self._time_data = collections.deque(maxlen=self.mem_len)
        for vial in self.vials:
            self._past_growth_rate[vial] = collections.deque(maxlen=self.mem_len)
            self._past_growth_rate[vial].append(0)

            self._drug_log[vial] = collections.deque(maxlen=self._DRUG_LOG_SIZE)

            self._offset[vial] = 0.0
            self._last_dilution[vial] = 0.0
            self._od_filtered[vial] = collections.deque(maxlen=self._DEQUE_BUFFER)

        self._d_coef = evolver_utils.causal_differentiator_coefs(self.mem_len)
        self._od_filter = evolver_utils.causal_smoother_coefs(self.mem_len)
        self._gr_filter = evolver_utils.causal_smoother_coefs(self._GR_FILTER_SIZE)
        self._od_denom: int = 2 ** (self.mem_len - 1)
        self._gr_denom: int = 2 ** (self._GR_FILTER_SIZE - 1)
        self._sampling_period: float = 0

    @property
    def control_rate(self):
        return self._morbidostat_settings.control_rate

    @property
    def drug_limits(self):
        return self._morbidostat_settings.drug_limits

    @property
    def controllers(self):
        return self._morbidostat_settings.controllers

    @property
    def drug_source(self):
        return self._morbidostat_settings.drug_source

    def update(
        self, broadcast: type_hints.Broadcast, curr_time: float
    ) -> type_hints.UpdateMsg:

        update_msg: type_hints.UpdateMsg = {
            "time": float("nan"),
            "record": None,
            "vials": self.vials,
            "od": self.od_readings,
            "temp": self.temp_readings,
            "end": False,
        }

        start, records = self.pre_update(broadcast=broadcast, curr_time=curr_time)

        self._time_data.append(broadcast["timestamp"])
        self._sampling_period = np.average(np.diff(self._time_data))
        # Adjust sampling period

        if not start:
            return update_msg

        # Iterative growth rate computation with noise-robust differentiator
        control_param_set = zip(
            self.drug_limits, self.drug_source, self.controllers, self._data_fits
        )

        dilution_param_set = zip(
            self._morbidostat_settings.lower_thresh,
            self._morbidostat_settings.upper_thresh,
        )

        morbidostat_params = zip(self.vials, control_param_set, dilution_param_set)

        change_temp = []
        for vial, control_params, dilution_params in morbidostat_params:
            past_data = self.od_history[vial]

            od_filtered = self._od_filter * past_data
            self._od_filtered[vial].append(od_filtered)

            # Check to see if we should adjust controller
            if curr_time - self._last_dilution[vial] > (1 / self.control_rate):
                drug, response = self.get_drug_concentration(vial)
                _, drug_source, *_ = control_params
                if drug_source.lower() != "temp":
                    self._diluting[vial] = tuple(
                        (drug, control_params, dilution_params)
                    )
                    command = "pump"
                else:
                    change_temp.append((vial, drug, control_params))
                    command = "temp"

                record = type_hints.MorbidostatRecord(
                    vial=vial,
                    command=command,
                    growth_rate=self._past_growth_rate[vial][-1],
                    response=response,
                )
                records.append(record)

            # Check to make sure things don't need diluting

        # Processing dilution commands
        in1_to_adjust = []
        in2_to_adjust = []
        out_to_adjust = []
        pump_vials = []

        for vial, drug, params in self._diluting.items():
            control_params, dilution_params = params

            average_od = np.average(self.od_history[vial])
            lower, upper = dilution_params
            if average_od < lower:
                del self._diluting[vial]
                continue

            # Get the volume needed to approach od target
            drug_limits, _, drug_source, fits = control_params
            bolus_volume = self.compute_bolus_volume(
                average_od, upper, lower, self.volume, self._SERIAL_DILUTIONS
            )

            # Calculate the ratio needed to adjust drug concentration
            in1_s, in2_s, out_s = self.compute_pump_time(
                drug, drug_limits, drug_source, bolus_volume, fits
            )
            pump_vials.append(vial)
            in1_to_adjust.append(in1_s)
            in2_to_adjust.append(in2_s)
            out_to_adjust.append(out_s)

        self._evolver.fluid_command(
            pump_vials, in1_to_adjust, in2_to_adjust, out_to_adjust, immediate=True
        )

        # Processing Temp change Commands
        temp_vials = []
        temp_settings = []
        temp_temperatures = []
        for vial, drug, control_params in change_temp:
            drug_limits, *_ = control_params
            temp_setting, temp = self.compute_heater_val(drug, drug_limits, fits)
            temp_vials.append(vial)
            temp_settings.append(temp_setting)
            temp_temperatures.append(temp)

        self.modify_temp_setpoint(temp_vials, temp_temperatures)
        self._evolver.update_temperature(temp_vials, temp_settings, immediate=True)

        update_msg["record"] = record

    ### TODO Add pump command and heater change in setpoint

    @classmethod
    def compute_bolus_volume(
        cls,
        average_od: float,
        upper_bound: float,
        lower_bound: float,
        volume: float,
        serial_dilutions: int,
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
        if serial_dilutions <= 1:
            return volume * (average_od / lower_bound - 1)
        scale = np.log(upper_bound / lower_bound)
        location = (serial_dilutions - 1) * np.log(average_od / lower_bound) / scale
        robustness_param = 1 - 2 * (location / serial_dilutions) ** cls._POW_PARAM
        dilutions_left = location + cls._CONST_PARAM * robustness_param
        dilution_factor = average_od / lower_bound
        bolus_volume = (np.power(dilution_factor, 1 / dilutions_left) - 1) * volume

        if bolus_volume < 0:
            return 0

        bolus_volume = min(bolus_volume, cls._BOLUS_MAX)
        bolus_volume = max(bolus_volume, cls._BOLUS_MIN)
        return bolus_volume

    def compute_heater_val(
        self, drug: float, drug_limits: tuple[float, float], temp_fit: bioreactor.FitSet
    ) -> tuple[int, float]:
        """Function to compute heater specification after pid controller input"""
        drug = drug * -np.diff(drug_limits) + drug_limits[0]
        sensor_val = temp_fit.temp_fit.get_inverse(drug)
        return int(round(sensor_val, 0)), drug

    def modify_temp_setpoint(
        self, vials_to_edit: Iterable[int], temps_to_edit: Iterable[float]
    ):
        """Method to alter reactor's interal record of temperature settings."""
        temp_setpoints = list(self.temp_setpoints)
        for vial, temp in zip(vials_to_edit, temps_to_edit):
            idx = self.vials.index(vial)
            temp_setpoints[idx] = temp
        self._base_settings.temp = tuple(temp_setpoints)

    def compute_pump_time(
        self,
        drug: float,
        drug_limits: tuple[float, float],
        drug_pump: str,
        bolus_volume: float,
        pump_fits: bioreactor.FitSet,
    ) -> tuple[float, float, float]:
        drug = drug / np.sum(drug_limits)
        idx = self._evolver.PUMP_SET.index(drug_pump.upper())
        pump_vals = [
            bolus_volume / pump_fits.in1.get_val(0),
            bolus_volume / pump_fits.in2.get_val(0),
            0,
        ]
        pump_vals[idx] *= drug
        pump_vals[1 - idx] *= drug
        pump_vals[-1] = sum(pump_vals[:-1]) + self._OUTFLOW_EXTRA

        in1, in2, out = pump_vals

        for idx, val in enumerate(pump_vals):
            val = min(val, self._PUMP_TIME_MAX)
            val = round(val, self._evolver.FLOAT_RESOLUTION)
            pump_vals[idx] = val

        return in1, in2, out

    def get_drug_concentration(self, vial: int) -> tuple[float, float]:
        """Helper function to compute drug concentration and update growth rates.

        Args:
          vial: the vial to act on
          drug_limits: the bounds drug_limits can take
        """
        # Obtain growth rate, and if we have enough growth rates, filter it
        param, *_ = optimize.curve_fit(
            f=exp.Exp.equation,
            xdata=self._time_data,
            ydata=self._od_filtered[vial],
            p0=exp.Exp.get_initial_values(self._time_data, self._od_filtered[vial]),
            maxfev=100000,
        )
        growth_rate = param[1]
        last_growth_rate = self._past_growth_rate[vial][-1]
        self._past_growth_rate[vial].append(growth_rate)
        if len(self._past_growth_rate[vial]) > self._GR_FILTER_SIZE:
            past_gr = self._past_growth_rate[vial][-self._GR_FILTER_SIZE - 1 :]
            last_growth_rate = self._gr_filter * past_gr[:-1] / self._gr_denom
            growth_rate = self._gr_filter * past_gr[1:] / self._gr_denom

        response = self.controllers[vial].get_response(growth_rate)

        setpoint = self.controllers[vial].setpoint

        if growth_rate < setpoint and last_growth_rate > setpoint:
            if len(self._drug_log[vial]) == self._DRUG_LOG_SIZE:
                self._offset[vial] = np.average(
                    self._drug_log[vial][-self._DRUG_LOG_SIZE // 2 :]
                )
            else:
                self._offset[vial] = self._drug_log[vial][-1]
        drug = self._offset[vial] + response
        # Keep the drug concentration between 0 and 1
        drug = min(drug, 1)
        drug = max(drug, 0)
        return drug, response

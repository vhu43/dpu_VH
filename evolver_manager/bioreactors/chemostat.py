"""class to handle chemostat"""
from evolver_manager import evolver_controls
from evolver_manager.utils import type_hints
from . import bioreactor
import dataclasses
import numpy as np
import global_config

@dataclasses.dataclass
class ChemostatSettings:
  """Settings class for storing chemostat reactor settings"""
  base_settings: bioreactor.ReactorSettings
  start_od: tuple[float, ...] = dataclasses.field(default_factory=tuple)
  bolus: tuple[float, ...] = dataclasses.field(default_factory=tuple)
  rates: tuple[float, ...] = dataclasses.field(default_factory=tuple)
  pump_ratios:tuple[tuple[float, float], ...] = dataclasses.field(
    default_factory=tuple)

  def __post_init__(self):
    vials = self.base_settings.vials
    self.bolus = tuple(self.bolus)
    self.rates = tuple(self.rates)
    self.pump_ratios = tuple(self.pump_ratios)

    invalid_settings = False
    wrong_settings = []
    if len(vials) != len(self.start_od):
      invalid_settings = True
      wrong_settings.append("start_od")
    if len(vials) != len(self.bolus):
      invalid_settings = True
      wrong_settings.append("bolus volumes")
    if len(vials) != len(self.rates):
      invalid_settings = True
      wrong_settings.append("dilution rates")
    if len(vials) != len(self.pump_ratios):
      invalid_settings = True
      wrong_settings.append("pump_ratios")
    if invalid_settings:
      wrong_settings = ", ".join(wrong_settings)
      raise ValueError("Number of vials do not match number of elements in"
                        f" {wrong_settings}")

class Chemostat(bioreactor.Bioreactor):
  """Class to handle Chemostat on the eVOLVER for a single experiment.

  A class to store run parameters and update rules for handling chemostat
  situtions for a single experiment. This implements an object defining the run
  conditions for a single experiment that contains certain vials operating off
  of a specific configuration file.


  """

  _OUTFLOW_EXTRA = 5 # The extra time given to outflow pumps
  _BOLUS_VOLUME_MIN = 0.2 # The minimum volume a bolus should be
  _BOLUS_VOLUME_MAX = 5.0 # The maximum volume a bolus should be
  _SECS_PER_UNIT_TIME = global_config.SECS_PER_UNIT_TIME

  def __init__(self,
               name: str,
               evolver: evolver_controls.EvolverControls,
               settings: ChemostatSettings,
               calibrations: type_hints.Calibration=None,
               manager: str = __name__):
    """Initializes the instance using common parameters for all runs

    Args:
      name: Name for the instance
      evolver: an evolver namespace for broadcasting and sending signals
      settings: volume, temp, stir, and led power settings
      calibration: the calibration dictionary to use for the bioreactor
      manager: the name used for the experiment manager for logging
      chemostat_settings: a object denoting chemostat settings
    """
    super().__init__(
      name, evolver, settings.base_settings, calibrations, manager)

    self._chemostat_settings: ChemostatSettings = settings
    self.check_pump_settings()
    self._awaiting_update: dict[int, bool] = {}
    for vial in self.vials:
      self._awaiting_update[vial] = True
    if max(self._chemostat_settings.start_od) > 0:

      self._check_od: bool = True

  @property
  def start_od(self):
    return self._chemostat_settings.start_od

  def check_pump_settings(self):
    """Adjust the bolus and rates and check that they are valid

    Helper method to check bolus parameters at instantiation
    """
    bolus_volumes = self._chemostat_settings.bolus
    dilutions_per_unit_time = self._chemostat_settings.rates

    pump_setting_parameters = zip(self.vials, bolus_volumes,
                                  dilutions_per_unit_time)

    new_boluses = []
    new_rates = []
    adjusted = False
    for vial, bolus, rate in pump_setting_parameters:
      if rate <= 0:
        adjust = True
        bolus = 0
        rate = 0
      else:
        adjust, bolus, rate = self._evolver.adjust_bolus_rate(
          bolus, rate, self.volume)

      if adjust:
        self._logger.info("Adjusted bolus and rate in vial %d to %.2f and %.2f "
                          "to fit within safe parameters for chemostat",
                          vial, bolus, rate)
        adjusted = True

      new_boluses.append(bolus)
      new_rates.append(rate)

    if adjusted:
      self._chemostat_settings.bolus = tuple(new_boluses)
      self._chemostat_settings.rates = tuple(new_rates)

  def update(self, broadcast: type_hints.Broadcast, curr_time: float
             ) -> type_hints.UpdateMsg:
    """A method to update chemostat action on broadcast events

    Defines how bioreactor handles new data coming in. After pre-update steps
    it will first check to see if all chemorates are updated. If not

    Arguments:
      broadcast: a broadcast from the evolver
      time: the elapsed time for the experiment
    """
    start, records = self.pre_update(broadcast=broadcast, curr_time=curr_time)

    update_msg: type_hints.UpdateMsg = {"time": float("nan"), "record": None,
                                          "vials": self.vials,
                                          "od": self.od_readings,
                                          "temp": self.temp_readings}

    # Not past minimum start delay
    if not start:
      return update_msg

    # All chemorates are updated
    if sum(self._awaiting_update.values) == 0:
      self._logger.debug("All chemostat vials updated, skipping checks")
      return update_msg

    # wait for more OD values
    if self._check_od and len(self.od_history) < self.mem_len:
      self._logger.debug("Not enough OD measurements for starting")
      return update_msg

    # Figuring out if we should adjust any vials
    vials_to_adjust: list[int] = []
    ratios = []
    rates = []
    fits = []
    boluses = []

    pump_params = zip(
      self._chemostat_settings.bolus, self._data_fits,
      self._chemostat_settings.rates, self._chemostat_settings.pump_ratios
    )
    bounds_list = zip(self.start_times, self.end_times, self.start_od)

    for vial, bounds, params in zip(self.vials, bounds_list, pump_params):
      start_time, end_time, start_od = bounds
      if curr_time < start_time or curr_time > end_time:
        continue

      avg_od = np.median(self.od_history[vial])
      if avg_od > start_od:
        bolus, fit, rate, ratio = params
        vials_to_adjust.append(vial)
        ratios.append(ratio)
        rates.append(rate)
        fits.append(fit)
        boluses.append(bolus)

    # Adjust the vials that need adjusting
    pump_settings = self._evolver.dilute_repeat(
      vials_to_adjust, ratios, boluses, rates, fits, self.volumes)
    for vial, pump_commands in pump_settings.items():
      in1_v, in2_v, out_v, period = pump_commands
      record: type_hints.ChemostatRecord = {
        "vial": vial, "command": "recurrent", "IN1": (in1_v, period),
        "IN2": (in2_v, period), "OUT": (out_v, period)
      }
      records.append(record)
      self.logger.info(f"chemostat initiated for vial {vial}",
                        f"period {period}")

      self._awaiting_update[vial] = False

    update_msg["time"] = curr_time
    update_msg["record"] = tuple(records)
    return update_msg




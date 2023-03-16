"""class to handle Turbidostat"""
from evolver_manager.utils import type_hints
from evolver_manager import evolver_controls
from . import bioreactor
import dataclasses
import numpy as np

@dataclasses.dataclass
class TurbidostatSettings:
  """Settings class for storing turbidostat reactor settings"""
  base_settings: bioreactor.ReactorSettings
  n_dilution_steps: int
  n_cycles: tuple[int, ...] = dataclasses.field(default_factory=tuple)
  lower_thresh: tuple[float, ...] = dataclasses.field(default_factory=tuple)
  upper_thresh: tuple[float, ...] = dataclasses.field(default_factory=tuple)
  pump_ratios:tuple[tuple[float, float], ...] = dataclasses.field(
    default_factory=tuple)

  def __post_init__(self):
    vials = self.base_settings.vials
    self.bolus = tuple(self.bolus)
    self.rates = tuple(self.rates)

    invalid_settings = False
    wrong_settings = []
    if len(vials) != len(self.n_cycles):
      invalid_settings = True
      wrong_settings.append("n_cycles")
    if len(vials) != len(self.lower_thresh):
      invalid_settings = True
      wrong_settings.append("lower_thresh")
    if len(vials) != len(self.upper_thresh):
      invalid_settings = True
      wrong_settings.append("upper_thresh")
    if len(vials) != len(self.pump_ratios):
      invalid_settings = True
      wrong_settings.append("pump_ratios")
    if invalid_settings:
      wrong_settings = ", ".join(wrong_settings)
      raise ValueError("Number of vials do not match number of elements in"
                        f" {wrong_settings}")

class Turbidostat(bioreactor.Bioreactor):
  """Class to handle Turbidostat on the eVOLVER for a single experiment.

  A class to store run parameters and update rules for handling Turbidostat
  situtions for a single experiment. This implements an object defining the run
  conditions for a single experiment that contains certain vials operating off
  of a specific configuration file. A turbidostat implementation includes two
  states per vial, a "growth" state and a "dilution" state. In the growth state
  the turbidostat only checks OD, and if that OD is greater than max, switch
  to the dilution state. In the dilution state, we routinely dilute until it
  gets below the max OD.


  """
  _OUTFLOW_EXTRA = 5 # The extra time given to outflow pumps
  _PUMP_TIME_MAX = 30 # Number of seconds to wait between pump events
  _BOLUS_MAX = 15 # The maximum number of mL to do in a dilution step
  _BOLUS_MIN = 0.5 # The minimum number of mL to do ina dilutions tep
  _POW_PARAM = 1
  _CONST_PARAM = 1

  def __init__(self,
               name: str,
               evolver: evolver_controls.EvolverControls,
               settings: TurbidostatSettings,
               calibrations: type_hints.Calibration=None,
               manager: str = __name__):
    """Initializes the instance using common parameters for all runs

    Args:
      name: Name for the instance
      evolver: an evolver namespace for broadcasting and sending signals
      settings: volume, temp, stir, and led power settings
      calibration: the calibration dictionary to use for the bioreactor
      manager: the name used for the experiment manager for logging
      turbidostat_settings: a object denoting turbidostat settings
    """
    super().__init__(name, evolver, settings.base_settings, calibrations,
                     manager)
    self._turbidostat_settings = settings
    self._is_diluting: dict[int, bool] = {}

    for vial in self.vials:
      self._is_diluting[vial] = False

    self.num_readings: int = 0
    self._num_dilutions_left = dict(zip(self.vials, settings.n_cycles))
    for vial, item in self._num_dilutions_left.items():
      if item == 0:
        del self._num_dilutions_left[vial]

  @property
  def dil_steps(self):
    """Read-only access to number of dilution steps to take
    """
    return self._turbidostat_settings.n_dilution_steps

  @property
  def od_upper_bounds(self):
    """Read-only access to upper thresholds
    """
    return self._turbidostat_settings.upper_thresh

  @property
  def od_lower_bounds(self):
    """Read-only access to lower thresholds
    """
    return self._turbidostat_settings.lower_thresh

  @property
  def pump_ratios(self):
    """Read-only access to pump_ratios
    """
    return self._turbidostat_settings.pump_ratios

  @property
  def is_diluting(self):
    """Read-only access to ref to a dictionary if vial is diluting or not
    """
    return self._is_diluting

  def update(self, broadcast: type_hints.Broadcast, curr_time: float
             ) -> type_hints.UpdateMsg:
    """A method to update Turbidostat action on broadcast events.

    This method first obtains OD data using pre_update(). Aftewards, it checks
    on vials in the growth stage to see if they are above the max OD threshold.
    If this is true, and they have remaining dilutions, then we switch them to
    dilution stage. Afterwards, if any are in dilution stage, then it will
    slowly dilute them based on class parameter _SERIAL_DILUTIONS.

    Arguments:
      broadcast: a broadcast from the evolver
      curr_time: the elapsed time for the experiment
    """
    update_msg: type_hints.UpdateMsg = {"time": broadcast["timestamp"],
                                        "record": None,
                                        "vials": self.vials,
                                        "od": self.od_readings,
                                        "temp": self.temp_readings}

    start, records = self.pre_update(broadcast=broadcast, curr_time=curr_time)

    if not start:
      return update_msg

    # Store pumping parameters
    vials_to_adjust: list[int] = []
    currents = []
    uppers = []
    lowers = []
    ratios = []
    fits = []
    volumes = []

    # Store stopping parameters
    vials_to_stop: list[int] = []

    # Check OD if they are in growth stage. If past max, change to dilution
    bounds_list = zip(self.start_times, self.end_times,
                      self.od_upper_bounds, self.od_lower_bounds)
    params_list = zip(self.pump_ratios, self._data_fits, self.volumes)
    for vial, bounds, params in zip(self.vials, bounds_list, params_list):
      start_time, end_time, upper_bound, lower_bound = bounds
      # Check if past start_time
      if curr_time < start_time or curr_time > end_time:
        continue

      average_od = np.median(self.od_history[vial])
      if not self._is_diluting[vial]:
        if average_od < upper_bound:
          continue

        # average_od > upper bound, so we should dilute
        remaining_dilutions = self._num_dilutions_left.get(vial, 1)
        if remaining_dilutions > 0:
          self._is_diluting[vial] = True
          if vial in self._num_dilutions_left:
            self._num_dilutions_left[vial] -= 1
        else:
          records.append({"vial": vial, "command": "stop"})
          vials_to_stop.append(vial)
          continue

      # During dilution, od will change rapidly, so work off previous od
      curr_od = self.od_history[vial][-1]

      if curr_od < lower_bound:
        self._is_diluting[vial] = False
      else:
        ratio, fit, volume = params
        vials_to_adjust.append(vial)
        currents.append(curr_od)
        uppers.append(upper_bound)
        lowers.append(lower_bound)
        ratios.append(ratio)
        fits.append(fit)
        volumes.append(volume)

    # Perform dilutions
    pump_settings = self._evolver.dilute_single(
      vials_to_adjust, currents, uppers, lowers, self.dil_steps,
      ratios, fits, volumes)
    #Store command change
    for vial, settings in pump_settings.items():
      bolus_vol, in1_bolus_s, in2_bolus_s, out_bolus_s = settings
      record: type_hints.TurbidostatRecord = {
        "vial": vial, "in1": in1_bolus_s,
        "in2": in2_bolus_s, "out": out_bolus_s
      }
      records.append(record)
      self.logger.info("Turbidostat called for dilution of vial %d with "
                       "volume %.1f", vial,  bolus_vol)

    self._evolver.stop_pumps(vials_to_stop)
    update_msg["time"] = curr_time
    update_msg["record"] = tuple(records)

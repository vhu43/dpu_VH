
"""A module to help with processing config files and decoding their outputs.

This module is an API that helps with processing configuration files for the
evolver. If you make a change to the configuration setup, please reflect the
change in the processing here. It also includes several utility functions to
aid in creating unique experiment names
"""
from pathlib import Path
import inspect
import importlib
import socketio
import json
import time
from evolver_manager import evolver_controls
from evolver_manager import bioreactors
from evolver_manager.bioreactors import bioreactor
from evolver_manager.utils import type_hints
from collections.abc import Iterable
import dataclasses

SLEEVE_MIN = 0
SLEEVE_MAX = 16
PUMP_SET = ["in1", "in2", "out"]

TIMEOUT = 10
EVOLVER_NAMESPACE = "/dpu-evolver"

def check_vials_and_modes(config: dict, use_global_vials=True,
                          extra_vials_ok=True
                          ) -> tuple[dict[str, tuple[int]], tuple[int]]:
  """Helper function to check vial and modes validity.

  Function to take a configuration and check the vial mode combination. First
  it ensures all vials inputted are valid. Next it checkes that all the modes
  that have been selected exist as valid runmodes (in the bioreactors folder).
  After that, it checks to make sure no vials are in two modes or forgotten.

  Args:
    config: A dictionary containing all the configurations for the evolver
    use_global_vials: If False, will obtain the global list of vials from
      individual vial specifications
    extra_vials_ok: If True, will ignore global vials that are not specified
      in the individual mode vial lists.

  Returns:
    A dictionary of modes matching their appropriate module name in bioreactors
  that point to a tuple of vials they manage.
  """


  # Figure out what modes are being run and check they are valid
  valid_modes: set[str] = set()
  bioreactors_list = Path(inspect.getfile(bioreactors)).parent
  for mode in bioreactors_list.iterdir():
    if mode.suffix == ".py" and mode.stem not in ("fit", "template"):
      valid_modes.add(mode.stem)

  # Obtain all the runmodes specified in the config
  seen_vials = {}
  seen_count = 0
  reactors = {}

  for reactor in valid_modes:
    if reactor in config:
      if "vials" in config[reactor] and config[reactor]["vials"] != "None":
        mode_vials = config[reactor]["vials"]
        reactors[reactor] = mode_vials

        # Check to make sure no vials are duplicated between modes
        seen_vials.update(zip(mode_vials, [reactor] * len(mode_vials)))
        if len(seen_vials) - seen_count != len(mode_vials):
          raise ValueError(
            f"Vial collision in {reactor} with listed vials. "
            "Please check these entries in the config file"
          )
        seen_count = len(seen_vials)

  # Check validity of vials
  if use_global_vials:
    vials = config["vials"]
  else:
    vials = tuple(sorted(seen_vials))

  # Checking global mode specification
  if "mode" in config:
    if config["mode"] not in reactors:
      reactor = config["mode"]
      mode_vials = set(vials) - set(sorted(seen_vials))

      if len(mode_vials) == 0:
        raise ValueError(f"global mode is {reactor} but no vials to attach")
      reactors[config["mode"]] = mode_vials
      seen_vials.update(zip(mode_vials, [reactor] * len(mode_vials)))

  if len(vials) != len(seen_vials):
    if extra_vials_ok:
      print(f"vials {set(vials) - set(seen_vials)} not attached... Ignoring")
    else:
      raise IndexError("Not all vials accounted for. Please check config")

  # Check for invalid modes
  invalid_modes = []
  for reactor in reactors:
    if reactor not in valid_modes:
      invalid_modes.append(reactor)
  if len(invalid_modes) > 0:
    modestr = ", ".join(invalid_modes)
    raise ValueError(f"Invalid mode of type(s) {modestr}")

  for vial in vials:
    if vial < SLEEVE_MIN or vial >= SLEEVE_MAX:
      raise ValueError("Inappropriate vial selection of {vial} in vials")

  return reactors, vials

def check_settings(in_dict: dict, reactor, vials, fields: Iterable,
                   tuple_fields: Iterable, alias: dict = None
                   ) -> dict[str, dict]:
  """Helper function to check special settings for configuration"""
  if alias is None:
    alias = {}
  settings = {}
  num_vials = len(vials)
  for setting in fields:
    # Make exception for vials
    if setting == "vials":
      settings["vials"] = tuple(vials)
      continue

    # Figure out which name to look for on the in_dict dictionary
    if setting in alias:
      field_value = in_dict[alias[setting]]
    else:
      field_value = in_dict[setting]

    # Check if it should be tuple. If so, fix it, otherwise, add and move on
    if setting not in tuple_fields:
      settings[setting] = field_value
      continue

    if not isinstance(field_value, Iterable) or isinstance(field_value, str):
      settings[setting] = tuple([field_value] * num_vials)
    elif len(field_value) == 1:
      settings[setting] = tuple(field_value * num_vials)
    else:
      if len(field_value) == SLEEVE_MAX:
        configurations = field_value
      else:
        configurations = dict(zip(vials, field_value))
      subset = []
      for vial in vials:
        subset.append(configurations[vial])
      settings[setting] = tuple(subset)

  num_fields = len((fields))

  if len(settings) != num_fields:
    raise ValueError(f"{reactor} configuration missing a field. Expected "
                     f"{num_fields} but only got {len(settings)}")
  return settings

def add_fluid_tracking(config: dict, reactors:dict[str, tuple[int, ...]]):
  """Buidling fluid tracking dictionaries to send to the evolver.
  """
  if "fluids" not in config or config["fluids"] == "None":
    return {}, {}

  fluid_key = {}
  starting_vol = {"alert": ["email@domain.com"], "fluids":{}}
  reactor_key = {}

  if "receiver_email" in config and config["receiver_email"] != "None":
    to_alert = config["receiver_email"]
    if not isinstance(to_alert, Iterable):
      to_alert = [to_alert]
    starting_vol["alert"] = to_alert

  for reactor, vials in reactors.items():
    fluid_key[reactor] = {}
    for vial in vials:
      fluid_key[reactor][vial] = {}
      reactor_key[vial] = reactor

  for fluid in config["fluids"]:
    starting_vol["fluids"][fluid["name"]] = fluid["vol"]
    for pump in PUMP_SET:
      if pump == "out":
        continue
      if pump in fluid and fluid[pump] != "None":
        for vial in fluid[pump]:
          fluid_key[reactor_key[vial]][vial][pump] = fluid["name"]
  return fluid_key, starting_vol

def encode_start(name, working_directory, config):
  """Build a set of command to send to the evolver daemon

  returns:
    commands: A list of commands to send to the evolver
    reactors: A dictionary of reactors, vials where each reactor defines
      a runmode
  """
  ip = config["EVOLVER_IP"]
  port = config["EVOLVER_PORT"]
  url = f"http://{str(ip)}:{str(port)}"

  # Build dictionaries that process config file
  calibrations = get_calibration(working_directory, config, url)
  global_vials = "vials" in config and config["vials"] != "None"
  reactors, _ = check_vials_and_modes(config, global_vials)

  settings = {}
  special_settings = {}
  for reactor, vial_subset in reactors.items():
    settings[reactor] = check_settings(
      config, reactor, vial_subset, bioreactors.BASE_FIELDS,
      bioreactors.BASE_TUPLE_FIELDS, {"mem_len":"OD_values_to_average"})

    special_settings[reactor] = check_settings(
      config[reactor], reactor, vial_subset,
      bioreactors.REACTOR_SETTINGS_FIELDS[reactor]["fields"],
      bioreactors.REACTOR_SETTINGS_FIELDS[reactor]["tuple_fields"]
    )

  fluid_key, volumes = add_fluid_tracking(config, reactors)
  # Handle reactor configuration serialization
  commands = {}
  for reactor in reactors:
    to_write = [{"working_dir": str(working_directory),
                 "url": url,
                 "calibration": calibrations,
                 "base_setting": settings[reactor],
                 "special_settings": special_settings[reactor],
                 "fluid_key": fluid_key[reactor]}]
    commands[f"{name}.{reactor}.{timestamp()}"] = to_write

  # Handle fluid tracking
  if len(volumes) != 0:
    print("Volume tracking enabled")
  return commands, reactors, volumes

def build_reactor(name: str, mode: str, configurations: dict,
                  controls: evolver_controls.EvolverControls,
                  caller: str
                 ) -> bioreactor.Bioreactor:
  # Obtain experiment name and reactor name

  # Unpack yaml file
  calibrations = configurations["calibration"]
  base_config = configurations["base_settings"]
  special_settings = configurations["special_settings"]

  # Build reactor settings
  base_settings = dict_to_dataclass(base_config, bioreactor.ReactorSettings,
                                    bioreactors.BASE_FIELDS)
  special_settings["base_settings"] = base_settings
  reactor_module = importlib.import_module(f"bioreactor.{mode}")
  setting_cls_name = f"{mode.capitalize()}{'Settings'}"
  reactor_cls = getattr(reactor_module, setting_cls_name)
  settings = dict_to_dataclass(field_dict=special_settings, cls_obj=reactor_cls)

  # Build reactor
  reactor_cls = getattr(reactor_module, mode.capitalize())
  reactor = reactor_cls(name, controls, settings, calibrations, caller)
  return reactor

def dict_to_dataclass(field_dict: dict, cls_obj, fields: Iterable[str]=None):
  if fields is None:
    fields = {f.name for f in dataclasses.fields(cls_obj) if f.init}
  valid_args = {k:v for k, v in field_dict.items() if k in fields}
  return cls_obj(**valid_args)

def get_calibration(working_directory: Path, config: dict, url: str,
                    timeout: float=TIMEOUT) -> type_hints.Calibration:
  """Helper function to obtain calibrations from config and evolver.

  A helper functiont to obtain calibrations. If the calibration files exist
  in the directory, it will attempt to obtain them. If the calibrations do not
  then it will attempt to obtain them from the evolver.

  Args:
    working_directory: The directory that contains the calibrations
    config: The read yaml configuration file
    url: The url to connect to the evolver
    timeout: the number of seconds to wait before giving up on connecting
      to the evolver
  """
  # Obtain calibrations automatically from active calibration if not there
  to_get = []
  calibrations = {}
  for field_name in ["pump_cal", "temp_cal", "od_cal"]:
    if field_name not in config or config[field_name] == "None":
      to_get.append(field_name[:-len("_cal")])
      continue
    filename = Path(working_directory, config[field_name])
    if filename.is_file():
      with open(config[field_name], "r", encoding="utf8") as f:
        calibrations[field_name[:-len("_cal")]] = json.load(f)

  if len(to_get) == 0:
    return calibrations

  missing_cal_str = ", ".join(to_get)
  print(f"{missing_cal_str} are missing, obtaining calibrations...", end="")

  sio = socketio.Client()
  controls = evolver_controls.EvolverControls(EVOLVER_NAMESPACE)
  sio.register_namespace(controls)
  sio.connect(url)

  active_calibrations = controls.request_active_calibrations(timeout, False)

  sio.disconnect()
  sio.eio.disconnect(True)

  for field in to_get:
    calibrations[field] = active_calibrations[field]
  return calibrations

def timestamp(resolution=2, places=4) -> int:
  """Obtains a integer timestamp from the time

  Args:
    resolution: The decimal place to use. 0 means second-level resolution
    places: The order of the number of seconds to reset with. 10 means
      10 second reset of the timestamp
  """
  curr_time= time.time()
  return int(10**resolution * (curr_time % 10**places))

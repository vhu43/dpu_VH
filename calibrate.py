"""
VH 2023-03-09 updates:
Code has been updated to use fit classes so that fits are all stored in one
place and can be consistent for new code.

Updated callibration script for evolver
# Command to get calibrations:
# python calibrate.py -a <ip.address> -g

# Command to calibrate based off file
# python calibrate.py -a <ip.address> -n <file name in quotes>
      -t <type:sigmoid/linear/3d/exp> -f <name.after.fit> -p <od_135 vs od_90>

ZZ 2021-10-23 updates:
Code has been updated to enable generation of linear and exponential fits of
detector values to OD. These can be called via the -linear_od and -exp flags for
fit type (after -t). The data will similarly saved onto eVOLVER, and can be set
as active via the electron GUI interface. However, it is not certain whether
electron GUI will displace the correct conversion of raw data and OD. Once any
calibration is selected as active on the electron GUI, the eVOLVER.py script
should automatically pull calibration data when run. Alternatively, the
calibration file can be manually extracted from the calibrations.json
file located on pi@10.0.0.3:~/evolver/evolver/calibrations.json
"""

import sys
import argparse
import socketio
from pathlib import Path
from evolver_manager.utils import calibration_utils
from evolver_manager import evolver_controls
from evolver_manager import fits

EVOLVER_PORT = 8081
TIMEOUT = 10

def get_options():
  #Initiliaze fit type namespace
  valid_fit_string = ", ".join(fits.VALID_FIT_TYPES)

  # Parse arguments
  parser = argparse.ArgumentParser(
    prog="python calibrate.py",
    description="Create internal calibration for the evolver"
  )
  parser.add_argument("-a", "--ip", action = "store", dest = "ipaddress",
                      help = "IP address of eVOLVER", required=True)
  parser.add_argument(
    "-g", "--get-calibration-names", action = "store_true", dest = "getnames",
    help = "Prints out all calibration names present on the eVOLVER.")
  parser.add_argument("-n", "--calibration-name", action = "store",
                    dest = "calname", help = "Name of the calibration.")
  parser.add_argument("-t", "--fit-type", action = "store", dest = "fittype",
                    help = f"Valid options: {valid_fit_string}")
  parser.add_argument("-f", "--fit-name", action = "store", dest = "fitname",
                    help = "Desired name for the fit.")
  parser.add_argument(
    "-p", "--params", action = "store", dest = "params",
    help = "Desired parameter(s) to fit. Comma separated, no spaces")
  return parser

def main(sio: socketio.Client):
  parser = get_options()
  options = parser.parse_args()
  cal_name = options.calname
  get_names = options.getnames
  fit_type = options.fittype
  fit_name = options.fitname
  params = options.params

  ip_address = "http://" + options.ipaddress + str(EVOLVER_PORT)
  controls = evolver_controls.EvolverControls("/dpu-evolver")
  sio.register_namespace(controls)
  sio.connect(ip_address)

  if get_names:
    print("Getting calibration names...")
    calibration_names = controls.request_calibration_names()
    print()
    if calibration_names is None:
      print("Evolver did not respond in time")
      return -1

    for calibration_name in calibration_names:
      print(f"name: {calibration_name['name']}\n"
            f"\ttype: {calibration_name['calibrationType']}")
    print("fValid Fit Types:\n{valid_fit_string}\n")
    print("  To fit a paramter use:\n"
          f"python calibrate.py -a {options.ipaddress} -n <calibration name>"
          " -t <fit type> -f <fit name> -p <od_135, od_90, or temp>")
    return 0

  if cal_name:
    if fit_name is None:
      print("Please input a name for the fit!")
      parser.print_help()
      sys.exit(2)
    if fit_type not in fits.VALID_FIT_TYPES:
      print("Invalid fit type!")
      parser.print_help()
      sys.exit(2)
    if params is None:
      print("Must provide at least 1 parameter!")
      parser.print_help()
      sys.exit(2)
    calibration = controls.get_calibration_by_name(cal_name)
    params = params.strip().split(",")

  if cal_name is not None and not get_names:
    fit, fig, raw = calibration_utils.fit_curve(calibration, fit_type, fit_name,
                                                params)
    update_cal = input("Update eVOLVER with calibration? (y/n): ")
    data_dir = Path("Calibration_data")
    if update_cal == "y":
      fig_file = Path(data_dir, f"{fit_name}_{fit_type}_fit_plot")
      fig.savefig(fig_file)

      calibration_utils.log_calibration(raw, data_dir, fit_name, params)
      with open("Calibration_list.csv", "a", encoding="utf8") as f:
        f.write(", ".join((fit_name, "[" + "".join(params) + "]", fit_type)))
        f.write("\n")
  controls.push_calibration_fit(cal_name, fit)

if __name__ == "__main__":
  client = socketio.Client()
  main(client)
  client.eio.disconnect(True)
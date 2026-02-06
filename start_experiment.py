"""Command to start an experiment"""

import argparse
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

import socketio
import yaml

import daemon
import global_config
from evolver_manager import bioreactors
from evolver_manager.utils import config_utils

DEFAULT_PORT = global_config.DEFAULT_PORT
URL = global_config.DAEMON_HOST
BASE_EXP_DIR = Path(".")
LOG_DIR = global_config.log_dir
DAEMON = "summon_daemon.py"
BAR_CHAR = "\U00002501"
TIMEOUT = 10

# From MSDN
CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008


def main():
    """A method to initialize experiments and try to attach them to the daemon."""

    # Parse Arguments (This was missing!)
    options = get_options()
    name = options.exp_name

    # Assuming the working directory is a folder named after the experiment
    working_directory = BASE_EXP_DIR / name

    # Check Run Mode (New vs Restart)
    if options.runmode == "new":
        if working_directory.exists() and not options.always_yes:
            print(f"Directory {working_directory} already exists.")
            response = input("Overwrite? (y/n): ")
            if response.lower() not in ["y", "yes"]:
                print("Aborting.")
                return 0
    elif options.runmode == "restart":
        if not working_directory.exists():
            print(f"Cannot restart: Directory {working_directory} does not exist.")
            return -1

    # Tries to start a evolver manager daemon.
    kwargs = {}
    if platform.system() == "Windows":
        kwargs.update(creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)
    else:
        kwargs.update(start_new_session=True)

    daemon_cmd = [
        sys.executable,
        DAEMON,
        URL,
        str(DEFAULT_PORT),
        str(LOG_DIR),
        "--detatch",
    ]

    print(BAR_CHAR * os.get_terminal_size().columns)
    print("Daemon:")
    daemon_p = subprocess.Popen(daemon_cmd, stdout=subprocess.PIPE, **kwargs)

    # Wait briefly for daemon to initialize
    start_wait = time.time()
    while daemon_p.poll() is None:
        if time.time() - start_wait > 2.0:  # Don't hang forever
            break
        data = daemon_p.stdout.readline()
        if not data:
            continue
        line = data.decode("utf-8").rstrip()
        if line == "\U00000004":  # EOT
            break
        print(f"  {line}")
    print(f"\n{BAR_CHAR * os.get_terminal_size().columns}")

    # Build Configuration
    print("Building experiment configs to send to manager...", end="")
    try:
        config_file = working_directory / "config.yaml"

        if not config_file.exists():
            print(f"\nError: Config file not found at {config_file}")
            return -1

        with open(config_file, encoding="utf8") as f:
            config = yaml.safe_load(f)

        commands, modes, fluids = config_utils.encode_start(
            name, working_directory, config
        )
    except socketio.exceptions.ConnectionError as inst:
        print("\nUnable to connect to the evolver to obtain calibration:")
        print(f"\t{inst.args[0]}")
        return -1
    except (ValueError, KeyError) as inst:
        print("\nI think Something is wrong with the configuration")
        print(f"\t{inst.args[0]} invalid")
        return -1
    print("Built!")

    # Setup Directory (Filesystem)
    print("Setting up directory for your experiment...", end="")
    setup_directory(working_directory, modes, restart=(options.runmode == "restart"))
    print("Set up!")

    # Send to Daemon
    print("Sending configurations to the daemon...", end="")
    try:
        daemon.Daemon.command_initialize(
            URL, DEFAULT_PORT, name, commands.values(), modes
        )
        print("Sent!")
    except Exception as e:
        print(f"\nFailed to initialize daemon: {e}")
        return -1

    if fluids:
        print("Filling fluid configurations to the daemon...", end="")
        try:
            daemon.Daemon.command_refill(URL, DEFAULT_PORT, fluids, wait_time=10)
            print("Filled!")
        except Exception as e:
            print(f"\nFailed to refill fluids: {e}")

    print("Configuration pushed to the daemon, It is out of our hands now.")
    print("Goodbye")
    return 0


def get_options():
    """Create options parser for running the evolver."""
    description = "Run an eVOLVER experiment from the command line"
    parser = argparse.ArgumentParser(description=description)

    parser.add_argument(
        "exp_name",
        metavar="Experiment Name",
        action="store",
        help=("Enter the name of the experiment you are aiming to Start in this run"),
    )

    runmode = parser.add_mutually_exclusive_group(required=True)
    runmode.add_argument(
        "-r",
        "--restart",
        action="store_const",
        dest="runmode",
        const="restart",
        help=(
            "If entered, will restart the experiment if "
            "there is a file directory present"
        ),
    )

    runmode.add_argument(
        "-n",
        "--new",
        action="store_const",
        dest="runmode",
        const="new",
        help=("If enabled, start a new experiment. Use caution with this flag."),
    )

    runmode.add_argument(
        "-C",
        "--confirm_new",
        action="store_const",
        dest="runmode",
        const="confirm_new",
        help=(
            "If enabled, will not ask to confirm the "
            "overwrite of the new directory. Use extreme"
            "caution with this flag."
        ),
    )

    runmode.add_argument(
        "-E",
        "--emergency-restart",
        action="store_const",
        dest="runmode",
        const="emergency_restart",
        help=(
            "This should only be called via a bash script "
            "that restarts the experiment due to an unplanned"
            " restart that has stopped experiments mid-run."
        ),
    )

    parser.add_argument(
        "--always-yes",
        action="store_true",
        default=False,
        help="Answer yes to all questions "
        "(i.e. continues from existing experiment, "
        "overwrites existing data and blanks OD "
        "measurements)",
    )
    parser.add_argument(
        "--log-name",
        default="evolver.log",
        help="Log file name directory (default: %(default)s)",
    )

    log_nolog = parser.add_mutually_exclusive_group()
    log_nolog.add_argument(
        "--verbose",
        action="count",
        default=0,
        help="Increase logging verbosity level to DEBUG (default: INFO)",
    )
    log_nolog.add_argument(
        "--quiet",
        action="store_true",
        default=False,
        help="Disable logging to file entirely",
    )
    return parser.parse_args()


def setup_directory(working_directory, modes, restart=False):
    top_level_dirs = ["raw_data", "OD", "config", "logs"]

    if restart:
        print("\nRestarting experiment, clearing old directories...", end="")
        for directory in top_level_dirs + list(bioreactors.VALID_REACTORS):
            path = Path(working_directory, directory)
            if path.is_dir():
                rm_r(path)
        print(" Done.")

    # Set up directory in experiment directory
    for directory in ["raw_data", "OD", "config", "logs"]:
        Path(working_directory, directory).mkdir(parents=True, exist_ok=True)

    for directory in ["od_90_raw", "od_135_raw", "temp_raw"]:
        Path(working_directory, "raw_data", directory).mkdir(
            parents=True, exist_ok=True
        )

    for mode in modes:
        Path(working_directory, mode).mkdir(parents=True, exist_ok=True)


def rm_r(path: Path):
    """Recursive directory removal"""
    for child in path.iterdir():
        if child.is_file():
            child.unlink()
        else:
            rm_r(child)
    path.rmdir()


if __name__ == "__main__":
    # Ensure log dir exists for daemon
    LOG_DIR = Path("logs")
    LOG_DIR.mkdir(exist_ok=True)

    exitcode = main()
    sys.exit(exitcode)

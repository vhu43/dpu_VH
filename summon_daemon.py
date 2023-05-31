"""A daemon to manage connections to evolver units"""
from pathlib import Path
import sys
import argparse
import os
import asyncio
import daemon
import time

NUM_CYCLES_TO_WAIT = 1000
CYCLE_LENGTH = 4 # Number of seconds to wait between cycles
EXISTING_DAEMON_EXCODE = 3
UNICODE_EOT = "\U00000004"
EVOLVER_NAMESPACE = "/dpu-evolver"

DEFAULT_IP ="localhost"
DEFAULT_PORT = 60888

def get_options():
  parser = argparse.ArgumentParser(
  prog="evolver_daemon.py",
  description=("Manges evolver and does atomic updating to send commands and "
                "log experimental outcomes. Normally called as a subprocess "
                "when starting an experiment")
  )

  parser.add_argument("ip", metavar="ip", type=str, nargs="?",
                      help="The ip to bind to for communicating")
  parser.add_argument("port", metavar="port", type=int, nargs="?",
                      help="The port to bind to for communicating")
  parser.add_argument("log_dir", metavar="log_dir", nargs="?",
                      help="The directory the runtime log files are written to")
  parser.add_argument("-d", "--detatch", action="store_true", default=False,
                      help="Detatch from stdout")
  return parser.parse_args()

def main():
  arguments = get_options()
  ip = arguments.ip
  port = arguments.port
  log_path = Path(arguments.log_dir, "log.log")
  detatch = arguments.detatch

  if ip is None:
    ip = DEFAULT_IP
  if port is None:
    port = DEFAULT_PORT

  print(f"Attempting to summon a daemon at {ip}:{port} that logs into "
        f"{log_path}", flush=True)
  try:
    evolver_daemon = daemon.Daemon(ip, port, __name__, log_path)
  except OSError as e:
    print("An instance of the daemon exists or it's socket is occupied",
          flush=True)
    print(UNICODE_EOT, flush=True)

    return EXISTING_DAEMON_EXCODE
  else:
    print("Successfully summoned daemon, now waiting for experiment",
          flush=True)
  if detatch:
    print("Now silencing stdout and detacthing...", flush=True)
    time.sleep(2)
    print(" Goodbye", flush=True)
    print(UNICODE_EOT, flush=True)
    sys.stdout = open(os.devnull, "a", encoding="utf8")
  print(UNICODE_EOT, flush=True)
  asyncio.run(evolver_daemon.main_job())
  return 0

if __name__ == "__main__":
  exit_code = main()
  sys.exit(exit_code)

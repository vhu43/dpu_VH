"""Evolver Daemon that manages a set of evolvers
"""

from collections.abc import Iterable
import os
from evolver_manager import evolver_controls, evolver_manager
from evolver_manager.utils import config_utils
from evolver_manager.bioreactors import bioreactor
import logging
import time
import asyncio
import socketio
import tempfile
import yaml
from pathlib import Path
import smtplib
import ssl
import email.message
import global_config
import socket

class Daemon:
  """A class that defines a Daemon to manage all connections to evolvers.

  This class implements asynchronous control of a main server to manage all
  concurrent evolver connections. it implements an asynchronous event loop
  to check for new experiments pushed in, and it

  Attributes:
    waited_cycles: The number of cycles the daemon has waited with nothing to do
    curr_time: the current time for the Daemon
    logger: The logger for logging events
    _log_dir: The global logging directory for all activities
    _update_queue: The list of all updates to be done
  """
  # parameters for daemon activity
  DEFAULT_PAUSE = 4
  NUM_CYCLES_TO_WAIT = 10
  MIN_FLUID_VOL = global_config.MIN_FLUID_VOL

  # Parameters for client handling
  CLIENT_PERIOD = 0.2
  TIMEOUT = 10
  EOT = "\u0004" # Code to tell client it is done
  ENQ = "\u0005" # Code to ask client for more info
  ACK = "\u0006"
  VT = "\u0011"

  EVOLVER_NAMESPACE = "/dpu-evolver"

  def __init__(self, ip, port, caller: str, log_dir: Path):
    # Parameters for evolver runtime
    self.waited_cycles: int = 0
    self.curr_time = time.time()
    self.logger = logging.Logger(caller)
    self._log_dir = log_dir
    self._update_queue = []

    # States for daemon to be in
    self._pause = False
    self._alive = False

    # For starting new experiments
    self._to_start: dict[str, list[int, float]] = {}
    self._to_end: list[str] = []
    self._temp_dir = Path(tempfile.mkdtemp())

    # For tracking individual evolvers
    self.evolvers: dict[str, evolver_manager.EvolverManager] = {}
    self._exp_key: dict[
      str,
      list[tuple[evolver_manager.EvolverManager, bioreactor.Bioreactor]]
      ]  = {}

    # For fluid tracking
    self._fluid_key: dict[int, dict[str, str]] = {}
    self._vial_key:dict[str, set[int]] = {}
    self._fluids: dict[str, float] = {}
    self._recurrent: dict[int, dict[str, list[float]]] = {}
    self._needs_refill = set()
    self._fluid_custodians = {}

    ## Constructing asynchronous listener for commands, but does not make server
    self.server_coroutine = asyncio.start_server(self.handle_client, ip, port)
    self._server: asyncio.Server = None

  async def main_job(self):
    # Start up server
    self._server = await self.server_coroutine
    client_handling = asyncio.create_task(self._server.start_serving()) # Done
    exp_loading = asyncio.create_task(self.update_experiment_list()) # Done
    manage_updates = asyncio.create_task(self.manage_updates()) # Done
    activity_checking = asyncio.create_task(self.check_for_activity()) # Done

    self._alive = True

    await client_handling
    await exp_loading
    await manage_updates
    await activity_checking
    # Closing server and dying
    print("Close Job")
    self._server.close()

  # Command handling methods

  async def handle_client(self, reader: asyncio.StreamReader,
                          writer: asyncio.StreamWriter):
    """Timeout-enabled handling of clients communicating with Daemon"""
    self.logger.info("A command was requested")
    try:
      await asyncio.wait_for(self.process_command(reader, writer), self.TIMEOUT)
    except asyncio.TimeoutError:
      self.logger.error("Daemon waited too long and closed connection")
    except ConnectionError:
      self.logger.error("Client unexpectedly lost connection")
    except ValueError:
      self.logger.error("Incorrect command?")
    writer.close()
    await asyncio.sleep(0.5)

  async def process_command(self, reader: asyncio.StreamReader,
                            writer: asyncio.StreamWriter):
    """The command process tree."""
    request = await(reader.read())
    response = None
    match request.decode("utf8"):
      case "initialize": # Send them the address to the temp_dir for loading
        writer.write(str(self._temp_dir).encode("utf8"))
        await writer.drain()

        # Expect exp_name, num_to_init
        msg = (await reader.read()).decode("utf8")
        writer.write(str(self.ACK).encode("utf8"))
        await writer.drain()
        exp_name, num_to_init, *_ = msg.split(self.VT)
        if exp_name is None or num_to_init.isdigit():
          return
        num_to_init = int(num_to_init)

        # Build experiment
        window = self.NUM_CYCLES_TO_WAIT * self.DEFAULT_PAUSE
        self._to_start[exp_name] = {"num_left":num_to_init,
                                    "window": time.time() + window}
        writer.write(self.EOT.encode("utf8"))
        await writer.drain()

      case "pause": # Try to pause, and return EOT when paused
        if not self._pause:
          writer.write(self.ACK.encode("utf8"))
          await self.pause()
        writer.write(self.EOT.encode("utf8"))
        await writer.drain()

      case "refill":
        while response != self.EOT:
          writer.write(self.ACK.encode("utf8"))
          await writer.drain()
          response = (await reader.read()).decode("utf8").split(self.VT)
          if len(response) > 1:
            fluid, vol, *_ = response
            if fluid in self._fluids:
              self._fluids[fluid] = float(vol)
        writer.write(self.EOT.encode("utf8"))
        await writer.drain()

      case "unpause": # Try to unpause, and return EOT when unpaused
        if self._pause:
          writer.write(self.ACK.encode("utf8"))
          await self.unpause()
        writer.write(self.EOT.encode("utf8"))
        await writer.drain()

      case "stop_exp":
        while response != self.EOT:
          response = (await reader.read()).decode("utf8")
          self._to_end.append(response)
          writer.write(self.ACK.encode("utf8"))
          await writer.drain()
        writer.write(self.EOT.encode("utf8"))
        await writer.drain()

      case _:
        self.logger.error("Unknown command received. Ignoring")
        writer.write(self.EOT.encode("utf8"))
    return

  # Daemon tasks

  async def check_for_activity(self): #Done
    """A task to periodically check to make sure managers are working.

    A task to check for Evolver activity. If there is none for too long, kill
    the daemon and start shutting down. Othewise, populate the update queue
    with updates.
    """
    start = time.time()
    # Only check a certain amount of cycles before retiring daemon
    while self.waited_cycles <= self.NUM_CYCLES_TO_WAIT:
      for evolver, manager in self.evolvers.items():
        while manager.has_updates():
          self._update_queue.append(manager.updates.pop())
        if manager.has_no_active_experiments:
          self.dettach_evolver(manager)
          del self.evolvers[evolver]
      if len(self.evolvers) == 0:
        self.waited_cycles += 1
      print(f"Checking for activity ({self.waited_cycles} {self._alive})")
      await asyncio.sleep(max(self.DEFAULT_PAUSE - time.time() + start, 0))
      start = time.time()
    self.logger.info("Daemon has no active jobs and has waited too long.")
    self._alive = False
    self.logger.info("Daemon has been killed")
    return

  async def update_experiment_list(self):
    """A task to periodically check to for new experiments and end finished ones
    """
    start = time.time()
    while self._alive:
      # Only keep a experiment to start for a certain amount of time
      for name, values in self._to_start.items():
        if time.time() > values[1]:
          del self._to_start[name]
          self.logger.error("Took too long to find %s. Deleting", name)

      # Deleting Unknown files
      for file in self._temp_dir.iterdir():
        *name, mode, _ = file.stem.split(".")
        name = ".".join(name)
        if name not in self._to_start: #Unknown file?
          file.unlink(missing_ok=True)
          continue

        with open(file, "r", encoding="utf8") as f:
          experiment_dict = next(yaml.load_all(f, Loader=yaml.FullLoader))
        url = experiment_dict["url"]
        if url not in self.evolvers:
          self.attach_evolver(url)
        manager = self.evolvers[url]

        # Build the reactors needed for experiment
        working_dir = experiment_dict["working_dir"]
        reactor = config_utils.build_reactor(
          name, mode, experiment_dict, manager.controls, __name__)
        manager.add_experiment(name, reactor, working_dir)

        self._to_start[name][0] -= 1
        if self._to_start[name][0] == 0:
          del self._to_start[name]
          self.logger.info("Found all experiments for %s", name)

        fluid_dict: dict[int, dict[str, str]] = experiment_dict["fluid_key"]
        for vial, pump_dict in fluid_dict.items():
          for _, fluid_name in pump_dict.items():
            if fluid_name not in self._fluids:
              self._fluids[fluid_name] = 0
              self._vial_key[fluid_name] = set()
              self._vial_key[fluid_name].add(vial)
        self._fluid_key.update(fluid_dict)

      # End Experiments
      # Delete them from the manager, and delete them from fluid management
      vials = []
      for exp in self._to_end:
        for manager, reactor in self._exp_key[exp]:
          vials += reactor.vials
          manager.end_experiment(exp)
      for vial in vials:
        for pump in config_utils.PUMP_SET[:-1]:
          fluid_name = self._fluid_key[vial].get(pump, None)
          if fluid_name:
            self._vial_key[fluid_name].discard(vial)
        del self._fluid_key[vial]

      # Clean up fluid Lists
      for fluid_name, vial_set in self._vial_key.items():
        if len(vial_set) == 0:
          del self._vial_key[fluid_name]
          del self._fluids[fluid_name]
      print(f"Updating experiment list {self.waited_cycles}, {self._alive}")
      await asyncio.sleep(max(self.DEFAULT_PAUSE - time.time() + start, 0))
      start = time.time()
    return

  async def manage_updates(self):
    """Coroutine to manage the update queue.

    Coroutine to manage the update queue. Will pop all updates, then work out
    changes in fluids and logging of data in the respective working directories
    """
    pumps = global_config.PUMP_SET[:-1]

    while self._alive:
      start = time.time()
      # Check for any new updates
      evolver: evolver_manager.EvolverManager
      for _, evolver in self.evolvers.items():
        while len(evolver.updates) > 0:
          self._update_queue.append(evolver.updates.pop())

      # Check for fluids and things to record
      while len(self._update_queue) > 0:
        update_list = self._update_queue.pop()
        for update in update_list:
          match update["type"]:
            case "fluid":
              single_dict = update["single"]
              for vial, pump_dict in single_dict.items():
                for pump in pumps:
                  fluid_name = self._fluid_key[vial].get(pump, None)
                  if fluid_name is None:
                    continue
                  self._fluids[fluid_name] -= pump_dict[pump]
              recurr_dict = update["recurrent"]
              for vial, pump_dict in recurr_dict.items():
                for pump in pumps:
                  fluid_name = self._fluid_key[vial].get(pump, None)
                  if fluid_name is None:
                    continue
                  vol, period = pump_dict[pump]
                  if period > 0:
                    self._recurrent[fluid_name] = [vol, period, start]

      # Finished all updates, now update all fluids
      for fluid_name, params in self._recurrent.items():
        vol, period, last = params
        elapsed_time = start - last
        if elapsed_time > period:
          dilutions = elapsed_time // period
          new_last = start - elapsed_time % period
          self._recurrent[fluid_name] = [vol, period, new_last]
          self._fluids[fluid_name] -= dilutions * vol

      #Handle Alerts
      for fluid_name, vol in self._fluids.items():
        if vol < self.MIN_FLUID_VOL:
          self._needs_refill.add(fluid_name)
        if fluid_name in self._fluid_custodians:
          msg = email.message.EmailMessage()
          body = (f"\n{fluid_name} is below {self.MIN_FLUID_VOL} mL",
                  "\n\nCheers,\nDaemon")
          msg.set_content(body)
          msg["To"] = "; ".join(self._fluid_custodians[fluid_name])
          msg["Subject"] = f"URGENT: FLUID ALERT {fluid_name}"
          self.send_email(msg)

      # Don't eat up CPU
      await asyncio.sleep(max(self.DEFAULT_PAUSE - time.time() + start, 0))
      start = time.time()

  # Helper Methods

  def attach_evolver(self, evolver_address):
    """Method to add an evolver under the daemon's management.
    """
    sio = socketio.Client()
    controls = evolver_controls.EvolverControls(self.EVOLVER_NAMESPACE)
    manager = evolver_manager.EvolverManager(
      self.EVOLVER_NAMESPACE, Path(self._log_dir), controls)

    sio.register_namespace(controls)
    sio.register_namespace(manager)
    sio.connect(evolver_address)
    self.evolvers[evolver_address] = manager

  def dettach_evolver(self, manager: evolver_manager.EvolverManager):
    """Helper function to detach an evolver
    """
    manager.client.disconnect()
    manager.client.eio.disconnect()

  def send_email(
      self, email_msg: email.message.EmailMessage,
      smtp_server=global_config.SMTP_SERVER,
      port=global_config.PORT):
    """Send an email message using the daemon's email address.
    """
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(smtp_server, port, context) as server:
      server.login(global_config.SENDER_EMAIL, global_config.EMAIL_PASSWORD)
      server.send_message(email_msg)
    self.logger.info("Sent an email to %s regarding ")

  async def pause(self):
    """Helper function to pause evolver commands"""
    for _, manager in self.evolvers.items():
      manager.controls.lock()
    self._paused = True

  async def unpause(self):
    for _, manager in self.evolvers.items():
      manager.controls.unlock()
    self._paused = False

  # Methods to talk to the Daemon

  @classmethod
  def push_to_daemon(cls, name: str, to_write: Iterable, data_dir: Path):
    """A helper function to atomically write a experiment initialization file.

    A helper function to atomically write an experiment initialization file.
    Creates a temporary directory in the same location as the intended endpoint,
    then makes a temporary file in that directory, then automatically moves it
    to the proper directory.
    """
    with tempfile.TemporaryDirectory(dir=data_dir.parent,
                                    ignore_cleanup_errors=True) as d:
      with tempfile.NamedTemporaryFile("w", dir=d, delete=False) as f:
        for stuff in to_write:
          yaml.dump(stuff, f, explicit_start=True, explicit_end=True)
        f.flush()
        os.fsync(f.fileno())
      os.replace(f.name, Path(data_dir, name))

  # Commands to handle talking to daemon

  @classmethod
  def _command(cls, ip, port, command, timeout):
    """Helper method to connect to daemon and send a command
    """
    # Try to connect to the daemon
    try:
      s = socket.create_connection((ip, port), timeout=timeout)
      s.send(f"{command}".encode("utf8"))
    except TimeoutError:
      print(f"Failed to connect to daemon within {timeout} seconds")
      return -1
    except ConnectionRefusedError:
      print("Daemon refused connection. Can you check port?")
      print(f"\turl : {ip}\n\tport: {port}")
      return -1
    print("Connected to Daemon")
    return s

  @classmethod
  def command_initialize(cls, ip, port, exp_name, packages, package_types,
                         timeout=10):
    s = cls._command(ip, port, "initialize", timeout)
    msg = s.recv(global_config.MSG_LENGTH)
    dirname = Path(msg.decode())
    # Tell Daemon we want to initialize, and start sending packages
    s.send("initialize".encode("utf8"))
    print(f"Loading area is {dirname}")
    msg = s.recv(global_config.MSG_LENGTH).decode("utf8")
    while msg == cls.ACK:
      s.send(f"{exp_name}{cls.VT}{str(len(packages))}".encode("utf8"))
      msg = s.recv(global_config.MSG_LENGTH).decode("utf8")
    s.send(cls.EOT.encode("utf8"))
    for package, package_type in zip(packages, package_types):
      name = f"{exp_name}.{package_type}.yaml"
      cls.push_to_daemon(name, package, dirname)
    return

  @classmethod
  def _change_pause_status(cls, ip, port, pause=True, timeout=10):
    """Command to pause or unpause the running of the evolver
    """
    if pause:
      cmd = "pause"
    else:
      cmd = "unpause"
    s = cls._command(ip, port, cmd, timeout)
    msg = s.recv(global_config.MSG_LENGTH).decode("utf8")
    while msg != cls.EOT:
      msg = s.recv(global_config.MSG_LENGTH).decode("utf8")
    s.send(cls.EOT.encode("utf8"))
    s.close()

  @classmethod
  def command_refill(cls, ip, port, fluids: dict, wait_time=300, timeout=10):
    """Command to pause the evolver, send a fluid refill, then unpause
    """
    cls._change_pause_status(ip, port, pause=True, timeout=timeout)
    s = cls._command(ip, port, "refill", timeout)
    for fluid, vol in fluids.items():
      s.send(f"{fluid}{cls.VT}{str(vol)}".encode("utf8"))
      msg = s.recv(global_config.MSG_LENGTH).decode("utf8")
      if msg != cls.ACK:
        raise ValueError(f"Unexpected token received. Expected {cls.ACK}, "
                         f"{msg} recevied")
    s.send(cls.EOT.encode("utf8"))
    s.close()
    time.sleep(wait_time)
    cls._change_pause_status(ip, port, pause=False, timeout=timeout)
    return

  @classmethod
  def end_experiments(cls, ip: str, port: int, exp_names: Iterable[str]):
    s = cls._command(ip, port, "stop_exp", 10)
    for exp_name in exp_names:
      s.send(exp_name.encode("utf8"))
      msg = s.recv(global_config.MSG_LENGTH).decode("utf8")
      if msg != cls.ACK:
        raise ValueError(f"Unexpected token received, Expected {cls.ACK}, "
                         f"{msg} recevied")
    s.send(cls.EOT.encode("utf8"))
    s.close()
    return

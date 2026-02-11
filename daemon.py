"""Evolver Daemon that manages a set of evolvers"""

import asyncio
import email.message
import json
import logging
import os
import smtplib
import socket
import ssl
import struct
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import socketio
import yaml

import global_config
from evolver_manager import evolver_controls, evolver_manager
from evolver_manager.bioreactors import bioreactor
from evolver_manager.utils import config_utils


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
        self._to_start: dict[str, dict[str, int | float]] = {}
        self._to_end: list[str] = []
        self._temp_dir = Path(tempfile.mkdtemp())

        # For tracking individual evolvers
        self.evolvers: dict[str, evolver_manager.EvolverManager] = {}
        self._exp_key: dict[
            str, list[tuple[evolver_manager.EvolverManager, bioreactor.Bioreactor]]
        ] = {}

        # For fluid tracking
        self._fluid_key: dict[int, dict[str, str]] = {}
        self._vial_key: dict[str, set[int]] = {}
        self._fluids: dict[str, float] = {}
        self._recurrent: dict[str, list[float]] = {}
        self._needs_refill = set()
        self._fluid_custodians = {}

        ## Constructing asynchronous listener for commands, but does not make server
        self.server_coroutine = asyncio.start_server(self.handle_client, ip, port)
        self._server: asyncio.Server | None = None

    async def main_job(self):
        # Start up server
        self._server = await self.server_coroutine
        client_handling = asyncio.create_task(self._server.start_serving())  # Done
        exp_loading = asyncio.create_task(self.update_experiment_list())  # Done
        manage_updates = asyncio.create_task(self.manage_updates())  # Done
        activity_checking = asyncio.create_task(self.check_for_activity())  # Done

        self._alive = True

        print("\u0004", flush=True)

        await client_handling
        await exp_loading
        await manage_updates
        await activity_checking
        # Closing server and dying
        print("Close Job")
        self._server.close()

    # Command handling methods

    async def handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ):
        """Robust command handler using JSON messaging."""
        self.logger.info("Client connected")
        try:
            # Wait for the command dictionary
            request = await asyncio.wait_for(self.recv_msg(reader), self.TIMEOUT)

            if request:
                command = request.get("command")
                args = request.get("args", {})
                response = {"status": "ok"}  # Default success response

                # -- ROUTING LOGIC --
                if command == "initialize":
                    # 1. Send temp dir path first so client knows where to write files
                    await self.send_msg(writer, {"path": str(self._temp_dir)})

                    # 2. Wait for experiment details from client
                    details = await self.recv_msg(reader)
                    if details:
                        # FIX: Logic was inverted. Now correctly checks if it IS valid.
                        if not str(details["num"]).isdigit():
                            response = {"status": "error", "msg": "Invalid number"}
                        else:
                            # Register the experiment start
                            window = self.NUM_CYCLES_TO_WAIT * self.DEFAULT_PAUSE
                            self._to_start[details["name"]] = {
                                "num_left": int(details["num"]),
                                "window": time.time() + window,
                            }
                            response = {"status": "ok"}

                elif command == "refill":
                    for fluid, vol in args.items():
                        if fluid in self._fluids:
                            self._fluids[fluid] = float(vol)
                    response = {"status": "ok"}

                elif command == "pause":
                    if not self._pause:
                        await self.pause()
                    response = {"status": "ok"}

                elif command == "unpause":
                    if self._pause:
                        await self.unpause()
                    response = {"status": "ok"}

                elif command == "stop_exp":
                    self._to_end.append(args.get("name"))
                    response = {"status": "ok"}

                else:
                    self.logger.error(f"Unknown command: {command}")
                    response = {"status": "error", "msg": "Unknown command"}

                # Send Final Response
                await self.send_msg(writer, response)

        except Exception as e:
            self.logger.error(f"Error handling client: {e}")
            # Try to send error back if connection is still open
            try:
                await self.send_msg(writer, {"status": "error", "msg": str(e)})
            except Exception:
                self.logger.debug("Failed to send error response to client", exc_info=True)
        finally:
            writer.close()

    # Daemon tasks
    async def check_for_activity(self):  # Done
        """A task to periodically check to make sure managers are working.

        A task to check for Evolver activity. If there is none for too long, kill
        the daemon and start shutting down. Othewise, populate the update queue
        with updates.
        """
        start = time.time()
        # Only check a certain amount of cycles before retiring daemon
        while self.waited_cycles <= self.NUM_CYCLES_TO_WAIT:
            for evolver, manager in self.evolvers.items():
                while manager.has_updates:
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
        """A task to periodically check to for new experiments and end finished ones"""
        start = time.time()
        while self._alive:
            # Only keep a experiment to start for a certain amount of time
            expired = [
                name
                for name, values in self._to_start.items()
                if time.time() > values["window"]
            ]
            for name in expired:
                del self._to_start[name]
                self.logger.error("Took too long to find %s. Deleting", name)

            # Deleting Unknown files
            for file in self._temp_dir.iterdir():
                *name, mode, _ = file.stem.split(".")
                name = ".".join(name)
                if name not in self._to_start:  # Unknown file?
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
                    name, mode, experiment_dict, manager.controls, __name__
                )
                manager.add_experiment(name, reactor, working_dir)

                self._to_start[name]["num_left"] -= 1
                if self._to_start[name]["num_left"] == 0:
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
            empty_fluids = [
                fluid_name
                for fluid_name, vial_set in self._vial_key.items()
                if len(vial_set) == 0
            ]
            for fluid_name in empty_fluids:
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
                                        self._recurrent[fluid_name] = [
                                            vol,
                                            period,
                                            start,
                                        ]
                                    else:
                                        if fluid_name in self._recurrent:
                                            del self._recurrent[fluid_name]

            # Finished all updates, now update all fluids
            for fluid_name, params in self._recurrent.items():
                vol, period, last = params
                elapsed_time = start - last
                if elapsed_time > period:
                    dilutions = elapsed_time // period
                    new_last = start - elapsed_time % period
                    self._recurrent[fluid_name] = [vol, period, new_last]
                    self._fluids[fluid_name] -= dilutions * vol

            # Handle Alerts
            for fluid_name, vol in self._fluids.items():
                if vol < self.MIN_FLUID_VOL:
                    self._needs_refill.add(fluid_name)
                if fluid_name in self._fluid_custodians:
                    msg = email.message.EmailMessage()
                    body = (
                        f"\n{fluid_name} is below {self.MIN_FLUID_VOL} mL",
                        "\n\nCheers,\nDaemon",
                    )
                    msg.set_content(body)
                    msg["To"] = "; ".join(self._fluid_custodians[fluid_name])
                    msg["Subject"] = f"URGENT: FLUID ALERT {fluid_name}"
                    self.send_email(msg)

            # Don't eat up CPU
            await asyncio.sleep(max(self.DEFAULT_PAUSE - time.time() + start, 0))
            start = time.time()

    # Helper Methods

    def attach_evolver(self, evolver_address):
        """Method to add an evolver under the daemon's management."""
        sio = socketio.Client()
        controls = evolver_controls.EvolverControls(self.EVOLVER_NAMESPACE)
        manager = evolver_manager.EvolverManager(
            self.EVOLVER_NAMESPACE, Path(self._log_dir), controls
        )

        sio.register_namespace(controls)
        sio.register_namespace(manager)
        sio.connect(evolver_address)
        self.evolvers[evolver_address] = manager

    def dettach_evolver(self, manager: evolver_manager.EvolverManager):
        """Helper function to detach an evolver"""
        if manager.client is not None:
            manager.client.disconnect()
            manager.client.eio.disconnect()

    def send_email(
        self,
        email_msg: email.message.EmailMessage,
        smtp_server=global_config.SMTP_SERVER,
        port=global_config.PORT,
    ):
        """Send an email message using the daemon's email address."""
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(smtp_server, port, context=context) as server:
            server.login(global_config.SENDER_EMAIL, global_config.EMAIL_PASSWORD)
            server.send_message(email_msg)
        self.logger.info("Sent an email to %s regarding ")

    async def pause(self):
        """Helper function to pause evolver commands"""
        for _, manager in self.evolvers.items():
            manager.controls.lock()
        self._pause = True

    async def unpause(self):
        for _, manager in self.evolvers.items():
            manager.controls.unlock()
        self._pause = False

    # Methods to talk to the Daemon

    @classmethod
    def push_to_daemon(cls, name: str, to_write: Iterable[Any], data_dir: Path):
        """A helper function to atomically write a experiment initialization file.

        A helper function to atomically write an experiment initialization file.
        Creates a temporary directory in the same location as the intended endpoint,
        then makes a temporary file in that directory, then automatically moves it
        to the proper directory.
        """
        with tempfile.TemporaryDirectory(
            dir=data_dir.parent, ignore_cleanup_errors=True
        ) as d:
            with tempfile.NamedTemporaryFile("w", dir=d, delete=False) as f:
                for stuff in to_write:
                    yaml.dump(stuff, f, explicit_start=True, explicit_end=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(f.name, Path(data_dir, name))

    # Commands to handle talking to daemon

    async def send_msg(self, writer: asyncio.StreamWriter, data: dict[str, Any]):
        """Sends a dictionary as a length-prefixed JSON string."""
        msg_bytes = json.dumps(data).encode("utf-8")
        # Pack the length of the message into 4 bytes (Big Endian Unsigned Int)
        header = struct.pack("!I", len(msg_bytes))
        writer.write(header + msg_bytes)
        await writer.drain()

    async def recv_msg(self, reader: asyncio.StreamReader) -> dict[str, Any] | None:
        """Receives a length-prefixed JSON string."""
        try:
            # 4-byte header for length
            header = await reader.readexactly(4)
            (length,) = struct.unpack("!I", header)
            # read exactly length bytes
            payload = await reader.readexactly(length)
            return json.loads(payload.decode("utf-8"))
        except (asyncio.IncompleteReadError, struct.error):
            # Connection closed or malformed header
            return None

    @classmethod
    def _command(cls, ip, port, command, args=None, timeout=10):
        """Helper method to connect to daemon and send a JSON command."""
        if args is None:
            args = {}

        # Create socket connection
        s = socket.create_connection((ip, port), timeout=timeout)

        # Pack and Send Request
        payload = json.dumps({"command": command, "args": args}).encode("utf-8")
        header = struct.pack("!I", len(payload))
        s.sendall(header + payload)

        # Receive Response Header
        resp_header = s.recv(4)
        if not resp_header:
            s.close()
            raise ConnectionError("Daemon closed connection")

        (length,) = struct.unpack("!I", resp_header)

        # Receive Response Body
        resp_payload = b""
        while len(resp_payload) < length:
            chunk = s.recv(length - len(resp_payload))
            if not chunk:
                raise ConnectionError("Incomplete read")
            resp_payload += chunk

        return json.loads(resp_payload.decode("utf-8")), s

    @classmethod
    def command_initialize(
        cls, ip, port, exp_name, packages, package_types, timeout=10
    ):
        # 1. Send "initialize" command
        response, sock = cls._command(ip, port, "initialize", timeout=timeout)

        # 2. Get the temp directory from the response
        dirname = Path(response["path"])
        print(f"Loading area is {dirname}")

        # 3. Send the experiment details (This replaces the old double-send logic)
        details = {"name": exp_name, "num": len(packages)}
        payload = json.dumps(details).encode("utf-8")
        header = struct.pack("!I", len(payload))
        sock.sendall(header + payload)

        # 4. Wait for final acknowledgement
        resp_header = sock.recv(4)
        sock.close()  # Done talking

        # 5. Push files to the directory
        for package, package_type in zip(packages, package_types):
            name = f"{exp_name}.{package_type}.yaml"
            cls.push_to_daemon(name, package, dirname)
        return

    @classmethod
    def _change_pause_status(cls, ip, port, pause=True, timeout=10):
        """Command to pause or unpause the running of the evolver"""
        cmd = "pause" if pause else "unpause"
        # The _command helper now handles the full JSON transaction.
        # We don't need to wait for EOT or ACK anymore.
        response, sock = cls._command(ip, port, cmd, timeout=timeout)
        sock.close()

        if response.get("status") != "ok":
            print(f"Error changing pause status: {response.get('msg')}")

    @classmethod
    def command_refill(cls, ip, port, fluids: dict[str, Any], wait_time=300, timeout=10):
        """Command to pause the evolver, send a fluid refill, then unpause"""
        # 1. Pause
        cls._change_pause_status(ip, port, pause=True, timeout=timeout)

        # 2. Refill (Send data as JSON args, not raw strings!)
        # The server expects args={"fluid_name": "volume", ...}
        response, sock = cls._command(ip, port, "refill", args=fluids, timeout=timeout)
        sock.close()

        if response.get("status") != "ok":
            print(f"Refill error: {response.get('msg')}")
            # We still try to unpause even if refill failed

        # 3. Wait and Unpause
        print(f"Refill sent. Waiting {wait_time} seconds...")
        time.sleep(wait_time)
        cls._change_pause_status(ip, port, pause=False, timeout=timeout)
        return

    @classmethod
    def end_experiments(cls, ip: str, port: int, exp_names: Iterable[str]):
        """Command to stop specific experiments"""
        for exp_name in exp_names:
            # Send one command per experiment to ensure atomic handling
            response, sock = cls._command(
                ip, port, "stop_exp", args={"name": exp_name}, timeout=10
            )
            sock.close()
            if response.get("status") != "ok":
                print(f"Error stopping {exp_name}: {response.get('msg')}")
        return

"""One serial-port owner shared by all local GateKeeper processes."""

from __future__ import annotations

import os
import threading
import time
import uuid
import zlib
from contextlib import suppress
from multiprocessing.connection import Client, Listener
from typing import Any

import serial

_AUTHKEY = b"gatekeeperlib-local-broker-v1"
_HOST = "127.0.0.1"


def broker_address(port: str) -> tuple[str, int]:
    key = port.strip().upper().encode("utf-8")
    return _HOST, 42000 + zlib.crc32(key) % 10000


class BrokerState:
    def __init__(self, port: str, baud_rate: int, timeout: float) -> None:
        self.port = port
        self.baud_rate = baud_rate
        self.timeout = timeout
        self.serial = self._open_serial()
        self.read_lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.transaction = threading.Condition()
        self.owner: str | None = None
        self.owner_depth = 0
        self.active_owner: str | None = None
        self.active_continuous = False
        self.clients = 0
        self.connections: set[Any] = set()
        self.last_client_left = time.monotonic()

    def _open_serial(self):
        return serial.Serial(
            port=self.port,
            baudrate=self.baud_rate,
            bytesize=serial.EIGHTBITS,
            timeout=0.05,
            write_timeout=self.timeout,
        )

    def acquire(self, client_id: str) -> None:
        with self.transaction:
            while self.owner not in (None, client_id):
                self.transaction.wait()
            self.owner = client_id
            self.owner_depth += 1

    def release(self, client_id: str) -> None:
        with self.transaction:
            if self.owner != client_id:
                raise RuntimeError("serial transaction is not owned by this client")
            self.owner_depth -= 1
            if self.owner_depth == 0:
                if self.active_owner == client_id and not self.active_continuous:
                    self.active_owner = None
                self.owner = None
                self.transaction.notify_all()

    def write(self, client_id: str, data: bytes, *, interrupt: bool = False) -> int:
        with self.write_lock:
            old_timeout = self.serial.write_timeout
            # ASCII commands can be much larger than the old fixed five-second
            # timeout. Include wire time plus a generous host scheduling margin.
            self.serial.write_timeout = max(self.timeout, len(data) * 10 / self.baud_rate + 2.0)
            try:
                written = self.serial.write(data)
                if written != len(data):
                    raise serial.SerialTimeoutException(
                        f"wrote {written} of {len(data)} serial bytes"
                    )
                self.serial.flush()
            except BaseException:
                self._recover_locked()
                raise
            finally:
                self.serial.write_timeout = old_timeout

        command = data.decode("ascii", errors="ignore").strip().upper()
        if command.startswith("AWG_BUFFER_RAMP,"):
            self.active_owner = client_id
            self.active_continuous = True
        elif command and command != "STOP":
            self.active_owner = client_id
            self.active_continuous = False
        if interrupt or command == "STOP":
            self.active_owner = None
            self.active_continuous = False
        return written

    def interrupt(self) -> bytes:
        """Stop an active owner, but never leave a stop flag while idle."""
        if self.active_owner is None:
            self.reset_input()
            return b""
        return self.recover()

    def read(self, count: int) -> bytes:
        with self.read_lock:
            return self.serial.read(count)

    def in_waiting(self) -> int:
        with self.read_lock:
            return int(self.serial.in_waiting)

    def reset_input(self) -> None:
        with self.read_lock:
            self.serial.reset_input_buffer()

    def recover(self) -> bytes:
        with self.write_lock:
            self._recover_locked()
        self.active_owner = None
        self.active_continuous = False
        with self.read_lock:
            return self._drain_locked()

    def _drain_locked(self) -> bytes:
        deadline = time.monotonic() + 0.75
        idle_since = time.monotonic()
        drained = bytearray()
        while time.monotonic() < deadline and time.monotonic() - idle_since < 0.08:
            waiting = int(self.serial.in_waiting)
            if waiting:
                drained += self.serial.read(waiting)
                idle_since = time.monotonic()
            else:
                time.sleep(0.001)
        return bytes(drained)

    def reopen(self) -> None:
        """Close, reopen, and resynchronize the broker-owned physical port."""
        with self.write_lock, self.read_lock:
            with suppress(serial.SerialException, OSError):
                self.serial.close()
            deadline = time.monotonic() + max(5.0, self.timeout)
            last_error: BaseException | None = None
            while time.monotonic() < deadline:
                try:
                    self.serial = self._open_serial()
                    self._recover_locked()
                    self._drain_locked()
                    self.active_owner = None
                    self.active_continuous = False
                    return
                except (serial.SerialException, OSError) as error:
                    last_error = error
                    time.sleep(0.1)
            raise serial.SerialException(f"could not reopen {self.port}: {last_error}")

    def _recover_locked(self) -> None:
        # The first LF terminates a possibly partial command. The first STOP may
        # consequently be consumed by that command, so STOP is intentionally sent twice.
        payload = b"\nSTOP\nSTOP\n"
        old_timeout = self.serial.write_timeout
        self.serial.write_timeout = max(self.timeout, 2.0)
        try:
            self.serial.write(payload)
            self.serial.flush()
        except (serial.SerialException, OSError):
            pass
        finally:
            self.serial.write_timeout = old_timeout

    def abandon(self, client_id: str) -> None:
        needs_recovery = self.active_owner == client_id
        with self.transaction:
            if self.owner == client_id:
                self.owner = None
                self.owner_depth = 0
                self.transaction.notify_all()
                needs_recovery = True
        if needs_recovery:
            self.recover()


def _reply(connection: Any, *, result: Any = None, error: BaseException | None = None) -> None:
    if error is None:
        connection.send({"ok": True, "result": result})
    else:
        connection.send(
            {
                "ok": False,
                "error_type": type(error).__name__,
                "error": str(error),
            }
        )


def _serve_client(state: BrokerState, connection: Any) -> None:
    client_id = uuid.uuid4().hex
    with state.transaction:
        state.clients += 1
        state.connections.add(connection)
    try:
        hello = connection.recv()
        if hello.get("op") != "hello" or hello.get("port", "").upper() != state.port.upper():
            raise RuntimeError("GateKeeper broker address collision")
        _reply(connection, result={"client_id": client_id, "pid": os.getpid()})
        while True:
            request = connection.recv()
            op = request.get("op")
            if op == "close":
                _reply(connection)
                break
            try:
                if op == "acquire":
                    result = state.acquire(client_id)
                elif op == "release":
                    result = state.release(client_id)
                elif op == "write":
                    result = state.write(client_id, request["data"])
                elif op == "interrupt":
                    result = state.interrupt()
                elif op == "recover":
                    result = state.recover()
                elif op == "reopen":
                    result = state.reopen()
                elif op == "read":
                    result = state.read(int(request["count"]))
                elif op == "in_waiting":
                    result = state.in_waiting()
                elif op == "reset_input":
                    result = state.reset_input()
                elif op == "set_timeout":
                    state.timeout = float(request["timeout"])
                    result = None
                else:
                    raise RuntimeError(f"unknown broker operation: {op!r}")
                _reply(connection, result=result)
            except BaseException as error:
                _reply(connection, error=error)
    except (EOFError, OSError, RuntimeError):
        pass
    finally:
        state.abandon(client_id)
        with suppress(OSError):
            connection.close()
        with state.transaction:
            state.clients -= 1
            state.connections.discard(connection)
            if state.clients == 0:
                state.last_client_left = time.monotonic()


class BrokerHost:
    """Broker hosted by the first Python process that claims a serial port."""

    def __init__(self, port: str, baud_rate: int, timeout: float) -> None:
        self.listener = Listener(broker_address(port), family="AF_INET", authkey=_AUTHKEY)
        try:
            self.state = BrokerState(port, baud_rate, timeout)
        except BaseException:
            self.listener.close()
            raise
        self._closed = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    @property
    def pid(self) -> int:
        return os.getpid()

    def _serve(self) -> None:
        while not self._closed:
            try:
                connection = self.listener.accept()
            except (OSError, EOFError):
                break
            threading.Thread(
                target=_serve_client, args=(self.state, connection), daemon=True
            ).start()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.state.active_owner is not None:
            self.state.recover()
        else:
            self.state.reset_input()
        self.state.serial.close()
        with self.state.transaction:
            connections = tuple(self.state.connections)
        for connection in connections:
            with suppress(OSError):
                connection.close()
        self.listener.close()


def start_host(port: str, baud_rate: int, timeout: float) -> BrokerHost:
    return BrokerHost(port, baud_rate, timeout)


def connect(port: str):
    return Client(broker_address(port), family="AF_INET", authkey=_AUTHKEY)

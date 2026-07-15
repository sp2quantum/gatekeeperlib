"""Client-side PySerial-shaped proxy for the local GateKeeper broker."""

from __future__ import annotations

import atexit
import os
import threading
import time
from contextlib import suppress
from typing import Any

import serial

from ._broker import BrokerHost, connect, start_host

_HOSTS: dict[str, BrokerHost] = {}


def _close_hosts() -> None:
    for host in tuple(_HOSTS.values()):
        host.close()
    _HOSTS.clear()


atexit.register(_close_hosts)


class SharedSerial:
    def __init__(self, port: str, baud_rate: int, timeout: float) -> None:
        self.port = port
        self.baud_rate = baud_rate
        self.timeout = 0.05
        self._write_timeout = timeout
        self._rpc_lock = threading.Lock()
        self._closed = False
        self._connection = self._connect_or_start()
        hello = self._hello()
        self.host_pid = int(hello["pid"])
        self.is_host = self.host_pid == os.getpid()

    def _hello(self) -> dict[str, Any]:
        self._connection.send({"op": "hello", "port": self.port})
        response = self._connection.recv()
        if not response["ok"]:
            raise serial.SerialException(response.get("error", "GateKeeper broker rejected client"))
        return dict(response["result"])

    def _connect_or_start(self):
        try:
            return connect(self.port)
        except (ConnectionRefusedError, FileNotFoundError, OSError):
            with suppress(OSError, serial.SerialException):
                _HOSTS[self.port.upper()] = start_host(
                    self.port, self.baud_rate, self._write_timeout
                )
            deadline = time.monotonic() + max(5.0, self._write_timeout)
            last_error: BaseException | None = None
            while time.monotonic() < deadline:
                try:
                    return connect(self.port)
                except (ConnectionRefusedError, FileNotFoundError, OSError) as error:
                    last_error = error
                    time.sleep(0.05)
            raise serial.SerialException(
                f"could not start the shared GateKeeper connection for {self.port}: {last_error}"
            ) from last_error

    def _rpc(self, op: str, **parts: Any) -> Any:
        if self._closed:
            raise serial.PortNotOpenError()
        with self._rpc_lock:
            try:
                self._connection.send({"op": op, **parts})
                response = self._connection.recv()
            except (EOFError, OSError) as error:
                # Restore the shared connection for the next command, but do not
                # replay this one: it may already have reached the instrument.
                self._connection.close()
                self._connection = self._connect_or_start()
                hello = self._hello()
                self.host_pid = int(hello["pid"])
                self.is_host = self.host_pid == os.getpid()
                raise serial.SerialException(
                    "GateKeeper broker connection was restored after disconnect; "
                    "the interrupted command was not replayed"
                ) from error
        if response["ok"]:
            return response.get("result")
        message = response.get("error", "shared serial operation failed")
        if response.get("error_type") == "SerialTimeoutException":
            raise serial.SerialTimeoutException(message)
        raise serial.SerialException(message)

    def acquire(self) -> None:
        self._rpc("acquire")

    def release(self) -> None:
        self._rpc("release")

    def write(self, data: bytes) -> int:
        return int(self._rpc("write", data=bytes(data)))

    def interrupt(self) -> None:
        self._rpc("interrupt")

    def recover(self) -> bytes:
        return bytes(self._rpc("recover"))

    def reopen(self) -> None:
        self._rpc("reopen")

    def read(self, count: int) -> bytes:
        return bytes(self._rpc("read", count=int(count)))

    @property
    def in_waiting(self) -> int:
        return int(self._rpc("in_waiting"))

    def reset_input_buffer(self) -> None:
        self._rpc("reset_input")

    def flush(self) -> None:
        # Broker writes are already flushed before their RPC returns.
        return None

    @property
    def write_timeout(self) -> float:
        return self._write_timeout

    @write_timeout.setter
    def write_timeout(self, value: float) -> None:
        self._write_timeout = float(value)
        if hasattr(self, "_connection") and not self._closed:
            self._rpc("set_timeout", timeout=self._write_timeout)

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._rpc("close")
        except (EOFError, OSError, serial.SerialException):
            pass
        finally:
            self._closed = True
            self._connection.close()


def open_shared_serial(port: str, baud_rate: int, timeout: float) -> SharedSerial:
    return SharedSerial(port, baud_rate, timeout)

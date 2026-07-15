"""Run the firmware repository's hardware suite through gatekeeperlib."""

from __future__ import annotations

import importlib.util
import math
import os
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any

import numpy as np

from .device import GateKeeper


def _argument(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, np.integer)):
        return str(int(value))
    if isinstance(value, (float, np.floating)):
        if not math.isfinite(float(value)):
            raise ValueError(f"Non-finite argument: {value}")
        return f"{float(value):.9g}"
    return str(value)


def _command(name: str, *arguments: Any) -> str:
    if not arguments:
        return name
    return name + "," + ",".join(_argument(value) for value in arguments)


class LibraryHarness:
    """The interface expected by the firmware suite, backed by GateKeeper."""

    def __init__(self, port: str, baud: int = 115_200) -> None:
        self.device = GateKeeper(port, baud_rate=baud)
        self.port = self.device.port
        self.ser = self
        time.sleep(0.25)
        self.reset_input_buffer()

    def reset_input_buffer(self) -> None:
        self.device.clear_input()

    def close(self) -> None:
        self.device.close()

    def _read_some(self, byte_count: int) -> bytes:
        return self.device.read_bytes(byte_count)

    def drain(self, idle: float = 0.08, timeout: float = 1.0) -> bytes:
        deadline = time.monotonic() + timeout
        last_data = time.monotonic()
        data = bytearray()
        while time.monotonic() < deadline:
            chunk = self._read_some(max(1, self.device.bytes_waiting()))
            if chunk:
                data += chunk
                last_data = time.monotonic()
            elif data and time.monotonic() - last_data >= idle:
                break
        return bytes(data)

    def write_command(self, name: str, *arguments: Any) -> str:
        command = _command(name, *arguments)
        self.device.write(command)
        return command

    def read_line(self, timeout: float = 2.0) -> str | None:
        deadline = time.monotonic() + timeout
        data = bytearray()
        while time.monotonic() < deadline:
            byte = self._read_some(1)
            if byte:
                data += byte
                if byte == b"\n":
                    break
        if not data:
            return None
        return data.decode("utf-8", errors="replace").strip()

    def read_idle_text(self, timeout: float = 2.0, idle: float = 0.12) -> str:
        return self.drain(idle=idle, timeout=timeout).decode("utf-8", errors="replace").strip()

    def query_line(self, name: str, *arguments: Any, timeout: float = 2.0) -> str:
        self.reset_input_buffer()
        self.write_command(name, *arguments)
        line = self.read_line(timeout=timeout)
        if line is None:
            raise TimeoutError(f"No response to {_command(name, *arguments)}")
        return line

    def query_multiline(self, name: str, *arguments: Any, timeout: float = 2.0) -> str:
        self.reset_input_buffer()
        self.write_command(name, *arguments)
        return self.read_idle_text(timeout=timeout)

    def command_no_reply(self, name: str, *arguments: Any, wait: float = 0.15) -> str:
        self.reset_input_buffer()
        self.write_command(name, *arguments)
        time.sleep(wait)
        return self.read_idle_text(timeout=0.25)

    def read_exact(self, byte_count: int, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        data = bytearray()
        while len(data) < byte_count and time.monotonic() < deadline:
            chunk = self._read_some(byte_count - len(data))
            if chunk:
                data += chunk
        if len(data) != byte_count:
            raise TimeoutError(f"Read {len(data)} of {byte_count} expected bytes")
        return bytes(data)

    def stop_worker(self) -> str:
        self.device.write("stop")
        return self.read_idle_text(timeout=2.0)


def find_firmware_test() -> Path:
    override = os.environ.get("GATEKEEPER_FIRMWARE_TEST")
    if override:
        path = Path(override).expanduser().resolve()
        if path.is_file():
            return path
        raise FileNotFoundError(f"GATEKEEPER_FIRMWARE_TEST does not exist: {path}")

    dac_folder = Path.home() / "Desktop" / "dac"
    candidates = (
        dac_folder / "gatekeeper-firmware" / "hardware_functionality_test.py",
        dac_folder / "dac-adc-firmware" / "hardware_functionality_test.py",
        dac_folder / "gatekeeper-firmware-afylab" / "hardware_functionality_test.py",
    )
    for path in candidates:
        if path.is_file():
            return path
    searched = "\n".join(f"  {path}" for path in candidates)
    raise FileNotFoundError("Could not find the firmware hardware suite. Searched:\n" + searched)


def load_firmware_suite(path: Path | None = None) -> ModuleType:
    path = path or find_firmware_test()
    spec = importlib.util.spec_from_file_location("gatekeeper_firmware_hardware_test", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load firmware hardware suite: {path}")
    suite = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = suite
    spec.loader.exec_module(suite)
    patched_suite: Any = suite
    patched_suite.GateKeeperSerial = LibraryHarness
    patched_suite.detect_port = detect_port
    return suite


def detect_port() -> str:
    ports = GateKeeper.find_ports()
    if len(ports) == 1:
        return ports[0]
    if not ports:
        raise RuntimeError("No GateKeeper USB CDC device found")
    raise RuntimeError(f"Multiple GateKeeper USB CDC devices found: {list(ports)}")


def ensure_output_dir_argument(arguments: list[str] | None = None) -> None:
    """Put generated hardware artifacts under the ignored testing directory."""
    arguments = sys.argv if arguments is None else arguments
    if any(arg == "--output-dir" or arg.startswith("--output-dir=") for arg in arguments):
        return
    stamp = time.strftime("%Y%m%d_%H%M%S")
    output_dir = Path.cwd() / "live_tests" / "test_outputs" / f"hardware_full_{stamp}"
    arguments.extend(["--output-dir", str(output_dir)])


def main() -> int:
    ensure_output_dir_argument()
    return int(load_firmware_suite().main())


if __name__ == "__main__":
    sys.exit(main())

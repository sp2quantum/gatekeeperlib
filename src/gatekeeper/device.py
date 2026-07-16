"""Synchronous control of one GateKeeper instrument."""

from __future__ import annotations

import math
import threading
import time
import warnings
from collections.abc import Callable, Sequence
from contextlib import contextmanager

import numpy as np
import serial
import serial.tools.list_ports

from ._shared_serial import open_shared_serial
from .models import AdcCapture, AdcReading, Axis, Number, RampLine, RampPoint

_VOLTAGE_MIN = -10.0
_VOLTAGE_MAX = 10.0
_CONVERSION_TIME_MIN = 82e-6
_CONVERSION_TIME_MAX = 2686e-6
_DAC_CODE_MAX = 1_048_575
_TIMEOUT_MARGIN = 5.0


class GateKeeperError(RuntimeError):
    """The instrument rejected a command or stopped responding."""


def _port_name(port: str | int) -> str:
    return f"COM{port}" if isinstance(port, int) else str(port).strip()


def _format(value: object) -> str:
    if isinstance(value, float):
        return f"{value:.9g}"
    return str(value)


def _channels(value: int | Sequence[int], *, name: str) -> list[int]:
    channels = [int(value)] if isinstance(value, (int, np.integer)) else [int(ch) for ch in value]
    if not channels:
        raise ValueError(f"{name} must contain at least one channel")
    if len(set(channels)) != len(channels):
        raise ValueError(f"{name} must not contain duplicate channels")
    invalid = [ch for ch in channels if ch not in range(8)]
    if invalid:
        raise ValueError(f"{name} must contain channels 0 through 7; got {invalid}")
    return channels


def _values(value: Number | Sequence[Number], count: int, *, name: str) -> list[float]:
    if isinstance(value, (int, float, np.integer, np.floating)):
        return [float(value)] * count
    values = [float(item) for item in value]
    if len(values) != count:
        raise ValueError(f"{name} must be a scalar or contain {count} values")
    if not all(math.isfinite(item) for item in values):
        raise ValueError(f"{name} must contain finite numbers")
    return values


def _positive_int(value: int, *, name: str) -> int:
    result = int(value)
    if result < 1:
        raise ValueError(f"{name} must be at least 1")
    return result


def _positive_time(value: float, *, name: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be a positive number of seconds")
    return result


def _microseconds(value: float, *, name: str) -> int:
    microseconds = int(_positive_time(value, name=name) * 1e6)
    if microseconds < 1:
        raise ValueError(f"{name} must be at least 1 microsecond")
    return microseconds


def _axis(axis: Axis, name: str) -> tuple[list[int], list[float], list[float], int]:
    try:
        channels = _channels(axis["ch"], name=f"{name}['ch']")
        start = _values(axis["start"], len(channels), name=f"{name}['start']")
        stop = _values(axis["stop"], len(channels), name=f"{name}['stop']")
        points = _positive_int(axis["points"], name=f"{name}['points']")
    except (KeyError, TypeError) as error:
        raise ValueError(
            f"{name} must look like dict(ch=0, start=-1.0, stop=1.0, points=160)"
        ) from error
    return channels, start, stop, points


def _plane(
    fast: Axis, slow: Axis
) -> tuple[list[int], list[float], list[float], list[float], int, int]:
    fast_channels, fast_start, fast_stop, fast_points = _axis(fast, "fast")
    slow_channels, slow_start, slow_stop, slow_points = _axis(slow, "slow")
    channels = fast_channels.copy()
    for channel in slow_channels:
        if channel not in channels:
            channels.append(channel)

    origins: dict[int, float] = {}
    for channel, start in zip(fast_channels, fast_start, strict=True):
        origins[channel] = start
    for channel, start in zip(slow_channels, slow_start, strict=True):
        if channel in origins and not math.isclose(origins[channel], start, abs_tol=1e-12):
            raise ValueError(f"fast and slow specify different starting voltages for DAC {channel}")
        origins[channel] = start

    fast_span = {
        channel: stop - start
        for channel, start, stop in zip(fast_channels, fast_start, fast_stop, strict=True)
    }
    slow_span = {
        channel: stop - start
        for channel, start, stop in zip(slow_channels, slow_start, slow_stop, strict=True)
    }
    start_point = [origins[channel] for channel in channels]
    fast_vector = [fast_span.get(channel, 0.0) for channel in channels]
    slow_vector = [slow_span.get(channel, 0.0) for channel in channels]
    return channels, start_point, fast_vector, slow_vector, fast_points, slow_points


class GateKeeper:
    """A synchronous connection to one GateKeeper.

    All public times are seconds and all voltages are volts. Serial protocol
    units and binary framing are handled internally.
    """

    N_DACS = 8
    N_ADCS = 8

    def __init__(
        self,
        port: str | int | None = None,
        *,
        baud_rate: int = 115_200,
        timeout: float = 5.0,
    ) -> None:
        if port is None:
            available_ports = self.find_ports()
            if not available_ports:
                raise GateKeeperError("no GateKeeper ports were found")
            if len(available_ports) > 1:
                ports = ", ".join(available_ports)
                raise GateKeeperError(
                    f"multiple GateKeeper ports were found ({ports}); specify a port"
                )
            port = available_ports[0]
            print(f"Auto-selected GateKeeper on {port}.")
        self.port = _port_name(port)
        self._default_timeout = _positive_time(timeout, name="timeout")
        self._thread_lock = threading.RLock()
        self._continuous_active = False
        self._serial = open_shared_serial(self.port, int(baud_rate), self._default_timeout)
        self._pending_bytes = bytearray()
        self.clear_input()

    @staticmethod
    def find_ports() -> tuple[str, ...]:
        """Return USB ports whose vendor and product IDs match GateKeeper."""
        return tuple(
            port.device
            for port in serial.tools.list_ports.comports()
            if port.vid == 0x2341 and port.pid == 0x0266
        )

    def close(self) -> None:
        """Release the serial connection."""
        with self._thread_lock:
            if self._continuous_active:
                self.stop(settling_time=0.1)
            self._serial.close()

    def __enter__(self) -> GateKeeper:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def serial_host_pid(self) -> int | None:
        """PID of the Python process that currently owns the physical port."""
        return getattr(self._serial, "host_pid", None)

    @property
    def is_serial_host(self) -> bool:
        """Whether this Python process owns the physical serial port."""
        return bool(getattr(self._serial, "is_host", False))

    @contextmanager
    def _transaction(self):
        """Own the complete command/response exchange locally and across processes."""
        with self._thread_lock:
            acquire = getattr(self._serial, "acquire", None)
            release = getattr(self._serial, "release", None)
            if acquire:
                acquire()
            try:
                yield
            finally:
                if release:
                    release()

    def _write_line(self, command: str) -> None:
        line = command.rstrip("\r\n") + "\n"
        encoded = line.encode("ascii")
        written = self._serial.write(encoded)
        if written != len(encoded):
            raise GateKeeperError(f"wrote {written} of {len(encoded)} command bytes")
        self._serial.flush()

    def write(self, command: str) -> None:
        """Send one raw command without waiting for a response."""
        with self._transaction():
            try:
                self._write_line(command)
            except (serial.SerialException, OSError):
                self._recover_serial(reopen=True)
                raise

    def read(self) -> str:
        """Read one raw text response."""
        with self._transaction():
            try:
                return self._check_response(self._read_line(self._default_timeout))
            except GateKeeperError as error:
                if "timed out" in str(error).lower():
                    self._recover_serial(reopen=True)
                raise

    def query(self, command: str) -> str:
        """Send one raw command and read one text response."""
        with self._transaction():
            try:
                self._write_line(command)
                return self._check_response(self._read_line(self._default_timeout))
            except (serial.SerialException, OSError):
                self._recover_serial(reopen=True)
                raise
            except GateKeeperError as error:
                if "timed out" in str(error).lower():
                    self._recover_serial(reopen=True)
                raise

    def _read_line(self, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        data = bytearray()
        while time.monotonic() < deadline:
            byte = self._read_available(1)
            if byte:
                data += byte
                if byte == b"\n":
                    return bytes(data).decode(errors="replace").strip()
        raise GateKeeperError("timed out waiting for a text response")

    def _check_response(self, response: str) -> str:
        if response.upper().startswith(("FAILURE", "ERROR")):
            raise GateKeeperError(response)
        return response

    def _command(self, name: str, *parts: object) -> str:
        return ",".join([name, *(_format(part) for part in parts)])

    def set_timeout(self, timeout: float) -> None:
        """Change the default text-command timeout."""
        self._default_timeout = _positive_time(timeout, name="timeout")
        self._serial.write_timeout = self._default_timeout

    def _query_with_timeout(self, command: str, expected_duration: float) -> str:
        with self._transaction():
            try:
                self._write_line(command)
                response = self._read_line(expected_duration + _TIMEOUT_MARGIN)
                return self._check_response(response)
            except (serial.SerialException, OSError):
                self._recover_serial(reopen=True)
                raise
            except GateKeeperError as error:
                if "timed out" in str(error).lower():
                    self._recover_serial(reopen=True)
                raise

    def _bytes_waiting(self) -> int:
        return len(self._pending_bytes) + int(self._serial.in_waiting)

    def _read_available(self, count: int) -> bytes:
        data = bytearray()
        if self._pending_bytes:
            take = min(count, len(self._pending_bytes))
            data += self._pending_bytes[:take]
            del self._pending_bytes[:take]
        remaining = count - len(data)
        if remaining:
            data += self._serial.read(remaining)
        return bytes(data)

    def bytes_waiting(self) -> int:
        """Return the number of unread bytes in the serial input buffer."""
        with self._transaction():
            return self._bytes_waiting()

    def read_bytes(self, byte_count: int) -> bytes:
        """Read up to ``byte_count`` raw bytes from the instrument."""
        byte_count = int(byte_count)
        if byte_count < 0:
            raise ValueError("byte_count must not be negative")
        with self._transaction():
            return self._read_available(byte_count)

    def clear_input(self, quiet_period: float = 0.05, max_wait: float = 0.5) -> None:
        """Discard stale bytes left by an interrupted binary operation."""
        with self._transaction():
            deadline = time.monotonic() + max_wait
            idle_since = time.monotonic()
            self._pending_bytes.clear()
            while time.monotonic() - idle_since < quiet_period and time.monotonic() < deadline:
                waiting = self._bytes_waiting()
                if waiting:
                    self._serial.read(waiting)
                    idle_since = time.monotonic()
                else:
                    time.sleep(0.001)

    def _recover_serial(self, *, reopen: bool = False) -> None:
        self._pending_bytes.clear()
        if reopen:
            reopen_port = getattr(self._serial, "reopen", None)
            if reopen_port:
                reopen_port()
                return
        recover = getattr(self._serial, "recover", None)
        if recover:
            recover()
        else:
            try:
                self._serial.write(b"\nSTOP\nSTOP\n")
                self._serial.flush()
            except (serial.SerialException, OSError):
                pass

    @contextmanager
    def _operation(self, command: str):
        """Run one blocking firmware operation with exception-safe recovery."""
        with self._transaction():
            try:
                self._write_line(command)
                yield
            except BaseException as error:
                timed_out = isinstance(
                    error, (GateKeeperError, serial.SerialTimeoutException)
                ) and (
                    "timed out" in str(error).lower()
                    or isinstance(error, serial.SerialTimeoutException)
                )
                self._recover_serial(reopen=timed_out)
                raise

    def _read_exact(self, byte_count: int, expected_duration: float) -> bytes:
        deadline = time.monotonic() + expected_duration + _TIMEOUT_MARGIN
        data = bytearray()
        failure_prefix = b"FAILURE"
        while True:
            if len(data) >= byte_count:
                upper = bytes(data).upper()
                if not failure_prefix.startswith(upper):
                    if len(data) > byte_count:
                        self._pending_bytes[:0] = data[byte_count:]
                    return bytes(data[:byte_count])
            waiting = self._bytes_waiting()
            if waiting:
                needed = max(1, byte_count - len(data))
                if failure_prefix.startswith(bytes(data).upper()):
                    needed = max(needed, len(failure_prefix) - len(data))
                data += self._read_available(min(waiting, needed))
                if bytes(data).upper().startswith(failure_prefix):
                    raise GateKeeperError(self._finish_failure(data, deadline))
                continue
            if time.monotonic() >= deadline:
                raise GateKeeperError(
                    f"timed out after receiving {len(data)} of {byte_count} bytes"
                )
            time.sleep(0.001)

    def _finish_failure(self, data: bytearray, deadline: float) -> str:
        while not data.endswith(b"\n") and time.monotonic() < deadline:
            waiting = self._bytes_waiting()
            if waiting:
                data += self._read_available(waiting)
            else:
                time.sleep(0.001)
        return bytes(data).decode(errors="replace").strip()

    def _read_trailer(self, wait: float = 0.1) -> str:
        deadline = time.monotonic() + wait
        data = bytearray()
        while time.monotonic() < deadline:
            waiting = self._bytes_waiting()
            if waiting:
                data += self._read_available(waiting)
                if data.endswith(b"\n"):
                    break
                deadline = max(deadline, time.monotonic() + 0.02)
            else:
                time.sleep(0.001)
        trailer = bytes(data).decode(errors="replace").strip()
        self._check_response(trailer)
        return trailer

    def _read_samples(
        self, reads: Sequence[int], points: int, expected_duration: float
    ) -> np.ndarray:
        raw = self._read_exact(len(reads) * points * 4, expected_duration)
        self._read_trailer()
        samples = np.frombuffer(raw, dtype="<f4").astype(np.float64)
        return samples.reshape(points, len(reads)).T.copy()

    # Identity and connection health

    def idn(self) -> str:
        return self.query("*IDN?")

    def ready(self) -> str:
        return self.query("*RDY?")

    def nop(self) -> str:
        return self.query("NOP")

    def serial_number(self) -> str:
        return self.query("SERIAL_NUMBER")

    # DAC control

    def set_voltage(self, channel: int, voltage: float) -> float:
        channel = _channels(channel, name="channel")[0]
        voltage = float(voltage)
        if not _VOLTAGE_MIN <= voltage <= _VOLTAGE_MAX:
            raise ValueError("voltage must be between -10 V and +10 V")
        response = self.query(self._command("SET", channel, voltage))
        value = response.upper().partition(" TO ")[2].strip().split()
        if not value:
            raise GateKeeperError(f"unexpected SET response: {response!r}")
        try:
            return float(value[0])
        except ValueError as error:
            raise GateKeeperError(f"unexpected SET response: {response!r}") from error

    def get_dac(self, channel: int) -> float:
        channel = _channels(channel, name="channel")[0]
        return float(self.query(self._command("GET_DAC", channel)))

    def set_dac_code(self, channel: int, code: int) -> str:
        channel = _channels(channel, name="channel")[0]
        code = int(code)
        if code not in range(_DAC_CODE_MAX + 1):
            raise ValueError(f"code must be between 0 and {_DAC_CODE_MAX}")
        return self.query(self._command("SET_DAC_CODE", channel, code))

    def set_full_scale(self, channel: int, voltage: float) -> str:
        channel = _channels(channel, name="channel")[0]
        return self.query(self._command("FULL_SCALE", channel, float(voltage)))

    def get_full_scale(self, channel: int) -> float:
        channel = _channels(channel, name="channel")[0]
        return float(self.query(self._command("GET_FULL_SCALE", channel)))

    def initialize(self) -> str:
        return self.query("INITIALIZE")

    def set_upper_limit(self, channel: int, voltage: float) -> str:
        channel = _channels(channel, name="channel")[0]
        return self.query(self._command("SET_UPPER_LIMIT", channel, float(voltage)))

    def set_lower_limit(self, channel: int, voltage: float) -> str:
        channel = _channels(channel, name="channel")[0]
        return self.query(self._command("SET_LOWER_LIMIT", channel, float(voltage)))

    def get_upper_limit(self, channel: int) -> float:
        channel = _channels(channel, name="channel")[0]
        return float(self.query(self._command("GET_UPPER_LIMIT", channel)))

    def get_lower_limit(self, channel: int) -> float:
        channel = _channels(channel, name="channel")[0]
        return float(self.query(self._command("GET_LOWER_LIMIT", channel)))

    def set_offset_and_gain(self, channel: int, offset: float, gain: float) -> str:
        channel = _channels(channel, name="channel")[0]
        return self.query(self._command("SET_OSG", channel, float(offset), float(gain)))

    def get_offset_and_gain(self) -> tuple[np.ndarray, np.ndarray]:
        """Return the eight DAC offsets and eight gains from current firmware."""
        with self._operation("INQUIRY_OSG"):
            raw = self._read_exact(16 * 4, self._default_timeout)
            self._read_trailer()
        values = np.frombuffer(raw, dtype="<f4").astype(np.float64)
        return values[:8].copy(), values[8:].copy()

    def calibrate_dacs(self, timeout: float = 120.0) -> str:
        return self._query_with_timeout("DAC_CH_CAL", timeout)

    def ramp(
        self,
        channels: int | Sequence[int],
        start: Number | Sequence[Number],
        stop: Number | Sequence[Number],
        points: int,
        dac_interval: float,
    ) -> str:
        dac_channels = _channels(channels, name="channels")
        starts = _values(start, len(dac_channels), name="start")
        stops = _values(stop, len(dac_channels), name="stop")
        points = _positive_int(points, name="points")
        dac_interval_us = _microseconds(dac_interval, name="dac_interval")
        if len(dac_channels) == 1:
            command = self._command(
                "RAMP1", dac_channels[0], starts[0], stops[0], points, dac_interval_us
            )
        elif len(dac_channels) == 2:
            command = self._command(
                "RAMP2",
                dac_channels[0],
                dac_channels[1],
                starts[0],
                starts[1],
                stops[0],
                stops[1],
                points,
                dac_interval_us,
            )
        else:
            command = self._command(
                "RAMP_N",
                len(dac_channels),
                points,
                dac_interval_us,
                *dac_channels,
                *starts,
                *stops,
            )
        return self._query_with_timeout(command, points * dac_interval_us * 1e-6)

    # ADC control

    def read_voltage(self, channel: int) -> float:
        channel = _channels(channel, name="channel")[0]
        return float(self.query(self._command("GET_ADC", channel)))

    def read_voltages(self, channels: int | Sequence[int]) -> np.ndarray:
        return np.array(
            [self.read_voltage(channel) for channel in _channels(channels, name="channels")],
            dtype=np.float64,
        )

    def set_chopping(self, enabled: bool) -> None:
        """Set the ADC chopping mode used by conversion-time and filter-word commands."""
        self.write(self._command("SET_CHOP", int(bool(enabled))))

    def get_chopping(self) -> bool:
        """Return the ADC chopping mode used by conversion-time and filter-word commands."""
        response = self.query("GET_CHOP").strip().lower()
        if response not in {"true", "false"}:
            raise GateKeeperError(f"unexpected GET_CHOP response: {response!r}")
        return response == "true"

    def set_conversion_time(self, channel: int, conversion_time: float) -> float:
        channel = _channels(channel, name="channel")[0]
        conversion_time = _positive_time(conversion_time, name="conversion_time")
        if not _CONVERSION_TIME_MIN <= conversion_time <= _CONVERSION_TIME_MAX:
            raise ValueError("conversion_time must be between 82e-6 and 2686e-6 seconds")
        result_us = float(self.query(self._command("CONVERT_TIME", channel, conversion_time * 1e6)))
        return result_us * 1e-6

    def set_conversion_filter(self, channel: int, filter_word: int) -> float:
        channel = _channels(channel, name="channel")[0]
        filter_word = int(filter_word)
        if filter_word not in range(2, 128):
            raise ValueError("filter_word must be between 2 and 127")
        result_us = float(self.query(self._command("CONVERT_TIME_FW", channel, filter_word)))
        return result_us * 1e-6

    def get_conversion_time(self, channel: int) -> float:
        channel = _channels(channel, name="channel")[0]
        return float(self.query(self._command("GET_CONVERT_TIME", channel))) * 1e-6

    def idle_adc(self, channel: int) -> str:
        channel = _channels(channel, name="channel")[0]
        return self.query(self._command("IDLE_MODE", channel))

    def active_adc_channels(self) -> list[int]:
        response = self.query("GET_CHANNELS_ACTIVE")
        return [int(value) for value in response.split(",") if value.strip().lstrip("-").isdigit()]

    def reset_adcs(self) -> None:
        # RESET intentionally returns an empty OperationResult in the firmware.
        self.write("RESET")

    def hard_reset_adcs(self) -> str:
        return self.query("HARD_RESET")

    def calibrate_adc_zero(self, channel: int | None = None) -> str:
        if channel is None:
            return self.query("CALIBRATE_ALL_ADC_CHANNELS_ZERO_SCALE")
        channel = _channels(channel, name="channel")[0]
        return self.query(self._command("CALIBRATE_ADC_CHANNEL_ZERO_SCALE", channel))

    def calibrate_adc_full_scale(self, channel: int | None = None) -> str:
        if channel is None:
            return self.query("CALIBRATE_ALL_ADC_CHANNELS_FULL_SCALE")
        channel = _channels(channel, name="channel")[0]
        return self.query(self._command("CALIBRATE_ADC_CHANNEL_FULL_SCALE", channel))

    # Buffered acquisition

    def _auto_settling_time(
        self, reads: Sequence[int], dac_interval: float, averages: int
    ) -> tuple[float, float]:
        time_per_board: dict[int, float] = {}
        for channel in reads:
            board = channel // 4
            time_per_board[board] = time_per_board.get(board, 0.0) + self.get_conversion_time(
                channel
            )
        conversion_budget = max(time_per_board.values(), default=0.0) * averages
        settling_time = round(dac_interval * 0.8, 6)
        overhead = 180e-6
        if conversion_budget + settling_time + overhead >= dac_interval:
            settling_time = max(dac_interval - conversion_budget - overhead - 1e-6, 100e-6)
        if conversion_budget + settling_time + overhead >= dac_interval:
            dac_interval = conversion_budget + settling_time + overhead + 1e-6
            warnings.warn(
                "dac_interval is too short for the selected ADCs; "
                f"using {dac_interval:.6g} seconds",
                stacklevel=3,
            )
        return settling_time, dac_interval

    def dac_led_buffer_ramp(
        self,
        channels: int | Sequence[int],
        reads: int | Sequence[int],
        start: Number | Sequence[Number],
        stop: Number | Sequence[Number],
        points: int,
        dac_interval: float,
        settling_time: float | None = None,
        averages: int = 1,
    ) -> np.ndarray:
        dac_channels = _channels(channels, name="channels")
        adc_channels = _channels(reads, name="reads")
        starts = _values(start, len(dac_channels), name="start")
        stops = _values(stop, len(dac_channels), name="stop")
        points = _positive_int(points, name="points")
        averages = _positive_int(averages, name="averages")
        dac_interval = _positive_time(dac_interval, name="dac_interval")
        if settling_time is None:
            settling_time, dac_interval = self._auto_settling_time(
                adc_channels, dac_interval, averages
            )
        else:
            settling_time = _positive_time(settling_time, name="settling_time")
        dac_interval_us = _microseconds(dac_interval, name="dac_interval")
        settling_time_us = _microseconds(settling_time, name="settling_time")

        command = self._command(
            "DAC_LED_BUFFER_RAMP",
            len(dac_channels),
            len(adc_channels),
            points,
            averages,
            dac_interval_us,
            settling_time_us,
            *dac_channels,
            *starts,
            *stops,
            *adc_channels,
        )
        with self._operation(command):
            return self._read_samples(adc_channels, points, points * dac_interval_us * 1e-6)

    def time_series_buffer_ramp(
        self,
        channels: int | Sequence[int],
        reads: int | Sequence[int],
        start: Number | Sequence[Number],
        stop: Number | Sequence[Number],
        points: int,
        dac_interval: float,
        adc_interval: float,
    ) -> np.ndarray:
        dac_channels = _channels(channels, name="channels")
        adc_channels = _channels(reads, name="reads")
        starts = _values(start, len(dac_channels), name="start")
        stops = _values(stop, len(dac_channels), name="stop")
        points = _positive_int(points, name="points")
        dac_interval_us = _microseconds(dac_interval, name="dac_interval")
        adc_interval_us = _microseconds(adc_interval, name="adc_interval")
        sample_count = max(1, points * dac_interval_us // adc_interval_us)
        command = self._command(
            "TIME_SERIES_BUFFER_RAMP",
            len(dac_channels),
            len(adc_channels),
            points,
            dac_interval_us,
            adc_interval_us,
            *dac_channels,
            *starts,
            *stops,
            *adc_channels,
        )
        with self._operation(command):
            return self._read_samples(adc_channels, sample_count, points * dac_interval_us * 1e-6)

    def adc_read(
        self,
        reads: int | Sequence[int],
        *,
        duration: float,
        conversion_time: float,
    ) -> AdcCapture:
        adc_channels = _channels(reads, name="reads")
        duration_us = _microseconds(duration, name="duration")
        conversion_time_us = _microseconds(conversion_time, name="conversion_time")
        command = self._command(
            "TIME_SERIES_ADC_READ",
            len(adc_channels),
            *adc_channels,
            conversion_time_us,
            duration_us,
        )
        with self._operation(command):
            period_raw = self._read_exact(4, duration_us * 1e-6)
            period_us = float(np.frombuffer(period_raw, dtype="<f4")[0])
            if not math.isfinite(period_us) or period_us <= 0:
                raise GateKeeperError(f"device returned an invalid sample period: {period_us!r}")
            sample_count = int(duration_us / period_us)
            data = self._read_samples(adc_channels, sample_count, duration_us * 1e-6)
        return AdcCapture(data=data, sample_period=period_us * 1e-6)

    def time_series_adc_read(
        self,
        reads: int | Sequence[int],
        *,
        points: int,
        rate: float,
    ) -> np.ndarray:
        adc_channels = _channels(reads, name="reads")
        points = _positive_int(points, name="points")
        rate = float(rate)
        if not math.isfinite(rate) or rate <= 0:
            raise ValueError("rate must be a positive number of samples per second")
        channels_per_board: dict[int, int] = {}
        for channel in adc_channels:
            board = channel // 4
            channels_per_board[board] = channels_per_board.get(board, 0) + 1
        conversion_time = 1.0 / (rate * max(channels_per_board.values()))
        if not _CONVERSION_TIME_MIN <= conversion_time <= _CONVERSION_TIME_MAX:
            raise ValueError(
                "rate is outside the range supported by the selected ADC channel layout"
            )
        requested_frames = math.ceil(points * 1.02) + 1
        capture = self.adc_read(
            adc_channels,
            duration=requested_frames / rate,
            conversion_time=conversion_time,
        )
        if capture.data.shape[1] < points:
            raise GateKeeperError(
                f"device returned {capture.data.shape[1]} samples; {points} were requested"
            )
        return capture.data[:, :points].copy()

    def _run_2d(
        self,
        command: str,
        dac_channels: Sequence[int],
        adc_channels: Sequence[int],
        start_point: Sequence[float],
        fast_vector: Sequence[float],
        slow_vector: Sequence[float],
        points_per_line: int,
        slow_points: int,
        retrace: bool,
        snake: bool,
        expected_duration: float,
        on_point: Callable[[RampPoint], None] | None,
        on_line: Callable[[RampLine], None] | None,
        hdf5_path: str | None,
        hdf5_metadata: dict[str, object],
    ) -> np.ndarray:
        lines = slow_points * (2 if retrace and not snake else 1)
        result = np.full((len(adc_channels), lines * points_per_line), np.nan, dtype=np.float64)
        writer = None
        if hdf5_path:
            from .storage import RampWriter

            writer = RampWriter(
                hdf5_path,
                dac_channels=dac_channels,
                adc_channels=adc_channels,
                points_per_line=points_per_line,
                slow_points=slow_points,
                start_point=start_point,
                fast_axis_vector=fast_vector,
                slow_axis_vector=slow_vector,
                retrace=retrace,
                snake=snake,
                metadata=hdf5_metadata,
            )

        try:
            with self._operation(command):
                for line_index in range(lines):
                    slow_index = line_index // 2 if retrace and not snake else line_index
                    forward = not (
                        (retrace and not snake and line_index % 2) or (snake and slow_index % 2)
                    )
                    slow_fraction = slow_index / (slow_points - 1) if slow_points > 1 else 0.0
                    slow_position = tuple(
                        start + slow_fraction * span
                        for start, span in zip(start_point, slow_vector, strict=True)
                    )
                    direction = "forward" if forward else "backward"
                    for acquisition_index in range(points_per_line):
                        raw = self._read_exact(len(adc_channels) * 4, expected_duration)
                        values = np.frombuffer(raw, dtype="<f4").astype(np.float64)
                        physical_index = (
                            acquisition_index
                            if forward
                            else points_per_line - acquisition_index - 1
                        )
                        result_index = line_index * points_per_line + acquisition_index
                        result[:, result_index] = values
                        point = RampPoint(
                            line_index=line_index,
                            slow_index=slow_index,
                            point_index=acquisition_index,
                            acquisition_index=acquisition_index,
                            fast_fraction=(
                                physical_index / (points_per_line - 1)
                                if points_per_line > 1
                                else 0.0
                            ),
                            slow_fraction=slow_fraction,
                            direction=direction,
                            dac_channels=tuple(dac_channels),
                            adc_channels=tuple(adc_channels),
                            slow_position=slow_position,
                            values=values.copy(),
                        )
                        if writer:
                            writer.write_point(point)
                        if on_point:
                            on_point(point)

                    line_start = line_index * points_per_line
                    line = result[:, line_start : line_start + points_per_line].copy()
                    info = RampLine(
                        index=line_index,
                        slow_index=slow_index,
                        slow_fraction=slow_fraction,
                        direction=direction,
                        dac_channels=tuple(dac_channels),
                        adc_channels=tuple(adc_channels),
                        slow_position=slow_position,
                        data=line,
                    )
                    if on_line:
                        on_line(info)
                self._read_trailer()
        finally:
            if writer:
                writer.close(delete_if_empty=True)
        return result

    def dac_led_buffer_ramp_2d(
        self,
        fast: Axis,
        slow: Axis,
        reads: int | Sequence[int],
        dac_interval: float,
        settling_time: float | None = None,
        averages: int = 1,
        retrace: bool = False,
        snake: bool = False,
        on_point: Callable[[RampPoint], None] | None = None,
        on_line: Callable[[RampLine], None] | None = None,
        hdf5_path: str | None = None,
    ) -> np.ndarray:
        (
            dac_channels,
            start_point,
            fast_vector,
            slow_vector,
            fast_points,
            slow_points,
        ) = _plane(fast, slow)
        adc_channels = _channels(reads, name="reads")
        dac_interval = _positive_time(dac_interval, name="dac_interval")
        averages = _positive_int(averages, name="averages")
        if settling_time is None:
            settling_time, dac_interval = self._auto_settling_time(
                adc_channels, dac_interval, averages
            )
        else:
            settling_time = _positive_time(settling_time, name="settling_time")
        dac_interval_us = _microseconds(dac_interval, name="dac_interval")
        settling_time_us = _microseconds(settling_time, name="settling_time")
        command = self._command(
            "2D_DAC_LED_BUFFER_RAMP",
            len(dac_channels),
            len(adc_channels),
            fast_points,
            slow_points,
            dac_interval_us,
            settling_time_us,
            float(retrace),
            float(snake),
            averages,
            *dac_channels,
            *start_point,
            *fast_vector,
            *slow_vector,
            *adc_channels,
        )
        line_count = slow_points * (2 if retrace and not snake else 1)
        return self._run_2d(
            command,
            dac_channels,
            adc_channels,
            start_point,
            fast_vector,
            slow_vector,
            fast_points,
            slow_points,
            retrace,
            snake,
            line_count * fast_points * dac_interval_us * 1e-6,
            on_point,
            on_line,
            hdf5_path,
            {
                "start_point": start_point,
                "fast_axis_vector": fast_vector,
                "slow_axis_vector": slow_vector,
                "steps_fast": fast_points,
                "steps_slow": slow_points,
                "retrace": bool(retrace),
                "snake": bool(snake),
                "num_adc_averages": averages,
                "dac_interval_us": float(dac_interval_us),
                "dac_settling_time_us": float(settling_time_us),
                "ramp_type": "dac_led",
            },
        )

    def time_series_buffer_ramp_2d(
        self,
        fast: Axis,
        slow: Axis,
        reads: int | Sequence[int],
        dac_interval: float,
        adc_interval: float,
        retrace: bool = False,
        snake: bool = False,
        on_point: Callable[[RampPoint], None] | None = None,
        on_line: Callable[[RampLine], None] | None = None,
        hdf5_path: str | None = None,
    ) -> np.ndarray:
        (
            dac_channels,
            start_point,
            fast_vector,
            slow_vector,
            fast_points,
            slow_points,
        ) = _plane(fast, slow)
        adc_channels = _channels(reads, name="reads")
        dac_interval_us = _microseconds(dac_interval, name="dac_interval")
        adc_interval_us = _microseconds(adc_interval, name="adc_interval")
        points_per_line = max(1, fast_points * dac_interval_us // adc_interval_us)
        command = self._command(
            "2D_TIME_SERIES_BUFFER_RAMP",
            len(dac_channels),
            len(adc_channels),
            fast_points,
            slow_points,
            dac_interval_us,
            adc_interval_us,
            float(retrace),
            float(snake),
            *dac_channels,
            *start_point,
            *fast_vector,
            *slow_vector,
            *adc_channels,
        )
        line_count = slow_points * (2 if retrace and not snake else 1)
        return self._run_2d(
            command,
            dac_channels,
            adc_channels,
            start_point,
            fast_vector,
            slow_vector,
            points_per_line,
            slow_points,
            retrace,
            snake,
            line_count * fast_points * dac_interval_us * 1e-6,
            on_point,
            on_line,
            hdf5_path,
            {
                "start_point": start_point,
                "fast_axis_vector": fast_vector,
                "slow_axis_vector": slow_vector,
                "steps_fast": fast_points,
                "steps_slow": slow_points,
                "retrace": bool(retrace),
                "snake": bool(snake),
                "dac_interval_us": float(dac_interval_us),
                "adc_interval_us": float(adc_interval_us),
                "ramp_type": "time_series",
            },
        )

    def boxcar_buffer_ramp(
        self,
        channels: int | Sequence[int],
        reads: int | Sequence[int],
        start_low: Number | Sequence[Number],
        stop_low: Number | Sequence[Number],
        start_high: Number | Sequence[Number],
        stop_high: Number | Sequence[Number],
        points: int,
        measures_per_step: int,
        averages: int,
        conversion_time: float,
    ) -> np.ndarray:
        dac_channels = _channels(channels, name="channels")
        adc_channels = _channels(reads, name="reads")
        low_start = _values(start_low, len(dac_channels), name="start_low")
        low_stop = _values(stop_low, len(dac_channels), name="stop_low")
        high_start = _values(start_high, len(dac_channels), name="start_high")
        high_stop = _values(stop_high, len(dac_channels), name="stop_high")
        points = _positive_int(points, name="points")
        measures_per_step = _positive_int(measures_per_step, name="measures_per_step")
        averages = _positive_int(averages, name="averages")
        conversion_time_us = _microseconds(conversion_time, name="conversion_time")
        command = self._command(
            "BOXCAR_BUFFER_RAMP",
            len(dac_channels),
            len(adc_channels),
            points,
            measures_per_step,
            averages,
            conversion_time_us,
            *dac_channels,
            *low_start,
            *low_stop,
            *high_start,
            *high_stop,
            *adc_channels,
        )
        sample_count = 2 * points * measures_per_step * averages
        with self._operation(command):
            return self._read_samples(
                adc_channels,
                sample_count,
                sample_count * conversion_time_us * 1e-6,
            )

    # Arbitrary waveforms

    def _waveform(
        self, channels: int | Sequence[int], waveform: object
    ) -> tuple[list[int], np.ndarray]:
        dac_channels = _channels(channels, name="channels")
        values = np.asarray(waveform, dtype=np.float64)
        if values.ndim == 1:
            values = values[np.newaxis, :]
        if values.ndim != 2 or values.shape[0] != len(dac_channels):
            raise ValueError("waveform must have one row per DAC channel")
        if values.shape[1] < 1:
            raise ValueError("waveform must contain at least one sample")
        if not np.all(np.isfinite(values)):
            raise ValueError("waveform must contain finite voltages")
        if np.max(np.abs(values)) > _VOLTAGE_MAX:
            raise ValueError("waveform voltages must stay between -10 V and +10 V")
        return dac_channels, values

    def awg_write(
        self,
        channels: int | Sequence[int],
        waveform: object,
        rate: float,
    ) -> None:
        dac_channels, values = self._waveform(channels, waveform)
        rate = float(rate)
        if not math.isfinite(rate) or rate <= 0:
            raise ValueError("rate must be a positive number of samples per second")
        interval_us = max(1, round(1e6 / rate))
        command = self._command(
            "AWG_BUFFER_RAMP",
            len(dac_channels),
            values.shape[1],
            interval_us,
            *dac_channels,
            *values.ravel(),
        )
        with self._transaction():
            try:
                self._write_line(command)
                self._read_trailer()
                self._continuous_active = True
            except BaseException:
                self._recover_serial()
                raise

    def awg_stream(
        self,
        channels: int | Sequence[int],
        reads: int | Sequence[int],
        waveform: object,
        rate: float,
        cycles: int = 1,
        on_reading: Callable[[AdcReading], None] | None = None,
        hdf5_path: str | None = None,
    ) -> np.ndarray:
        dac_channels, values = self._waveform(channels, waveform)
        adc_channels = _channels(reads, name="reads")
        rate = float(rate)
        if not math.isfinite(rate) or rate <= 0:
            raise ValueError("rate must be a positive number of samples per second")
        cycles = _positive_int(cycles, name="cycles")
        interval_us = max(1, round(1e6 / rate))
        frame_count = values.shape[1] * cycles
        command = self._command(
            "AWG_WITH_ADC",
            len(dac_channels),
            len(adc_channels),
            values.shape[1],
            interval_us,
            cycles,
            *dac_channels,
            *adc_channels,
            *values.ravel(),
        )
        output = np.empty((len(adc_channels), frame_count), dtype=np.float64)
        writer = None
        if hdf5_path:
            from .storage import AdcWriter

            writer = AdcWriter(
                hdf5_path,
                dac_channels=dac_channels,
                adc_channels=adc_channels,
                waveform=values,
                interval_us=interval_us,
                cycles=cycles,
                conversion_time_us=self.get_conversion_time(adc_channels[0]) * 1e6,
            )
        try:
            with self._operation(command):
                for index in range(frame_count):
                    raw = self._read_exact(len(adc_channels) * 4, frame_count / rate)
                    reading = np.frombuffer(raw, dtype="<f4").astype(np.float64)
                    output[:, index] = reading
                    if writer:
                        writer.write(index, reading)
                    if on_reading:
                        on_reading(AdcReading(index, tuple(adc_channels), reading.copy()))
                self._read_trailer()
        finally:
            if writer:
                writer.close(delete_if_empty=True)
            self.stop(settling_time=0.1)
        return output

    def stop(self, settling_time: float = 0.25) -> None:
        """Stop a running ramp or waveform and clear its remaining output."""
        with self._thread_lock:
            interrupt = getattr(self._serial, "interrupt", None)
            if interrupt:
                self._pending_bytes.clear()
                interrupt()
            else:
                self._write_line("STOP")
                time.sleep(max(0.0, float(settling_time)))
                self.clear_input()
            self._continuous_active = False

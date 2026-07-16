from __future__ import annotations

import struct
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import h5py
import numpy as np
import pytest

from gatekeeper import GateKeeper, GateKeeperError, RampLine, RampPoint


class FakeSerial:
    def __init__(self) -> None:
        self.bytes = bytearray()
        self.writes: list[str] = []
        self.responses: dict[str, str] = {}
        self.on_write: Callable[[str], None] | None = None
        self.closed = False
        self.options: dict[str, object] = {}
        self.write_timeout = 0.0
        self.reopens = 0

    @property
    def in_waiting(self) -> int:
        return len(self.bytes)

    def write(self, data: bytes) -> int:
        command = data.decode("ascii").strip()
        self.writes.append(command)
        if command in self.responses:
            self.bytes += self.responses[command].encode() + b"\n"
        if self.on_write:
            self.on_write(command)
        return len(data)

    def read(self, count: int) -> bytes:
        result = bytes(self.bytes[:count])
        del self.bytes[:count]
        return result

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    def reopen(self) -> None:
        self.reopens += 1
        self.bytes.clear()


@pytest.fixture
def connection(monkeypatch: pytest.MonkeyPatch) -> tuple[GateKeeper, FakeSerial]:
    port = FakeSerial()

    def open_serial(port_name: str, baud_rate: int, timeout: float) -> FakeSerial:
        port.options = {
            "port": port_name,
            "baudrate": baud_rate,
            "timeout": 0.05,
            "write_timeout": timeout,
        }
        return port

    monkeypatch.setattr("gatekeeper.device.open_shared_serial", open_serial)
    device = GateKeeper("COM3")
    return device, port


def float_bytes(values: list[float]) -> bytes:
    return np.asarray(values, dtype="<f4").tobytes()


def test_constructor_auto_selects_only_gatekeeper_port(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    instrument = FakeSerial()
    monkeypatch.setattr(GateKeeper, "find_ports", staticmethod(lambda: ("COM7",)))
    monkeypatch.setattr(
        "gatekeeper.device.open_shared_serial",
        lambda port, baud_rate, timeout: instrument,
    )

    device = GateKeeper()

    assert device.port == "COM7"
    assert capsys.readouterr().out == "Auto-selected GateKeeper on COM7.\n"


def test_constructor_without_port_errors_when_none_are_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(GateKeeper, "find_ports", staticmethod(tuple))

    with pytest.raises(GateKeeperError, match="no GateKeeper ports were found"):
        GateKeeper()


def test_constructor_without_port_errors_when_multiple_are_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        GateKeeper,
        "find_ports",
        staticmethod(lambda: ("COM7", "COM8")),
    )

    with pytest.raises(GateKeeperError, match=r"multiple GateKeeper ports.*specify a port"):
        GateKeeper()


def test_connection_and_text_commands(
    connection: tuple[GateKeeper, FakeSerial],
) -> None:
    device, instrument = connection
    assert instrument.options == {
        "port": "COM3",
        "baudrate": 115_200,
        "timeout": 0.05,
        "write_timeout": 5.0,
    }

    instrument.responses["SET,2,1.25"] = "DAC 2 UPDATED TO 1.250000 V"
    assert device.set_voltage(2, 1.25) == 1.25
    assert instrument.writes[-1] == "SET,2,1.25"

    instrument.responses["SET_DAC_CODE,2,1048575"] = "DAC 2 CODE UPDATED TO 1048575"
    device.set_dac_code(2, 1_048_575)

    instrument.bytes += b"abc"
    assert device.bytes_waiting() == 3
    assert device.read_bytes(2) == b"ab"
    assert device.read_bytes(1) == b"c"


def test_queries_on_one_instance_are_serialized_between_threads(
    connection: tuple[GateKeeper, FakeSerial],
) -> None:
    device, instrument = connection
    instrument.responses.update({"NOP": "NOP", "*RDY?": "READY"})
    original_read = instrument.read

    def slow_read(count: int) -> bytes:
        time.sleep(0.0005)
        return original_read(count)

    instrument.read = slow_read  # ty: ignore[invalid-assignment]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(device.query, ["NOP", "*RDY?"] * 10))

    assert results == ["NOP", "READY"] * 10


def test_text_timeout_reopens_the_shared_port(
    connection: tuple[GateKeeper, FakeSerial],
) -> None:
    device, instrument = connection
    device.set_timeout(0.01)

    with pytest.raises(GateKeeperError, match="timed out"):
        device.nop()

    assert instrument.reopens == 1


def test_general_ramp_uses_channel_start_stop_arrays(
    connection: tuple[GateKeeper, FakeSerial],
) -> None:
    device, instrument = connection
    command = "RAMP_N,3,5,800,0,2,4,-1,-2,-3,1,2,3"
    instrument.responses[command] = "RAMPING 3 DACS"

    device.ramp(
        [0, 2, 4],
        [-1, -2, -3],
        [1, 2, 3],
        points=5,
        dac_interval=800e-6,
    )

    assert instrument.writes[-1] == command


def test_adc_status_commands_and_filter_words_follow_chopping(
    connection: tuple[GateKeeper, FakeSerial],
) -> None:
    device, instrument = connection
    instrument.responses["GET_CHOP"] = "false"
    instrument.responses["CONVERT_TIME_FW,0,3"] = "64.94140625"
    instrument.responses["IDLE_MODE,0"] = "Returned ADC 0 to idle mode"

    device.set_chopping(False)
    assert device.get_chopping() is False
    assert device.set_conversion_filter(0, 3) == pytest.approx(64.94140625e-6)
    assert device.idle_adc(0) == "Returned ADC 0 to idle mode"
    assert device.reset_adcs() is None
    assert instrument.writes[-5:] == [
        "SET_CHOP,0",
        "GET_CHOP",
        "CONVERT_TIME_FW,0,3",
        "IDLE_MODE,0",
        "RESET",
    ]


def test_firmware_version_and_adc_calibration_getters(
    connection: tuple[GateKeeper, FakeSerial],
) -> None:
    device, instrument = connection
    instrument.responses.update(
        {
            "GET_FIRMWARE_VERSION": "Commit Hash: abc123",
            "GET_ZERO_SCALE_CAL,2": "8388608",
            "GET_FULL_SCALE_CAL,2": "2097152",
            "GET_SAVED_ZERO_SCALE_CAL,2": "8388609",
            "GET_SAVED_FULL_SCALE_CAL,2": "2097153",
        }
    )

    assert device.firmware_version() == "Commit Hash: abc123"
    assert device.get_adc_zero_scale_calibration(2) == 8_388_608
    assert device.get_adc_full_scale_calibration(2) == 2_097_152
    assert device.get_saved_adc_zero_scale_calibration(2) == 8_388_609
    assert device.get_saved_adc_full_scale_calibration(2) == 2_097_153


def test_adc_calibration_setters_and_reset(
    connection: tuple[GateKeeper, FakeSerial],
) -> None:
    device, instrument = connection
    instrument.responses.update(
        {
            "SET_SAVED_ZERO_SCALE_CAL,2,8388608": "Saved zero scale calibration",
            "SET_SAVED_FULL_SCALE_CAL,2,2097152": "Saved full scale calibration",
            "SET_ZERO_SCALE_CAL,2,8388608": "Saved zero scale calibration",
            "SET_FULL_SCALE_CAL,2,2097152": "Saved full scale calibration",
            "HARD_RESET_CALIBRATION": "Calibration data reset to defaults",
        }
    )

    assert device.set_saved_adc_zero_scale_calibration(2, 8_388_608) == (
        "Saved zero scale calibration"
    )
    assert device.set_saved_adc_full_scale_calibration(2, 2_097_152) == (
        "Saved full scale calibration"
    )
    assert device.set_adc_zero_scale_calibration(2, 8_388_608) == (
        "Saved zero scale calibration"
    )
    assert device.set_adc_full_scale_calibration(2, 2_097_152) == (
        "Saved full scale calibration"
    )
    assert device.hard_reset_calibration() == "Calibration data reset to defaults"


def test_adc_calibration_methods_validate_channels_and_register_values(
    connection: tuple[GateKeeper, FakeSerial],
) -> None:
    device, _ = connection

    with pytest.raises(ValueError, match="channels 0 through 7"):
        device.get_adc_zero_scale_calibration(8)
    with pytest.raises(ValueError, match="between 0 and 16777215"):
        device.set_adc_zero_scale_calibration(0, -1)
    with pytest.raises(ValueError, match="between 0 and 16777215"):
        device.set_saved_adc_full_scale_calibration(0, 0x1000000)


def test_dac_led_ramp_uses_working_server_argument_order(
    connection: tuple[GateKeeper, FakeSerial],
) -> None:
    device, instrument = connection
    samples = [10.0, 20.0, 11.0, 21.0, 12.0, 22.0]

    def respond(command: str) -> None:
        if command.startswith("DAC_LED_BUFFER_RAMP"):
            instrument.bytes += float_bytes(samples) + b"\n"

    instrument.on_write = respond
    result = device.dac_led_buffer_ramp(
        channels=[0, 2],
        reads=[1, 5],
        start=[-1, -2],
        stop=[1, 2],
        points=3,
        dac_interval=1e-3,
        settling_time=100e-6,
        averages=2,
    )

    assert instrument.writes[-1] == ("DAC_LED_BUFFER_RAMP,2,2,3,2,1000,100,0,2,-1,-2,1,2,1,5")
    np.testing.assert_array_equal(result, [[10, 11, 12], [20, 21, 22]])


def test_time_series_uses_the_firmwares_integer_microsecond_sample_count(
    connection: tuple[GateKeeper, FakeSerial],
) -> None:
    device, instrument = connection

    def respond(command: str) -> None:
        if command.startswith("TIME_SERIES_BUFFER_RAMP"):
            instrument.bytes += float_bytes(list(range(10))) + b"\n"

    instrument.on_write = respond
    result = device.time_series_buffer_ramp(
        channels=0,
        reads=1,
        start=-1,
        stop=1,
        points=3,
        dac_interval=1000.9e-6,
        adc_interval=300.9e-6,
    )

    assert instrument.writes[-1] == "TIME_SERIES_BUFFER_RAMP,1,1,3,1000,300,0,-1,1,1"
    np.testing.assert_array_equal(result, [list(range(10))])


def test_boxcar_uses_the_current_firmware_layout(
    connection: tuple[GateKeeper, FakeSerial],
) -> None:
    device, instrument = connection

    def respond(command: str) -> None:
        if command.startswith("BOXCAR_BUFFER_RAMP"):
            instrument.bytes += float_bytes(list(range(16))) + b"\n"

    instrument.on_write = respond
    result = device.boxcar_buffer_ramp(
        channels=0,
        reads=[1, 5],
        start_low=-1,
        stop_low=1,
        start_high=2,
        stop_high=3,
        points=2,
        measures_per_step=2,
        averages=1,
        conversion_time=300.9e-6,
    )

    assert instrument.writes[-1] == "BOXCAR_BUFFER_RAMP,1,2,2,2,1,300,0,-1,1,2,3,1,5"
    np.testing.assert_array_equal(result, [range(0, 16, 2), range(1, 16, 2)])


def test_2d_output_and_callbacks_match_labrad_acquisition_order(
    connection: tuple[GateKeeper, FakeSerial],
    tmp_path,
) -> None:
    device, instrument = connection
    lines: list[RampLine] = []
    points: list[RampPoint] = []
    hdf5_path = tmp_path / "scan.h5"

    def respond(command: str) -> None:
        if command.startswith("2D_DAC_LED_BUFFER_RAMP"):
            instrument.bytes += float_bytes([1, 2, 3, 30, 20, 10]) + b"\n"

    instrument.on_write = respond
    result = device.dac_led_buffer_ramp_2d(
        fast={"ch": 0, "start": -1.0, "stop": 1.0, "points": 3},
        slow={"ch": 1, "start": 2.0, "stop": 3.0, "points": 2},
        reads=4,
        dac_interval=1e-3,
        settling_time=100e-6,
        snake=True,
        on_point=points.append,
        on_line=lines.append,
        hdf5_path=str(hdf5_path),
    )

    assert instrument.writes[-1] == (
        "2D_DAC_LED_BUFFER_RAMP,2,1,3,2,1000,100,0,1,1,0,1,-1,2,2,0,0,1,4"
    )
    np.testing.assert_array_equal(result[0], [1, 2, 3, 30, 20, 10])
    assert [line.direction for line in lines] == ["forward", "backward"]
    assert lines[1].slow_position == (-1.0, 3.0)
    np.testing.assert_array_equal(lines[1].data, [[30, 20, 10]])
    assert [point.point_index for point in points] == [0, 1, 2, 0, 1, 2]
    assert [point.acquisition_index for point in points] == [0, 1, 2, 0, 1, 2]
    assert [point.fast_fraction for point in points] == [0.0, 0.5, 1.0, 1.0, 0.5, 0.0]
    assert [point.values[0] for point in points] == [1, 2, 3, 30, 20, 10]

    with h5py.File(hdf5_path, "r") as file:
        assert list(file.keys()) == ["data"]
        dataset = file["data"]
        assert list(dataset.attrs["column_names"]) == [
            "point_index",
            "line_index",
            "dac_0",
            "dac_1",
            "adc_4",
        ]
        assert dataset.attrs["points_per_line"] == 3
        assert dataset.attrs["total_lines"] == 2
        assert dataset.attrs["steps_fast"] == 3
        assert dataset.attrs["steps_slow"] == 2
        assert dataset.attrs["ramp_type"] == "dac_led"
        np.testing.assert_array_equal(dataset[:, 4], [1, 2, 3, 30, 20, 10])


def test_time_series_adc_read_returns_exact_requested_points(
    connection: tuple[GateKeeper, FakeSerial],
) -> None:
    device, instrument = connection

    def respond(command: str) -> None:
        if command.startswith("TIME_SERIES_ADC_READ"):
            instrument.bytes += struct.pack("<f", 1000.0)
            instrument.bytes += float_bytes([1, 2, 3, 4, 5, 6]) + b"\n"

    instrument.on_write = respond
    result = device.time_series_adc_read(reads=[1], points=4, rate=1e3)

    assert instrument.writes[-1] == "TIME_SERIES_ADC_READ,1,1,1000,6000"
    np.testing.assert_array_equal(result, [[1, 2, 3, 4]])


def test_awg_stream_reads_one_adc_frame_per_waveform_step(
    connection: tuple[GateKeeper, FakeSerial],
) -> None:
    device, instrument = connection
    readings = []

    def respond(command: str) -> None:
        if command.startswith("AWG_WITH_ADC"):
            instrument.bytes += float_bytes([1, 10, 2, 20, 3, 30, 4, 40]) + b"\n"

    instrument.on_write = respond
    result = device.awg_stream(
        channels=0,
        reads=[1, 2],
        waveform=[-1, 0, 1, 0],
        rate=2e3,
        on_reading=readings.append,
    )

    assert instrument.writes[0] == "AWG_WITH_ADC,1,2,4,500,1,0,1,2,-1,0,1,0"
    assert instrument.writes[-1] == "STOP"
    np.testing.assert_array_equal(result, [[1, 2, 3, 4], [10, 20, 30, 40]])
    assert len(readings) == 4


def test_website_awg_write_is_channel_major(
    connection: tuple[GateKeeper, FakeSerial],
) -> None:
    device, instrument = connection
    device.awg_write(
        channels=[0, 2],
        waveform=[[-1, 0, 1], [2, 3, 4]],
        rate=10e3,
    )
    assert instrument.writes[-1] == "AWG_BUFFER_RAMP,2,3,100,0,2,-1,0,1,2,3,4"


def test_binary_failure_becomes_gatekeeper_error(
    connection: tuple[GateKeeper, FakeSerial],
) -> None:
    device, instrument = connection

    def respond(command: str) -> None:
        if command.startswith("TIME_SERIES_BUFFER_RAMP"):
            instrument.bytes += b"FAILURE: unsafe timing\n"

    instrument.on_write = respond
    with pytest.raises(GateKeeperError, match="unsafe timing"):
        device.time_series_buffer_ramp(
            channels=0,
            reads=0,
            start=-1,
            stop=1,
            points=10,
            dac_interval=1e-3,
            adc_interval=1e-3,
        )


def test_adc_header_failure_is_not_mistaken_for_a_float(
    connection: tuple[GateKeeper, FakeSerial],
) -> None:
    device, instrument = connection

    def respond(command: str) -> None:
        if command.startswith("TIME_SERIES_ADC_READ"):
            instrument.bytes += b"FAILURE: conversion time too short\n"

    instrument.on_write = respond
    with pytest.raises(GateKeeperError, match="conversion time too short"):
        device.adc_read(reads=0, duration=0.01, conversion_time=60e-6)

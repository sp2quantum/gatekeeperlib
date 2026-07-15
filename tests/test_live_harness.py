from __future__ import annotations

import ast

import pytest

from gatekeeper import hardware_test as live_test


class FakeGateKeeper:
    def __init__(self, port: str, *, baud_rate: int) -> None:
        self.port = port
        self.baud_rate = baud_rate
        self.input = bytearray()
        self.writes: list[str] = []
        self.responses: dict[str, bytes] = {}
        self.closed = False

    def clear_input(self) -> None:
        self.input.clear()

    def close(self) -> None:
        self.closed = True

    def write(self, command: str) -> None:
        self.writes.append(command)
        self.input += self.responses.get(command, b"")

    def bytes_waiting(self) -> int:
        return len(self.input)

    def read_bytes(self, byte_count: int) -> bytes:
        data = bytes(self.input[:byte_count])
        del self.input[:byte_count]
        return data


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> live_test.LibraryHarness:
    monkeypatch.setattr(live_test, "GateKeeper", FakeGateKeeper)
    monkeypatch.setattr(live_test.time, "sleep", lambda _: None)
    return live_test.LibraryHarness("TEST_PORT")


def test_library_harness_sends_commands_through_gatekeeper(
    harness: live_test.LibraryHarness,
) -> None:
    device = harness.device
    assert isinstance(device, FakeGateKeeper)
    device.responses["SET,2,1.25"] = b"DAC 2 UPDATED TO 1.250000 V\n"

    assert harness.query_line("SET", 2, 1.25) == "DAC 2 UPDATED TO 1.250000 V"
    assert device.writes == ["SET,2,1.25"]
    assert device.baud_rate == 115_200


def test_library_harness_reads_binary_data(
    harness: live_test.LibraryHarness,
) -> None:
    device = harness.device
    assert isinstance(device, FakeGateKeeper)
    device.input += b"\x00\x01\x02\x03"

    assert harness.read_exact(4, timeout=0.1) == b"\x00\x01\x02\x03"


def test_harness_matches_firmware_serial_interface() -> None:
    firmware_test = live_test.find_firmware_test()
    source = ast.parse(firmware_test.read_text(encoding="utf-8"))
    serial_class = next(
        node
        for node in source.body
        if isinstance(node, ast.ClassDef) and node.name == "GateKeeperSerial"
    )
    required_methods = {
        node.name for node in serial_class.body if isinstance(node, ast.FunctionDef)
    }

    assert required_methods <= set(dir(live_test.LibraryHarness))


def test_loader_replaces_only_the_firmware_connection(tmp_path) -> None:
    suite_path = tmp_path / "hardware_functionality_test.py"
    suite_path.write_text(
        "class GateKeeperSerial:\n"
        "    pass\n"
        "def detect_port():\n"
        "    return 'direct'\n"
        "def main():\n"
        "    return 7\n",
        encoding="utf-8",
    )

    suite = live_test.load_firmware_suite(suite_path)

    assert suite.GateKeeperSerial is live_test.LibraryHarness
    assert suite.detect_port is live_test.detect_port
    assert suite.main() == 7


def test_hardware_runner_defaults_to_ignored_testing_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    arguments = ["gatekeeper-hardware-test", "--port", "COM8"]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(live_test.time, "strftime", lambda _: "20260715_150000")

    live_test.ensure_output_dir_argument(arguments)

    assert arguments[-2:] == [
        "--output-dir",
        str(tmp_path / "live_tests" / "test_outputs" / "hardware_full_20260715_150000"),
    ]


def test_hardware_runner_preserves_explicit_output_dir() -> None:
    arguments = ["gatekeeper-hardware-test", "--output-dir", "chosen"]
    live_test.ensure_output_dir_argument(arguments)
    assert arguments == ["gatekeeper-hardware-test", "--output-dir", "chosen"]

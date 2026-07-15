from __future__ import annotations

import os
import uuid

import pytest
import serial

from gatekeeper import _broker, _shared_serial


class FakePhysicalSerial:
    instances: list[FakePhysicalSerial] = []

    def __init__(self, **options: object) -> None:
        self.options = options
        self.timeout = options["timeout"]
        self.write_timeout = options["write_timeout"]
        self.input = bytearray()
        self.closed = False
        self.instances.append(self)

    def write(self, data: bytes) -> int:
        if data == b"NOP\n":
            self.input += b"NOP\n"
        return len(data)

    def flush(self) -> None:
        pass

    def read(self, count: int) -> bytes:
        result = bytes(self.input[:count])
        del self.input[:count]
        return result

    @property
    def in_waiting(self) -> int:
        return len(self.input)

    def reset_input_buffer(self) -> None:
        self.input.clear()

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def clean_hosts(monkeypatch: pytest.MonkeyPatch):
    _shared_serial._close_hosts()
    FakePhysicalSerial.instances.clear()
    monkeypatch.setattr(_broker.serial, "Serial", FakePhysicalSerial)
    yield
    _shared_serial._close_hosts()


def test_first_process_hosts_and_closes_the_physical_port() -> None:
    port = f"FAKE-{uuid.uuid4().hex}"
    first = _shared_serial.SharedSerial(port, 115_200, 1.0)
    second = _shared_serial.SharedSerial(port, 115_200, 1.0)

    assert first.is_host is True
    assert second.is_host is True
    assert first.host_pid == second.host_pid == os.getpid()
    assert len(FakePhysicalSerial.instances) == 1

    first.acquire()
    assert first.write(b"NOP\n") == 4
    assert first.read(4) == b"NOP\n"
    first.release()

    first.close()
    second.close()
    _shared_serial._close_hosts()
    assert FakePhysicalSerial.instances[0].closed is True


def test_client_reconnects_after_host_disappears() -> None:
    port = f"FAKE-{uuid.uuid4().hex}"
    client = _shared_serial.SharedSerial(port, 115_200, 1.0)
    host = _shared_serial._HOSTS.pop(port.upper())
    host.close()

    with pytest.raises(serial.SerialException, match="restored after disconnect"):
        client.acquire()

    client.acquire()
    client.release()
    assert client.is_host is True
    assert len(FakePhysicalSerial.instances) == 2
    client.close()

from __future__ import annotations

import h5py
import numpy as np
import pytest

from gatekeeper.models import RampPoint
from gatekeeper.storage import AdcWriter, RampWriter


def make_ramp_writer(path, **overrides) -> RampWriter:
    arguments = {
        "dac_channels": [0, 1],
        "adc_channels": [3],
        "points_per_line": 3,
        "slow_points": 2,
        "start_point": [-1.0, 2.0],
        "fast_axis_vector": [2.0, 0.0],
        "slow_axis_vector": [0.0, 1.0],
        "retrace": False,
        "snake": True,
        "metadata": {
            "start_point": [-1.0, 2.0],
            "fast_axis_vector": [2.0, 0.0],
            "slow_axis_vector": [0.0, 1.0],
            "steps_fast": 3,
            "steps_slow": 2,
            "retrace": False,
            "snake": True,
            "num_adc_averages": 1,
            "dac_interval_us": 1000.0,
            "dac_settling_time_us": 100.0,
            "ramp_type": "dac_led",
        },
    }
    arguments.update(overrides)
    return RampWriter(str(path), **arguments)


def point(line_index: int, acquisition_index: int, value: float) -> RampPoint:
    return RampPoint(
        line_index=line_index,
        slow_index=line_index,
        point_index=acquisition_index,
        acquisition_index=acquisition_index,
        fast_fraction=acquisition_index / 2,
        slow_fraction=float(line_index),
        direction="forward" if line_index == 0 else "backward",
        dac_channels=(0, 1),
        adc_channels=(3,),
        slow_position=(-1.0, 2.0 + line_index),
        values=np.array([value]),
    )


def test_ramp_writer_matches_labrad_table_schema_and_coordinates(tmp_path) -> None:
    path = tmp_path / "scan.h5"
    writer = make_ramp_writer(path)
    writer.write_point(point(0, 0, 10.0))
    writer.write_point(point(1, 0, 30.0))
    writer.close()

    with h5py.File(path, "r") as file:
        assert list(file.keys()) == ["data"]
        data = file["data"]
        assert data.shape == (6, 5)
        assert list(data.attrs["column_names"]) == [
            "point_index",
            "line_index",
            "dac_0",
            "dac_1",
            "adc_3",
        ]
        np.testing.assert_array_equal(
            data[:, :4],
            [
                [0, 0, -1, 2],
                [1, 0, 0, 2],
                [2, 0, 1, 2],
                [0, 1, 1, 3],
                [1, 1, 0, 3],
                [2, 1, -1, 3],
            ],
        )
        assert data[0, 4] == 10
        assert data[3, 4] == 30
        assert data.attrs["points_per_line"] == 3
        assert data.attrs["total_lines"] == 2
        assert data.attrs["ramp_type"] == "dac_led"


def test_ramp_writer_never_overwrites(tmp_path) -> None:
    path = tmp_path / "scan.h5"
    path.write_bytes(b"existing")
    with pytest.raises(ValueError, match="refusing to overwrite"):
        make_ramp_writer(path)


def test_adc_writer_matches_labrad_awg_table_schema(tmp_path) -> None:
    path = tmp_path / "awg.h5"
    waveform = np.array([[0.0, 1.0, 0.0], [2.0, 3.0, 4.0]])
    writer = AdcWriter(
        str(path),
        dac_channels=[0, 2],
        adc_channels=[1, 5],
        waveform=waveform,
        interval_us=100,
        cycles=2,
        conversion_time_us=250.0,
    )
    writer.write(0, np.array([1.0, 10.0]))
    writer.write(1, np.array([2.0, 20.0]))
    writer.close()

    with h5py.File(path, "r") as file:
        assert list(file.keys()) == ["data"]
        data = file["data"]
        np.testing.assert_array_equal(data[:], [[0, 250, 1, 10], [1, 500, 2, 20]])
        assert list(data.attrs["column_names"]) == [
            "reading_index",
            "time_us",
            "adc_1",
            "adc_5",
        ]
        assert list(data.attrs["dac_ports"]) == [0, 2]
        assert list(data.attrs["adc_ports"]) == [1, 5]
        assert data.attrs["num_steps"] == 3
        assert data.attrs["num_cycles"] == 2
        np.testing.assert_array_equal(data.attrs["voltage_lists"], waveform)

"""LabRAD-compatible HDF5 output for live GateKeeper acquisitions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import h5py
import numpy as np

from .models import RampPoint


def _new_path(filename: str) -> Path:
    supplied_path = str(filename)
    path = Path(supplied_path).expanduser().resolve()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ValueError(
            f"Cannot create the HDF5 output directory {str(path.parent)!r}. "
            f"Check the spelling and permissions of hdf5_path "
            f"(supplied path: {supplied_path!r}). Windows reported: {error}"
        ) from error
    if path.exists():
        raise ValueError(f"HDF5 output file already exists; refusing to overwrite it: {path}")
    return path


class RampWriter:
    """Write the exact 2D table schema used by the LabRAD DAC-ADC-GIGA server."""

    def __init__(
        self,
        filename: str,
        *,
        dac_channels: Sequence[int],
        adc_channels: Sequence[int],
        points_per_line: int,
        slow_points: int,
        start_point: Sequence[float],
        fast_axis_vector: Sequence[float],
        slow_axis_vector: Sequence[float],
        retrace: bool,
        snake: bool,
        metadata: Mapping[str, object],
    ) -> None:
        self._path = _new_path(filename)
        self._dac_channels = list(dac_channels)
        self._adc_channels = list(adc_channels)
        self._points_per_line = int(points_per_line)
        self._lines = int(slow_points) * (2 if retrace and not snake else 1)
        self._points_written = 0
        column_names = (
            ["point_index", "line_index"]
            + [f"dac_{channel}" for channel in self._dac_channels]
            + [f"adc_{channel}" for channel in self._adc_channels]
        )

        self._file = h5py.File(self._path, "x", libver="latest")
        self._data = self._file.create_dataset(
            "data",
            shape=(self._lines * self._points_per_line, len(column_names)),
            dtype=np.float64,
            chunks=(self._points_per_line, len(column_names)),
        )
        self._data.attrs["column_names"] = column_names
        self._data.attrs["points_per_line"] = self._points_per_line
        self._data.attrs["total_lines"] = self._lines
        for key, value in metadata.items():
            self._data.attrs[key] = value

        start = np.asarray(start_point, dtype=np.float64)
        fast = np.asarray(fast_axis_vector, dtype=np.float64)
        slow = np.asarray(slow_axis_vector, dtype=np.float64)
        slow_denominator = slow_points - 1 if slow_points > 1 else 1
        coordinate_columns = 2 + len(self._dac_channels)
        for line_index in range(self._lines):
            if retrace and not snake:
                slow_step = line_index // 2
                forward = line_index % 2 == 0
            else:
                slow_step = line_index
                forward = not snake or slow_step % 2 == 0
            slow_fraction = float(slow_step) / slow_denominator
            slow_position = start + slow_fraction * slow
            fast_fraction = np.linspace(0.0, 1.0, self._points_per_line)
            if not forward:
                fast_fraction = fast_fraction[::-1]
            dac_values = slow_position[None, :] + fast_fraction[:, None] * fast
            coordinates = np.column_stack(
                (
                    np.arange(self._points_per_line),
                    np.full(self._points_per_line, line_index),
                    dac_values,
                )
            )
            row = line_index * self._points_per_line
            self._data[row : row + self._points_per_line, :coordinate_columns] = coordinates

        self._file.flush()
        self._file.swmr_mode = True

    def write_point(self, point: RampPoint) -> None:
        row = point.line_index * self._points_per_line + point.acquisition_index
        adc_column = 2 + len(self._dac_channels)
        count = min(len(point.values), len(self._adc_channels))
        self._data[row, adc_column : adc_column + count] = point.values[:count]
        self._points_written += 1
        self._data.flush()
        self._file.flush()

    def close(self, *, delete_if_empty: bool = False) -> None:
        self._file.flush()
        self._file.close()
        if delete_if_empty and self._points_written == 0:
            self._path.unlink(missing_ok=True)


class AdcWriter:
    """Write the exact AWG table schema used by the LabRAD save helper."""

    def __init__(
        self,
        filename: str,
        *,
        dac_channels: Sequence[int],
        adc_channels: Sequence[int],
        waveform: np.ndarray,
        interval_us: int,
        cycles: int,
        conversion_time_us: float,
    ) -> None:
        self._path = _new_path(filename)
        self._adc_channels = list(adc_channels)
        self._conversion_time_us = float(conversion_time_us)
        self._readings_written = 0
        column_names = ["reading_index", "time_us"] + [
            f"adc_{channel}" for channel in self._adc_channels
        ]

        self._file = h5py.File(self._path, "x")
        self._data = self._file.create_dataset(
            "data",
            shape=(0, len(column_names)),
            maxshape=(None, len(column_names)),
            dtype=np.float64,
            chunks=True,
        )
        self._data.attrs["column_names"] = column_names
        self._data.attrs["dac_ports"] = list(dac_channels)
        self._data.attrs["adc_ports"] = self._adc_channels
        self._data.attrs["num_steps"] = waveform.shape[1]
        self._data.attrs["num_cycles"] = int(cycles)
        self._data.attrs["dac_interval_us"] = float(interval_us)
        self._data.attrs["voltage_lists"] = waveform

    def write(self, index: int, values: np.ndarray) -> None:
        row = np.asarray(
            [float(index), (index + 1) * self._conversion_time_us, *values],
            dtype=np.float64,
        )
        self._data.resize((index + 1, self._data.shape[1]))
        self._data[index, :] = row
        self._readings_written += 1
        self._data.flush()
        self._file.flush()

    def close(self, *, delete_if_empty: bool = False) -> None:
        self._file.flush()
        self._file.close()
        if delete_if_empty and self._readings_written == 0:
            self._path.unlink(missing_ok=True)

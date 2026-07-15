"""Small public data types returned by GateKeeper operations."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TypedDict

import numpy as np

Number = int | float


class Axis(TypedDict):
    """One axis of a 2-D sweep.

    ``ch``, ``start``, and ``stop`` may be scalars or matching sequences.
    """

    ch: int | Sequence[int]
    start: Number | Sequence[Number]
    stop: Number | Sequence[Number]
    points: int


@dataclass(frozen=True)
class RampLine:
    """One completed line delivered during a 2-D acquisition."""

    index: int
    slow_index: int
    slow_fraction: float
    direction: str
    dac_channels: tuple[int, ...]
    adc_channels: tuple[int, ...]
    slow_position: tuple[float, ...]
    data: np.ndarray


@dataclass(frozen=True)
class RampPoint:
    """One ADC point delivered while a 2-D acquisition is running."""

    line_index: int
    slow_index: int
    point_index: int
    acquisition_index: int
    fast_fraction: float
    slow_fraction: float
    direction: str
    dac_channels: tuple[int, ...]
    adc_channels: tuple[int, ...]
    slow_position: tuple[float, ...]
    values: np.ndarray


@dataclass(frozen=True)
class AdcReading:
    """One simultaneous ADC reading delivered during AWG playback."""

    index: int
    channels: tuple[int, ...]
    values: np.ndarray


@dataclass(frozen=True)
class AdcCapture:
    """ADC samples together with the sample period reported by the device."""

    data: np.ndarray
    sample_period: float

    @property
    def sample_rate(self) -> float:
        return 1.0 / self.sample_period

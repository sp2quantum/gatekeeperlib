# sp2 Quantum gatekeeperlib

`gatekeeperlib` is a direct Python interface to the sp2 Quantum GateKeeper. One
`GateKeeper` object owns one serial connection and NumPy arrays carry
acquired data back to the caller.

## Install

gatekeeperlib requires Python 3.10 or newer. We highly recommend using `uv` as your package manager. To add it to a `uv` project:

```bash
uv add gatekeeperlib
```

If you would like to use pip instead, use:

```bash
python -m pip install gatekeeperlib
```


## Connect

If exactly one GateKeeper is connected, the port can be selected automatically:

```python
from gatekeeper import GateKeeper

gk = GateKeeper()
```

If multiple GateKeepers are connected, specify the port:

```python
from gatekeeper import GateKeeper

gk = GateKeeper("COM3")
print(gk.idn())
print(gk.serial_number())

gk.close()
```

A context manager closes that instance's broker connection even when an
experiment raises an error:

```python
with GateKeeper("/dev/cu.usbmodem14101") as gk:
    gk.set_voltage(0, 0.25)
    measured = gk.read_voltage(0)
```

An integer is shorthand for a Windows COM port:

```python
GateKeeper(3)  # COM3
```

Use `GateKeeper.find_ports()` to find connected GateKeepers.

The first instance for a port makes its Python process the serial host.
Instances in other Python processes automatically share that host, so the same
GateKeeper can be used from multiple shells without mixing responses. Commands
are serialized and `stop()` can interrupt an active waveform from another
shell. The physical port closes automatically when the host process exits; no
detached background process remains. If the host disappears, the next process
automatically becomes the host. After a timeout the host closes, reopens, and
resynchronizes the physical port before the next command.

```python
print(gk.is_serial_host, gk.serial_host_pid)
```

## Examples

### Output shapes

All acquired arrays are channel-major.

| Method | Shape |
| --- | --- |
| `dac_led_buffer_ramp` | `(n_adc, points)` |
| `time_series_buffer_ramp` | `(n_adc, points * dac_interval_us // adc_interval_us)` |
| `dac_led_buffer_ramp_2d` | `(n_adc, lines * fast_points)` |
| `time_series_buffer_ramp_2d` | `(n_adc, lines * fast_points * dac_interval_us // adc_interval_us)` |
| `time_series_adc_read` | `(n_adc, points)` |
| `adc_read().data` | `(n_adc, samples)` |
| `boxcar_buffer_ramp` | `(n_adc, 2 * points * measures_per_step * averages)` |
| `awg_stream` | `(n_adc, waveform_points * cycles)` |

For 2D ramps, `lines` equals the number of slow points. It doubles when
`retrace=True` and `snake=False`. The 2D result is flattened in LabRAD
acquisition order.

### Synchronized gate sweep

```python
data = gk.dac_led_buffer_ramp(
    channels=[2],
    reads=[1],
    start=-0.5,
    stop=0.5,
    points=600,
    dac_interval=200e-6,
)
```

A scalar start or stop is broadcast to every selected DAC. Pass a list to give
each DAC its own range. When `settling_time` is omitted, the library chooses it
from the active ADC conversion times.

### One-dimensional time-series ramp

To sample the ADC independently of the DAC steps, provide both intervals:

```python
trace = gk.time_series_buffer_ramp(
    channels=[0],
    reads=[0, 1],
    start=-0.5,
    stop=0.5,
    points=600,
    dac_interval=500e-6,
    adc_interval=250e-6,
)
```

Here, two ADC samples are acquired during each DAC interval, so `trace.shape`
is `(2, 1200)`.

### Two-dimensional map

```python
fast_gate = dict(ch=0, start=-1.0, stop=1.0, points=180)
slow_gate = dict(ch=1, start=4.0, stop=6.9, points=128)

image = gk.dac_led_buffer_ramp_2d(
    fast=fast_gate,
    slow=slow_gate,
    reads=[1],
    dac_interval=180e-6,
)
```

For an ordinary forward scan, reshape a channel before plotting it:

```python
adc_1_image = image[0].reshape(slow_gate["points"], fast_gate["points"])
```

An axis can move several DACs together by using lists for `ch`, `start`, and
`stop`. `snake=True` alternates acquisition direction and `retrace=True`
records both directions. Samples remain in acquisition order, exactly as they
do in the LabRAD return value.

To update a plot or another consumer after every acquired point:

```python
def show_point(point):
    print(point.line_index, point.point_index, point.values)

image = gk.dac_led_buffer_ramp_2d(
    fast=fast_gate,
    slow=slow_gate,
    reads=[0, 1],
    dac_interval=500e-6,
    on_point=show_point,
    hdf5_path="scan.h5",
)
```

When `hdf5_path` is provided, the file uses the exact LabRAD layout: one 2-D
dataset named `data`, with `point_index`, `line_index`, `dac_*`, and `adc_*`
columns plus the same scan metadata attributes. Coordinates for the complete
scan are present when the file is created, and each ADC row is flushed as its
point arrives, so the lab 2D plotter can open it in SWMR live mode. Use
`on_line=` when a completed-line callback is also useful. Existing files are
never overwritten.

### Two-dimensional time-series ramp

```python
time_series_image = gk.time_series_buffer_ramp_2d(
    fast=fast_gate,
    slow=slow_gate,
    reads=[0, 1],
    dac_interval=500e-6,
    adc_interval=250e-6,
)
```

With the axes above, `time_series_image.shape` is `(2, 46080)`.

### ADC-only acquisition

```python
trace = gk.time_series_adc_read(reads=[0, 1], points=1000, rate=1e3)
print(trace.shape)  # (2, 1000)
```

For access to the sample period measured by the firmware, use `adc_read`:

```python
capture = gk.adc_read(
    reads=[0, 1],
    duration=1.0,
    conversion_time=250e-6,
)

print(capture.sample_rate)
print(capture.data.shape)
```

### Arbitrary waveform generation

```python
import numpy as np

# One row per DAC channel and one column per waveform sample.
wave = np.array([
    [0.0, 0.1, 0.0, -0.1],  # DAC 0
    [0.0, 0.2, 0.0, -0.2],  # DAC 1
])

gk.awg_write(channels=[0, 1], waveform=wave, rate=10e3)

# AWG output repeats until stopped.
gk.stop()
```

To acquire one ADC frame for every waveform step:

```python
samples = gk.awg_stream(
    channels=[0, 1],
    reads=[0, 1],
    waveform=wave[:2],
    rate=10e3,
    cycles=4,
    hdf5_path="waveform-readback.h5",
)
```

Pass `on_reading=` to receive each `AdcReading` as it arrives.

## More than one GateKeeper

Create one normal connection per instrument:

```python
from gatekeeper import GateKeeper

with (
    GateKeeper("/dev/cu.usbmodem-left") as gk1,
    GateKeeper("/dev/cu.usbmodem-right") as gk2,
):
    gk1.set_voltage(0, 0.1)
    gk2.set_voltage(0, -0.1)

    left_reading = gk1.read_voltage(0)
    right_reading = gk2.read_voltage(0)
```

Each object owns only its own serial connection.

## API overview

All public times are seconds, rates are samples per second, voltages are volts,
and channels are integers from 0 through 7.

| Area | Methods |
| --- | --- |
| Connection | `find_ports`, `idn`, `ready`, `nop`, `serial_number`, `close` |
| Direct DAC | `set_voltage`, `get_dac`, `set_dac_code`, `initialize` |
| DAC limits | `set_full_scale`, `get_full_scale`, `set_upper_limit`, `set_lower_limit`, `get_upper_limit`, `get_lower_limit` |
| DAC calibration | `calibrate_dacs`, `set_offset_and_gain`, `get_offset_and_gain` |
| Direct ADC | `read_voltage`, `read_voltages`, `idle_adc`, `active_adc_channels`, `reset_adcs`, `hard_reset_adcs` |
| ADC timing | `set_chopping`, `get_chopping`, `set_conversion_time`, `set_conversion_filter`, `get_conversion_time` |
| ADC calibration | `calibrate_adc_zero`, `calibrate_adc_full_scale` |
| Ramps | `ramp`, `dac_led_buffer_ramp`, `time_series_buffer_ramp`, `dac_led_buffer_ramp_2d`, `time_series_buffer_ramp_2d`, `boxcar_buffer_ramp` |
| ADC capture | `adc_read`, `time_series_adc_read` |
| Waveforms | `awg_write`, `awg_stream`, `stop` |
| Low-level access | `write`, `read`, `query`, `read_bytes`, `bytes_waiting`, `clear_input`, `set_timeout` |

## Errors and interruption

Invalid local arguments raise `ValueError`. A firmware `FAILURE` response or a
binary-transfer timeout raises `GateKeeperError`. Call `stop()` to stop any blocking process; e.g., buffer ramps, AWG.

## Development

Live-device validation instructions are in
[`live_tests/LIVE_HARDWARE_TESTING.md`](live_tests/LIVE_HARDWARE_TESTING.md).

```bash
uv sync --extra test
uv run pytest
uv run ruff check .
uv run ty check src
uv build
```

## Publishing

The upload scripts load `test_pypi_key` or `pypi_key` from the ignored `.env`
file, build fresh distributions, validate them with Twine, and upload them:

```bash
uv run python upload_to_test_pypi.py
uv run python upload_to_pypi.py
```

To build and validate without uploading:

```bash
uv run python upload_to_test_pypi.py --check-only
uv run python upload_to_pypi.py --check-only
```

The same scripts work with ordinary Python on Windows, macOS, and Linux. If
`uv` is unavailable, install the publishing tools with
`python -m pip install build twine` first.

# Codex handoff: test and iterate gatekeeperlib on live hardware

You are validating `gatekeeperlib` on a different computer with a real sp2
Quantum GateKeeper. Work autonomously until the library passes both its offline
tests and the live tests below, or until you can identify a concrete hardware
blocker that cannot be resolved in software.

## Scope and safety

You may inspect and edit the `gatekeeperlib` checkout, run its tests, build the
package, communicate with the specified GateKeeper, and rerun tests after each
fix. Preserve unrelated local changes.

Do not flash firmware, change serial numbers, alter saved calibration, run any
`calibrate_*` method, call `set_offset_and_gain`, or call `hard_reset_adcs`.
Do not edit the firmware repository merely to make a library test pass. Treat
the current firmware command implementations and its
`hardware_functionality_test.py` as the protocol source of truth.

The full hardware suite:

- requires DAC channel `n` to be physically looped back to ADC channel `n` for
  all eight channels;
- drives all eight DACs through ramps and waveforms, including static targets
  from 0 V through 7 V;
- changes temporary ADC conversion settings, chopping state, DAC limits, and
  full-scale settings;
- avoids calibration commands and returns every DAC output to 0 V during
  cleanup.

Disconnect the GateKeeper from an experiment, cryostat, sample, amplifier, or
other voltage-sensitive equipment. Confirm the eight DAC-to-ADC loopbacks and
the device's normal ±10 V configuration before running the full suite. Use an
explicit serial port; never guess when multiple GateKeepers are connected.

## 1. Record the environment

Set these shell variables using paths and a port that exist on this computer:

```bash
export GK_LIB=/absolute/path/to/gatekeeperlib
export GK_FIRMWARE_TEST=/absolute/path/to/hardware_functionality_test.py
export GK_PORT=/dev/cu.usbmodemXXXX
```

On Windows PowerShell, set the equivalent environment variables and use a port
such as `COM3`.

The firmware test should come from the GateKeeper firmware checkout. Common
repository locations are:

```text
gatekeeper-firmware/hardware_functionality_test.py
dac-adc-firmware/hardware_functionality_test.py
gatekeeper-firmware-afylab/hardware_functionality_test.py
```

Before changing anything:

```bash
cd "$GK_LIB"
git status --short
git rev-parse HEAD
python3 --version
```

Record the operating system, Python version, library commit, firmware commit,
GateKeeper serial number, firmware version, and selected port in the final
report. Do not discard or overwrite pre-existing changes.

## 2. Install and run all offline checks

Use Python 3.10 or newer. Prefer `uv` when available:

```bash
cd "$GK_LIB"
uv sync --extra test --extra live-test
uv run ruff format --check .
uv run ruff check .
uv run ty check src
uv run pytest -q
uv build
```

If `uv` is unavailable, create a virtual environment and use pip:

```bash
cd "$GK_LIB"
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test,live-test]'
ruff format --check .
ruff check .
ty check src
pytest -q
python -m pip wheel . --no-deps --wheel-dir dist
```

Do not proceed to live hardware while an offline test, lint check, type check,
or package build is failing.

## 3. Confirm basic communication without moving a DAC

Run this first. It must identify the intended device and leave all outputs
untouched:

```bash
cd "$GK_LIB"
uv run python - "$GK_PORT" <<'PY'
import sys
from gatekeeper import GateKeeper

port = sys.argv[1]
with GateKeeper(port) as gk:
    print("port:", gk.port)
    print("id:", gk.idn())
    print("ready:", gk.ready())
    print("serial:", gk.serial_number())
PY
```

Use `python` instead of `uv run python` if working in the activated pip virtual
environment. Save this output in the test notes.

## 4. Run the firmware's exact full suite through the library

Create a unique output directory so previous results are never overwritten:

```bash
cd "$GK_LIB"
export GATEKEEPER_FIRMWARE_TEST="$GK_FIRMWARE_TEST"
export GK_RESULTS="$GK_LIB/live_tests/test_outputs/library_live_$(date +%Y%m%d_%H%M%S)"
uv run gatekeeper-hardware-test --port "$GK_PORT" --output-dir "$GK_RESULTS"
```

The executable loads the firmware repository's suite at runtime. It replaces
only the suite's direct serial connection with
`gatekeeper.hardware_test.LibraryHarness`; the checks, command arguments,
plots, cleanup, JSON report, and Markdown report remain those of the firmware
suite.

Inspect both reports even if the command exits successfully:

```bash
cat "$GK_RESULTS/hardware_test_report.md"
python3 - "$GK_RESULTS/hardware_test_report.json" <<'PY'
import json
import sys

report = json.load(open(sys.argv[1], encoding="utf-8"))
print(report["summary"])
for check in report["checks"]:
    if check["status"] != "PASS":
        print(check["status"], check["name"], check["details"])
PY
```

Keep the JSON, Markdown, PNG plots, terminal output, and exit status together.

## 5. Exercise the public library API directly

The full suite validates its exact firmware checks through the library
transport. This additional smoke test validates the user-facing methods and
array shapes. It assumes DAC 0/1 are looped to ADC 0/1.

```bash
cd "$GK_LIB"
uv run python - "$GK_PORT" <<'PY'
import sys
import time

import numpy as np

from gatekeeper import GateKeeper

port = sys.argv[1]
gk = GateKeeper(port)
try:
    print(gk.idn())
    print("serial:", gk.serial_number())

    for channel in (0, 1):
        actual = gk.set_conversion_time(channel, 82e-6)
        print(f"ADC {channel} conversion time: {actual:.9g} s")

    gk.set_voltage(0, 0.1)
    gk.set_voltage(1, -0.05)
    time.sleep(0.05)
    loopback = gk.read_voltages([0, 1])
    print("static loopback:", loopback)
    np.testing.assert_allclose(loopback, [0.1, -0.05], atol=0.15)

    line = gk.dac_led_buffer_ramp(
        channels=[0],
        reads=[0],
        start=-0.1,
        stop=0.1,
        points=21,
        dac_interval=500e-6,
    )
    assert line.shape == (1, 21), line.shape
    assert np.isfinite(line).all()
    correlation = float(np.corrcoef(line[0], np.linspace(-0.1, 0.1, 21))[0, 1])
    print("1D correlation:", correlation)
    assert correlation > 0.9, correlation

    image = gk.dac_led_buffer_ramp_2d(
        fast=dict(ch=0, start=-0.1, stop=0.1, points=11),
        slow=dict(ch=1, start=-0.05, stop=0.05, points=5),
        reads=[0, 1],
        dac_interval=500e-6,
        snake=True,
    )
    print("2D shape:", image.shape)
    assert image.shape == (2, 55), image.shape
    assert np.isfinite(image).all()

    capture = gk.adc_read(
        reads=[0, 1],
        duration=0.02,
        conversion_time=250e-6,
    )
    print("ADC capture:", capture.data.shape, capture.sample_rate)
    assert capture.data.shape[0] == 2
    assert capture.data.shape[1] > 0
    assert np.isfinite(capture.data).all()

    waveform = np.array([[-0.05, 0.0, 0.05, 0.0]])
    awg_data = gk.awg_stream(
        channels=[0],
        reads=[0],
        waveform=waveform,
        rate=1_000,
        cycles=2,
    )
    print("AWG capture:", awg_data.shape)
    assert awg_data.shape == (1, 8), awg_data.shape
    assert np.isfinite(awg_data).all()
finally:
    try:
        gk.stop(settling_time=0.1)
    finally:
        for channel in (0, 1):
            try:
                gk.set_voltage(channel, 0.0)
            except Exception as error:
                print(f"WARNING: could not return DAC {channel} to 0 V: {error}")
        gk.close()
PY
```

Afterward, explicitly confirm DAC 0 and DAC 1 read back approximately 0 V.
For example:

```bash
uv run python - "$GK_PORT" <<'PY'
import sys
import numpy as np
from gatekeeper import GateKeeper

with GateKeeper(sys.argv[1]) as gk:
    final = np.array([gk.get_dac(0), gk.get_dac(1)])
    print("final DAC readback:", final)
    np.testing.assert_allclose(final, [0.0, 0.0], atol=0.001)
PY
```

## 6. Diagnose failures before editing

For every failure, save the exact command, response or exception, expected byte
count, received byte count, firmware version, and relevant report entry.
Classify it before changing code:

1. **Connection failure:** wrong port, permissions, another program holding the
   port, missing USB CDC driver, cable, or power.
2. **Loopback/hardware failure:** one channel consistently disagrees in both
   static and buffered measurements, or the upstream direct suite fails the
   same check.
3. **Firmware/library protocol mismatch:** a `FAILURE` response, wrong command
   field order, stale status line, incorrect integer-microsecond calculation,
   wrong binary sample count, or incorrect channel-major reshaping.
4. **Timing failure:** firmware reports a minimum interval or conversion-time
   violation. Compare with the current firmware implementation; do not simply
   increase timeouts or weaken assertions.
5. **Library result bug:** raw transfer succeeds but public return shapes,
   orientation, units, callbacks, or cleanup are wrong.

The firmware C++ command handlers and its live suite take precedence over stale
prose in old command documentation. The known current layouts use separate
arrays for DAC channels, starts, stops, and 2-D vectors rather than interleaved
triplets. Current boxcar firmware does not accept the obsolete conversion-skip
field.

If necessary, run the upstream firmware suite directly against the same port
and wiring as a diagnostic baseline. Do this only after saving the failed
library-backed results. If both suites fail the same check, investigate wiring,
firmware, or hardware before changing the library. If the direct suite passes
and the library-backed suite fails, treat it as a library defect.

## 7. Iterate carefully

For a confirmed library defect:

1. Add the smallest offline regression test that reproduces the bad command,
   framing, sample count, shape, or error handling.
2. Make the smallest readable fix. Keep the public API centered on
   `gk = GateKeeper(port)`; do not reintroduce device groups, LabRAD concepts,
   VISA layers, compatibility aliases, factories, managers, or generic wrapper
   classes.
3. Run formatting, lint, types, the complete offline suite, and the build.
4. Run the smallest relevant live test.
5. Rerun the complete library-backed firmware suite.
6. Rerun the direct public-API smoke test.

Do not hide a real failure by broadly increasing tolerances, swallowing serial
exceptions, clearing buffers without understanding the unread data, or copying
the firmware's 965-line suite into this repository. The live runner must keep
loading the authoritative firmware suite so the two cannot drift.

## Completion criteria

Work is complete only when:

- all offline checks and package builds pass;
- the library-backed firmware suite has zero failed checks;
- every warning is understood and documented;
- the public-API smoke test passes with the documented shapes and finite data;
- cleanup leaves the exercised DAC outputs at 0 V;
- any code fix has an offline regression test;
- the final report lists all edits, commands run, live report paths, remaining
  warnings, hardware/firmware identifiers, and the final git diff.

If physical hardware or wiring prevents completion, stop changing software and
report the precise repeated blocker, the evidence that distinguishes it from a
library defect, and the safest next physical action.

"""Shared implementation for the repository's PyPI upload scripts."""

from __future__ import annotations

import argparse
import ast
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def load_dotenv(path: Path) -> None:
    """Load the small subset of dotenv syntax needed for API tokens."""
    if not path.is_file():
        raise RuntimeError(f"Missing environment file: {path}")
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise RuntimeError(f"Invalid .env entry on line {line_number}")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key.isidentifier():
            raise RuntimeError(f"Invalid .env key on line {line_number}: {key!r}")
        if value[:1] in {"'", '"'}:
            try:
                value = ast.literal_eval(value)
            except (SyntaxError, ValueError) as error:
                raise RuntimeError(f"Invalid quoted .env value on line {line_number}") from error
            if not isinstance(value, str):
                raise RuntimeError(f"The .env value on line {line_number} must be text")
        os.environ.setdefault(key, value)


def _tool_commands() -> tuple[list[str], list[str]]:
    if shutil.which("uv"):
        return ["uv", "build"], ["uvx", "twine"]
    if importlib.util.find_spec("build") is None or importlib.util.find_spec("twine") is None:
        raise RuntimeError("Install uv, or run: python -m pip install build twine")
    return [sys.executable, "-m", "build"], [sys.executable, "-m", "twine"]


def upload(*, repository_url: str, token_key: str, label: str) -> int:
    parser = argparse.ArgumentParser(description=f"Build and upload gatekeeperlib to {label}.")
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="build and run Twine validation without uploading",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parent
    build_command, twine_command = _tool_commands()
    with tempfile.TemporaryDirectory(prefix="gatekeeperlib-upload-") as temporary:
        output_dir = Path(temporary)
        subprocess.run(
            [*build_command, "--out-dir", str(output_dir)],
            cwd=root,
            check=True,
        )
        distributions = sorted(
            path for path in output_dir.iterdir() if path.name.endswith((".whl", ".tar.gz", ".zip"))
        )
        if not distributions:
            raise RuntimeError("The package build did not produce any distributions")
        subprocess.run(
            [*twine_command, "check", *(str(path) for path in distributions)],
            cwd=root,
            check=True,
        )
        if args.check_only:
            print(f"Build and Twine checks passed; nothing was uploaded to {label}.")
            return 0

        load_dotenv(root / ".env")
        token = os.environ.get(token_key, "").strip()
        if not token:
            raise RuntimeError(f"Missing {token_key} in {root / '.env'}")
        environment = os.environ.copy()
        environment.update(
            {
                "TWINE_USERNAME": "__token__",
                "TWINE_PASSWORD": token,
                "TWINE_NON_INTERACTIVE": "1",
            }
        )
        print(f"Uploading {len(distributions)} distribution(s) to {label}...")
        subprocess.run(
            [
                *twine_command,
                "upload",
                "--repository-url",
                repository_url,
                *(str(path) for path in distributions),
            ],
            cwd=root,
            env=environment,
            check=True,
        )
    return 0

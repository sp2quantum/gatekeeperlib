#!/usr/bin/env python3
"""Build, validate, and upload gatekeeperlib to TestPyPI."""

from _upload_common import upload

if __name__ == "__main__":
    raise SystemExit(
        upload(
            repository_url="https://test.pypi.org/legacy/",
            token_key="test_pypi_key",
            label="TestPyPI",
        )
    )

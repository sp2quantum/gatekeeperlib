#!/usr/bin/env python3
"""Build, validate, and upload gatekeeperlib to PyPI."""

from _upload_common import upload

if __name__ == "__main__":
    raise SystemExit(
        upload(
            repository_url="https://upload.pypi.org/legacy/",
            token_key="pypi_key",
            label="PyPI",
        )
    )

"""Shared exception types."""

from __future__ import annotations

import shlex


class WanshapeError(RuntimeError):
    """User-facing error."""


class CommandError(WanshapeError):
    def __init__(self, command: list[str], returncode: int, output: str):
        self.command = command
        self.returncode = returncode
        self.output = output.strip()
        super().__init__(
            f"command failed ({returncode}): {shlex.join(command)}\n{self.output}"
        )

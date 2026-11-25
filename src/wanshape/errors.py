"""Shared exception types."""

from __future__ import annotations

import shlex


class WanshapeError(RuntimeError):
    """User-facing error (exit code 1)."""

    exit_code = 1


class UnsupportedTarget(WanshapeError):
    """The target can't be shaped this way, e.g. host networking (exit code 2)."""

    exit_code = 2


class PrerequisiteError(WanshapeError):
    """A tool, kernel feature or privilege is missing (exit code 3)."""

    exit_code = 3


class ConflictError(WanshapeError):
    """Foreign tc state or overlapping targets; --force may override (exit code 4)."""

    exit_code = 4


class CommandError(WanshapeError):
    def __init__(self, command: list[str], returncode: int, output: str):
        self.command = command
        self.returncode = returncode
        self.output = output.strip()
        super().__init__(
            f"command failed ({returncode}): {shlex.join(command)}\n{self.output}"
        )

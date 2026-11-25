"""Test doubles for command execution."""

from __future__ import annotations

import subprocess


class FakeBackend:
    """Returns canned output keyed by the command tail after the nsenter prefix."""

    def __init__(self, responses: dict[tuple[str, ...], str] | None = None):
        self.responses = responses or {}
        self.commands: list[list[str]] = []

    def command(self, args: list[str]) -> list[str]:
        return ["nsenter", "-U", "-n", "-t", "1", "--", *args]

    def run(self, args: list[str]) -> str:
        self.commands.append(list(args))
        return self.responses.get(tuple(args), "")

    def run_result(self, args: list[str]) -> subprocess.CompletedProcess:
        output = self.run(args)
        return subprocess.CompletedProcess(
            self.command(args), 0 if output else 1, output, ""
        )

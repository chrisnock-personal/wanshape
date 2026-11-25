"""Command execution backends: enter the target's network namespace and run host tools."""

from __future__ import annotations

import subprocess
from typing import Sequence

from .errors import CommandError, PrerequisiteError, WanshapeError
from .target import Target


class NsenterBackend:
    """Run host binaries inside the container's netns, without entering its mount ns."""

    def __init__(self, target: Target):
        self.target = target

    @property
    def prefix(self) -> list[str]:
        # Rootless targets: enter the user namespace as its owner (uid 0 inside it),
        # then the netns. Validated on pasta, bridge and --userns=keep-id.
        if self.target.rootless:
            return ["nsenter", "-U", "-n", "-t", str(self.target.pid), "--"]
        return ["nsenter", "-n", "-t", str(self.target.pid), "--"]

    def command(self, args: Sequence[str]) -> list[str]:
        return self.prefix + list(args)

    def run_result(self, args: Sequence[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            self.command(args), text=True, capture_output=True
        )

    def run(self, args: Sequence[str]) -> str:
        result = self.run_result(args)
        if result.returncode != 0:
            raise CommandError(
                self.command(args), result.returncode, result.stdout + result.stderr
            )
        return result.stdout


Backend = NsenterBackend


def get_backend(target: Target, requested: str = "auto") -> Backend:
    if requested == "sidecar":
        raise WanshapeError(
            "the sidecar backend is not implemented in this prototype; "
            "use --backend nsenter"
        )
    if requested not in ("auto", "nsenter"):
        raise WanshapeError(f"unknown backend {requested!r}")
    if not target.rootless and not _is_root():
        raise PrerequisiteError(
            "target is rootful: run wanshape as root or use --backend sidecar (phase 2)"
        )
    return NsenterBackend(target)


def _is_root() -> bool:
    import os

    return os.geteuid() == 0

"""--duration: clear shaping automatically with a one-shot systemd timer.

The timer runs `wanshape clear ...` (or `clear-host`) through systemd-run:
the user's systemd for rootless containers, the system's when running as
root. Each target has one timer unit, named after the target, so applying
again replaces it and clearing cancels it.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .errors import PrerequisiteError, WanshapeError

_DURATION = re.compile(r"^(\d+(?:\.\d+)?)(s|m|h)?$")
# '.' is replaced too: systemd would read "web.service" as a unit type suffix.
_UNIT_SAFE = re.compile(r"[^A-Za-z0-9_-]")
_LEGACY_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")   # the scheme before v0.4


def parse_duration(text: str) -> int:
    """'90', '90s', '5m', '1.5h' -> whole seconds (at least 1)."""
    match = _DURATION.match(text.strip())
    if not match:
        raise WanshapeError(f"invalid duration {text!r} (e.g. 90s, 5m, 1h)")
    value = float(match.group(1)) * {"s": 1, "m": 60, "h": 3600}[match.group(2) or "s"]
    if value < 1:
        raise WanshapeError("duration must be at least 1 second")
    return int(round(value))


def unit_name(kind: str, name: str) -> str:
    """One systemd unit per target. The readable part can repeat ("web.1" and
    "web_1" both give "web_1"), so a short hash of the exact name keeps them apart."""
    digest = hashlib.sha1(f"{kind}:{name}".encode()).hexdigest()[:8]
    return f"wanshape-clear-{kind}-{_UNIT_SAFE.sub('_', name)}-{digest}"


def legacy_unit_names(kind: str, name: str) -> list[str]:
    """Unit names earlier versions used for this target, to cancel after an upgrade."""
    return [f"wanshape-clear-{kind}-{_LEGACY_UNSAFE.sub('_', name)}"]


def cancel_legacy(kind: str, name: str) -> None:
    for unit in legacy_unit_names(kind, name):
        cancel(unit)


def _user() -> bool:
    return os.geteuid() != 0


def _systemctl(*args: str) -> list[str]:
    return ["systemctl", *(["--user"] if _user() else []), *args]


def check_available() -> None:
    """Fail before anything is changed if a timer can't be scheduled."""
    if not shutil.which("systemd-run"):
        raise PrerequisiteError("--duration needs systemd-run, which isn't installed")
    state = subprocess.run(_systemctl("is-system-running"), text=True,
                           capture_output=True).stdout.strip()
    if state not in ("running", "degraded"):
        where = "your user's systemd" if _user() else "systemd"
        raise PrerequisiteError(f"--duration needs {where} to be running (it is {state!r})")


def schedule(unit: str, seconds: int, clear_args: list[str]) -> None:
    """Run `wanshape <clear_args>` once, `seconds` from now."""
    cancel(unit)
    package_root = str(Path(__file__).resolve().parent.parent)
    command = [
        "systemd-run", *(["--user"] if _user() else []), "--quiet",
        f"--unit={unit}", f"--on-active={seconds}s", "--timer-property=AccuracySec=1s",
        f"--setenv=PYTHONPATH={package_root}",
        *[f"--setenv={k}={os.environ[k]}" for k in ("XDG_STATE_HOME", "XDG_CONFIG_HOME")
          if k in os.environ],
        sys.executable, "-m", "wanshape.cli", *clear_args,
    ]
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode != 0:
        raise WanshapeError(f"could not schedule the clear: {result.stderr.strip()}")


def cancel(unit: str) -> None:
    """Stop a pending timer, if any (no error when there is none, or no systemd)."""
    try:
        subprocess.run(_systemctl("stop", f"{unit}.timer"), capture_output=True)
        subprocess.run(_systemctl("reset-failed", f"{unit}.service"), capture_output=True)
    except OSError:
        pass   # no systemctl: then no timer can have been scheduled either


def until(seconds: int) -> int:
    return int(time.time()) + seconds


def remaining(until_epoch: int | str | None) -> str | None:
    """'clears in 4m12s', or None when there is no time limit."""
    if until_epoch in (None, "", "0"):
        return None
    left = int(until_epoch) - int(time.time())
    if left <= 0:
        return "clearing now"
    minutes, secs = divmod(left, 60)
    hours, minutes = divmod(minutes, 60)
    text = (f"{hours}h{minutes:02d}m" if hours else f"{minutes}m{secs:02d}s" if minutes
            else f"{secs}s")
    return f"clears in {text}"

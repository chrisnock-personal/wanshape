"""Built-in and user-defined WAN impairment profiles."""

from __future__ import annotations

import os
import re
import shlex
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

BUILTIN_TOML = """
[profiles.office]
description = "Corporate LAN"
reorder_on_jitter = false
[profiles.office.up]
delay = "5ms"
jitter = "1ms"
distribution = "normal"
loss = "0%"
rate = "100mbit"
[profiles.office.down]
delay = "5ms"
jitter = "1ms"
distribution = "normal"
loss = "0%"
rate = "100mbit"

[profiles.4g]
description = "Good LTE"
reorder_on_jitter = false
[profiles.4g.up]
delay = "25ms"
jitter = "10ms"
distribution = "normal"
loss = "0.1%"
rate = "5mbit"
[profiles.4g.down]
delay = "25ms"
jitter = "10ms"
distribution = "normal"
loss = "0.1%"
rate = "10mbit"

[profiles.3g]
description = "Poor 3G"
reorder_on_jitter = true
[profiles.3g.up]
delay = "150ms"
jitter = "30ms"
distribution = "normal"
loss = "1%"
rate = "768kbit"
[profiles.3g.down]
delay = "150ms"
jitter = "30ms"
distribution = "normal"
loss = "1%"
rate = "1600kbit"

[profiles.satellite]
description = "GEO satellite"
reorder_on_jitter = false
[profiles.satellite.up]
delay = "300ms"
jitter = "20ms"
distribution = "normal"
loss = "0.5%"
rate = "1mbit"
[profiles.satellite.down]
delay = "300ms"
jitter = "20ms"
distribution = "normal"
loss = "0.5%"
rate = "5mbit"

[profiles.bad-wifi]
description = "Congested Wi-Fi"
reorder_on_jitter = true
[profiles.bad-wifi.up]
delay = "40ms"
jitter = "60ms"
distribution = "pareto"
loss = "5%"
rate = "1mbit"
[profiles.bad-wifi.down]
delay = "40ms"
jitter = "60ms"
distribution = "pareto"
loss = "5%"
rate = "2mbit"

[profiles.lossy-sip]
description = "VoIP with bursty congestion loss"
reorder_on_jitter = false
[profiles.lossy-sip.up]
delay = "50ms"
jitter = "15ms"
distribution = "normal"
loss_model = "gemodel 1% 25% 100% 0%"
rate = "512kbit"
[profiles.lossy-sip.down]
delay = "50ms"
jitter = "15ms"
distribution = "normal"
loss_model = "gemodel 1% 25% 100% 0%"
rate = "512kbit"

[profiles.leo-satellite]
description = "Low-orbit satellite (Starlink-style)"
reorder_on_jitter = false
[profiles.leo-satellite.up]
delay = "22ms"
jitter = "8ms"
distribution = "normal"
loss_model = "gemodel 0.1% 20% 100% 0%"
rate = "15mbit"
[profiles.leo-satellite.down]
delay = "22ms"
jitter = "8ms"
distribution = "normal"
loss_model = "gemodel 0.1% 20% 100% 0%"
rate = "100mbit"

[profiles.lte-congested]
description = "Busy LTE cell: bursty scheduling"
reorder_on_jitter = false
[profiles.lte-congested.up]
delay = "40ms"
jitter = "20ms"
distribution = "normal"
loss_model = "gemodel 0.25% 25% 100% 0%"
slot = "5ms 15ms"
rate = "1mbit"
[profiles.lte-congested.down]
delay = "40ms"
jitter = "20ms"
distribution = "normal"
loss_model = "gemodel 0.25% 25% 100% 0%"
slot = "5ms 15ms"
rate = "5mbit"

[profiles.2g-edge]
description = "GPRS/EDGE"
reorder_on_jitter = true
[profiles.2g-edge.up]
delay = "200ms"
jitter = "50ms"
distribution = "normal"
loss = "1%"
rate = "100kbit"
[profiles.2g-edge.down]
delay = "200ms"
jitter = "50ms"
distribution = "normal"
loss = "1%"
rate = "200kbit"

[profiles.dsl]
description = "ADSL2+ broadband"
reorder_on_jitter = false
[profiles.dsl.up]
delay = "15ms"
jitter = "2ms"
distribution = "normal"
rate = "1mbit"
[profiles.dsl.down]
delay = "15ms"
jitter = "2ms"
distribution = "normal"
rate = "16mbit"

[profiles.transatlantic]
description = "Long-haul but clean (London-New York)"
reorder_on_jitter = false
[profiles.transatlantic.up]
delay = "32ms"
jitter = "1ms"
distribution = "normal"
[profiles.transatlantic.down]
delay = "32ms"
jitter = "1ms"
distribution = "normal"

[profiles.wifi-roaming]
description = "Wi-Fi while moving between access points"
reorder_on_jitter = true
[profiles.wifi-roaming.up]
delay = "10ms"
jitter = "10ms"
delay_correlation = "50%"
distribution = "pareto"
loss_model = "gemodel 0.5% 15% 100% 0%"
rate = "10mbit"
[profiles.wifi-roaming.down]
delay = "10ms"
jitter = "10ms"
delay_correlation = "50%"
distribution = "pareto"
loss_model = "gemodel 0.5% 15% 100% 0%"
rate = "20mbit"

[profiles.voip-poor]
description = "VoIP, poor: noticeable but usable"
reorder_on_jitter = false
[profiles.voip-poor.up]
delay = "100ms"
jitter = "30ms"
distribution = "normal"
loss_model = "gemodel 0.5% 25% 100% 0%"
[profiles.voip-poor.down]
delay = "100ms"
jitter = "30ms"
distribution = "normal"
loss_model = "gemodel 0.5% 25% 100% 0%"

[profiles.voip-bad]
description = "VoIP, bad: past the ITU-T G.114 comfort limit"
reorder_on_jitter = false
[profiles.voip-bad.up]
delay = "200ms"
jitter = "60ms"
distribution = "normal"
loss_model = "gemodel 1.5% 25% 100% 0%"
[profiles.voip-bad.down]
delay = "200ms"
jitter = "60ms"
distribution = "normal"
loss_model = "gemodel 1.5% 25% 100% 0%"
"""

_TIME = re.compile(r"^\d+(?:\.\d+)?(?:us|ms|s)$")
_PERCENT = re.compile(r"^\d+(?:\.\d+)?%$")
_RATE = re.compile(r"^\d+(?:\.\d+)?(?:bit|kbit|mbit|gbit)$")


class ProfileError(ValueError):
    """Invalid profile definition."""


@dataclass
class Direction:
    delay: str | None = None
    jitter: str | None = None
    distribution: str | None = None
    loss: str | None = None
    loss_model: str | None = None
    rate: str | None = None
    delay_correlation: str | None = None  # "25%": each delay depends on the last
    reorder: str | None = None            # "25%" or "25% 50%": sent at once, ahead of others
    duplicate: str | None = None          # "1%"
    corrupt: str | None = None            # "0.1%": one flipped bit
    slot: str | None = None               # "10ms 30ms": released in bursts (Wi-Fi, LTE)
    seed: str | None = None               # fixed random seed for repeatable runs
    raw: str | None = None                # further netem arguments, appended as given

    def validate(self, label: str) -> None:
        if self.loss and self.loss_model:
            raise ProfileError(f"{label}: loss and loss_model are mutually exclusive")
        for name, value, pattern in (
            ("delay", self.delay, _TIME),
            ("jitter", self.jitter, _TIME),
            ("loss", self.loss, _PERCENT),
            ("rate", self.rate, _RATE),
        ):
            if value is not None and not pattern.match(str(value)):
                raise ProfileError(f"{label}: invalid {name} value {value!r}")
        if self.loss:
            pct = float(self.loss.rstrip("%"))
            if not 0 <= pct <= 100:
                raise ProfileError(f"{label}: loss must be between 0% and 100%")
        if self.loss_model and not re.match(
            r"^(?:gemodel|state)\s+\d+(?:\.\d+)?%(?:\s+\d+(?:\.\d+)?%)*$",
            self.loss_model,
        ):
            raise ProfileError(f"{label}: invalid loss_model {self.loss_model!r}")
        if self.distribution not in (None, "uniform", "normal", "pareto", "paretonormal"):
            raise ProfileError(f"{label}: unknown distribution {self.distribution!r}")
        if self.distribution == "normal" and self.delay and self.jitter:
            if _to_ms(self.jitter) > _to_ms(self.delay):
                raise ProfileError(f"{label}: jitter must be <= delay for normal distribution")
        if (self.jitter or self.distribution) and not self.delay:
            raise ProfileError(f"{label}: jitter/distribution require a delay")
        for name in ("delay_correlation", "duplicate", "corrupt"):
            value = getattr(self, name)
            if value is not None and not (_PERCENT.match(str(value))
                                          and 0 <= float(str(value)[:-1]) <= 100):
                raise ProfileError(f"{label}: invalid {name} {value!r} (a percentage)")
        if self.delay_correlation and not self.jitter:
            raise ProfileError(f"{label}: delay_correlation needs jitter")
        if self.reorder is not None:
            parts = str(self.reorder).split()
            if not 1 <= len(parts) <= 2 or not all(
                    _PERCENT.match(x) and float(x[:-1]) <= 100 for x in parts):
                raise ProfileError(f"{label}: invalid reorder {self.reorder!r} (e.g. 25% 50%)")
            if not self.delay:
                raise ProfileError(f"{label}: reorder needs a delay (netem sends the "
                                   "reordered packets at once and delays the rest)")
        if self.slot is not None:
            parts = str(self.slot).split()
            if not 1 <= len(parts) <= 2 or not all(_TIME.match(x) for x in parts):
                raise ProfileError(f"{label}: invalid slot {self.slot!r} (e.g. 10ms 30ms)")
        if self.seed is not None and not str(self.seed).isdigit():
            raise ProfileError(f"{label}: seed must be a whole number")
        if self.raw is not None and not str(self.raw).strip():
            raise ProfileError(f"{label}: raw is empty")

    def netem_args(self, include_rate: bool = True) -> list[str]:
        args: list[str] = []
        if self.delay:
            args += ["delay", self.delay]
            if self.jitter:
                args.append(self.jitter)
                if self.delay_correlation:
                    args.append(self.delay_correlation)
            if self.distribution:
                args += ["distribution", self.distribution]
        if self.loss_model:
            # tc expects the model and each probability as separate arguments.
            args += ["loss", *self.loss_model.split()]
        elif self.loss:
            args += ["loss", self.loss]
        if self.duplicate:
            args += ["duplicate", self.duplicate]
        if self.corrupt:
            args += ["corrupt", self.corrupt]
        if self.reorder:
            args += ["reorder", *str(self.reorder).split()]
        if self.rate and include_rate:
            args += ["rate", self.rate]
        if self.slot:
            args += ["slot", *str(self.slot).split()]
        if self.seed:
            args += ["seed", str(self.seed)]
        args += ["limit", "1000"]
        if self.raw:
            args += shlex.split(str(self.raw))
        return args


@dataclass
class Profile:
    name: str
    description: str = ""
    reorder_on_jitter: bool = True
    up: Direction = field(default_factory=Direction)
    down: Direction = field(default_factory=Direction)
    rate_mode: str = "netem"  # "accurate": HTB does the rate, netem below it

    def validate(self) -> None:
        if self.rate_mode not in ("netem", "accurate"):
            raise ProfileError(f"{self.name}: rate_mode must be netem or accurate")
        self.up.validate(f"{self.name}.up")
        self.down.validate(f"{self.name}.down")


def _to_ms(value: str) -> float:
    for suffix, factor in (("us", 0.001), ("ms", 1.0), ("s", 1000.0)):
        if value.endswith(suffix):
            return float(value[: -len(suffix)]) * factor
    raise ProfileError(f"invalid time {value!r}")


_DIRECTION_FIELDS = set(Direction.__dataclass_fields__)
_REMOVED = {
    "loss_correlation": "netem drops almost nothing with simple loss correlation "
                        "(measured: loss 3% 25% lost 0.03%); use loss_model "
                        "= \"gemodel P R 100% 0%\" for bursty loss",
}


def _direction_from(label: str, raw: dict) -> Direction:
    if not isinstance(raw, dict):
        raise ProfileError(f"{label}: expected a table of settings")
    for key in raw:
        if key in _REMOVED:
            raise ProfileError(f"{label}: {key} is not supported: {_REMOVED[key]}")
        if key not in _DIRECTION_FIELDS:
            raise ProfileError(f"{label}: unknown setting {key!r}")
    return Direction(**{k: str(v) for k, v in raw.items()})


_PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def _profile_from(name: str, raw: dict) -> Profile:
    if not _PROFILE_NAME.match(name):
        raise ProfileError(f"invalid profile name {name!r}: letters, digits, '.', '_', '-'")
    unknown = set(raw) - {"description", "reorder_on_jitter", "rate_mode", "up", "down"}
    if unknown:
        raise ProfileError(f"{name}: unknown setting(s) {', '.join(sorted(unknown))}")
    return Profile(
        name=name,
        description=str(raw.get("description", "")),
        reorder_on_jitter=bool(raw.get("reorder_on_jitter", True)),
        rate_mode=str(raw.get("rate_mode", "netem")),
        up=_direction_from(f"{name}.up", raw.get("up", {})),
        down=_direction_from(f"{name}.down", raw.get("down", {})),
    )


def _config_home() -> Path:
    """$XDG_CONFIG_HOME, else ~/.config; under sudo, the invoking user's ~/.config."""
    if os.environ.get("XDG_CONFIG_HOME"):
        return Path(os.environ["XDG_CONFIG_HOME"])
    sudo_user = os.environ.get("SUDO_USER")
    if os.geteuid() == 0 and sudo_user and sudo_user != "root":
        try:
            import pwd
            return Path(pwd.getpwnam(sudo_user).pw_dir) / ".config"
        except KeyError:
            pass
    return Path.home() / ".config"


def load_profiles() -> dict[str, Profile]:
    """Load built-ins, then overlay the user's config file."""
    data = tomllib.loads(BUILTIN_TOML)
    config = _config_home()
    user_file = config / "wanshape" / "profiles.toml"
    user_names: set[str] = set()
    if user_file.is_file():
        with user_file.open("rb") as fh:
            user_data = tomllib.load(fh).get("profiles", {})
        for name, raw in user_data.items():
            if not _PROFILE_NAME.match(str(name)):
                # Skip it rather than break every command, built-ins included.
                print(f"wanshape: skipping profile {name!r} in {user_file}: names use "
                      "letters, digits, '.', '_' and '-'", file=sys.stderr)
                continue
            data.setdefault("profiles", {})[name] = raw
            user_names.add(name)
    profiles: dict[str, Profile] = {}
    for name, raw in data.get("profiles", {}).items():
        try:
            profile = _profile_from(name, raw)
            profile.validate()
        except ProfileError as exc:
            if name not in user_names:
                raise   # a built-in must be valid
            # A bad user profile is skipped, not fatal for every command.
            print(f"wanshape: skipping profile {name!r} in {user_file}: {exc}",
                  file=sys.stderr)
            continue
        profiles[name] = profile
    return profiles

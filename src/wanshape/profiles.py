"""Built-in and user-defined WAN impairment profiles."""

from __future__ import annotations

import os
import re
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

    def netem_args(self) -> list[str]:
        args: list[str] = []
        if self.delay:
            args += ["delay", self.delay]
            if self.jitter:
                args.append(self.jitter)
            if self.distribution:
                args += ["distribution", self.distribution]
        if self.loss_model:
            # tc expects the model and each probability as separate arguments.
            args += ["loss", *self.loss_model.split()]
        elif self.loss:
            args += ["loss", self.loss]
        if self.rate:
            args += ["rate", self.rate]
        args += ["limit", "1000"]
        return args


@dataclass
class Profile:
    name: str
    description: str = ""
    reorder_on_jitter: bool = True
    up: Direction = field(default_factory=Direction)
    down: Direction = field(default_factory=Direction)

    def validate(self) -> None:
        self.up.validate(f"{self.name}.up")
        self.down.validate(f"{self.name}.down")


def _to_ms(value: str) -> float:
    number = float(value[:-2])
    return number if value.endswith("ms") else number * (1000 if value.endswith("s") else 0.001)


def _profile_from(name: str, raw: dict) -> Profile:
    return Profile(
        name=name,
        description=str(raw.get("description", "")),
        reorder_on_jitter=bool(raw.get("reorder_on_jitter", True)),
        up=Direction(**{k: str(v) for k, v in raw.get("up", {}).items()}),
        down=Direction(**{k: str(v) for k, v in raw.get("down", {}).items()}),
    )


def load_profiles() -> dict[str, Profile]:
    """Load built-ins, then overlay the user's config file."""
    data = tomllib.loads(BUILTIN_TOML)
    config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    user_file = config / "wanshape" / "profiles.toml"
    if user_file.is_file():
        with user_file.open("rb") as fh:
            user_data = tomllib.load(fh).get("profiles", {})
        for name, raw in user_data.items():
            data.setdefault("profiles", {})[name] = raw
    profiles = {
        name: _profile_from(name, raw) for name, raw in data.get("profiles", {}).items()
    }
    for profile in profiles.values():
        profile.validate()
    return profiles

"""Host mode: shape the traffic of a host-network container by its local ports.

A host-network container has no network namespace of its own, so its traffic is
picked out on a host interface by the local port it uses. Nothing replaces the
interface's root qdisc: `clsact` + `flower` filters redirect only matching
packets to a pair of IFB devices that carry the netem qdiscs.

    egress  (upload):   src_port in ranges -> mirred -> wsh<N>up (netem up)
    ingress (download): dst_port in ranges -> mirred -> wsh<N>dn (netem down)

Each target takes a slot N (0-9). Its filters use the prefs
49300 + 100*N .. +99 on both hooks, and the slot's metadata (target name,
profile, interface, ports, direction, whether wanshape added clsact) is stored
in the up IFB's interface alias. The kernel therefore holds everything that
status and clear need; there is no state file in host mode, and the state can
be read without root.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Sequence

from .errors import (
    CommandError,
    ConflictError,
    PrerequisiteError,
    UnsupportedTarget,
    WanshapeError,
)
from .inspector import HANDLE
from .profiles import Profile

PREF_BASE = 49300
PREFS_PER_SLOT = 100
MAX_SLOTS = 10
ALIAS_PREFIX = "wanshape-host"
PORTS_LABEL = "wanshape.ports"
_PROTOCOLS = (("ip", "udp"), ("ip", "tcp"), ("ipv6", "udp"), ("ipv6", "tcp"))
MAX_RANGES = PREFS_PER_SLOT // len(_PROTOCOLS)
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
_IFB = re.compile(r"^wsh(\d)(up|dn)$")
_PREF = re.compile(r"\bpref\s+(\d+)\b")
_DEV = re.compile(r"\bdev\s+(\S+)")

Command = list[str]


# --------------------------------------------------------------------- ports


@dataclass(frozen=True, order=True)
class PortRange:
    low: int
    high: int

    def __str__(self) -> str:
        return str(self.low) if self.low == self.high else f"{self.low}-{self.high}"

    def overlaps(self, other: "PortRange") -> bool:
        return self.low <= other.high and other.low <= self.high


def parse_ports(spec: str) -> list[PortRange]:
    """Parse "5060,10000-20000" into sorted, merged ranges."""
    ranges: list[PortRange] = []
    for part in spec.replace(" ", "").split(","):
        if not part:
            continue
        match = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not match:
            raise WanshapeError(f"invalid port or range {part!r} (use e.g. 5060,10000-20000)")
        low = int(match.group(1))
        high = int(match.group(2) or low)
        if not 1 <= low <= high <= 65535:
            raise WanshapeError(f"invalid port range {part!r}: need 1 <= low <= high <= 65535")
        ranges.append(PortRange(low, high))
    if not ranges:
        raise WanshapeError("no ports given")
    return _merge(ranges)


def _merge(ranges: list[PortRange]) -> list[PortRange]:
    merged: list[PortRange] = []
    for r in sorted(ranges):
        if merged and r.low <= merged[-1].high + 1:
            merged[-1] = PortRange(merged[-1].low, max(merged[-1].high, r.high))
        else:
            merged.append(r)
    return merged


def format_ports(ranges: Sequence[PortRange]) -> str:
    return ",".join(str(r) for r in ranges)


def ports_from_container(obj: dict) -> tuple[list[PortRange], str]:
    """Work out a container's ports: the `wanshape.ports` label, else its env.

    From the environment: every variable ending in SIP_PORT is a single port,
    and every <X>PORT_LOW / <X>PORT_HIGH pair (RTP_PORT_LOW/HIGH,
    LOAD_RTP_PORT_LOW/HIGH, ...) is a range. Returns the ranges and a
    description of where they came from.
    """
    config = obj.get("Config", {}) or {}
    labels = config.get("Labels") or {}
    if labels.get(PORTS_LABEL):
        return parse_ports(labels[PORTS_LABEL]), f"label {PORTS_LABEL}"
    env: dict[str, str] = {}
    for item in config.get("Env") or []:
        key, _, value = str(item).partition("=")
        env[key] = value
    found: list[PortRange] = []
    used: list[str] = []
    for key in sorted(env):
        value = env[key].strip()
        if key.endswith("SIP_PORT") and value.isdigit():
            found.append(PortRange(int(value), int(value)))
            used.append(key)
        elif key.endswith("PORT_LOW") and "PORT" in key:
            high_key = key[: -len("LOW")] + "HIGH"
            high = env.get(high_key, "").strip()
            if value.isdigit() and high.isdigit() and int(value) <= int(high):
                found.append(PortRange(int(value), int(high)))
                used.append(f"{key}..{high_key}")
    found = [r for r in found if 1 <= r.low <= r.high <= 65535]
    if not found:
        raise WanshapeError(
            "cannot work out the container's ports: no *SIP_PORT or *PORT_LOW/HIGH "
            f"variables and no {PORTS_LABEL} label; pass --ports explicitly"
        )
    return _merge(found), "environment " + ", ".join(used)


# ------------------------------------------------------------------- backend


class HostBackend:
    """Run ip/tc in the host's own network namespace."""

    def command(self, args: Sequence[str]) -> list[str]:
        return list(args)

    def run_result(self, args: Sequence[str]) -> subprocess.CompletedProcess:
        return subprocess.run(list(args), text=True, capture_output=True)

    def run(self, args: Sequence[str]) -> str:
        result = self.run_result(args)
        if result.returncode != 0:
            raise CommandError(list(args), result.returncode, result.stdout + result.stderr)
        return result.stdout


def require_root() -> None:
    if os.geteuid() != 0:
        raise PrerequisiteError(
            "host mode changes a host interface and needs root: run it with sudo "
            "(add --dry-run to see the commands without root)"
        )


# --------------------------------------------------------------- live state


@dataclass
class HostSlot:
    slot: int
    name: str
    profile: str
    iface: str
    ports: list[PortRange]
    direction: str
    added_clsact: bool

    @property
    def up(self) -> str:
        return f"wsh{self.slot}up"

    @property
    def down(self) -> str:
        return f"wsh{self.slot}dn"

    def alias(self) -> str:
        return (
            f"{ALIAS_PREFIX} name={self.name} profile={self.profile} iface={self.iface} "
            f"ports={format_ports(self.ports)} dir={self.direction} "
            f"clsact={int(self.added_clsact)}"
        )


def slot_prefs(slot: int) -> range:
    start = PREF_BASE + slot * PREFS_PER_SLOT
    return range(start, start + PREFS_PER_SLOT)


def parse_alias(slot: int, alias: str) -> HostSlot | None:
    if not alias.startswith(ALIAS_PREFIX + " "):
        return None
    fields = dict(
        part.split("=", 1) for part in alias[len(ALIAS_PREFIX) + 1 :].split() if "=" in part
    )
    try:
        return HostSlot(
            slot=slot,
            name=fields["name"],
            profile=fields.get("profile", "unknown"),
            iface=fields["iface"],
            ports=parse_ports(fields["ports"]),
            direction=fields.get("dir", "both"),
            added_clsact=fields.get("clsact") == "1",
        )
    except (KeyError, WanshapeError):
        return None


@dataclass
class IfaceState:
    clsact: bool = False
    egress_prefs: set[int] = field(default_factory=set)
    ingress_prefs: set[int] = field(default_factory=set)

    def prefs(self, hook: str) -> set[int]:
        return self.egress_prefs if hook == "egress" else self.ingress_prefs

    def foreign_filters(self) -> bool:
        ours = range(PREF_BASE, PREF_BASE + MAX_SLOTS * PREFS_PER_SLOT)
        return any(p not in ours for p in self.egress_prefs | self.ingress_prefs)


@dataclass
class HostLive:
    slots: dict[int, HostSlot] = field(default_factory=dict)
    ifbs: set[str] = field(default_factory=set)
    ifaces: dict[str, IfaceState] = field(default_factory=dict)

    def by_name(self, name: str) -> HostSlot | None:
        return next((s for s in self.slots.values() if s.name == name), None)

    def iface(self, name: str) -> IfaceState:
        return self.ifaces.setdefault(name, IfaceState())


def inspect_host(backend, ifaces: Sequence[str] = ()) -> HostLive:
    """Read wanshape's host-mode state from the kernel (no root needed)."""
    live = HostLive()
    raw = backend.run(["ip", "-j", "-d", "link", "show", "type", "ifb"]).strip() or "[]"
    try:
        links = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WanshapeError(f"cannot parse `ip -j link` output: {exc}") from exc
    for link in links:
        name = link.get("ifname", "")
        match = _IFB.match(name)
        if not match:
            continue
        live.ifbs.add(name)
        if match.group(2) == "up":
            slot = parse_alias(int(match.group(1)), link.get("ifalias", "") or "")
            if slot:
                live.slots[slot.slot] = slot
    wanted = set(ifaces) | {s.iface for s in live.slots.values()}
    for iface in sorted(wanted):
        state = live.iface(iface)
        if backend.run_result(["ip", "link", "show", "dev", iface]).returncode != 0:
            continue
        state.clsact = "clsact" in backend.run(["tc", "qdisc", "show", "dev", iface])
        if state.clsact:
            for hook in ("egress", "ingress"):
                text = backend.run(["tc", "filter", "show", "dev", iface, hook])
                state.prefs(hook).update(int(p) for p in _PREF.findall(text))
    return live


def resolve_host_iface(backend, iface: str | None = None, via: str | None = None) -> str:
    if iface:
        if backend.run_result(["ip", "link", "show", "dev", iface]).returncode != 0:
            raise WanshapeError(f"no such host interface {iface!r}")
        found = iface
    elif via:
        result = backend.run_result(["ip", "-o", "route", "get", via])
        match = _DEV.search(result.stdout) if result.returncode == 0 else None
        if not match:
            raise WanshapeError(f"cannot find the host interface towards {via!r}")
        found = match.group(1)
    else:
        output = backend.run(["ip", "-o", "route", "show", "default"])
        match = _DEV.search(output)
        if not match:
            raise WanshapeError("no default route on the host; pass --iface or --via")
        found = match.group(1)
    if found == "lo":
        raise UnsupportedTarget(
            "traffic to that address stays on loopback, which host mode can't shape"
        )
    return found


# ------------------------------------------------------------------ planning


@dataclass
class HostPlan:
    slot: HostSlot
    commands: list[Command] = field(default_factory=list)
    rollback: list[Command] = field(default_factory=list)


def build_host_apply(
    name: str,
    profile: Profile,
    ports: list[PortRange],
    iface: str,
    direction: str,
    live: HostLive,
) -> HostPlan:
    if direction not in ("egress", "ingress", "both"):
        raise WanshapeError(f"invalid direction {direction!r}")
    if not _NAME.match(name):
        raise WanshapeError(f"invalid target name {name!r}")
    if len(ports) > MAX_RANGES:
        raise WanshapeError(f"too many port ranges ({len(ports)}; at most {MAX_RANGES})")

    existing = live.by_name(name)
    for other in live.slots.values():
        if other is existing or other.iface != iface:
            continue
        clash = [f"{a}/{b}" for a in ports for b in other.ports if a.overlaps(b)]
        if clash:
            raise ConflictError(
                f"ports overlap host target {other.name!r} on {iface} ({', '.join(clash)}); "
                f"clear it first with `wanshape clear-host {other.name}`"
            )

    commands: list[Command] = []
    state = live.iface(iface)
    clsact_present = state.clsact
    if existing:
        # Re-apply: take down the old shaping, keep its slot, and keep clsact
        # on the same interface (with the record of who added it).
        same_iface = existing.iface == iface
        commands += build_host_clear(existing, live, keep_clsact=same_iface)
        slot_no = existing.slot
        added_clsact = existing.added_clsact if same_iface else False
    else:
        slot_no = _free_slot(live, iface)
        added_clsact = False

    if not clsact_present:
        added_clsact = True
    elif any(s.added_clsact for s in live.slots.values()
             if s.iface == iface and s is not existing):
        # Another wanshape target added this clsact. Share the ownership, so
        # whichever target is cleared last removes it, whatever the order.
        added_clsact = True
    slot = HostSlot(
        slot=slot_no, name=name, profile=profile.name, iface=iface,
        ports=list(ports), direction=direction, added_clsact=added_clsact,
    )
    alias = slot.alias()
    if len(alias.encode()) > 255:
        raise WanshapeError("too many port ranges to record; merge some of them")

    fresh: list[Command] = []
    for dev, spec in ((slot.up, profile.up), (slot.down, profile.down)):
        fresh += [
            ["ip", "link", "add", dev, "type", "ifb"],
            ["ip", "link", "set", "dev", dev, "up"],
            ["tc", "qdisc", "add", "dev", dev, "root", "handle", HANDLE,
             "netem", *spec.netem_args()],
        ]
        if not profile.reorder_on_jitter:
            fresh.append(["tc", "qdisc", "add", "dev", dev, "parent", f"{HANDLE}1",
                          "pfifo", "limit", "1000"])
    fresh.append(["ip", "link", "set", "dev", slot.up, "alias", alias])
    if not clsact_present:
        fresh.append(["tc", "qdisc", "add", "dev", iface, "clsact"])
    for hook, key, ifb in (("egress", "src_port", slot.up), ("ingress", "dst_port", slot.down)):
        if direction not in (hook, "both"):
            continue
        prefs = iter(slot_prefs(slot.slot))
        for r in ports:
            for proto, ip_proto in _PROTOCOLS:
                fresh.append([
                    "tc", "filter", "add", "dev", iface, hook, "pref", str(next(prefs)),
                    "protocol", proto, "flower", "ip_proto", ip_proto, key, str(r),
                    "action", "mirred", "egress", "redirect", "dev", ifb,
                ])

    rollback: list[Command] = []
    for hook in ("egress", "ingress"):
        for pref in slot_prefs(slot.slot)[: len(ports) * len(_PROTOCOLS)]:
            rollback.append(["tc", "filter", "del", "dev", iface, hook, "pref", str(pref)])
    rollback += [["ip", "link", "del", slot.up], ["ip", "link", "del", slot.down]]
    if not clsact_present:
        rollback.append(["tc", "qdisc", "del", "dev", iface, "clsact"])
    return HostPlan(slot=slot, commands=commands + fresh, rollback=rollback)


def _free_slot(live: HostLive, iface: str) -> int:
    state = live.iface(iface)
    used_prefs = state.egress_prefs | state.ingress_prefs
    for slot in range(MAX_SLOTS):
        if slot in live.slots or {f"wsh{slot}up", f"wsh{slot}dn"} & live.ifbs:
            continue
        if used_prefs & set(slot_prefs(slot)):
            continue
        return slot
    raise ConflictError(
        f"no free host-mode slot (at most {MAX_SLOTS} targets); clear one first"
    )


def build_host_clear(slot: HostSlot, live: HostLive, keep_clsact: bool = False) -> list[Command]:
    state = live.iface(slot.iface)
    commands: list[Command] = []
    ours = set(slot_prefs(slot.slot))
    for hook in ("egress", "ingress"):
        for pref in sorted(state.prefs(hook) & ours):
            commands.append(["tc", "filter", "del", "dev", slot.iface, hook, "pref", str(pref)])
    for dev in (slot.up, slot.down):
        if dev in live.ifbs:
            commands.append(["ip", "link", "del", dev])
    if keep_clsact or not (slot.added_clsact and state.clsact):
        return commands
    remaining = (state.egress_prefs | state.ingress_prefs) - ours
    others_here = any(
        s.iface == slot.iface for s in live.slots.values() if s.slot != slot.slot
    )
    if not remaining and not others_here:
        commands.append(["tc", "qdisc", "del", "dev", slot.iface, "clsact"])
    return commands


# ----------------------------------------------------------------- execution


def run_plan(backend, commands: Sequence[Command], rollback: Sequence[Command] = ()) -> None:
    """Run commands in order; on failure undo with `rollback` (best effort) and re-raise."""
    for command in commands:
        try:
            backend.run(command)
        except CommandError:
            for undo in rollback:
                backend.run_result(undo)
            raise


# --------------------------------------------------------------- containers


def inspect_container(name: str, runtime: str | None = None) -> tuple[str, dict]:
    """Inspect a container as the caller, or under sudo as the user who ran sudo.

    Rootless containers belong to that user, so root's own `podman inspect`
    can't see them.
    """
    candidates = [runtime] if runtime else ["podman", "docker"]
    for runtime_name in candidates:
        for prefix in _inspect_prefixes(runtime_name):
            result = subprocess.run(
                [*prefix, runtime_name, "inspect", "--type", "container", name],
                text=True, capture_output=True,
            )
            if result.returncode != 0:
                continue
            try:
                data = json.loads(result.stdout)
            except json.JSONDecodeError:
                continue
            if len(data) != 1:
                raise WanshapeError(f"ambiguous container name {name!r}: {len(data)} matches")
            return runtime_name, data[0]
    raise WanshapeError(
        f"container {name!r} not found; to shape ports without a container, pass --ports"
    )


def _inspect_prefixes(runtime: str) -> list[list[str]]:
    prefixes: list[list[str]] = [[]]
    sudo_user = os.environ.get("SUDO_USER")
    if runtime == "podman" and os.geteuid() == 0 and sudo_user and sudo_user != "root":
        uid = os.environ.get("SUDO_UID", "")
        prefixes.append(["runuser", "-u", sudo_user, "--", "env",
                         f"XDG_RUNTIME_DIR=/run/user/{uid}"])
    return prefixes


def host_ports_for(name: str, runtime: str | None, ports_spec: str | None,
                   inspect: Callable[[str, str | None], tuple[str, dict]] = inspect_container
                   ) -> tuple[list[PortRange], str]:
    """Ports to shape for `name`: --ports if given, else from the host-network container."""
    if ports_spec:
        return parse_ports(ports_spec), "--ports"
    _, obj = inspect(name, runtime)
    if not (obj.get("State") or {}).get("Running"):
        raise WanshapeError(f"container {name!r} is not running")
    mode = (obj.get("HostConfig") or {}).get("NetworkMode")
    if mode != "host":
        raise UnsupportedTarget(
            f"container {name!r} is not on host networking ({mode}); "
            f"use `wanshape apply {name}` to shape it inside its own namespace"
        )
    return ports_from_container(obj)

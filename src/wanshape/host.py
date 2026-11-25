"""Host mode: shape traffic on a host interface without touching its root qdisc.

Two kinds of target share this engine:

- ports: a host-network container (or any local ports), matched by port;
- vm: a virtual machine's tap device (libvirt vnetN), matched entirely.

A host-network container has no network namespace of its own, so its traffic is
picked out on a host interface by the local port it uses. Nothing replaces the
interface's root qdisc: `clsact` + `flower` filters redirect only matching
packets to a pair of IFB devices that carry the netem qdiscs.

    egress  (upload):   src_port in ranges -> mirred -> wsh<N>up (netem up)
    ingress (download): dst_port in ranges -> mirred -> wsh<N>dn (netem down)

For a VM the tap's hooks are the other way round, because the tap's egress is
what the host sends *to* the guest (measured in docs/SPIKE-RESULTS.md):

    tap ingress (from the VM, its upload):  matchall -> wsh<N>up (netem up)
    tap egress  (to the VM, its download):  matchall -> wsh<N>dn (netem down)

Each target takes a slot N (0-9). Its filters use the prefs
49300 + 100*N .. +99 on both hooks, and the slot's metadata (target name,
profile, interface, ports, direction, whether wanshape added clsact) is stored
in the up IFB's interface alias. The kernel therefore holds everything that
status and clear need; there is no state file in host mode, and the state can
be read without root.
"""

from __future__ import annotations

import contextlib
import fcntl
import ipaddress
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from .errors import (
    CommandError,
    ConflictError,
    PrerequisiteError,
    UnsupportedTarget,
    WanshapeError,
)
from .planner import netem_tree
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
    kind: str = "ports"  # "ports" or "vm"
    remote_ports: list[PortRange] = field(default_factory=list)
    peer: str | None = None  # only traffic with this address
    until: int | None = None  # epoch seconds when a --duration timer clears it
    device: str = "tap"  # VM mode: "tap" or "macvtap" (hooks the other way round)
    mac: str | None = None  # VM mode: the guest's MAC, to spot a tap name reused by another VM

    @property
    def up(self) -> str:
        return f"wsh{self.slot}up"

    @property
    def down(self) -> str:
        return f"wsh{self.slot}dn"

    def alias(self) -> str:
        ports = format_ports(self.ports) if self.ports else (
            "all" if self.kind == "vm" else "-")
        extra = ""
        if self.remote_ports:
            extra += f" rports={format_ports(self.remote_ports)}"
        if self.peer:
            extra += f" peer={self.peer}"
        if self.until:
            extra += f" until={self.until}"
        if self.kind == "vm" and self.device != "tap":
            extra += f" dev={self.device}"
        if self.kind == "vm" and self.mac:
            extra += f" mac={self.mac}"
        return (
            f"{ALIAS_PREFIX} name={self.name} kind={self.kind} profile={self.profile} "
            f"iface={self.iface} ports={ports} dir={self.direction} "
            f"clsact={int(self.added_clsact)}{extra}"
        )

    def describe(self) -> str:
        parts = []
        if self.ports:
            parts.append(f"ports={format_ports(self.ports)}")
        if self.remote_ports:
            parts.append(f"remote-ports={format_ports(self.remote_ports)}")
        if self.peer:
            parts.append(f"peer={self.peer}")
        return " ".join(parts)

    def filters(self) -> list[tuple[str, list[str], str]]:
        """(hook, match arguments, IFB) for each filter this target needs."""
        result: list[tuple[str, list[str], str]] = []
        if self.kind == "vm":
            # Tap: ingress = from the guest (up), egress = to the guest (down).
            # Macvtap: the other way round, like a container's own interface.
            hooks = ((("egress", "ingress", self.up), ("ingress", "egress", self.down))
                     if self.device == "tap" else
                     (("egress", "egress", self.up), ("ingress", "ingress", self.down)))
            for want, hook, ifb in hooks:
                if self.direction in (want, "both"):
                    result.append((hook, ["protocol", "all", "matchall"], ifb))
            return result
        families = ("ip", "ipv6")
        if self.peer:
            families = ("ipv6",) if ":" in self.peer else ("ip",)
        # Outgoing: our port is the source, the remote's the destination; incoming
        # the other way round.
        for hook, local_key, remote_key, ip_key, ifb in (
                ("egress", "src_port", "dst_port", "dst_ip", self.up),
                ("ingress", "dst_port", "src_port", "src_ip", self.down)):
            if self.direction not in (hook, "both"):
                continue
            peer = [ip_key, self.peer] if self.peer else []
            matches = ([(local_key, r) for r in self.ports]
                       + [(remote_key, r) for r in self.remote_ports])
            for proto in families:
                if not matches:  # peer only: everything with that host
                    result.append((hook, ["protocol", proto, "flower", *peer], ifb))
                for key, r in matches:
                    for ip_proto in ("udp", "tcp"):
                        result.append((hook, ["protocol", proto, "flower", "ip_proto",
                                              ip_proto, *peer, key, str(r)], ifb))
        return result


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
        kind = fields.get("kind", "ports")
        ports = fields["ports"]
        return HostSlot(
            slot=slot,
            name=fields["name"],
            profile=fields.get("profile", "unknown"),
            iface=fields["iface"],
            ports=[] if kind == "vm" or ports == "-" else parse_ports(ports),
            direction=fields.get("dir", "both"),
            added_clsact=fields.get("clsact") == "1",
            kind=kind,
            remote_ports=parse_ports(fields["rports"]) if "rports" in fields else [],
            peer=fields.get("peer"),
            until=int(fields["until"]) if fields.get("until", "").isdigit() else None,
            device=fields.get("dev", "tap"),
            mac=fields.get("mac"),
        )
    except (KeyError, WanshapeError):
        return None


@dataclass
class IfaceState:
    exists: bool = True
    clsact: bool = False
    ingress_qdisc: bool = False  # plain `ingress` qdisc: excludes clsact
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
            state.exists = False  # e.g. a VM restarted and its tap was renamed
            continue
        qdiscs = backend.run(["tc", "qdisc", "show", "dev", iface])
        state.clsact = "qdisc clsact" in qdiscs
        state.ingress_qdisc = "qdisc ingress" in qdiscs
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
    kind: str = "ports",
    remote_ports: list[PortRange] | None = None,
    peer: str | None = None,
    until: int | None = None,
    device: str = "tap",
    mac: str | None = None,
) -> HostPlan:
    """Plan a ports target, or with kind="vm" (and no ports) a whole tap device."""
    remote_ports = list(remote_ports or [])
    if peer is not None:
        try:
            peer = str(ipaddress.ip_address(peer))
        except ValueError as exc:
            raise WanshapeError(f"invalid peer address {peer!r}") from exc
    if direction not in ("egress", "ingress", "both"):
        raise WanshapeError(f"invalid direction {direction!r}")
    if not _NAME.match(name):
        raise WanshapeError(f"invalid target name {name!r}")
    if kind == "vm" and ports:
        raise WanshapeError("a VM target shapes the whole device; it takes no ports")
    if kind == "vm" and (remote_ports or peer):
        raise WanshapeError("a VM target shapes the whole device; no remote ports or peer")
    if kind == "ports" and not (ports or remote_ports or peer):
        raise WanshapeError("no ports given (local ports, --remote-ports or --peer)")

    existing = live.by_name(name)
    if existing and existing.kind != kind:
        raise ConflictError(
            f"{name!r} is already a {existing.kind} target; clear it first"
        )
    stale_here = [o for o in live.slots.values()
                  if o is not existing and o.iface == iface and o.kind == "vm" and kind == "vm"
                  and o.mac and mac and o.mac != mac.lower()]
    for other in live.slots.values():
        if other is existing or other.iface != iface or other in stale_here:
            continue
        if kind == "vm" or other.kind == "vm":
            raise ConflictError(
                f"{iface} is already shaped for {other.name!r}; "
                f"clear it first with `wanshape clear-host {other.name}`"
            )
        clash: list[str] = []
        if not peer or not other.peer or peer == other.peer:
            # Different peers can never match the same packet; otherwise any pair of
            # port sets can: local against local, remote against remote, and local
            # against remote (a packet from port 5060 to port 5060 matches both).
            mine = ports + remote_ports
            theirs = other.ports + other.remote_ports
            clash += [f"{a}/{b}" for a in mine for b in theirs if a.overlaps(b)]
            if not mine or not theirs:
                clash += [f"peer {peer or 'any'}/{other.peer or 'any'}"]
        if clash:
            raise ConflictError(
                f"ports overlap host target {other.name!r} on {iface} ({', '.join(clash)}); "
                f"clear it first with `wanshape clear-host {other.name}`"
            )

    commands: list[Command] = []
    for old in stale_here:
        # A VM that was shaped on this device name has gone, and libvirt gave the
        # name to this VM: its leftover filters and IFBs are cleared first.
        commands += build_host_clear(old, live, keep_clsact=True)
        live.slots.pop(old.slot, None)
    state = live.iface(iface)
    if state.ingress_qdisc and not state.clsact:
        raise ConflictError(
            f"{iface} has an `ingress` qdisc, which can't coexist with the clsact qdisc "
            "wanshape needs (libvirt adds one for <bandwidth><outbound>); remove that "
            "limit or the qdisc first"
        )
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
        ports=list(ports), direction=direction, added_clsact=added_clsact, kind=kind,
        remote_ports=remote_ports, peer=peer, until=until, device=device,
        mac=mac.lower() if mac else None,
    )
    if len(slot.filters()) > PREFS_PER_SLOT:
        raise WanshapeError(
            f"too many port ranges: they need {len(slot.filters())} filters, at most "
            f"{PREFS_PER_SLOT} per target")
    alias = slot.alias()
    if len(alias.encode()) > 255:
        raise WanshapeError("too many port ranges to record; merge some of them")

    fresh: list[Command] = []
    for dev, spec in ((slot.up, profile.up), (slot.down, profile.down)):
        fresh += [
            ["ip", "link", "add", dev, "type", "ifb"],
            ["ip", "link", "set", "dev", dev, "up"],
            *netem_tree("add", dev, spec, profile),
        ]
    fresh.append(["ip", "link", "set", "dev", slot.up, "alias", alias])
    if not clsact_present:
        fresh.append(["tc", "qdisc", "add", "dev", iface, "clsact"])
    prefs = {"egress": iter(slot_prefs(slot.slot)), "ingress": iter(slot_prefs(slot.slot))}
    used: dict[str, list[int]] = {"egress": [], "ingress": []}
    for hook, match, ifb in slot.filters():
        pref = next(prefs[hook])
        used[hook].append(pref)
        fresh.append(["tc", "filter", "add", "dev", iface, hook, "pref", str(pref),
                      *match, "action", "mirred", "egress", "redirect", "dev", ifb])

    rollback: list[Command] = []
    for hook in ("egress", "ingress"):
        for pref in used[hook]:
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
    if not state.exists:
        # The device is gone (and its filters with it): only our IFBs remain.
        return [["ip", "link", "del", dev] for dev in (slot.up, slot.down) if dev in live.ifbs]
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


def _undo(command: Command) -> Command | None:
    """The command that removes what `command` created, if it created something."""
    if command[:3] == ["ip", "link", "add"]:
        return ["ip", "link", "del", command[3]]
    if command[:3] == ["tc", "filter", "add"]:
        return ["tc", "filter", "del", *command[3:8]]      # dev D HOOK pref P
    if command[:3] == ["tc", "qdisc", "add"] and command[-1] == "clsact":
        return ["tc", "qdisc", "del", "dev", command[4], "clsact"]
    return None   # qdiscs on our own IFBs go with the IFB


def run_plan(backend, commands: Sequence[Command], rollback: Sequence[Command] = ()) -> None:
    """Run commands in order; on failure undo what *this run* created, and re-raise.

    Only the commands that succeeded are undone, in reverse order: if `ip link
    add wsh0up` fails because the device exists (another run took the slot),
    that device isn't ours and is left alone. `rollback` is kept for callers'
    compatibility and is no longer used.
    """
    done: list[Command] = []
    for command in commands:
        try:
            backend.run(command)
        except WanshapeError:
            for undo in filter(None, map(_undo, reversed(done))):
                backend.run_result(undo)
            raise
        done.append(command)


@contextlib.contextmanager
def host_lock():
    """Serialise host and VM changes (two applies would otherwise pick one slot).

    Best effort: without permission to create the lock file (a non-root dry run)
    it proceeds unlocked, since nothing is changed then.
    """
    path = Path(os.environ.get("WANSHAPE_LOCK", "/run/lock/wanshape.lock"))
    try:
        handle = open(path, "a")
    except OSError:
        yield
        return
    with handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


# --------------------------------------------------------------- containers


def inspect_container(name: str, runtime: str | None = None) -> tuple[str, dict]:
    """Inspect a container as the caller, or under sudo as the user who ran sudo.

    Rootless containers belong to that user, so root's own `podman inspect`
    can't see them.
    """
    candidates = [runtime] if runtime else ["podman", "docker"]
    for runtime_name in candidates:
        for prefix in _inspect_prefixes(runtime_name):
            try:
                result = subprocess.run(
                    [*prefix, runtime_name, "inspect", "--type", "container", name],
                    text=True, capture_output=True,
                )
            except FileNotFoundError:
                break  # this runtime isn't installed
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
                   inspect: Callable[[str, str | None], tuple[str, dict]] = inspect_container,
                   optional: bool = False) -> tuple[list[PortRange], str]:
    """Ports to shape for `name`: --ports if given, else from the host-network container.

    With optional=True (remote ports or a peer were given), a name that isn't a
    container is just a label and gets no local ports.
    """
    if ports_spec:
        return parse_ports(ports_spec), "--ports"
    try:
        _, obj = inspect(name, runtime)
    except WanshapeError:
        if optional:
            return [], "none"
        raise
    if not (obj.get("State") or {}).get("Running"):
        raise WanshapeError(f"container {name!r} is not running")
    mode = (obj.get("HostConfig") or {}).get("NetworkMode")
    if mode != "host":
        raise UnsupportedTarget(
            f"container {name!r} is not on host networking ({mode}); "
            f"use `wanshape apply {name}` to shape it inside its own namespace"
        )
    try:
        return ports_from_container(obj)
    except WanshapeError:
        if optional:
            return [], "none"
        raise


def remote_ports_from_container(name: str, runtime: str | None,
                                inspect: Callable[[str, str | None], tuple[str, dict]]
                                = inspect_container) -> list[PortRange]:
    """The `wanshape.remote_ports` label of a container, if it has one."""
    try:
        _, obj = inspect(name, runtime)
    except WanshapeError:
        return []
    label = ((obj.get("Config") or {}).get("Labels") or {}).get("wanshape.remote_ports")
    return parse_ports(label) if label else []

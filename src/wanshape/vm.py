"""VM mode: find a libvirt VM's host-side network device.

A libvirt VM on a `network` or `bridge` interface (and `ethernet`) is
connected by a tap device on the host, vnetN. wanshape shapes the whole tap
through the host-mode engine (host.py, kind="vm").

A `direct` interface is a macvtap device, also on the host. Its hooks run the
other way round from a tap's (measured: docs/SPIKE-RESULTS.md), which the
host-mode engine handles with `device="macvtap"`.

Not supported:
- `user` / passt (typical for qemu:///session): there is no host device; the
  VM's traffic leaves from host sockets, so `apply-host --ports` is the answer.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Callable, Sequence

from .errors import PrerequisiteError, UnsupportedTarget, WanshapeError

DEFAULT_URI = "qemu:///system"
TAP_TYPES = ("network", "bridge", "ethernet")
MACVTAP_TYPES = ("direct",)


def device_kind(iface_type: str) -> str:
    return "macvtap" if iface_type in MACVTAP_TYPES else "tap"


@dataclass
class VmInterface:
    device: str  # host-side device, "-" when the VM isn't running
    type: str
    source: str
    model: str
    mac: str


def parse_domiflist(text: str) -> list[VmInterface]:
    """Parse `virsh -q domiflist` (no header lines)."""
    result: list[VmInterface] = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) == 5 and fields[0] != "Interface" and not fields[0].startswith("-" * 3):
            result.append(VmInterface(*fields))
    return result


def _virsh(uri: str, *args: str) -> str:
    try:
        result = subprocess.run(["virsh", "-c", uri, "-q", *args],
                                text=True, capture_output=True)
    except FileNotFoundError as exc:
        raise PrerequisiteError("virsh not found: VM mode needs libvirt's client") from exc
    if result.returncode != 0:
        raise WanshapeError(f"virsh {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def choose_interface(domain: str, interfaces: Sequence[VmInterface],
                     mac: str | None = None) -> VmInterface:
    if mac:
        matches = [i for i in interfaces if i.mac.lower() == mac.lower()]
        if not matches:
            raise WanshapeError(
                f"VM {domain!r} has no interface with MAC {mac}; it has "
                + ", ".join(i.mac for i in interfaces)
            )
        chosen = matches[0]
    elif len(interfaces) == 1:
        chosen = interfaces[0]
    elif not interfaces:
        raise UnsupportedTarget(f"VM {domain!r} has no network interfaces")
    else:
        raise WanshapeError(
            f"VM {domain!r} has {len(interfaces)} interfaces; pick one with --mac: "
            + ", ".join(f"{i.mac} ({i.type} {i.source})" for i in interfaces)
        )
    if chosen.type not in TAP_TYPES + MACVTAP_TYPES:
        raise UnsupportedTarget(
            f"VM {domain!r} uses a {chosen.type!r} interface with no host-side device; "
            "use `wanshape apply-host --ports ...` for the ports it forwards"
        )
    if chosen.device in ("-", ""):
        raise WanshapeError(f"VM {domain!r} is not running (its interface has no device)")
    return chosen


def check_tap_mac(device: str, guest_mac: str,
                  run: Callable[[list[str]], str] | None = None) -> None:
    """libvirt gives a tap the guest's MAC with the first byte set to fe; a
    macvtap device has the guest's MAC itself.

    Checking it guards against a vnetN/macvtapN name that now belongs to another VM.
    """
    run = run or (lambda args: subprocess.run(args, text=True, capture_output=True).stdout)
    try:
        link = json.loads(run(["ip", "-j", "link", "show", "dev", device]) or "[]")
    except json.JSONDecodeError:
        link = []
    if not link:
        raise WanshapeError(f"host device {device} doesn't exist (did the VM just restart?)")
    tap_mac = str(link[0].get("address", "")).lower()
    expected = "fe" + guest_mac.lower()[2:]
    if tap_mac not in (expected, guest_mac.lower()):
        raise WanshapeError(
            f"{device} has MAC {tap_mac}, not the {expected} libvirt gives this VM's tap; "
            "refusing to shape what may be another VM"
        )


def resolve_vm(domain: str, uri: str = DEFAULT_URI, mac: str | None = None,
               virsh: Callable[..., str] | None = None) -> VmInterface:
    virsh = virsh or _virsh
    chosen = choose_interface(domain, parse_domiflist(virsh(uri, "domiflist", domain)), mac)
    check_tap_mac(chosen.device, chosen.mac)
    return chosen

"""Re-apply VM shaping when libvirt starts a VM (issue #3).

`apply-vm --persist` records the VM's settings in /etc/wanshape/vms.json, with
the resolved profile itself, so the hook doesn't depend on anyone's profile
file. libvirt runs /etc/libvirt/hooks/qemu.d/wanshape as

    wanshape <domain> <operation> <sub-operation> <extra>    (domain XML on stdin)

and on "started begin" wanshape re-applies the recorded shaping to the VM's new
tap device; on "release end" it removes the IFBs left from the old one. The tap
is read from the domain XML: libvirt's docs forbid calling back into libvirt
(virsh) from a hook, which can deadlock. The hook must never fail a VM start,
so it always exits 0 and logs what it did to stderr (libvirt's log).
"""

from __future__ import annotations

import json
import os
import sys
import xml.etree.ElementTree as ET
from dataclasses import asdict
from pathlib import Path

from .errors import WanshapeError
from .profiles import Direction, Profile

CONFIG = Path(os.environ.get("WANSHAPE_VM_CONFIG", "/etc/wanshape/vms.json"))
HOOK_DIR = Path(os.environ.get("WANSHAPE_LIBVIRT_HOOKS", "/etc/libvirt/hooks")) / "qemu.d"
HOOK = HOOK_DIR / "wanshape"


def load() -> dict:
    try:
        data = json.loads(CONFIG.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, CONFIG)


def remember(domain: str, profile: Profile, direction: str, mac: str,
             device: str = "tap") -> None:
    data = load()
    data[domain] = {"profile": asdict(profile), "direction": direction, "mac": mac.lower(),
                    "device": device}
    _save(data)


def forget(domain: str) -> bool:
    data = load()
    if domain not in data:
        return False
    del data[domain]
    _save(data)
    return True


def profile_from(entry: dict) -> Profile:
    raw = dict(entry["profile"])
    up, down = Direction(**raw.pop("up")), Direction(**raw.pop("down"))
    profile = Profile(**raw, up=up, down=down)
    profile.validate()
    return profile


def tap_from_xml(xml_text: str, mac: str) -> str | None:
    """The host-side device of the interface with this MAC, from libvirt's domain XML."""
    root = ET.fromstring(xml_text)
    for iface in root.iter("interface"):
        mac_el, target = iface.find("mac"), iface.find("target")
        if (mac_el is not None and target is not None
                and mac_el.get("address", "").lower() == mac.lower()):
            return target.get("dev")
    return None


def hook_script() -> str:
    package_root = Path(__file__).resolve().parent.parent
    return (
        "#!/bin/sh\n"
        "# Installed by `wanshape install-vm-hook`: re-applies wanshape VM shaping\n"
        "# when libvirt starts a VM. Remove with `wanshape uninstall-vm-hook`.\n"
        f"PYTHONPATH={package_root} exec {sys.executable} -m wanshape.cli vm-hook \"$@\"\n"
    )


def install() -> Path:
    HOOK_DIR.mkdir(parents=True, exist_ok=True)
    HOOK.write_text(hook_script())
    os.chmod(HOOK, 0o755)
    return HOOK


def uninstall() -> bool:
    if HOOK.exists():
        HOOK.unlink()
        return True
    return False


def run_hook(domain: str, operation: str, sub_operation: str, xml_text: str,
             apply_fn, release_fn) -> str:
    """Dispatch one libvirt hook call; returns what was done, for the log."""
    entry = load().get(domain)
    if not entry:
        return "not a wanshape VM"
    if (operation, sub_operation) == ("started", "begin"):
        device = tap_from_xml(xml_text, entry["mac"])
        if not device:
            raise WanshapeError(f"no interface with MAC {entry['mac']} in the domain XML")
        apply_fn(domain, profile_from(entry), entry["direction"], device,
                 entry.get("device", "tap"))
        return f"re-applied to {device}"
    if (operation, sub_operation) == ("release", "end"):
        release_fn(domain)
        return "removed the old IFBs"
    return f"nothing to do for {operation} {sub_operation}"

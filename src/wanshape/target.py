"""Resolve a container name/ID to its network namespace and interface."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass

from .errors import UnsupportedTarget, WanshapeError

_DEV = re.compile(r"\bdev\s+(\S+)")


@dataclass
class Target:
    name: str
    id: str
    runtime: str
    rootless: bool
    pid: int
    netns_inode: int
    userns_inode: int
    network_mode: str
    iface: str
    owner_uid: int

    def verify(self) -> None:
        """Re-check the netns inode immediately before mutating it (TOCTOU guard)."""
        try:
            current = os.stat(f"/proc/{self.pid}/ns/net").st_ino
        except OSError as exc:
            raise WanshapeError(f"container {self.name} disappeared: {exc}") from exc
        if current != self.netns_inode:
            raise WanshapeError(
                f"container {self.name} was recreated while shaping "
                f"(netns inode {self.netns_inode} -> {current}); refusing to touch it"
            )


def resolve(name: str, runtime: str | None = None, iface: str | None = None) -> Target:
    candidates = [runtime] if runtime else ["podman", "docker"]
    errors: list[str] = []
    for runtime_name in candidates:
        data = _inspect(runtime_name, name)
        if data is None:
            errors.append(f"{runtime_name}: container {name!r} not found")
            continue
        if len(data) != 1:
            raise WanshapeError(
                f"ambiguous container name {name!r}: {len(data)} matches"
            )
        return _build_target(runtime_name, data[0], name, iface)
    raise WanshapeError("; ".join(errors))


def _inspect(runtime: str, name: str) -> list[dict] | None:
    try:
        result = subprocess.run(
            [runtime, "inspect", "--type", "container", name],
            text=True,
            capture_output=True,
        )
    except FileNotFoundError:
        return None  # this runtime isn't installed
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise WanshapeError(f"{runtime} inspect returned invalid JSON: {exc}") from exc


def _build_target(runtime: str, obj: dict, name: str, iface: str | None) -> Target:
    container_id = str(obj.get("Id", ""))
    names = obj.get("Names") or ([obj["Name"]] if obj.get("Name") else [])
    real_names = {str(n).lstrip("/") for n in names}
    if name not in real_names and container_id.startswith(name):
        # Resolved by ID: use the container's real name for messages/state.
        name = str(names[0]).lstrip("/")
    state = obj.get("State", {})
    if not state.get("Running") or not state.get("Pid"):
        raise WanshapeError(f"container {name!r} is not running")
    mode = obj.get("HostConfig", {}).get("NetworkMode") or "unknown"
    if mode == "host":
        raise UnsupportedTarget(
            f"container {name!r} uses host networking: it has no per-container netns; "
            f"use `wanshape apply-host {name}` to shape its ports on the host interface"
        )
    pid = int(state["Pid"])
    try:
        netns_inode = os.stat(f"/proc/{pid}/ns/net").st_ino
        userns_inode = os.stat(f"/proc/{pid}/ns/user").st_ino
        owner_uid = os.stat(f"/proc/{pid}").st_uid
    except OSError as exc:
        raise WanshapeError(f"cannot inspect namespaces of PID {pid}: {exc}") from exc

    rootless = _is_rootless(runtime)
    target = Target(
        name=name,
        id=str(obj.get("Id", "")),
        runtime=runtime,
        rootless=rootless,
        pid=pid,
        netns_inode=netns_inode,
        userns_inode=userns_inode,
        network_mode=mode,
        iface=iface or "",
        owner_uid=owner_uid,
    )
    if not target.iface:
        target.iface = _resolve_iface(target)
    return target


def _is_rootless(runtime: str) -> bool:
    if runtime != "podman":
        return False
    result = subprocess.run(
        [runtime, "info", "--format", "{{.Host.Security.Rootless}}"],
        text=True,
        capture_output=True,
    )
    return result.stdout.strip().lower() == "true"


def _resolve_iface(target: Target) -> str:
    # Imported here to avoid a module-level cycle (backends imports Target).
    from .backends import NsenterBackend

    output = NsenterBackend(target).run(["ip", "-o", "route", "show", "default"])
    for line in output.splitlines():
        match = _DEV.search(line)
        if match:
            return match.group(1)
    raise WanshapeError(
        f"cannot resolve the interface of {target.name!r} from its default route; "
        "pass --iface explicitly"
    )


def netns_members(target: Target) -> list[str]:
    """Other running containers of the same runtime that share the target's netns.

    Pods and `--network container:X` share one network namespace, so shaping
    one container shapes all of them.
    """
    try:
        ids = subprocess.run([target.runtime, "ps", "-q", "--no-trunc"],
                             text=True, capture_output=True).stdout.split()
    except FileNotFoundError:
        return []
    ids = [i for i in ids if i != target.id]
    if not ids:
        return []
    result = subprocess.run(
        [target.runtime, "inspect", "--type", "container",
         "--format", "{{.State.Pid}} {{.Name}}", *ids],
        text=True, capture_output=True,
    )
    members: list[str] = []
    for line in result.stdout.splitlines():
        pid, _, name = line.partition(" ")
        if not pid.isdigit() or pid == "0":
            continue
        try:
            if os.stat(f"/proc/{pid}/ns/net").st_ino == target.netns_inode:
                members.append(name.lstrip("/"))
        except OSError:
            continue
    return sorted(members)


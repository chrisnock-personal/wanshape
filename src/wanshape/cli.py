"""Command-line interface for wanshape."""

from __future__ import annotations

import argparse
import shlex
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .backends import get_backend
from .host import (
    HostBackend,
    build_host_apply,
    build_host_clear,
    format_ports,
    host_ports_for,
    inspect_host,
    require_root,
    resolve_host_iface,
    run_plan,
)
from .errors import CommandError, WanshapeError
from .inspector import HANDLE, inspect_live, stats
from .planner import build_apply_plan, build_clear_plan
from .profiles import Direction, Profile, ProfileError, load_profiles
from .state import load_state, make_entry, save_state
from .target import Target, netns_members, resolve
from .vm import resolve_vm

EXIT_ERROR = 1
EXIT_UNSUPPORTED = 2
EXIT_PREREQ = 3
EXIT_CONFLICT = 4


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wanshape",
        description="Apply netem/tc WAN impairments to one container, one service's "
        "ports (host mode) or one libvirt VM",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="check tools, kernel modules and runtimes")
    sub.add_parser("list", help="list available profiles")
    show = sub.add_parser("show", help="expand a profile")
    show.add_argument("profile")

    apply_cmd = sub.add_parser("apply", help="apply a profile to a container")
    apply_cmd.add_argument("container")
    _add_profile_args(apply_cmd)
    apply_cmd.add_argument("--runtime", choices=("podman", "docker"))
    apply_cmd.add_argument("--iface", help="override the resolved interface")
    apply_cmd.add_argument("--backend", choices=("auto", "nsenter", "sidecar"), default="auto")
    apply_cmd.add_argument("--force", action="store_true", help="replace foreign qdisc/filters")
    apply_cmd.add_argument("--allow-partial", action="store_true",
                           help="if one direction fails, keep the other instead of rolling back")
    apply_cmd.add_argument("--duration", help="not implemented in this prototype")

    host = sub.add_parser(
        "apply-host",
        help="shape a host-network container (or any ports) on a host interface; needs root",
        description="Shape traffic by local port on a host interface. With a container "
        "name, the ports come from its wanshape.ports label or its *SIP_PORT and "
        "*PORT_LOW/HIGH environment variables; --ports overrides that, and then NAME is "
        "only a label and needs no container.",
    )
    host.add_argument("name", help="host-network container, or a label when --ports is given")
    _add_profile_args(host)
    host.add_argument("--ports", help="local ports to shape, e.g. 5060,10000-20000 (UDP and TCP)")
    host.add_argument("--runtime", choices=("podman", "docker"))
    where = host.add_mutually_exclusive_group()
    where.add_argument("--iface", help="host interface (default: the default route's)")
    where.add_argument("--via", metavar="ADDRESS",
                       help="use the interface that routes to ADDRESS, e.g. the PBX")

    vm = sub.add_parser(
        "apply-vm",
        help="shape a libvirt VM's network device (tap: network/bridge); needs root",
        description="Shape all of a libvirt VM's traffic on its host-side tap device "
        "(vnetN). Delay and loss apply to traffic to and from the VM, including other "
        "VMs on the same bridge. The shaping is lost when the VM restarts; apply again.",
    )
    vm.add_argument("domain", help="libvirt domain name")
    _add_profile_args(vm)
    vm.add_argument("--connect", "-c", default="qemu:///system", metavar="URI",
                    help="libvirt URI (default qemu:///system)")
    vm.add_argument("--mac", help="which interface, when the VM has several")

    clear_host = sub.add_parser("clear-host", aliases=["clear-vm"],
                                help="remove host-mode or VM shaping")
    target_group = clear_host.add_mutually_exclusive_group(required=True)
    target_group.add_argument("name", nargs="?")
    target_group.add_argument("--all", action="store_true", help="clear every host-mode target")
    clear_host.add_argument("--dry-run", action="store_true")

    status = sub.add_parser("status", help="show live shaping state")
    status.add_argument("container", nargs="?")
    status.add_argument("--all", action="store_true",
                        help="also scan every running container for wanshape shaping, "
                        "including any the state file doesn't know about")
    status.add_argument("--prune", action="store_true",
                        help="drop state entries for containers that are gone or recreated")

    clear = sub.add_parser("clear", help="remove wanshape's qdiscs")
    clear.add_argument("container")
    clear.add_argument("--direction", choices=("egress", "ingress", "both"), default="both")
    clear.add_argument("--runtime", choices=("podman", "docker"))
    clear.add_argument("--iface")
    clear.add_argument("--dry-run", action="store_true")
    return parser


def _add_profile_args(parser: argparse.ArgumentParser) -> None:
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--profile", help="named profile")
    mode.add_argument("--delay", help="inline profile: one-way delay, e.g. 150ms")
    parser.add_argument("--jitter", help="delay variation, e.g. 30ms")
    parser.add_argument("--distribution", choices=("uniform", "normal", "pareto", "paretonormal"),
                        help="jitter distribution (inline profile)")
    parser.add_argument("--loss", help="random loss percentage, e.g. 1%%")
    parser.add_argument("--loss-model", help="bursty loss, e.g. gemodel:1%%,25%%,100%%,0%%")
    parser.add_argument("--rate", help="bandwidth cap, e.g. 768kbit")
    parser.add_argument("--direction", choices=("egress", "ingress", "both"), default="both",
                        help="egress = upload from the target, ingress = download")
    parser.add_argument("--no-reorder", action="store_true",
                        help="attach pfifo under netem to prevent jitter reordering")
    parser.add_argument("--dry-run", action="store_true", help="print commands only")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        handler = {"apply-host": _apply_host, "clear-host": _clear_host,
                   "clear-vm": _clear_host, "apply-vm": _apply_vm}.get(
            args.command) or getattr(_Commands, args.command)
        return handler(args)
    except ProfileError as exc:
        print(f"wanshape: invalid profile: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except WanshapeError as exc:
        print(f"wanshape: {exc}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("wanshape: interrupted", file=sys.stderr)
        return 130


class _Commands:
    @staticmethod
    def doctor(_args: argparse.Namespace) -> int:
        missing = [tool for tool in ("tc", "ip", "nsenter") if not shutil.which(tool)]
        if missing:
            print(f"MISSING tools: {', '.join(missing)}")
            return EXIT_PREREQ
        print("tools: tc, ip, nsenter found")
        runtimes = [r for r in ("podman", "docker") if shutil.which(r)]
        print("containers: " + (", ".join(runtimes) if runtimes else
                                "no podman or docker (container modes unavailable)"))
        print("VMs: " + ("virsh found" if shutil.which("virsh") else
                         "no virsh (apply-vm unavailable)"))
        modules = ("sch_netem", "ifb", "act_mirred", "cls_matchall", "cls_flower")
        for module in modules:
            if Path(f"/sys/module/{module}").exists():
                status = "loaded"
            else:
                result = subprocess.run(["modinfo", module], text=True, capture_output=True)
                status = "available (autoload on use)" if result.returncode == 0 else "NOT FOUND"
            print(f"{module}: {status}")
        print("root: " + ("yes" if os.geteuid() == 0 else
                          "no (fine for rootless containers; apply-host and apply-vm need sudo)"))
        return 0

    @staticmethod
    def list(_args: argparse.Namespace) -> int:
        for name, profile in sorted(load_profiles().items()):
            print(f"{name:12} {profile.description}")
        return 0

    @staticmethod
    def show(args: argparse.Namespace) -> int:
        profiles = load_profiles()
        if args.profile not in profiles:
            raise WanshapeError(f"unknown profile {args.profile!r}; try `wanshape list`")
        p = profiles[args.profile]
        print(f"{p.name}: {p.description}")
        print(f"  reorder_on_jitter = {p.reorder_on_jitter}")
        for direction in ("up", "down"):
            d = getattr(p, direction)
            parts = [f"{key}={value}" for key, value in d.__dict__.items() if value is not None]
            print(f"  {direction}: {' '.join(parts)}")
        return 0

    @staticmethod
    def apply(args: argparse.Namespace) -> int:
        if args.duration:
            raise WanshapeError("--duration is not implemented in this prototype")
        profile = _profile_from_args(args)
        target = resolve(args.container, args.runtime, args.iface)
        backend = get_backend(target, args.backend)
        target.verify()
        live = inspect_live(backend, target.iface)
        plan = build_apply_plan(profile, target.iface, args.direction, live, args.force)
        _print_plan(plan, backend, target)
        members = netns_members(target)
        if members:
            print(f"# {target.name} shares its network namespace with {', '.join(members)}: "
                  "they are shaped too", file=sys.stderr)

        if args.dry_run:
            return 0
        applied: list[str] = []
        failure: tuple[str, CommandError] | None = None
        for group in ("egress", "ingress"):
            if not getattr(plan, group):
                continue
            try:
                for command in getattr(plan, group):
                    target.verify()
                    backend.run(command)
            except CommandError as exc:
                failure = (group, exc)
                break
            applied.append(group)
        if failure:
            failed_group, error = failure
            print(f"wanshape: {failed_group} shaping failed: {error}", file=sys.stderr)
            if applied and args.allow_partial:
                _save_and_report(target, profile.name, "+".join(applied), live, plan,
                                 partial=True)
                print(f"wanshape: {'+'.join(applied)} remains active; run `wanshape clear {target.name}`",
                      file=sys.stderr)
            else:
                _rollback(target, backend, plan)
            return EXIT_ERROR
        return _save_and_report(target, profile.name, args.direction, live, plan)

    @staticmethod
    def status(args: argparse.Namespace) -> int:
        entries = load_state()
        if args.prune:
            _prune(entries)
            entries = load_state()
        targets: list[tuple[Target, dict | None]] = []
        if args.container:
            try:
                target = resolve(args.container)
                targets.append((target, _entry_for(entries, target)))
            except WanshapeError:
                # Host-network containers and --ports labels live in host mode only.
                if not inspect_host(HostBackend()).by_name(args.container):
                    raise
        else:
            for entry in entries.values():
                target = None
                for key in (entry.get("id"), entry.get("name")):
                    if not key:
                        continue
                    try:
                        target = resolve(key, entry.get("runtime"))
                        break
                    except WanshapeError:
                        pass
                if target is None:
                    print(f"wanshape: {entry.get('name')}: container is gone "
                          "(`wanshape status --prune` forgets it)", file=sys.stderr)
                    continue
                if any(t.netns_inode == target.netns_inode for t, _ in targets):
                    continue  # another name for a namespace already listed
                targets.append((target, entry))
            if args.all:
                known = {t.netns_inode for t, _ in targets}
                for target in _scan_running():
                    if target.netns_inode not in known:
                        known.add(target.netns_inode)
                        targets.append((target, None))
        host_backend = HostBackend()
        host_live = inspect_host(host_backend)
        host_slots = [
            slot for slot in sorted(host_live.slots.values(), key=lambda x: x.slot)
            if not args.container or slot.name == args.container
        ]
        for slot in host_slots:
            gone = not host_live.iface(slot.iface).exists
            what = (f"vm mode, device={slot.iface}" if slot.kind == "vm"
                    else f"host mode, iface={slot.iface} ports={format_ports(slot.ports)}")
            print(f"{slot.name}: {what} profile={slot.profile} direction={slot.direction}"
                  + (f" STALE ({slot.iface} is gone; re-apply or clear-host)" if gone else ""))
            for dev in (slot.up, slot.down):
                print(host_backend.run(["tc", "-s", "qdisc", "show", "dev", dev]).strip())
        shown = len(host_slots)
        for target, entry in targets:
            backend = get_backend(target)
            live = inspect_live(backend, target.iface)
            if entry is None and args.all and not args.container and not (
                    live.egress or live.ingress):
                continue  # scanned and not shaped: not worth a line
            shown += 1
            profile = entry.get("profile", "unknown") if entry else "unknown"
            stale = bool(entry and entry.get("netns_inode") != target.netns_inode)
            print(f"{target.name}: profile={profile}"
                  f" egress={'on' if live.egress else 'off'}"
                  f" ingress={'on' if live.ingress else 'off'}"
                  + (" STALE (container recreated)" if stale else ""))
            if live.egress or live.ingress:
                print(stats(backend, target.iface))
        if not shown:
            print("no wanshape shaping found" if args.all else "no wanshape state found")
        return 0

    @staticmethod
    def clear(args: argparse.Namespace) -> int:
        target = resolve(args.container, args.runtime, args.iface)
        backend = get_backend(target, "auto")
        entry = _entry_for(load_state(), target) or {}
        target.verify()
        live = inspect_live(backend, target.iface)
        plan = build_clear_plan(
            target.iface, args.direction, live, bool(entry.get("added_clsact")),
            entry.get("prior_root_qdisc"),
        )
        if plan.empty:
            print(f"{target.name}: nothing to clear")
            return 0
        _print_plan(plan, backend, target)
        if args.dry_run:
            return 0
        for command in plan.commands:
            target.verify()
            backend.run(command)
        entries = load_state()
        for key in [k for k, e in entries.items()
                    if k == target.id or e.get("netns_inode") == target.netns_inode]:
            entries.pop(key)
        save_state(entries)
        print(f"{target.name}: cleared; state removed")
        return 0


def _apply_host(args: argparse.Namespace) -> int:
    profile = _profile_from_args(args)
    ports, source = host_ports_for(args.name, args.runtime, args.ports)
    backend = HostBackend()
    iface = resolve_host_iface(backend, args.iface, args.via)
    live = inspect_host(backend, [iface])
    plan = build_host_apply(args.name, profile, ports, iface, args.direction, live)
    print(f"# host target: {args.name}, ports {format_ports(ports)} (from {source})")
    print(f"# iface {iface}, slot {plan.slot.slot}, {args.direction}")
    for command in plan.commands:
        print("+ " + shlex.join(command))
    if args.dry_run:
        return 0
    require_root()
    try:
        run_plan(backend, plan.commands, plan.rollback)
    except CommandError as exc:
        print(f"wanshape: apply-host failed and was rolled back: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(f"{args.name}: applied {profile.name} to {args.direction} on {iface} "
          f"ports {format_ports(ports)}")
    return 0


def _apply_vm(args: argparse.Namespace) -> int:
    profile = _profile_from_args(args)
    iface = resolve_vm(args.domain, args.connect, args.mac)
    backend = HostBackend()
    live = inspect_host(backend, [iface.device])
    plan = build_host_apply(args.domain, profile, [], iface.device, args.direction, live,
                            kind="vm")
    print(f"# VM: {args.domain}, {iface.type} {iface.source}, MAC {iface.mac}")
    print(f"# host device {iface.device}, slot {plan.slot.slot}, {args.direction} "
          "(egress = from the VM)")
    for command in plan.commands:
        print("+ " + shlex.join(command))
    if args.dry_run:
        return 0
    require_root()
    try:
        run_plan(backend, plan.commands, plan.rollback)
    except CommandError as exc:
        print(f"wanshape: apply-vm failed and was rolled back: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(f"{args.domain}: applied {profile.name} to {args.direction} on {iface.device}")
    return 0


def _clear_host(args: argparse.Namespace) -> int:
    backend = HostBackend()
    live = inspect_host(backend)
    if args.all:
        slots = sorted(live.slots.values(), key=lambda x: x.slot)
    else:
        slot = live.by_name(args.name)
        if not slot:
            print(f"{args.name}: no host-mode shaping found")
            return 0
        slots = [slot]
    if not slots:
        print("no host-mode shaping found")
        return 0
    for slot in slots:
        commands = build_host_clear(slot, live)
        for command in commands:
            print("+ " + shlex.join(command))
        if args.dry_run:
            continue
        require_root()
        run_plan(backend, commands)
        label = "VM" if slot.kind == "vm" else "host-mode"
        print(f"{slot.name}: cleared {label} shaping on {slot.iface}")
        # Later slots must see this one gone (e.g. to decide on removing clsact).
        live = inspect_host(backend)
    return 0


def _profile_from_args(args: argparse.Namespace) -> Profile:
    if args.profile:
        return load_profiles()[args.profile] if args.profile in load_profiles() else _unknown(args.profile)
    # CLI form is gemodel:P,R,1-H,1-K; the profile form is "gemodel P R 1-H 1-K".
    loss_model = (
        " ".join(args.loss_model.replace(":", ",").split(",")).strip()
        if args.loss_model else None
    )
    direction = Direction(
        delay=args.delay,
        jitter=args.jitter,
        distribution=getattr(args, "distribution", None),
        loss=args.loss,
        loss_model=loss_model,
        rate=args.rate,
    )
    profile = Profile(
        name="inline",
        description="inline CLI settings",
        reorder_on_jitter=not args.no_reorder,
        up=direction,
        down=direction,
    )
    profile.validate()
    return profile


def _unknown(name: str) -> Profile:
    raise WanshapeError(f"unknown profile {name!r}; try `wanshape list`")


def _print_plan(plan, backend, target: Target) -> None:
    print(f"# target: {target.name} ({target.runtime}, {target.network_mode}, pid {target.pid})")
    print(f"# netns {target.netns_inode}, iface {target.iface}")
    for command in plan.commands:
        print("+ " + shlex.join(backend.command(command)))


def _entry_for(entries: dict, target: Target) -> dict | None:
    """The state entry for this container, or for another member of its netns."""
    if target.id in entries:
        return entries[target.id]
    return next((e for e in entries.values()
                 if e.get("netns_inode") == target.netns_inode), None)


def _rollback(target: Target, backend, plan) -> None:
    """Undo a failed container apply: remove everything wanshape set on the target.

    For a re-apply this also removes the shaping that was there before; the
    message says so. The state entry keeps what the first apply recorded about
    the interface, so the clear restores the original root qdisc and clsact.
    """
    entries = load_state()
    previous = _entry_for(entries, target) or {}
    same_netns = previous.get("netns_inode") == target.netns_inode
    prior = previous.get("prior_root_qdisc") if same_netns else plan.prior_root_qdisc
    added_clsact = plan.added_clsact or (same_netns and bool(previous.get("added_clsact")))
    try:
        live = inspect_live(backend, target.iface)
        undo = build_clear_plan(target.iface, "both", live, added_clsact, prior)
    except WanshapeError as exc:
        print(f"wanshape: could not roll back: {exc}", file=sys.stderr)
        return
    for command in undo.commands:
        backend.run_result(command)
    for key in [k for k, e in entries.items()
                if k == target.id or e.get("netns_inode") == target.netns_inode]:
        entries.pop(key)
    save_state(entries)
    print(f"wanshape: rolled back; {target.name} is not shaped "
          "(use --allow-partial to keep the direction that worked)", file=sys.stderr)


def _prune(entries: dict) -> None:
    kept = {}
    for key, entry in entries.items():
        try:
            target = resolve(entry.get("id") or key, entry.get("runtime"))
        except WanshapeError:
            print(f"pruned {entry.get('name', key)}: container is gone")
            continue
        if entry.get("netns_inode") != target.netns_inode:
            print(f"pruned {entry.get('name', key)}: container was recreated")
            continue
        kept[key] = entry
    save_state(kept)


def _scan_running() -> list[Target]:
    """Every running container this user can shape (host-network ones are skipped)."""
    found: list[Target] = []
    for runtime in ("podman", "docker"):
        try:
            result = subprocess.run([runtime, "ps", "-q", "--no-trunc"],
                                    text=True, capture_output=True)
        except FileNotFoundError:
            continue
        for cid in result.stdout.split():
            try:
                target = resolve(cid, runtime)
                get_backend(target)
            except WanshapeError:
                continue  # host networking, rootful without root, gone meanwhile
            found.append(target)
    return found


def _save_and_report(target, profile_name, direction, live, plan, partial=False) -> int:
    entries = load_state()
    prior_root_qdisc = plan.prior_root_qdisc or "unknown"
    added_clsact = plan.added_clsact
    # Re-applying over our own shaping: what we see now is wanshape's state, not the
    # original. Keep what the first apply recorded so `clear` can still restore it.
    previous = _entry_for(entries, target)
    # One network namespace is one shaping domain, whichever member it was applied to.
    for key in [k for k, e in entries.items()
                if k != target.id and e.get("netns_inode") == target.netns_inode]:
        entries.pop(key)
    if previous and previous.get("netns_inode") == target.netns_inode:
        if live.root_handle == HANDLE:
            prior_root_qdisc = previous.get("prior_root_qdisc", prior_root_qdisc)
        if live.clsact and live.our_filter:
            added_clsact = bool(previous.get("added_clsact")) or added_clsact
    entries[target.id] = make_entry(
        target, profile_name, direction, prior_root_qdisc, added_clsact
    )
    save_state(entries)
    suffix = " (partial)" if partial else ""
    print(f"{target.name}: applied {profile_name} to {direction}{suffix}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

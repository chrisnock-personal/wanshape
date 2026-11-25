"""Command-line interface for wanshape."""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from . import timer, vmhook
from .backends import get_backend
from .errors import CommandError, WanshapeError
from .host import (
    HostBackend,
    build_host_apply,
    build_host_clear,
    host_ports_for,
    inspect_host,
    parse_ports,
    remote_ports_from_container,
    require_root,
    resolve_host_iface,
    run_plan,
)
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
    apply_cmd.add_argument("container", nargs="+",
                           help="one or more containers (space- or comma-separated)")
    _add_profile_args(apply_cmd)
    apply_cmd.add_argument("--runtime", choices=("podman", "docker"))
    apply_cmd.add_argument("--iface", help="override the resolved interface")
    apply_cmd.add_argument("--backend", choices=("auto", "nsenter", "sidecar"), default="auto")
    apply_cmd.add_argument("--force", action="store_true", help="replace foreign qdisc/filters")
    apply_cmd.add_argument("--allow-partial", action="store_true",
                           help="if one direction fails, keep the other instead of rolling back")

    host = sub.add_parser(
        "apply-host",
        help="shape a host-network container (or any ports) on a host interface; needs root",
        description="Shape traffic by local port on a host interface. With a container "
        "name, the ports come from its wanshape.ports label or its *SIP_PORT and "
        "*PORT_LOW/HIGH environment variables; --ports overrides that, and then NAME is "
        "only a label and needs no container.",
    )
    host.add_argument("name", nargs="+",
                      help="host-network container(s), or a label when --ports is given")
    _add_profile_args(host)
    host.add_argument("--ports", help="local ports to shape, e.g. 5060,10000-20000 (UDP and TCP)")
    host.add_argument("--remote-ports", metavar="PORTS",
                      help="also shape traffic to/from these remote ports, e.g. 8088 for "
                      "clients on random local ports (SIP over WebSocket)")
    host.add_argument("--peer", metavar="ADDRESS",
                      help="only traffic with this address; alone, all traffic with it")
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
    vm.add_argument("domain", nargs="+", help="libvirt domain name(s)")
    _add_profile_args(vm)
    vm.add_argument("--connect", "-c", default="qemu:///system", metavar="URI",
                    help="libvirt URI (default qemu:///system)")
    vm.add_argument("--mac", help="which interface, when the VM has several")
    vm.add_argument("--persist", action="store_true",
                    help="re-apply whenever libvirt starts the VM (needs install-vm-hook once)")

    hook = sub.add_parser("vm-hook", help=argparse.SUPPRESS)
    hook.add_argument("hook_args", nargs="*")
    sub.add_parser("install-vm-hook",
                   help="install the libvirt hook that re-applies --persist VM shaping; root")
    sub.add_parser("uninstall-vm-hook", help="remove that libvirt hook; root")

    clear_host = sub.add_parser("clear-host", aliases=["clear-vm"],
                                help="remove host-mode or VM shaping")
    clear_host.add_argument("name", nargs="*", help="target name(s)")
    clear_host.add_argument("--all", action="store_true", help="clear every host-mode target")
    clear_host.add_argument("--dry-run", action="store_true")

    status = sub.add_parser("status", help="show live shaping state")
    status.add_argument("container", nargs="*", help="limit to these targets")
    status.add_argument("--all", action="store_true",
                        help="also scan every running container for wanshape shaping, "
                        "including any the state file doesn't know about")
    status.add_argument("--prune", action="store_true",
                        help="drop state entries for containers that are gone or recreated")

    clear = sub.add_parser("clear", help="remove wanshape's qdiscs")
    clear.add_argument("container", nargs="+")
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
    extra = parser.add_argument_group("more netem options (inline profile)")
    extra.add_argument("--delay-correlation", metavar="PCT",
                       help="how much each delay depends on the last, e.g. 25%%")
    extra.add_argument("--reorder", metavar="PCT[,CORR]",
                       help="send this share at once, ahead of others, e.g. 25%%,50%%")
    extra.add_argument("--duplicate", metavar="PCT", help="duplicate packets, e.g. 1%%")
    extra.add_argument("--corrupt", metavar="PCT", help="flip a bit in packets, e.g. 0.1%%")
    extra.add_argument("--slot", metavar="MIN[,MAX]",
                       help="release packets in bursts, e.g. 10ms,30ms (Wi-Fi, LTE)")
    extra.add_argument("--seed", help="fixed random seed, for repeatable runs")
    extra.add_argument("--raw-netem", metavar="ARGS",
                       help="further netem arguments, appended as given, e.g. \"ecn\"")
    parser.add_argument("--direction", choices=("egress", "ingress", "both"), default="both",
                        help="egress = upload from the target, ingress = download")
    parser.add_argument("--no-reorder", action="store_true",
                        help="attach pfifo under netem to prevent jitter reordering")
    parser.add_argument("--rate-mode", choices=("netem", "accurate"),
                        help="accurate: HTB enforces --rate, netem adds the rest "
                        "(steadier than netem's own rate); also overrides a profile's")
    parser.add_argument("--dry-run", action="store_true", help="print commands only")
    parser.add_argument("--keep-going", action="store_true",
                        help="with several targets: if one fails, keep the others "
                        "(default: undo them all)")
    parser.add_argument("--duration", metavar="TIME",
                        help="clear automatically after this long, e.g. 90s, 5m, 1h "
                        "(a one-shot systemd timer)")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        handler = {"apply-host": _apply_host, "clear-host": _clear_host,
                   "clear-vm": _clear_host, "apply-vm": _apply_vm, "vm-hook": _vm_hook,
                   "install-vm-hook": _install_vm_hook,
                   "uninstall-vm-hook": _uninstall_vm_hook}.get(
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
            print(f"{name:14} {profile.description}")
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
        seconds = _duration(args)
        profile = _profile_from_args(args)
        names = _names(args.container)
        # Resolve and plan every target before changing anything.
        preps = []
        seen: dict[int, str] = {}
        for name in names:
            target = resolve(name, args.runtime, args.iface)
            backend = get_backend(target, args.backend)
            target.verify()
            if target.netns_inode in seen:
                print(f"# {target.name} shares a network namespace with {seen[target.netns_inode]}"
                      ": shaped once", file=sys.stderr)
                continue
            seen[target.netns_inode] = target.name
            live = inspect_live(backend, target.iface)
            plan = build_apply_plan(profile, target.iface, args.direction, live, args.force)
            _print_plan(plan, backend, target)
            members = [m for m in netns_members(target) if m not in names]
            if members:
                print(f"# {target.name} shares its network namespace with {', '.join(members)}: "
                      "they are shaped too", file=sys.stderr)
            preps.append((target, backend, live, plan))
        if args.dry_run:
            return 0
        done, failed = [], []
        for prep in preps:
            if _apply_container(prep, args, profile, seconds):
                done.append(prep)
                continue
            failed.append(prep[0].name)
            if not args.keep_going:
                for target, backend, _, plan in reversed(done):
                    _rollback(target, backend, plan)
                    timer.cancel(timer.unit_name("container", target.name))
                return EXIT_ERROR
        return _summary([p[0].name for p in done], failed)

    @staticmethod
    def status(args: argparse.Namespace) -> int:
        entries = load_state()
        if args.prune:
            _prune(entries)
            entries = load_state()
        targets: list[tuple[Target, dict | None]] = []
        names = _names(args.container)
        if names:
            host_names = {s.name for s in inspect_host(HostBackend()).slots.values()}
            for name in names:
                try:
                    target = resolve(name)
                    targets.append((target, _entry_for(entries, target)))
                except WanshapeError:
                    # Host-network containers and --ports labels live in host mode only.
                    if name not in host_names:
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
            if not names or slot.name in names
        ]
        for slot in host_slots:
            gone = not host_live.iface(slot.iface).exists
            what = (f"vm mode, device={slot.iface}" if slot.kind == "vm"
                    else f"host mode, iface={slot.iface} {slot.describe()}")
            left = timer.remaining(slot.until)
            print(f"{slot.name}: {what} profile={slot.profile} direction={slot.direction}"
                  + (f" ({left})" if left else "")
                  + (f" STALE ({slot.iface} is gone; re-apply or clear-host)" if gone else ""))
            for dev in (slot.up, slot.down):
                print(host_backend.run(["tc", "-s", "qdisc", "show", "dev", dev]).strip())
        shown = len(host_slots)
        for target, entry in targets:
            backend = get_backend(target)
            live = inspect_live(backend, target.iface)
            if entry is None and args.all and not names and not (
                    live.egress or live.ingress):
                continue  # scanned and not shaped: not worth a line
            shown += 1
            profile = entry.get("profile", "unknown") if entry else "unknown"
            stale = bool(entry and entry.get("netns_inode") != target.netns_inode)
            left = timer.remaining(entry.get("until")) if entry else None
            print(f"{target.name}: profile={profile}"
                  f" egress={'on' if live.egress else 'off'}"
                  f" ingress={'on' if live.ingress else 'off'}"
                  + (f" ({left})" if left and (live.egress or live.ingress) else "")
                  + (" STALE (container recreated)" if stale else ""))
            if live.egress or live.ingress:
                print(stats(backend, target.iface))
        if not shown:
            print("no wanshape shaping found" if args.all else "no wanshape state found")
        return 0

    @staticmethod
    def clear(args: argparse.Namespace) -> int:
        targets = [resolve(name, args.runtime, args.iface) for name in _names(args.container)]
        for target in targets:
            _clear_container(target, args)
        return 0


def _clear_container(target: Target, args: argparse.Namespace) -> None:
    """Clear one container target (any member of a shared namespace)."""
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
        return
    _print_plan(plan, backend, target)
    if args.dry_run:
        return
    for command in plan.commands:
        target.verify()
        backend.run(command)
    timer.cancel(timer.unit_name("container", target.name))
    entries = load_state()
    for key in [k for k, e in entries.items()
                if k == target.id or e.get("netns_inode") == target.netns_inode]:
        entries.pop(key)
    save_state(entries)
    print(f"{target.name}: cleared; state removed")


def _apply_container(prep, args: argparse.Namespace, profile: Profile,
                     seconds: int | None) -> bool:
    """Apply one planned container target; False if it failed (and was handled)."""
    target, backend, live, plan = prep
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
        print(f"wanshape: {target.name}: {failed_group} shaping failed: {error}", file=sys.stderr)
        if applied and args.allow_partial:
            _save_and_report(target, profile.name, "+".join(applied), live, plan, partial=True)
            print(f"wanshape: {'+'.join(applied)} remains active; run `wanshape clear "
                  f"{target.name}`", file=sys.stderr)
        else:
            _rollback(target, backend, plan)
        return False
    unit = timer.unit_name("container", target.name)
    if seconds:
        timer.schedule(unit, seconds, ["clear", target.id, "--runtime", target.runtime])
    else:
        timer.cancel(unit)
    _save_and_report(target, profile.name, args.direction, live, plan,
                     until=timer.until(seconds) if seconds else None)
    return True


def _names(values) -> list[str]:
    """Targets from positional arguments: space- and/or comma-separated, deduplicated."""
    if isinstance(values, str):
        values = [values]
    names: list[str] = []
    for value in values or []:
        for name in str(value).split(","):
            name = name.strip()
            if name and name not in names:
                names.append(name)
    return names


def _summary(done: list[str], failed: list[str]) -> int:
    if failed:
        print(f"wanshape: failed: {', '.join(failed)}"
              + (f"; applied: {', '.join(done)}" if done else ""), file=sys.stderr)
        return EXIT_ERROR
    return 0


def _simulate(live, plan) -> None:
    """Record a planned host/VM target in `live`, so later targets plan around it."""
    slot = plan.slot
    live.slots = {k: v for k, v in live.slots.items() if v.name != slot.name}
    live.slots[slot.slot] = slot
    live.ifbs |= {slot.up, slot.down}
    state = live.iface(slot.iface)
    state.clsact = True
    for command in plan.commands:
        if command[:3] == ["tc", "filter", "add"]:
            state.prefs(command[5]).add(int(command[7]))


def _run_host_plans(backend, plans, args: argparse.Namespace, profile: Profile,
                    seconds: int | None) -> int:
    """Apply planned host/VM targets in order; all or nothing unless --keep-going."""
    require_root()
    done, failed = [], []
    for name, plan in plans:
        try:
            run_plan(backend, plan.commands, plan.rollback)
        except CommandError as exc:
            print(f"wanshape: {name}: failed and was rolled back: {exc}", file=sys.stderr)
            failed.append(name)
            if not args.keep_going:
                for done_name, done_plan in reversed(done):
                    live = inspect_host(backend)
                    slot = live.by_name(done_name)
                    if slot:
                        for command in build_host_clear(slot, live):
                            backend.run_result(command)
                    timer.cancel(timer.unit_name(done_plan.slot.kind, done_name))
                    print(f"wanshape: {done_name}: undone (use --keep-going to keep "
                          "the targets that worked)", file=sys.stderr)
                return EXIT_ERROR
            continue
        _host_timer(plan.slot.kind, name, seconds)
        print(f"{name}: applied {profile.name} to {args.direction} on {plan.slot.iface}"
              + (f" {plan.slot.describe()}" if plan.slot.describe() else ""))
        done.append((name, plan))
    return _summary([n for n, _ in done], failed)


def _apply_host(args: argparse.Namespace) -> int:
    seconds = _duration(args)
    profile = _profile_from_args(args)
    names = _names(args.name)
    if len(names) > 1 and (args.ports or args.remote_ports or args.peer):
        raise WanshapeError(
            "--ports, --remote-ports and --peer describe one target: give one name, or "
            "label each container (wanshape.ports, wanshape.remote_ports)")
    backend = HostBackend()
    iface = resolve_host_iface(backend, args.iface, args.via or args.peer)
    live = inspect_host(backend, [iface])
    plans = []
    for name in names:
        remote = (parse_ports(args.remote_ports) if args.remote_ports
                  else remote_ports_from_container(name, args.runtime))
        ports, source = host_ports_for(name, args.runtime, args.ports,
                                       optional=bool(remote or args.peer))
        plan = build_host_apply(name, profile, ports, iface, args.direction, live,
                                remote_ports=remote, peer=args.peer,
                                until=timer.until(seconds) if seconds else None)
        print(f"# host target: {name}, {plan.slot.describe()} (local ports from {source})")
        print(f"# iface {iface}, slot {plan.slot.slot}, {args.direction}")
        for command in plan.commands:
            print("+ " + shlex.join(command))
        _simulate(live, plan)
        plans.append((name, plan))
    if args.dry_run:
        return 0
    return _run_host_plans(backend, plans, args, profile, seconds)


def _apply_vm(args: argparse.Namespace) -> int:
    seconds = _duration(args)
    profile = _profile_from_args(args)
    names = _names(args.domain)
    if len(names) > 1 and args.mac:
        raise WanshapeError("--mac picks one VM's interface: give one VM with it")
    vms = [(name, resolve_vm(name, args.connect, args.mac)) for name in names]
    backend = HostBackend()
    live = inspect_host(backend, [iface.device for _, iface in vms])
    plans = []
    for name, iface in vms:
        plan = build_host_apply(name, profile, [], iface.device, args.direction, live,
                                kind="vm", until=timer.until(seconds) if seconds else None)
        print(f"# VM: {name}, {iface.type} {iface.source}, MAC {iface.mac}")
        print(f"# host device {iface.device}, slot {plan.slot.slot}, {args.direction} "
              "(egress = from the VM)")
        for command in plan.commands:
            print("+ " + shlex.join(command))
        _simulate(live, plan)
        plans.append((name, plan))
    if args.dry_run:
        return 0
    code = _run_host_plans(backend, plans, args, profile, seconds)
    if code == 0 and args.persist:
        for name, iface in vms:
            vmhook.remember(name, profile, args.direction, iface.mac)
        print(f"recorded in {vmhook.CONFIG}: re-applied whenever libvirt starts "
              + ", ".join(n for n, _ in vms))
        if not vmhook.HOOK.exists():
            print("wanshape: the libvirt hook isn't installed yet: run "
                  "`wanshape install-vm-hook` once", file=sys.stderr)
    return code


def _vm_hook(args: argparse.Namespace) -> int:
    """Called by libvirt (see vmhook.py). Never fails, so it never blocks a VM start."""
    hook_args = list(args.hook_args) + ["", "", ""]
    domain, operation, sub_operation = hook_args[:3]
    try:
        xml_text = sys.stdin.read() if not sys.stdin.isatty() else ""
        backend = HostBackend()

        def apply_fn(name, profile, direction, device):
            live = inspect_host(backend, [device])
            plan = build_host_apply(name, profile, [], device, direction, live, kind="vm")
            run_plan(backend, plan.commands, plan.rollback)

        def release_fn(name):
            live = inspect_host(backend)
            slot = live.by_name(name)
            if slot and not live.iface(slot.iface).exists:
                for command in build_host_clear(slot, live):
                    backend.run_result(command)

        done = vmhook.run_hook(domain, operation, sub_operation, xml_text, apply_fn, release_fn)
        if done != "not a wanshape VM":
            print(f"wanshape vm-hook: {domain} {operation} {sub_operation}: {done}",
                  file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - a hook must never fail the VM start
        print(f"wanshape vm-hook: {domain} {operation} {sub_operation}: {exc}", file=sys.stderr)
    return 0


def _install_vm_hook(_args: argparse.Namespace) -> int:
    require_root()
    path = vmhook.install()
    print(f"installed {path}")
    print("libvirt reads its hooks when it starts: restart it once, e.g. "
          "`sudo systemctl restart virtqemud` (or libvirtd)")
    return 0


def _uninstall_vm_hook(_args: argparse.Namespace) -> int:
    require_root()
    print(f"removed {vmhook.HOOK}" if vmhook.uninstall() else "the hook wasn't installed")
    return 0


def _duration(args: argparse.Namespace) -> int | None:
    """Parse --duration and check a timer can be set, before anything changes."""
    if not getattr(args, "duration", None):
        return None
    seconds = timer.parse_duration(args.duration)
    if not args.dry_run:
        timer.check_available()
    return seconds


def _host_timer(kind: str, name: str, seconds: int | None) -> None:
    unit = timer.unit_name(kind, name)
    if seconds:
        timer.schedule(unit, seconds, ["clear-host", name])
        print(f"{name}: {timer.remaining(timer.until(seconds))}")
    else:
        timer.cancel(unit)


def _clear_host(args: argparse.Namespace) -> int:
    backend = HostBackend()
    live = inspect_host(backend)
    names = _names(args.name)
    if args.all == bool(names):
        raise WanshapeError("give target name(s) or --all")
    if args.all:
        slots = sorted(live.slots.values(), key=lambda x: x.slot)
    else:
        slots = []
        for name in names:
            slot = live.by_name(name)
            if slot:
                slots.append(slot)
            else:
                print(f"{name}: no host-mode shaping found")
        if not args.dry_run:
            for name in names:
                if name in vmhook.load():
                    require_root()
                    vmhook.forget(name)
                    print(f"{name}: no longer re-applied when the VM starts")
        if not slots:
            return 0
    if not slots:
        print("no host-mode shaping found")
        return 0
    for slot in slots:
        slot = live.by_name(slot.name) or slot
        commands = build_host_clear(slot, live)
        for command in commands:
            print("+ " + shlex.join(command))
        if args.dry_run:
            continue
        require_root()
        run_plan(backend, commands)
        timer.cancel(timer.unit_name(slot.kind, slot.name))
        label = "VM" if slot.kind == "vm" else "host-mode"
        print(f"{slot.name}: cleared {label} shaping on {slot.iface}")
        # Later slots must see this one gone (e.g. to decide on removing clsact).
        live = inspect_host(backend)
    return 0


def _profile_from_args(args: argparse.Namespace) -> Profile:
    profile = _base_profile(args)
    if getattr(args, "rate_mode", None):
        profile.rate_mode = args.rate_mode
        profile.validate()
    return profile


def _base_profile(args: argparse.Namespace) -> Profile:
    if args.profile:
        profiles = load_profiles()
        if args.profile not in profiles:
            _unknown(args.profile)
        return profiles[args.profile]
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
        delay_correlation=getattr(args, "delay_correlation", None),
        reorder=_spaced(getattr(args, "reorder", None)),
        duplicate=getattr(args, "duplicate", None),
        corrupt=getattr(args, "corrupt", None),
        slot=_spaced(getattr(args, "slot", None)),
        seed=getattr(args, "seed", None),
        raw=getattr(args, "raw_netem", None),
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


def _spaced(value: str | None) -> str | None:
    """CLI lists use commas ("25%,50%"); profiles use spaces like tc."""
    return " ".join(value.replace(",", " ").split()) if value else None


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


def _save_and_report(target, profile_name, direction, live, plan, partial=False,
                     until=None) -> int:
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
        target, profile_name, direction, prior_root_qdisc, added_clsact, until
    )
    save_state(entries)
    suffix = " (partial)" if partial else ""
    if until:
        suffix += f", {timer.remaining(until)}"
    print(f"{target.name}: applied {profile_name} to {direction}{suffix}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

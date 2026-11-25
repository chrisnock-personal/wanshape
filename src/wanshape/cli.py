"""Command-line interface for wanshape."""

from __future__ import annotations

import argparse
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from .backends import get_backend
from .errors import CommandError, WanshapeError
from .inspector import HANDLE, inspect_live, stats
from .planner import build_apply_plan, build_clear_plan
from .profiles import Direction, Profile, ProfileError, load_profiles
from .state import load_state, make_entry, save_state
from .target import Target, resolve

EXIT_ERROR = 1
EXIT_UNSUPPORTED = 2
EXIT_PREREQ = 3
EXIT_CONFLICT = 4


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wanshape",
        description="Apply netem/tc WAN impairments to individual containers",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="check tools, kernel modules and runtimes")
    sub.add_parser("list", help="list available profiles")
    show = sub.add_parser("show", help="expand a profile")
    show.add_argument("profile")

    apply_cmd = sub.add_parser("apply", help="apply a profile to a container")
    apply_cmd.add_argument("container")
    mode = apply_cmd.add_mutually_exclusive_group(required=True)
    mode.add_argument("--profile", help="named profile")
    mode.add_argument("--delay", help="inline profile: one-way delay, e.g. 150ms")
    apply_cmd.add_argument("--jitter", help="delay variation, e.g. 30ms")
    apply_cmd.add_argument("--loss", help="random loss percentage, e.g. 1%%")
    apply_cmd.add_argument("--loss-model", help="bursty loss, e.g. gemodel:1%%,25%%,100%%,0%%")
    apply_cmd.add_argument("--rate", help="bandwidth cap, e.g. 768kbit")
    apply_cmd.add_argument("--runtime", choices=("podman", "docker"))
    apply_cmd.add_argument("--iface", help="override the resolved interface")
    apply_cmd.add_argument("--direction", choices=("egress", "ingress", "both"), default="both")
    apply_cmd.add_argument("--no-reorder", action="store_true",
                           help="attach pfifo under netem to prevent jitter reordering")
    apply_cmd.add_argument("--backend", choices=("auto", "nsenter", "sidecar"), default="auto")
    apply_cmd.add_argument("--dry-run", action="store_true", help="print commands only")
    apply_cmd.add_argument("--force", action="store_true", help="replace foreign qdisc/filters")
    apply_cmd.add_argument("--duration", help="not implemented in this prototype")

    status = sub.add_parser("status", help="show live shaping state")
    status.add_argument("container", nargs="?")

    clear = sub.add_parser("clear", help="remove wanshape's qdiscs")
    clear.add_argument("container")
    clear.add_argument("--direction", choices=("egress", "ingress", "both"), default="both")
    clear.add_argument("--runtime", choices=("podman", "docker"))
    clear.add_argument("--iface")
    clear.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        handler = getattr(_Commands, args.command)
        return handler(args)
    except ProfileError as exc:
        print(f"wanshape: invalid profile: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except WanshapeError as exc:
        print(f"wanshape: {exc}", file=sys.stderr)
        return EXIT_CONFLICT if "foreign" in str(exc) else EXIT_ERROR
    except KeyboardInterrupt:
        print("wanshape: interrupted", file=sys.stderr)
        return 130


class _Commands:
    @staticmethod
    def doctor(_args: argparse.Namespace) -> int:
        missing = [tool for tool in ("tc", "nsenter", "ip", "podman") if not shutil.which(tool)]
        if missing:
            print(f"MISSING tools: {', '.join(missing)}")
            return EXIT_PREREQ
        print("tools: tc, nsenter, ip, podman found"
              + (" (docker also found)" if shutil.which("docker") else ""))
        modules = ("sch_netem", "ifb", "act_mirred", "cls_matchall", "cls_u32")
        for module in modules:
            if Path(f"/sys/module/{module}").exists():
                status = "loaded"
            else:
                result = subprocess.run(["modinfo", module], text=True, capture_output=True)
                status = "available (autoload on use)" if result.returncode == 0 else "NOT FOUND"
            print(f"{module}: {status}")
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
            if applied:
                _save_and_report(target, profile.name, "+".join(applied), live, plan,
                                 partial=True)
                print(f"wanshape: {'+'.join(applied)} remains active; run `wanshape clear {target.name}`",
                      file=sys.stderr)
            return EXIT_ERROR
        return _save_and_report(target, profile.name, args.direction, live, plan)

    @staticmethod
    def status(args: argparse.Namespace) -> int:
        entries = load_state()
        targets: list[tuple[Target, dict | None]] = []
        if args.container:
            target = resolve(args.container)
            targets.append((target, entries.get(target.id)))
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
                    print(f"wanshape: {entry.get('name')}: container is gone", file=sys.stderr)
                    continue
                targets.append((target, entry))
        if not targets:
            print("no wanshape state found")
        for target, entry in targets:
            backend = get_backend(target)
            live = inspect_live(backend, target.iface)
            profile = entry.get("profile", "unknown") if entry else "unknown"
            stale = bool(entry and entry.get("netns_inode") != target.netns_inode)
            print(f"{target.name}: profile={profile}"
                  f" egress={'on' if live.egress else 'off'}"
                  f" ingress={'on' if live.ingress else 'off'}"
                  + (" STALE (container recreated)" if stale else ""))
            if live.egress or live.ingress:
                print(stats(backend, target.iface))
        return 0

    @staticmethod
    def clear(args: argparse.Namespace) -> int:
        target = resolve(args.container, args.runtime, args.iface)
        backend = get_backend(target, "auto")
        entry = load_state().get(target.id, {})
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
        entries.pop(target.id, None)
        save_state(entries)
        print(f"{target.name}: cleared; state removed")
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


def _save_and_report(target, profile_name, direction, live, plan, partial=False) -> int:
    entries = load_state()
    prior_root_qdisc = plan.prior_root_qdisc or "unknown"
    added_clsact = plan.added_clsact
    # Re-applying over our own shaping: what we see now is wanshape's state, not the
    # original. Keep what the first apply recorded so `clear` can still restore it.
    previous = entries.get(target.id)
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

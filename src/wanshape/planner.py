"""Turn a profile and live state into an ordered list of ip/tc commands."""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import ConflictError, WanshapeError
from .inspector import FILTER_PREF, HANDLE, IFB, LiveState
from .profiles import Profile

Command = list[str]


@dataclass
class Plan:
    egress: list[Command] = field(default_factory=list)
    ingress: list[Command] = field(default_factory=list)
    prior_root_qdisc: str | None = None
    added_clsact: bool = False

    @property
    def commands(self) -> list[Command]:
        return self.egress + self.ingress

    @property
    def empty(self) -> bool:
        return not self.commands


def build_apply_plan(
    profile: Profile,
    iface: str,
    direction: str,
    live: LiveState,
    force: bool = False,
) -> Plan:
    if direction not in ("egress", "ingress", "both"):
        raise WanshapeError(f"invalid direction {direction!r}")
    plan = Plan(prior_root_qdisc=f"{live.root_kind} {live.root_handle or ''}".strip())

    if direction in ("egress", "both"):
        if live.root_handle != HANDLE and live.root_kind not in ("noqueue", "fq_codel"):
            if not force:
                raise ConflictError(
                    f"{iface} has a foreign root qdisc "
                    f"({live.root_kind} {live.root_handle}); use --force to replace it"
                )
        plan.egress.append(
            [
                "tc", "qdisc", "replace", "dev", iface,
                "root", "handle", HANDLE, "netem", *profile.up.netem_args(),
            ]
        )
        if live.root_handle == HANDLE and live.root_kind == "netem":
            # Re-apply: `replace` would only change netem's options and keep any
            # pfifo child from before, so rebuild the root to get the child right.
            plan.egress.insert(0, ["tc", "qdisc", "del", "dev", iface, "root"])
        if not profile.reorder_on_jitter:
            plan.egress.append(
                ["tc", "qdisc", "replace", "dev", iface, "parent", f"{HANDLE}1",
                 "pfifo", "limit", "1000"]
            )

    if direction in ("ingress", "both"):
        # An empty clsact (e.g. left by a clear that had no state entry) is fine to use.
        if live.foreign_filters or (live.ifb and not live.our_filter):
            if not force:
                raise ConflictError(
                    f"{iface} already has foreign clsact/IFB state; use --force to replace it"
                )
        plan.added_clsact = not live.clsact
        plan.ingress = []
        if not live.ifb:
            # Re-applying keeps the existing IFB; `ip link add` would fail with EEXIST.
            plan.ingress.append(["ip", "link", "add", IFB, "type", "ifb"])
        plan.ingress += [
            ["ip", "link", "set", IFB, "up"],
            ["tc", "qdisc", "replace", "dev", iface, "clsact"],
        ]
        if not live.our_filter:
            # matchall can't be replaced in place (EEXIST), and ours never changes:
            # it always redirects everything to the IFB. Keep it on re-apply.
            plan.ingress.append(
                ["tc", "filter", "replace", "dev", iface, "ingress",
                 "pref", FILTER_PREF, "protocol", "all", "matchall",
                 "action", "mirred", "egress", "redirect", "dev", IFB])
        plan.ingress.append(
            ["tc", "qdisc", "replace", "dev", IFB, "root", "handle", HANDLE,
             "netem", *profile.down.netem_args()])
    return plan


SIMPLE_QDISCS = {"fq_codel", "pfifo_fast", "pfifo"}


def build_clear_plan(
    iface: str,
    direction: str,
    live: LiveState,
    added_clsact: bool = False,
    prior_root_qdisc: str | None = None,
) -> Plan:
    if direction not in ("egress", "ingress", "both"):
        raise WanshapeError(f"invalid direction {direction!r}")
    plan = Plan(added_clsact=added_clsact)
    if direction in ("egress", "both") and live.root_handle == HANDLE:
        plan.egress.append(["tc", "qdisc", "del", "dev", iface, "root"])
        prior_kind = (prior_root_qdisc or "").split()[0] if prior_root_qdisc else ""
        if prior_kind in SIMPLE_QDISCS:
            plan.egress.append(["tc", "qdisc", "replace", "dev", iface, "root", prior_kind])
    if direction in ("ingress", "both"):
        if live.our_filter:
            plan.ingress.append(
                ["tc", "filter", "del", "dev", iface, "ingress", "pref", FILTER_PREF]
            )
        # Remove clsact if wanshape added it, or if nothing but our filter is on it.
        if live.clsact and (added_clsact or not live.foreign_filters):
            plan.ingress.append(["tc", "qdisc", "del", "dev", iface, "clsact"])
        if live.ifb:
            plan.ingress.append(["ip", "link", "del", IFB])
    return plan

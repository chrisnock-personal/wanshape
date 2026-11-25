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
        if live.root_handle == HANDLE:
            # Re-apply: `replace` would only change the root's options and keep its
            # children (a pfifo, or netem under HTB), so rebuild the tree.
            plan.egress.append(["tc", "qdisc", "del", "dev", iface, "root"])
        plan.egress += netem_tree("replace", iface, profile.up, profile)

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
        if live.ifb:
            # Same as egress: rebuild the IFB's tree rather than patch it.
            plan.ingress.append(["tc", "qdisc", "del", "dev", IFB, "root"])
        plan.ingress += netem_tree("replace", IFB, profile.down, profile)
    return plan


SIMPLE_QDISCS = {"fq_codel", "pfifo_fast", "pfifo"}
NETEM_UNDER_HTB = "5754:"


def netem_tree(verb: str, dev: str, spec, profile: Profile) -> list[Command]:
    """The qdiscs that shape one direction on `dev`, rooted at handle 5753:.

    Default: netem at the root (it does the rate itself), with a pfifo child
    when jitter mustn't reorder. With rate_mode "accurate" and a rate: an HTB
    root whose one class holds the rate, and netem (without a rate) below it,
    because netem's own rate is affected by timer granularity and bursts.
    """
    accurate = profile.rate_mode == "accurate" and spec.rate
    if not accurate:
        commands = [["tc", "qdisc", verb, "dev", dev, "root", "handle", HANDLE,
                     "netem", *spec.netem_args()]]
        netem = HANDLE
    else:
        commands = [
            ["tc", "qdisc", verb, "dev", dev, "root", "handle", HANDLE, "htb", "default", "1"],
            ["tc", "class", verb, "dev", dev, "parent", HANDLE, "classid", f"{HANDLE}1",
             "htb", "rate", spec.rate, "ceil", spec.rate],
            ["tc", "qdisc", verb, "dev", dev, "parent", f"{HANDLE}1", "handle",
             NETEM_UNDER_HTB, "netem", *spec.netem_args(include_rate=False)],
        ]
        netem = NETEM_UNDER_HTB
    if not profile.reorder_on_jitter:
        commands.append(["tc", "qdisc", verb, "dev", dev, "parent", f"{netem}1",
                         "pfifo", "limit", "1000"])
    return commands


def build_clear_plan(
    iface: str,
    direction: str,
    live: LiveState,
    added_clsact: bool = False,
    prior_root_qdisc: str | None = None,
) -> Plan:
    """Commands that remove wanshape's shaping from a container interface.

    `added_clsact` is only recorded on the plan: whether clsact is removed
    depends on the live state alone (removed when nothing but wanshape's
    filter is on it), because another tool may have added filters since.
    """
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
        # Remove clsact only when nothing but our filter is on it, even if wanshape
        # added it: another tool may have put filters there since.
        if live.clsact and not live.foreign_filters:
            plan.ingress.append(["tc", "qdisc", "del", "dev", iface, "clsact"])
        if live.ifb:
            plan.ingress.append(["ip", "link", "del", IFB])
    return plan

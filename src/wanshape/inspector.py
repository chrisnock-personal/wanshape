"""Read the kernel's qdisc state; the authoritative source for status and clear."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .backends import Backend

HANDLE = "5753:"
FILTER_PREF = "49152"
IFB = "wanshape0"
_ROOT = re.compile(r"^qdisc\s+(\S+)\s+(\S+)\s+root\b")


@dataclass
class LiveState:
    root_kind: str | None
    root_handle: str | None
    clsact: bool
    our_filter: bool
    ifb: bool
    foreign_filters: bool = False  # any clsact filter that isn't wanshape's

    @property
    def egress(self) -> bool:
        return self.root_handle == HANDLE and self.root_kind in ("netem", "htb")

    @property
    def ingress(self) -> bool:
        return self.ifb and self.our_filter


def inspect_live(backend: Backend, iface: str) -> LiveState:
    qdiscs = backend.run(["tc", "qdisc", "show", "dev", iface])
    root_kind = root_handle = None
    for line in qdiscs.splitlines():
        match = _ROOT.match(line.strip())
        if match:
            root_kind, root_handle = match.group(1), match.group(2)
            break
    clsact = "clsact" in qdiscs
    filters = backend.run(["tc", "filter", "show", "dev", iface, "ingress"])
    egress_filters = (backend.run(["tc", "filter", "show", "dev", iface, "egress"])
                      if clsact else "")
    other_prefs = {p for p in re.findall(r"\bpref\s+(\d+)", filters) if p != FILTER_PREF}
    return LiveState(
        root_kind=root_kind,
        root_handle=root_handle,
        clsact=clsact,
        our_filter=_is_our_filter(filters),
        ifb=backend.run_result(["ip", "link", "show", IFB]).returncode == 0,
        foreign_filters=bool(other_prefs or egress_filters.strip()
                             or (FILTER_PREF in filters and not _is_our_filter(filters))),
    )


def _is_our_filter(text: str) -> bool:
    """Recognise our own matchall/mirred filter, not just a matching pref."""
    lines = text.splitlines()
    selected: list[str] = []
    for index, line in enumerate(lines):
        if FILTER_PREF in line:
            selected.append(line)
            selected.extend(lines[index + 1 : index + 4])
    joined = "\n".join(selected)
    return "matchall" in joined and IFB in joined


def stats(backend: Backend, iface: str) -> str:
    parts = [backend.run(["tc", "-s", "qdisc", "show", "dev", iface]).strip()]
    if backend.run_result(["ip", "link", "show", IFB]).returncode == 0:
        parts.append(backend.run(["tc", "-s", "qdisc", "show", "dev", IFB]).strip())
    return "\n".join(parts)

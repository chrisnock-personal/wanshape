"""Small XDG state file for metadata the kernel can't tell us."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def state_path() -> Path:
    root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return root / "wanshape" / "state.json"


def load_state() -> dict[str, dict]:
    path = state_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(entries: dict[str, dict]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix="state.", suffix=".json")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(entries, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def make_entry(target, profile_name: str, direction: str, prior_qdisc: str,
               added_clsact: bool) -> dict:
    return {
        "id": target.id,
        "name": target.name,
        "runtime": target.runtime,
        "netns_inode": target.netns_inode,
        "profile": profile_name,
        "direction": direction,
        "applied_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "prior_root_qdisc": prior_qdisc,
        "added_clsact": added_clsact,
        "timer": None,
    }

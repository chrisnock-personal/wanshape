"""Unit tests never touch the user's systemd: the timer calls are stubbed.

The real functions stay available in REAL_TIMER for tests/test_timer.py.
"""

from unittest import mock

from wanshape import timer as _timer

REAL_TIMER = {name: getattr(_timer, name) for name in ("cancel", "schedule", "check_available")}
for _name in REAL_TIMER:
    mock.patch.object(_timer, _name, lambda *a, **k: None).start()

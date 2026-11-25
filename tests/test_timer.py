import subprocess
import time
import unittest
from unittest import mock

from tests import REAL_TIMER
from wanshape import timer
from wanshape.errors import PrerequisiteError, WanshapeError


class DurationTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(timer.parse_duration("90"), 90)
        self.assertEqual(timer.parse_duration("90s"), 90)
        self.assertEqual(timer.parse_duration("5m"), 300)
        self.assertEqual(timer.parse_duration("1.5h"), 5400)
        for bad in ("", "5x", "-1m", "0", "0.2s", "m"):
            with self.assertRaises(WanshapeError, msg=bad):
                timer.parse_duration(bad)

    def test_unit_name_is_safe(self):
        self.assertEqual(timer.unit_name("vm", "web 1/x"), "wanshape-clear-vm-web_1_x")

    def test_remaining(self):
        self.assertIsNone(timer.remaining(None))
        now = int(time.time())
        self.assertEqual(timer.remaining(now + 252), "clears in 4m12s")
        self.assertEqual(timer.remaining(now + 7380), "clears in 2h03m")
        self.assertEqual(timer.remaining(now - 5), "clearing now")


class ScheduleTests(unittest.TestCase):
    def test_systemd_run_command(self):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "running\n", "")

        with mock.patch("subprocess.run", side_effect=fake_run), \
                mock.patch("os.geteuid", return_value=1000):
            REAL_TIMER["check_available"]()
            REAL_TIMER["schedule"]("wanshape-clear-container-web", 300, ["clear", "abc"])
        run = next(c for c in calls if c[0] == "systemd-run")
        self.assertIn("--user", run)
        self.assertIn("--unit=wanshape-clear-container-web", run)
        self.assertIn("--on-active=300s", run)
        self.assertEqual(run[-3:], ["wanshape.cli", "clear", "abc"])
        # The schedule goes through the module's cancel (stubbed here); as root
        # there is no --user.
        with mock.patch("subprocess.run", side_effect=fake_run), \
                mock.patch("os.geteuid", return_value=0):
            REAL_TIMER["cancel"]("wanshape-clear-vm-web")
        self.assertEqual(calls[-2][:3], ["systemctl", "stop", "wanshape-clear-vm-web.timer"])

    def test_unavailable_systemd_is_a_prerequisite_error(self):
        with mock.patch("shutil.which", return_value=None):
            with self.assertRaises(PrerequisiteError):
                REAL_TIMER["check_available"]()
        with mock.patch("shutil.which", return_value="/usr/bin/systemd-run"), \
                mock.patch("subprocess.run", return_value=subprocess.CompletedProcess(
                    [], 1, "offline\n", "")), \
                mock.patch("os.geteuid", return_value=1000):
            with self.assertRaises(PrerequisiteError):
                REAL_TIMER["check_available"]()


if __name__ == "__main__":
    unittest.main()

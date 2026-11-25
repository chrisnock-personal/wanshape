import os
import tempfile
import unittest
from pathlib import Path

from wanshape.profiles import Direction, Profile, ProfileError, load_profiles


class ProfileTests(unittest.TestCase):
    def test_builtins_validate(self):
        for profile in load_profiles().values():
            with self.subTest(profile.name):
                profile.validate()

    def test_netem_args(self):
        direction = Direction(
            delay="150ms", jitter="30ms", distribution="normal",
            loss="1%", rate="768kbit",
        )
        direction.validate("test.up")
        self.assertEqual(
            direction.netem_args(),
            ["delay", "150ms", "30ms", "distribution", "normal", "loss", "1%",
             "rate", "768kbit", "limit", "1000"],
        )

    def test_bursty_loss(self):
        direction = Direction(loss_model="gemodel 1% 25% 100% 0%")
        direction.validate("test.up")
        self.assertEqual(direction.netem_args()[:6],
                         ["loss", "gemodel", "1%", "25%", "100%", "0%"])

    def test_mutually_exclusive_loss(self):
        direction = Direction(loss="1%", loss_model="gemodel 1% 25% 100% 0%")
        with self.assertRaises(ProfileError):
            direction.validate("test.up")

    def test_normal_jitter_above_delay_rejected(self):
        direction = Direction(delay="10ms", jitter="20ms", distribution="normal")
        with self.assertRaises(ProfileError):
            direction.validate("test.up")

    def test_user_profile_overrides_builtin(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "wanshape"
            config.mkdir()
            (config / "profiles.toml").write_text(
                '[profiles.office]\ndescription = "custom"\n'
                '[profiles.office.up]\ndelay = "1ms"\n'
            )
            old = os.environ.get("XDG_CONFIG_HOME")
            os.environ["XDG_CONFIG_HOME"] = tmp
            try:
                self.assertEqual(load_profiles()["office"].description, "custom")
            finally:
                if old is None:
                    os.environ.pop("XDG_CONFIG_HOME", None)
                else:
                    os.environ["XDG_CONFIG_HOME"] = old

    def test_invalid_user_profile_fails_loudly(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "wanshape"
            config.mkdir()
            (config / "profiles.toml").write_text(
                '[profiles.bad]\n[profiles.bad.up]\nloss = "101%"\n'
            )
            old = os.environ.get("XDG_CONFIG_HOME")
            os.environ["XDG_CONFIG_HOME"] = tmp
            try:
                with self.assertRaises(ProfileError):
                    load_profiles()
            finally:
                if old is None:
                    os.environ.pop("XDG_CONFIG_HOME", None)
                else:
                    os.environ["XDG_CONFIG_HOME"] = old


class TimeUnitTests(unittest.TestCase):
    def test_to_ms(self):
        from wanshape.profiles import _to_ms
        self.assertEqual(_to_ms("1s"), 1000)
        self.assertEqual(_to_ms("1.5s"), 1500)
        self.assertEqual(_to_ms("20ms"), 20)
        self.assertEqual(_to_ms("500us"), 0.5)

    def test_jitter_in_seconds_validates(self):
        Direction(delay="1s", jitter="200ms", distribution="normal").validate("t")


if __name__ == "__main__":
    unittest.main()

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


class MoreNetemOptionTests(unittest.TestCase):
    """#10: correlation, reorder, duplicate, corrupt, slot, seed, raw."""

    def test_args_in_tc_order(self):
        d = Direction(delay="50ms", jitter="10ms", delay_correlation="25%",
                      distribution="normal", loss="2%", loss_correlation="30%",
                      reorder="10% 50%", duplicate="1%", corrupt="0.1%", rate="1mbit",
                      slot="5ms 20ms", seed="42", raw="ecn")
        d.validate("t")
        self.assertEqual(" ".join(d.netem_args()),
                         "delay 50ms 10ms 25% distribution normal loss 2% 30% duplicate 1% "
                         "corrupt 0.1% reorder 10% 50% rate 1mbit slot 5ms 20ms seed 42 "
                         "limit 1000 ecn")

    def test_invalid_combinations(self):
        bad = [
            Direction(delay="50ms", delay_correlation="25%"),           # no jitter
            Direction(delay="50ms", loss_model="gemodel 1%", loss_correlation="5%"),
            Direction(reorder="10%"),                                   # no delay
            Direction(delay="5ms", reorder="10% 50% 3%"),
            Direction(delay="5ms", reorder="150%"),
            Direction(duplicate="1"),
            Direction(corrupt="101%"),
            Direction(slot="fast"),
            Direction(seed="-1"),
            Direction(raw="  "),
        ]
        for d in bad:
            with self.assertRaises(ProfileError, msg=repr(d)):
                d.validate("t")

    def test_profile_file_accepts_new_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wanshape"
            path.mkdir()
            (path / "profiles.toml").write_text(
                '[profiles.lte-busy.up]\ndelay = "30ms"\nslot = "5ms 15ms"\n'
                '[profiles.lte-busy.down]\ndelay = "30ms"\nreorder = "5%"\n')
            old = os.environ.get("XDG_CONFIG_HOME")
            os.environ["XDG_CONFIG_HOME"] = tmp
            try:
                p = load_profiles()["lte-busy"]
            finally:
                if old is None:
                    os.environ.pop("XDG_CONFIG_HOME", None)
                else:
                    os.environ["XDG_CONFIG_HOME"] = old
        self.assertIn("slot", p.up.netem_args())
        self.assertIn("reorder", p.down.netem_args())


if __name__ == "__main__":
    unittest.main()

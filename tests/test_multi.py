"""#6: several targets in one command."""

import argparse
import io
import unittest
from unittest import mock

from tests.fakes import FakeBackend
from tests.test_cli import args
from wanshape import cli
from wanshape.errors import CommandError, ConflictError, WanshapeError
from wanshape.host import HostLive, build_host_apply, parse_ports
from wanshape.inspector import LiveState
from wanshape.profiles import load_profiles
from wanshape.target import Target


def target(name, inode):
    return Target(name=name, id=f"id-{name}", runtime="podman", rootless=True, pid=1,
                  netns_inode=inode, userns_inode=1, network_mode="bridge", iface="eth0",
                  owner_uid=1000)


class NamesTests(unittest.TestCase):
    def test_spaces_commas_and_duplicates(self):
        self.assertEqual(cli._names(["web,api", "worker", "api", " ,db "]),
                         ["web", "api", "worker", "db"])
        self.assertEqual(cli._names("solo"), ["solo"])


class FailOn(FakeBackend):
    def __init__(self, bad):
        super().__init__()
        self.bad = bad

    def run(self, cmd):
        if self.bad and cmd[:3] == ["ip", "link", "add"]:
            self.commands.append(list(cmd))
            raise CommandError(cmd, 2, "no ifb")
        return super().run(cmd)


class ContainerMultiTests(unittest.TestCase):
    def setUp(self):
        self.targets = {"web": target("web", 10), "api": target("api", 20),
                        "twin": target("twin", 10)}
        self.backends = {"web": FailOn(False), "api": FailOn(True), "twin": FailOn(False)}
        self.state = {}
        clean = LiveState("noqueue", "0:", False, False, False)
        shaped = LiveState("netem", "5753:", True, True, True)

        def live(backend, iface):
            # Clean until wanshape has run commands on this target's backend.
            return shaped if backend.commands else clean

        patches = [
            mock.patch.object(cli, "resolve", side_effect=lambda n, *a: self.targets[n]),
            mock.patch.object(cli, "get_backend",
                              side_effect=lambda t, *a: self.backends[t.name]),
            mock.patch.object(cli, "inspect_live", side_effect=live),
            mock.patch.object(cli, "load_state", side_effect=lambda: dict(self.state)),
            mock.patch.object(cli, "save_state", side_effect=self._save),
            mock.patch.object(cli, "netns_members", return_value=[]),
            mock.patch.object(Target, "verify", return_value=None),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _save(self, entries):
        self.state = entries

    def run_apply(self, names, **kw):
        with mock.patch("sys.stdout", new_callable=io.StringIO), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            code = cli._Commands.apply(args(container=names, **kw))
        return code, err.getvalue()

    def test_all_or_nothing(self):
        code, err = self.run_apply(["web,api"])
        self.assertEqual(code, 1)
        # web was applied, then undone when api failed.
        self.assertIn(["tc", "qdisc", "del", "dev", "eth0", "root"], self.backends["web"].commands)
        self.assertEqual(self.state, {})

    def test_keep_going(self):
        code, err = self.run_apply(["web", "api"], keep_going=True)
        self.assertEqual(code, 1)
        self.assertIn("failed: api; applied: web", err)
        self.assertEqual(set(self.state), {"id-web"})

    def test_shared_namespace_in_list_is_shaped_once(self):
        code, err = self.run_apply(["web,twin"])
        self.assertEqual(code, 0)
        self.assertIn("shares a network namespace with web", err)
        self.assertEqual(self.backends["twin"].commands, [])

    def test_unknown_name_fails_before_any_change(self):
        self.targets.pop("api")
        with self.assertRaises(KeyError):
            self.run_apply(["web,api"])
        self.assertEqual(self.backends["web"].commands, [])


class HostMultiTests(unittest.TestCase):
    def test_list_targets_get_their_own_slots_and_are_checked_against_each_other(self):
        live = HostLive()
        profile = load_profiles()["3g"]
        a = build_host_apply("a", profile, parse_ports("5060"), "eth0", "both", live)
        cli._simulate(live, a)
        b = build_host_apply("b", profile, parse_ports("5062"), "eth0", "both", live)
        self.assertEqual((a.slot.slot, b.slot.slot), (0, 1))
        self.assertTrue(b.slot.added_clsact)   # shares the clsact a adds
        self.assertNotIn(["tc", "qdisc", "add", "dev", "eth0", "clsact"], b.commands)
        cli._simulate(live, b)
        with self.assertRaises(ConflictError):
            build_host_apply("c", profile, parse_ports("5062"), "eth0", "both", live)

    def test_ports_options_need_one_name(self):
        a = argparse.Namespace(name=["x", "y"], ports="5060", remote_ports=None, peer=None,
                               duration=None, dry_run=True, profile="3g", delay=None,
                               no_reorder=False)
        with self.assertRaises(WanshapeError):
            cli._apply_host(a)


if __name__ == "__main__":
    unittest.main()

"""Regression tests for the bug hunt of 2026-11-25."""

import argparse
import io
import unittest
from unittest import mock

from tests.fakes import FakeBackend
from tests.test_cli import args, target
from wanshape import cli, host, timer
from wanshape.errors import CommandError, ConflictError, WanshapeError
from wanshape.host import HostLive, HostSlot, IfaceState, build_host_apply, parse_ports
from wanshape.inspector import LiveState
from wanshape.planner import build_clear_plan
from wanshape.profiles import ProfileError, _profile_from, load_profiles
from wanshape.target import Target


class RollbackScopeTests(unittest.TestCase):
    """1: a failed apply undoes only what it created."""

    def test_existing_ifb_is_not_deleted(self):
        backend = FakeBackend()

        def run(cmd):
            backend.commands.append(list(cmd))
            if cmd[:4] == ["ip", "link", "add", "wsh0up"]:
                raise CommandError(cmd, 2, "RTNETLINK answers: File exists")
            return ""
        backend.run = run
        plan = build_host_apply("b", load_profiles()["3g"], parse_ports("6000"), "eth0",
                                "both", HostLive())
        with self.assertRaises(CommandError):
            host.run_plan(backend, plan.commands)
        self.assertNotIn(["ip", "link", "del", "wsh0up"], backend.commands)
        self.assertNotIn(["ip", "link", "del", "wsh0dn"], backend.commands)

    def test_what_was_created_is_undone_in_reverse(self):
        backend = FakeBackend()

        def run(cmd):
            backend.commands.append(list(cmd))
            if cmd[:3] == ["tc", "filter", "add"] and cmd[7] == "49301":
                raise CommandError(cmd, 2, "boom")
            return ""
        backend.run = run
        plan = build_host_apply("b", load_profiles()["3g"], parse_ports("6000"), "eth0",
                                "both", HostLive())
        with self.assertRaises(CommandError):
            host.run_plan(backend, plan.commands)
        tail = backend.commands[-4:]
        self.assertEqual(tail, [
            ["tc", "filter", "del", "dev", "eth0", "egress", "pref", "49300"],
            ["tc", "qdisc", "del", "dev", "eth0", "clsact"],
            ["ip", "link", "del", "wsh0dn"],
            ["ip", "link", "del", "wsh0up"],
        ])


class OverlapTests(unittest.TestCase):
    """2: local against remote ports; different peers never clash."""

    def live_with(self, **kw):
        other = HostSlot(0, "a", "3g", "eth0", [], "both", True, **kw)
        return HostLive(slots={0: other}, ifbs={"wsh0up", "wsh0dn"},
                        ifaces={"eth0": IfaceState(clsact=True)})

    def test_local_against_remote(self):
        live = self.live_with(remote_ports=parse_ports("5060"))
        with self.assertRaises(ConflictError):
            build_host_apply("b", load_profiles()["3g"], parse_ports("5060"), "eth0", "both",
                             live)

    def test_different_peers_are_fine(self):
        live = self.live_with(peer="10.0.0.1")
        live.slots[0].ports = parse_ports("5060")
        build_host_apply("b", load_profiles()["3g"], parse_ports("5060"), "eth0", "both",
                         live, peer="10.0.0.2")


class ContainerClearTests(unittest.TestCase):
    def test_clsact_with_another_tools_filters_is_kept_even_if_we_added_it(self):
        """4"""
        live = LiveState("netem", "5753:", True, True, True, foreign_filters=True)
        plan = build_clear_plan("eth0", "both", live, added_clsact=True)
        self.assertNotIn(["tc", "qdisc", "del", "dev", "eth0", "clsact"], plan.commands)


class TimerUnitTests(unittest.TestCase):
    def test_container_timer_is_per_namespace_and_keeps_iface(self):
        """3 and 6"""
        a = Target(name="a", id="ida", runtime="podman", rootless=True, pid=1, netns_inode=99,
                   userns_inode=1, network_mode="bridge", iface="eth1", owner_uid=1000)
        b = Target(name="b", id="idb", runtime="podman", rootless=True, pid=2, netns_inode=99,
                   userns_inode=1, network_mode="container:x", iface="eth1", owner_uid=1000)
        self.assertEqual(cli._container_unit(a), cli._container_unit(b))
        backend = FakeBackend()
        clean = LiveState("noqueue", "0:", False, False, False)
        plan = cli.build_apply_plan(load_profiles()["3g"], "eth1", "egress", clean)
        with mock.patch.object(timer, "schedule") as schedule, \
                mock.patch.object(cli, "_save_and_report"), \
                mock.patch.object(Target, "verify", return_value=None):
            ok = cli._apply_container((a, backend, clean, plan), args(container=["a"]),
                                      load_profiles()["3g"], 300)
        self.assertTrue(ok)
        clear_args = schedule.call_args[0][2]
        self.assertEqual(clear_args[-2:], ["--iface", "eth1"])

    def test_unit_names_have_no_dots(self):
        self.assertEqual(timer.unit_name("vm", "web.service"), "wanshape-clear-vm-web_service")

    def test_timer_failure_rolls_the_container_back(self):
        """8"""
        backend = FakeBackend()
        t = target()
        clean = LiveState("noqueue", "0:", False, False, False)
        plan = cli.build_apply_plan(load_profiles()["3g"], "eth0", "egress", clean)
        with mock.patch.object(timer, "schedule", side_effect=WanshapeError("no systemd")), \
                mock.patch.object(cli, "_rollback") as rollback, \
                mock.patch.object(Target, "verify", return_value=None), \
                mock.patch("sys.stderr", new_callable=io.StringIO):
            ok = cli._apply_container((t, backend, clean, plan), args(), load_profiles()["3g"], 60)
        self.assertFalse(ok)
        rollback.assert_called_once()

    def test_vanishing_target_is_a_failure_not_a_crash(self):
        """8"""
        backend = FakeBackend()
        t = target()
        clean = LiveState("noqueue", "0:", False, False, False)
        plan = cli.build_apply_plan(load_profiles()["3g"], "eth0", "egress", clean)
        with mock.patch.object(Target, "verify", side_effect=WanshapeError("gone")), \
                mock.patch.object(cli, "_rollback") as rollback, \
                mock.patch("sys.stderr", new_callable=io.StringIO):
            ok = cli._apply_container((t, backend, clean, plan), args(), load_profiles()["3g"],
                                      None)
        self.assertFalse(ok)
        rollback.assert_called_once()


class PartialClearTests(unittest.TestCase):
    """5: clearing one direction keeps the other's state."""

    def test_entry_kept_while_egress_remains(self):
        state = {"abc": {"id": "abc", "netns_inode": 42, "profile": "3g",
                         "direction": "both", "until": 123}}
        saved = {}
        shaped = LiveState("netem", "5753:", True, True, True)
        egress_only = LiveState("netem", "5753:", False, False, False)
        with mock.patch.object(cli, "get_backend", return_value=FakeBackend()), \
                mock.patch.object(cli, "inspect_live", side_effect=[shaped, egress_only]), \
                mock.patch.object(cli, "load_state", side_effect=lambda: dict(state)), \
                mock.patch.object(cli, "save_state", side_effect=saved.update), \
                mock.patch.object(timer, "cancel") as cancel, \
                mock.patch.object(Target, "verify", return_value=None), \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            cli._clear_container(target(), argparse.Namespace(direction="ingress",
                                                              dry_run=False))
        cancel.assert_not_called()
        self.assertEqual(saved["abc"]["direction"], "egress")


class NameTests(unittest.TestCase):
    def test_profile_names_cannot_inject_alias_fields(self):
        """7"""
        for bad in ("x kind=vm", "a b", "x=y", ""):
            with self.assertRaises(ProfileError, msg=bad):
                _profile_from(bad, {"up": {"delay": "5ms"}})


class StaleVmTests(unittest.TestCase):
    def test_tap_name_reused_by_another_vm_is_taken_over(self):
        old = HostSlot(0, "vm-a", "3g", "vnet0", [], "both", True, kind="vm",
                       mac="52:54:00:00:00:0a")
        live = HostLive(slots={0: old}, ifbs={"wsh0up", "wsh0dn"},
                        ifaces={"vnet0": IfaceState(clsact=True, egress_prefs={49300},
                                                    ingress_prefs={49300})})
        plan = build_host_apply("vm-b", load_profiles()["3g"], [], "vnet0", "both", live,
                                kind="vm", mac="52:54:00:00:00:0b")
        self.assertIn(["ip", "link", "del", "wsh0up"], plan.commands)
        self.assertEqual(plan.slot.name, "vm-b")

    def test_same_vm_on_same_device_still_conflicts_with_a_ports_target(self):
        other = HostSlot(0, "p", "3g", "vnet0", parse_ports("5060"), "both", True)
        live = HostLive(slots={0: other}, ifbs={"wsh0up", "wsh0dn"},
                        ifaces={"vnet0": IfaceState(clsact=True)})
        with self.assertRaises(ConflictError):
            build_host_apply("vm-b", load_profiles()["3g"], [], "vnet0", "both", live,
                             kind="vm", mac="52:54:00:00:00:0b")


class SudoProfileTests(unittest.TestCase):
    def test_profiles_come_from_the_sudo_users_home(self):
        import os
        import pwd
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as home:
            cfg = Path(home) / ".config" / "wanshape"
            cfg.mkdir(parents=True)
            (cfg / "profiles.toml").write_text('[profiles.mine.up]\ndelay = "5ms"\n')
            env = {k: v for k, v in os.environ.items() if k != "XDG_CONFIG_HOME"}
            env["SUDO_USER"] = "someone"
            entry = mock.Mock(pw_dir=home)
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch("os.geteuid", return_value=0), \
                    mock.patch.object(pwd, "getpwnam", return_value=entry):
                self.assertIn("mine", load_profiles())


class CanonicalNameTests(unittest.TestCase):
    def test_uuid_becomes_the_name(self):
        from wanshape.vm import canonical_name
        self.assertEqual(canonical_name("3f2a-uuid", virsh=lambda uri, *a: "web\n"), "web")

        def missing(uri, *a):
            raise WanshapeError("no domain")
        self.assertEqual(canonical_name("nope", virsh=missing), "nope")


if __name__ == "__main__":
    unittest.main()

import argparse
import io
import unittest
from unittest import mock

from tests.fakes import FakeBackend
from wanshape import cli
from wanshape.inspector import LiveState
from wanshape.target import Target


def target() -> Target:
    return Target(
        name="test", id="abc", runtime="podman", rootless=True, pid=1,
        netns_inode=42, userns_inode=43, network_mode="bridge", iface="eth0",
        owner_uid=1000,
    )


def args(**overrides) -> argparse.Namespace:
    values = dict(
        container="test", profile=None, delay="10ms", jitter=None, loss=None,
        loss_model=None, rate=None, runtime=None, iface=None, direction="both",
        no_reorder=False, backend="nsenter", dry_run=False, force=False,
        duration=None, allow_partial=False, distribution=None, keep_going=False,
    )
    values.update(overrides)
    return argparse.Namespace(**values)


class CliApplyTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.state = {}
        self.patches = [
            mock.patch.object(cli, "resolve", return_value=target()),
            mock.patch.object(cli, "get_backend", return_value=self.backend),
            mock.patch.object(
                cli, "inspect_live",
                return_value=LiveState(root_kind="noqueue", root_handle="0:",
                                       clsact=False, our_filter=False, ifb=False),
            ),
            mock.patch.object(cli, "load_state", side_effect=lambda: self.state),
            mock.patch.object(cli, "save_state", side_effect=lambda s: self.state.update(s)),
            mock.patch.object(cli, "make_entry", return_value={"profile": "inline"}),
            mock.patch.object(Target, "verify", return_value=None),
            mock.patch.object(cli, "netns_members", return_value=[]),
        ]
        for patch in self.patches:
            patch.start()
        self.addCleanup(self.stop_patches)

    def stop_patches(self):
        for patch in reversed(self.patches):
            patch.stop()

    def test_ingress_only_is_applied(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(cli._Commands.apply(args(direction="ingress")), 0)
        self.assertTrue(any("mirred" in command for command in self.backend.commands))
        self.assertFalse(any(command[:6] == ["tc", "qdisc", "replace", "dev", "eth0", "root"]
                             for command in self.backend.commands))

    def test_egress_only_is_applied(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(cli._Commands.apply(args(direction="egress")), 0)
        self.assertTrue(any("netem" in command for command in self.backend.commands))
        self.assertFalse(any("mirred" in command for command in self.backend.commands))

    def test_lossy_sip_reaches_tc_as_separate_args(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(cli._Commands.apply(
                args(delay=None, profile="lossy-sip", direction="egress")), 0)
        netem = next(c for c in self.backend.commands if "netem" in c)
        self.assertIn("gemodel", netem)
        self.assertNotIn("gemodel 1% 25% 100% 0%", netem)

    def test_dry_run_makes_no_changes(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(cli._Commands.apply(args(dry_run=True)), 0)
        self.assertEqual(self.backend.commands, [])
        self.assertEqual(self.state, {})


class CliNetemOptionTests(unittest.TestCase):
    def test_comma_lists_become_tc_spacing(self):
        a = args(jitter="5ms", reorder="10%,50%", slot="5ms,20ms", duplicate="1%",
                 seed="7", raw_netem="ecn")
        netem = " ".join(cli._profile_from_args(a).up.netem_args())
        self.assertIn("reorder 10% 50%", netem)
        self.assertIn("slot 5ms 20ms", netem)
        self.assertIn("seed 7", netem)
        self.assertTrue(netem.endswith("ecn"))


class CliLossModelTests(unittest.TestCase):
    def test_cli_loss_model_syntax(self):
        profile = cli._profile_from_args(args(loss_model="gemodel:1%,25%,100%,0%"))
        self.assertEqual(profile.up.loss_model, "gemodel 1% 25% 100% 0%")
        netem = profile.up.netem_args()
        self.assertEqual(netem[netem.index("loss"):netem.index("loss") + 6],
                         ["loss", "gemodel", "1%", "25%", "100%", "0%"])

    def test_cli_loss_model_rejects_garbage(self):
        with self.assertRaises(cli.ProfileError):
            cli._profile_from_args(args(loss_model="gemodel:lots"))


class CliReapplyTests(unittest.TestCase):
    """A second apply over our own shaping must keep what the first one recorded."""

    def setUp(self):
        self.backend = FakeBackend()
        self.state = {
            "abc": {"id": "abc", "netns_inode": 42, "profile": "3g",
                    "prior_root_qdisc": "fq_codel 0:", "added_clsact": True},
        }
        shaped = LiveState(root_kind="netem", root_handle="5753:", clsact=True,
                           our_filter=True, ifb=True)
        patches = [
            mock.patch.object(cli, "resolve", return_value=target()),
            mock.patch.object(cli, "get_backend", return_value=self.backend),
            mock.patch.object(cli, "inspect_live", return_value=shaped),
            mock.patch.object(cli, "load_state", side_effect=lambda: dict(self.state)),
            mock.patch.object(cli, "save_state", side_effect=self._save),
            mock.patch.object(Target, "verify", return_value=None),
            mock.patch.object(cli, "netns_members", return_value=[]),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _save(self, entries):
        self.state = entries

    def test_reapply_keeps_prior_qdisc_and_clsact_ownership(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(cli._Commands.apply(args(profile="4g", delay=None)), 0)
        entry = self.state["abc"]
        self.assertEqual(entry["profile"], "4g")
        self.assertEqual(entry["prior_root_qdisc"], "fq_codel 0:")
        self.assertTrue(entry["added_clsact"])
        self.assertNotIn(["ip", "link", "add", "wanshape0", "type", "ifb"],
                         self.backend.commands)

    def test_reapply_then_clear_removes_clsact(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            cli._Commands.apply(args(profile="4g", delay=None))
            cli._Commands.clear(argparse.Namespace(
                container="test", runtime=None, iface=None, direction="both", dry_run=False))
        self.assertIn(["tc", "qdisc", "del", "dev", "eth0", "clsact"], self.backend.commands)
        self.assertIn(["tc", "qdisc", "replace", "dev", "eth0", "root", "fq_codel"],
                      self.backend.commands)

    def test_recreated_container_does_not_inherit_old_state(self):
        self.state["abc"]["netns_inode"] = 7
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            cli._Commands.apply(args(profile="4g", delay=None))
        self.assertEqual(self.state["abc"]["prior_root_qdisc"], "netem 5753:")


class MissingRuntimeTests(unittest.TestCase):
    def test_unknown_name_without_docker_installed(self):
        """Podman doesn't know the name and docker isn't installed: an error, not a traceback."""
        from wanshape import host, target
        from wanshape.errors import WanshapeError

        def fake_run(cmd, **kwargs):
            if cmd[0] == "docker" or "docker" in cmd:
                raise FileNotFoundError(2, "No such file or directory", "docker")
            import subprocess
            return subprocess.CompletedProcess(cmd, 125, "", "no such container")

        with mock.patch("subprocess.run", side_effect=fake_run):
            with self.assertRaises(WanshapeError):
                target.resolve("nothing-here")
            with self.assertRaises(WanshapeError):
                host.inspect_container("nothing-here")


class FailingBackend(FakeBackend):
    """Fails the IFB creation, as on a kernel without the ifb module."""

    def run(self, args):
        if args[:3] == ["ip", "link", "add"]:
            self.commands.append(list(args))
            raise cli.CommandError(self.command(args), 2, "Error: Unknown device type.")
        return super().run(args)


class CliRollbackTests(unittest.TestCase):
    """#9: a failed container apply is undone unless --allow-partial."""

    def setUp(self):
        self.backend = FailingBackend()
        self.state = {}
        clean = LiveState(root_kind="noqueue", root_handle="0:", clsact=False,
                          our_filter=False, ifb=False)
        egress_on = LiveState(root_kind="netem", root_handle="5753:", clsact=True,
                              our_filter=False, ifb=False)
        patches = [
            mock.patch.object(cli, "resolve", return_value=target()),
            mock.patch.object(cli, "get_backend", return_value=self.backend),
            mock.patch.object(cli, "inspect_live", side_effect=[clean, egress_on]),
            mock.patch.object(cli, "load_state", side_effect=lambda: dict(self.state)),
            mock.patch.object(cli, "save_state", side_effect=self._save),
            mock.patch.object(Target, "verify", return_value=None),
            mock.patch.object(cli, "netns_members", return_value=[]),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _save(self, entries):
        self.state = entries

    def run_apply(self, **kw):
        with mock.patch("sys.stdout", new_callable=io.StringIO), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            code = cli._Commands.apply(args(**kw))
        return code, err.getvalue()

    def test_failed_ingress_rolls_back_egress(self):
        code, err = self.run_apply()
        self.assertEqual(code, 1)
        self.assertIn("rolled back", err)
        self.assertIn(["tc", "qdisc", "del", "dev", "eth0", "root"], self.backend.commands)
        self.assertIn(["tc", "qdisc", "del", "dev", "eth0", "clsact"], self.backend.commands)
        self.assertEqual(self.state, {})

    def test_allow_partial_keeps_egress(self):
        code, err = self.run_apply(allow_partial=True)
        self.assertEqual(code, 1)
        self.assertIn("egress remains active", err)
        self.assertNotIn(["tc", "qdisc", "del", "dev", "eth0", "root"], self.backend.commands)
        self.assertEqual(self.state["abc"]["direction"], "egress")


class CliStatusTests(unittest.TestCase):
    """#8: status --all finds shaping without a state entry; --prune drops stale entries."""

    def status_args(self, **kw):
        values = dict(container=None, all=False, prune=False)
        values.update(kw)
        return argparse.Namespace(**values)

    def test_prune_drops_gone_and_recreated(self):
        state = {
            "gone": {"id": "gone", "name": "old", "netns_inode": 1},
            "abc": {"id": "abc", "name": "test", "netns_inode": 7},    # recreated: now 42
            "keep": {"id": "keep", "name": "keep", "netns_inode": 42},
        }
        saved = {}

        def fake_resolve(key, runtime=None):
            if key == "gone":
                raise cli.WanshapeError("not found")
            return target()

        with mock.patch.object(cli, "resolve", side_effect=fake_resolve), \
                mock.patch.object(cli, "save_state", side_effect=saved.update), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            cli._prune(state)
        self.assertEqual(set(saved), {"keep"})
        self.assertIn("pruned old: container is gone", out.getvalue())
        self.assertIn("pruned test: container was recreated", out.getvalue())

    def test_all_reports_unrecorded_shaping(self):
        shaped = LiveState(root_kind="netem", root_handle="5753:", clsact=True,
                           our_filter=True, ifb=True)
        from wanshape.host import HostLive
        with mock.patch.object(cli, "load_state", return_value={}), \
                mock.patch.object(cli, "_scan_running", return_value=[target()]), \
                mock.patch.object(cli, "get_backend", return_value=FakeBackend()), \
                mock.patch.object(cli, "inspect_live", return_value=shaped), \
                mock.patch.object(cli, "stats", return_value=""), \
                mock.patch.object(cli, "inspect_host", return_value=HostLive()), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(cli._Commands.status(self.status_args(all=True)), 0)
        self.assertIn("test: profile=unknown egress=on ingress=on", out.getvalue())

    def test_all_skips_unshaped_containers(self):
        clean = LiveState(root_kind="noqueue", root_handle="0:", clsact=False,
                          our_filter=False, ifb=False)
        from wanshape.host import HostLive
        with mock.patch.object(cli, "load_state", return_value={}), \
                mock.patch.object(cli, "_scan_running", return_value=[target()]), \
                mock.patch.object(cli, "get_backend", return_value=FakeBackend()), \
                mock.patch.object(cli, "inspect_live", return_value=clean), \
                mock.patch.object(cli, "inspect_host", return_value=HostLive()), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            cli._Commands.status(self.status_args(all=True))
        self.assertNotIn("test:", out.getvalue())


class SharedNetnsTests(unittest.TestCase):
    """#7: one network namespace is one shaping domain."""

    def setUp(self):
        self.backend = FakeBackend()
        # Applied earlier to "other", a container sharing test's netns (inode 42).
        self.state = {"other": {"id": "other", "name": "other", "netns_inode": 42,
                                "profile": "3g", "prior_root_qdisc": "noqueue 0:",
                                "added_clsact": True}}
        shaped = LiveState(root_kind="netem", root_handle="5753:", clsact=True,
                           our_filter=True, ifb=True)
        patches = [
            mock.patch.object(cli, "resolve", return_value=target()),
            mock.patch.object(cli, "get_backend", return_value=self.backend),
            mock.patch.object(cli, "inspect_live", return_value=shaped),
            mock.patch.object(cli, "load_state", side_effect=lambda: dict(self.state)),
            mock.patch.object(cli, "save_state", side_effect=self._save),
            mock.patch.object(Target, "verify", return_value=None),
            mock.patch.object(cli, "netns_members", return_value=["other"]),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _save(self, entries):
        self.state = entries

    def test_apply_warns_and_keeps_one_entry(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            cli._Commands.apply(args(profile="4g", delay=None))
        self.assertIn("shares its network namespace with other", err.getvalue())
        self.assertEqual(set(self.state), {"abc"})
        # What the first apply recorded about the interface is kept.
        self.assertTrue(self.state["abc"]["added_clsact"])

    def test_clear_via_another_member_removes_the_entry(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            cli._Commands.clear(argparse.Namespace(
                container="test", runtime=None, iface=None, direction="both", dry_run=False))
        self.assertEqual(self.state, {})
        self.assertIn(["tc", "qdisc", "del", "dev", "eth0", "clsact"], self.backend.commands)


if __name__ == "__main__":
    unittest.main()

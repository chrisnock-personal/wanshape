"""#3: re-apply VM shaping when libvirt starts the VM."""

import importlib
import os
import tempfile
import unittest
from pathlib import Path

from wanshape.profiles import load_profiles

XML = """<domain type='kvm'><name>web</name><devices>
  <interface type='network'><mac address='52:54:00:AA:BB:01'/><source network='default'/>
    <target dev='vnet7'/><model type='virtio'/></interface>
  <interface type='network'><mac address='52:54:00:aa:bb:02'/><target dev='vnet8'/></interface>
</devices></domain>"""


class VmHookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["WANSHAPE_VM_CONFIG"] = str(Path(self.tmp.name) / "vms.json")
        os.environ["WANSHAPE_LIBVIRT_HOOKS"] = str(Path(self.tmp.name) / "hooks")
        self.addCleanup(os.environ.pop, "WANSHAPE_VM_CONFIG")
        self.addCleanup(os.environ.pop, "WANSHAPE_LIBVIRT_HOOKS")
        from wanshape import vmhook
        self.vmhook = importlib.reload(vmhook)

    def test_tap_from_xml_matches_mac_case_insensitively(self):
        self.assertEqual(self.vmhook.tap_from_xml(XML, "52:54:00:aa:bb:01"), "vnet7")
        self.assertEqual(self.vmhook.tap_from_xml(XML, "52:54:00:AA:BB:02"), "vnet8")
        self.assertIsNone(self.vmhook.tap_from_xml(XML, "52:54:00:00:00:00"))

    def test_remember_and_forget_round_trip_the_profile(self):
        profile = load_profiles()["lte-congested"]
        profile.rate_mode = "accurate"
        self.vmhook.remember("web", profile, "both", "52:54:00:AA:BB:01")
        entry = self.vmhook.load()["web"]
        back = self.vmhook.profile_from(entry)
        self.assertEqual(back, profile)
        self.assertEqual(entry["mac"], "52:54:00:aa:bb:01")
        self.assertTrue(self.vmhook.forget("web"))
        self.assertFalse(self.vmhook.forget("web"))

    def test_run_hook_dispatch(self):
        self.vmhook.remember("web", load_profiles()["3g"], "egress", "52:54:00:aa:bb:01")
        applied, released = [], []
        apply_fn = lambda *a: applied.append(a)  # noqa: E731
        release_fn = released.append
        self.assertEqual(self.vmhook.run_hook("web", "started", "begin", XML, apply_fn, release_fn),
                         "re-applied to vnet7")
        self.assertEqual(applied[0][0], "web")
        self.assertEqual(applied[0][2:], ("egress", "vnet7", "tap", "52:54:00:aa:bb:01"))
        self.vmhook.run_hook("web", "release", "end", XML, apply_fn, release_fn)
        self.assertEqual(released, ["web"])
        self.assertEqual(self.vmhook.run_hook("other", "started", "begin", XML, apply_fn,
                                              release_fn), "not a wanshape VM")
        self.assertIn("nothing to do", self.vmhook.run_hook("web", "prepare", "begin", XML,
                                                            apply_fn, release_fn))

    def test_install_writes_an_executable_wrapper(self):
        path = self.vmhook.install()
        self.assertTrue(os.access(path, os.X_OK))
        text = path.read_text()
        self.assertIn("-m wanshape.cli vm-hook", text)
        self.assertIn("PYTHONPATH=", text)
        self.assertTrue(self.vmhook.uninstall())
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()

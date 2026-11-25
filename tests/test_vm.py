import json
import unittest

from wanshape.errors import ConflictError, UnsupportedTarget, WanshapeError
from wanshape.host import HostLive, HostSlot, IfaceState, build_host_apply, parse_alias, parse_ports
from wanshape.profiles import load_profiles
from wanshape.vm import check_tap_mac, choose_interface, parse_domiflist, resolve_vm

# `virsh -q domiflist` as printed by libvirt 11 on the laptop (direct) plus tap cases.
DIRECT = " -   direct   enp0s13f0u1u2c2   e1000   00:0c:29:85:e5:1d\n"
TWO = (" vnet3   network   default   virtio   52:54:00:aa:bb:01\n"
       " vnet4   bridge    br0       virtio   52:54:00:aa:bb:02\n")


class DomiflistTests(unittest.TestCase):
    def test_parse(self):
        [one] = parse_domiflist(DIRECT)
        self.assertEqual((one.device, one.type, one.source), ("-", "direct", "enp0s13f0u1u2c2"))
        self.assertEqual(len(parse_domiflist(TWO)), 2)

    def test_header_lines_ignored(self):
        text = (" Interface   Type      Source    Model    MAC\n"
                "-------------------------------------------------------\n" + TWO)
        self.assertEqual([i.device for i in parse_domiflist(text)], ["vnet3", "vnet4"])

    def test_direct_refused(self):
        with self.assertRaises(UnsupportedTarget):
            choose_interface("freepbx", parse_domiflist(DIRECT))

    def test_user_networking_refused(self):
        ifaces = parse_domiflist(" -   user   -   virtio   52:54:00:00:00:01\n")
        with self.assertRaises(UnsupportedTarget):
            choose_interface("vm", ifaces)

    def test_several_need_mac(self):
        with self.assertRaises(WanshapeError):
            choose_interface("vm", parse_domiflist(TWO))
        self.assertEqual(choose_interface("vm", parse_domiflist(TWO), "52:54:00:AA:BB:02").device,
                         "vnet4")

    def test_not_running(self):
        ifaces = parse_domiflist(" -   network   default   virtio   52:54:00:00:00:01\n")
        with self.assertRaises(WanshapeError):
            choose_interface("vm", ifaces)

    def test_tap_mac_check(self):
        ok = lambda args: json.dumps([{"address": "fe:54:00:aa:bb:01"}])
        check_tap_mac("vnet3", "52:54:00:aa:bb:01", run=ok)
        other = lambda args: json.dumps([{"address": "fe:54:00:99:99:99"}])
        with self.assertRaises(WanshapeError):
            check_tap_mac("vnet3", "52:54:00:aa:bb:01", run=other)
        with self.assertRaises(WanshapeError):
            check_tap_mac("vnet3", "52:54:00:aa:bb:01", run=lambda args: "")

    def test_resolve_uses_uri(self):
        calls = []

        def virsh(uri, *args):
            calls.append((uri, args))
            return " vnet3   network   default   virtio   52:54:00:aa:bb:01\n"

        import wanshape.vm as vm
        original = vm.check_tap_mac
        vm.check_tap_mac = lambda device, mac: None
        try:
            self.assertEqual(resolve_vm("web", "qemu:///session", virsh=virsh).device, "vnet3")
        finally:
            vm.check_tap_mac = original
        self.assertEqual(calls, [("qemu:///session", ("domiflist", "web"))])


class VmPlanTests(unittest.TestCase):
    def plan(self, direction="both", live=None):
        return build_host_apply("web", load_profiles()["3g"], [], "vnet3", direction,
                                live or HostLive(), kind="vm")

    def filters(self, plan):
        return [c for c in plan.commands if c[:3] == ["tc", "filter", "add"]]

    def test_tap_hooks_are_reversed(self):
        f = self.filters(self.plan())
        self.assertEqual(len(f), 2)
        by_hook = {c[5]: c for c in f}
        self.assertIn("matchall", by_hook["ingress"])
        self.assertEqual(by_hook["ingress"][-1], "wsh0up")   # from the VM = its upload
        self.assertEqual(by_hook["egress"][-1], "wsh0dn")    # to the VM = its download

    def test_direction(self):
        [only] = self.filters(self.plan("egress"))
        self.assertEqual((only[5], only[-1]), ("ingress", "wsh0up"))

    def test_alias_round_trip(self):
        alias = next(c[-1] for c in self.plan().commands if "alias" in c)
        slot = parse_alias(0, alias)
        self.assertEqual((slot.kind, slot.name, slot.ports, slot.iface), ("vm", "web", [], "vnet3"))

    def test_ports_not_allowed(self):
        with self.assertRaises(WanshapeError):
            build_host_apply("web", load_profiles()["3g"], parse_ports("5060"), "vnet3",
                             "both", HostLive(), kind="vm")

    def test_device_already_shaped(self):
        other = HostSlot(0, "x", "3g", "vnet3", parse_ports("5060"), "both", True)
        with self.assertRaises(ConflictError):
            self.plan(live=HostLive(slots={0: other}, ifbs={"wsh0up", "wsh0dn"}))

    def test_ingress_qdisc_conflict(self):
        live = HostLive(ifaces={"vnet3": IfaceState(ingress_qdisc=True)})
        with self.assertRaises(ConflictError):
            self.plan(live=live)

    def test_restart_moves_to_new_device(self):
        old = HostSlot(0, "web", "3g", "vnet3", [], "both", True, kind="vm")
        live = HostLive(slots={0: old}, ifbs={"wsh0up", "wsh0dn"},
                        ifaces={"vnet3": IfaceState(exists=False)})
        plan = build_host_apply("web", load_profiles()["4g"], [], "vnet7", "both", live,
                                kind="vm")
        self.assertEqual(plan.slot.iface, "vnet7")
        self.assertFalse(any(c[:3] == ["tc", "filter", "del"] for c in plan.commands))
        self.assertIn(["ip", "link", "del", "wsh0up"], plan.commands)


if __name__ == "__main__":
    unittest.main()

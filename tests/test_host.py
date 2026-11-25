import json
import subprocess
import unittest
from unittest import mock

from wanshape import host
from wanshape.errors import ConflictError, UnsupportedTarget, WanshapeError
from wanshape.host import (
    HostLive,
    HostSlot,
    IfaceState,
    PortRange,
    build_host_apply,
    build_host_clear,
    format_ports,
    host_ports_for,
    inspect_host,
    parse_ports,
    ports_from_container,
)
from wanshape.profiles import load_profiles


def profile(name="3g"):
    return load_profiles()[name]


def slot(n=0, name="a", ports="5060", iface="eth0", added_clsact=True):
    return HostSlot(slot=n, name=name, profile="3g", iface=iface,
                    ports=parse_ports(ports), direction="both", added_clsact=added_clsact)


def filters(commands, hook=None):
    return [c for c in commands if c[:3] == ["tc", "filter", "add"]
            and (hook is None or c[5] == hook)]


class PortTests(unittest.TestCase):
    def test_parse_and_merge(self):
        self.assertEqual(format_ports(parse_ports("10000-20000, 5060,5061,15000-25000")),
                         "5060-5061,10000-25000")

    def test_parse_rejects_bad_input(self):
        for spec in ("", "0", "70000", "20-10", "abc", "5060-"):
            with self.assertRaises(WanshapeError, msg=spec):
                parse_ports(spec)

    def test_overlap(self):
        self.assertTrue(PortRange(10, 20).overlaps(PortRange(20, 30)))
        self.assertFalse(PortRange(10, 20).overlaps(PortRange(21, 30)))

    def test_ports_from_callraven_env(self):
        obj = {"Config": {"Env": [
            "PORT=3000", "SIP_PORT=5060", "RTP_PORT_LOW=10000", "RTP_PORT_HIGH=20000",
            "LOAD_SIP_PORT=5070", "LOAD_RTP_PORT_LOW=30000", "LOAD_RTP_PORT_HIGH=30999",
        ]}}
        ports, source = ports_from_container(obj)
        self.assertEqual(format_ports(ports), "5060,5070,10000-20000,30000-30999")
        self.assertIn("RTP_PORT_LOW..RTP_PORT_HIGH", source)
        self.assertNotIn("3000,", format_ports(ports))

    def test_label_wins_over_env(self):
        obj = {"Config": {"Labels": {"wanshape.ports": "7000-7010"},
                          "Env": ["SIP_PORT=5060"]}}
        ports, source = ports_from_container(obj)
        self.assertEqual(format_ports(ports), "7000-7010")
        self.assertIn("label", source)

    def test_no_ports_found(self):
        with self.assertRaises(WanshapeError):
            ports_from_container({"Config": {"Env": ["PORT=3000"]}})

    def test_ports_option_needs_no_container(self):
        inspect = mock.Mock()
        ports, source = host_ports_for("label", None, "4000", inspect=inspect)
        self.assertEqual(format_ports(ports), "4000")
        inspect.assert_not_called()

    def test_bridge_container_refused(self):
        obj = {"State": {"Running": True}, "HostConfig": {"NetworkMode": "bridge"}}
        with self.assertRaises(UnsupportedTarget):
            host_ports_for("x", None, None, inspect=lambda n, r: ("podman", obj))


class ApplyTests(unittest.TestCase):
    def test_fresh_apply(self):
        plan = build_host_apply("cr", profile(), parse_ports("5060,10000-20000"), "eth0",
                                "both", HostLive())
        self.assertEqual(plan.slot.slot, 0)
        self.assertTrue(plan.slot.added_clsact)
        self.assertIn(["tc", "qdisc", "add", "dev", "eth0", "clsact"], plan.commands)
        egress, ingress = filters(plan.commands, "egress"), filters(plan.commands, "ingress")
        self.assertEqual(len(egress), 8)   # 2 ranges x (ip, ipv6) x (udp, tcp)
        self.assertEqual(len(ingress), 8)
        self.assertIn("src_port", egress[0])
        self.assertEqual(egress[0][-1], "wsh0up")
        self.assertIn("dst_port", ingress[0])
        self.assertEqual(ingress[0][-1], "wsh0dn")
        self.assertEqual(sorted(int(c[7]) for c in egress), list(range(49300, 49308)))
        # IFBs and netem come before the filters that redirect to them.
        first_filter = plan.commands.index(egress[0])
        self.assertLess(plan.commands.index(["ip", "link", "add", "wsh0dn", "type", "ifb"]),
                        first_filter)
        alias = next(c[-1] for c in plan.commands if "alias" in c)
        self.assertEqual(host.parse_alias(0, alias).ports, parse_ports("5060,10000-20000"))

    def test_direction_limits_filters(self):
        plan = build_host_apply("cr", profile(), parse_ports("5060"), "eth0", "egress",
                                HostLive())
        self.assertEqual(len(filters(plan.commands, "ingress")), 0)
        self.assertEqual(len(filters(plan.commands, "egress")), 4)

    def test_no_reorder_adds_pfifo(self):
        plan = build_host_apply("cr", profile("lossy-sip"), parse_ports("5060"), "eth0",
                                "both", HostLive())
        self.assertEqual(sum("pfifo" in c for c in plan.commands), 2)
        netem = next(c for c in plan.commands if "netem" in c)
        self.assertIn("gemodel", netem)

    def test_foreign_clsact_is_kept(self):
        live = HostLive(ifaces={"eth0": IfaceState(clsact=True, egress_prefs={1})})
        plan = build_host_apply("cr", profile(), parse_ports("5060"), "eth0", "both", live)
        self.assertFalse(plan.slot.added_clsact)
        self.assertNotIn(["tc", "qdisc", "add", "dev", "eth0", "clsact"], plan.commands)
        self.assertNotIn(["tc", "qdisc", "del", "dev", "eth0", "clsact"], plan.rollback)

    def test_overlap_refused(self):
        live = HostLive(slots={0: slot(0, "a", "10000-20000")}, ifbs={"wsh0up", "wsh0dn"},
                        ifaces={"eth0": IfaceState(clsact=True)})
        with self.assertRaises(ConflictError):
            build_host_apply("b", profile(), parse_ports("19000-21000"), "eth0", "both", live)

    def test_second_target_takes_next_slot_and_shares_clsact(self):
        live = HostLive(slots={0: slot(0, "a", "5060")}, ifbs={"wsh0up", "wsh0dn"},
                        ifaces={"eth0": IfaceState(clsact=True,
                                                   egress_prefs=set(range(49300, 49304)))})
        plan = build_host_apply("b", profile(), parse_ports("5062"), "eth0", "both", live)
        self.assertEqual(plan.slot.slot, 1)
        self.assertTrue(plan.slot.added_clsact)
        self.assertEqual(filters(plan.commands)[0][7], "49400")

    def test_reapply_reuses_slot_and_replaces_filters(self):
        old = slot(2, "a", "5060")
        live = HostLive(slots={2: old}, ifbs={"wsh2up", "wsh2dn"},
                        ifaces={"eth0": IfaceState(clsact=True,
                                                   egress_prefs=set(range(49500, 49504)),
                                                   ingress_prefs=set(range(49500, 49504)))})
        plan = build_host_apply("a", profile("4g"), parse_ports("5070"), "eth0", "both", live)
        self.assertEqual(plan.slot.slot, 2)
        self.assertTrue(plan.slot.added_clsact)
        dels = [c for c in plan.commands if c[:3] == ["tc", "filter", "del"]]
        self.assertEqual(len(dels), 8)
        self.assertNotIn(["tc", "qdisc", "del", "dev", "eth0", "clsact"], plan.commands)
        self.assertLess(plan.commands.index(["ip", "link", "del", "wsh2up"]),
                        plan.commands.index(["ip", "link", "add", "wsh2up", "type", "ifb"]))

    def test_slots_exhausted(self):
        live = HostLive(slots={n: slot(n, f"t{n}", str(6000 + n)) for n in range(10)})
        with self.assertRaises(ConflictError):
            build_host_apply("x", profile(), parse_ports("7000"), "eth0", "both", live)

    def test_orphan_ifb_slot_skipped(self):
        live = HostLive(ifbs={"wsh0up"})
        plan = build_host_apply("x", profile(), parse_ports("7000"), "eth0", "both", live)
        self.assertEqual(plan.slot.slot, 1)


class RemotePeerTests(unittest.TestCase):
    """#11: remote ports and a peer address."""

    def plan(self, ports="", remote=None, peer=None, live=None):
        return build_host_apply("cr", profile(), parse_ports(ports) if ports else [], "eth0",
                                "both", live or HostLive(),
                                remote_ports=parse_ports(remote) if remote else None, peer=peer)

    def test_remote_ports_swap_the_keys(self):
        f = filters(self.plan(remote="8088").commands)
        egress = [c for c in f if c[5] == "egress"]
        ingress = [c for c in f if c[5] == "ingress"]
        self.assertTrue(all("dst_port" in c for c in egress))
        self.assertTrue(all("src_port" in c for c in ingress))
        self.assertEqual(len(f), 8)

    def test_peer_limits_family_and_address(self):
        f = filters(self.plan(ports="5060", peer="192.168.1.167").commands)
        self.assertEqual(len(f), 4)   # IPv4 only: udp+tcp, both hooks
        self.assertTrue(all("ip" == c[c.index("protocol") + 1] for c in f))
        egress = next(c for c in f if c[5] == "egress")
        self.assertEqual(egress[egress.index("dst_ip") + 1], "192.168.1.167")
        ingress = next(c for c in f if c[5] == "ingress")
        self.assertEqual(ingress[ingress.index("src_ip") + 1], "192.168.1.167")

    def test_peer_only_matches_all_traffic_with_it(self):
        f = filters(self.plan(peer="2001:db8::1").commands)
        self.assertEqual(len(f), 2)
        self.assertNotIn("ip_proto", f[0])
        self.assertIn("ipv6", f[0])

    def test_alias_round_trip(self):
        plan = self.plan(remote="8088,5060", peer="10.0.0.1")
        alias = next(c[-1] for c in plan.commands if "alias" in c)
        back = host.parse_alias(0, alias)
        self.assertEqual((back.ports, format_ports(back.remote_ports), back.peer),
                         ([], "5060,8088", "10.0.0.1"))

    def test_nothing_to_match(self):
        with self.assertRaises(WanshapeError):
            self.plan()
        with self.assertRaises(WanshapeError):
            self.plan(ports="5060", peer="not-an-address")

    def test_overlapping_remote_ports_refused(self):
        other = HostSlot(0, "a", "3g", "eth0", [], "both", True, remote_ports=parse_ports("8088"))
        live = HostLive(slots={0: other}, ifbs={"wsh0up", "wsh0dn"},
                        ifaces={"eth0": IfaceState(clsact=True)})
        with self.assertRaises(ConflictError):
            self.plan(remote="8000-9000", live=live)
        # A different peer makes them distinct.
        other.peer = "10.0.0.1"
        self.plan(remote="8000-9000", peer="10.0.0.2", live=live)

    def test_label_name_without_container(self):
        def missing(name, runtime):
            raise WanshapeError("not found")
        self.assertEqual(host_ports_for("ws", None, None, inspect=missing, optional=True),
                         ([], "none"))
        with self.assertRaises(WanshapeError):
            host_ports_for("ws", None, None, inspect=missing)


class ClearTests(unittest.TestCase):
    def test_clear_last_target_removes_clsact(self):
        live = HostLive(slots={0: slot()}, ifbs={"wsh0up", "wsh0dn"},
                        ifaces={"eth0": IfaceState(clsact=True, egress_prefs={49300},
                                                   ingress_prefs={49300})})
        commands = build_host_clear(slot(), live)
        self.assertEqual(commands, [
            ["tc", "filter", "del", "dev", "eth0", "egress", "pref", "49300"],
            ["tc", "filter", "del", "dev", "eth0", "ingress", "pref", "49300"],
            ["ip", "link", "del", "wsh0up"],
            ["ip", "link", "del", "wsh0dn"],
            ["tc", "qdisc", "del", "dev", "eth0", "clsact"],
        ])

    def test_clear_keeps_clsact_with_foreign_filters(self):
        live = HostLive(slots={0: slot()}, ifbs={"wsh0up", "wsh0dn"},
                        ifaces={"eth0": IfaceState(clsact=True, egress_prefs={49300, 1})})
        self.assertNotIn(["tc", "qdisc", "del", "dev", "eth0", "clsact"],
                         build_host_clear(slot(), live))

    def test_clear_keeps_clsact_for_other_target(self):
        live = HostLive(slots={0: slot(), 1: slot(1, "b", "5070")},
                        ifbs={"wsh0up", "wsh0dn", "wsh1up", "wsh1dn"},
                        ifaces={"eth0": IfaceState(clsact=True, egress_prefs={49300, 49400})})
        commands = build_host_clear(slot(), live)
        self.assertNotIn(["tc", "qdisc", "del", "dev", "eth0", "clsact"], commands)
        self.assertNotIn(["tc", "filter", "del", "dev", "eth0", "egress", "pref", "49400"],
                         commands)


class InspectTests(unittest.TestCase):
    def test_reads_slots_from_ifb_aliases(self):
        alias = slot(3, "cr", "5060,10000-20000").alias()
        responses = {
            ("ip", "-j", "-d", "link", "show", "type", "ifb"): json.dumps([
                {"ifname": "wsh3up", "ifalias": alias},
                {"ifname": "wsh3dn"},
                {"ifname": "other0"},
            ]),
            ("ip", "link", "show", "dev", "eth0"): "2: eth0: <UP>",
            ("tc", "qdisc", "show", "dev", "eth0"): "qdisc clsact ffff: parent ffff:fff1",
            ("tc", "filter", "show", "dev", "eth0", "egress"):
                "filter protocol ip pref 49600 flower chain 0\n"
                "filter protocol ip pref 49600 flower chain 0 handle 0x1\n",
            ("tc", "filter", "show", "dev", "eth0", "ingress"): "",
        }

        class Backend:
            def run(self, args):
                return responses.get(tuple(args), "")

            def run_result(self, args):
                out = responses.get(tuple(args), "")
                return subprocess.CompletedProcess(args, 0 if out else 1, out, "")

        live = inspect_host(Backend())
        self.assertEqual(set(live.slots), {3})
        self.assertEqual(live.slots[3].name, "cr")
        self.assertEqual(format_ports(live.slots[3].ports), "5060,10000-20000")
        self.assertEqual(live.ifbs, {"wsh3up", "wsh3dn"})
        self.assertTrue(live.ifaces["eth0"].clsact)
        self.assertEqual(live.ifaces["eth0"].egress_prefs, {49600})

    def test_bad_alias_ignored(self):
        self.assertIsNone(host.parse_alias(0, "something else"))
        self.assertIsNone(host.parse_alias(0, "wanshape-host name=x"))


if __name__ == "__main__":
    unittest.main()

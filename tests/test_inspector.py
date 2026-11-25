import unittest

from wanshape.inspector import inspect_live
from tests.fakes import FakeBackend

QDISC = """qdisc netem 5753: root refcnt 2 limit 1000 delay 150ms 30ms
qdisc clsact ffff: parent ffff:fff1
"""


class InspectorTests(unittest.TestCase):
    def test_detects_live_state(self):
        backend = FakeBackend({
            ("tc", "qdisc", "show", "dev", "eth0"): QDISC,
            ("tc", "filter", "show", "dev", "eth0", "ingress"): (
                "filter protocol all pref 49152 matchall chain 0\n"
                "  action order 1: mirred (Egress Redirect to device wanshape0) stolen\n"
            ),
            ("ip", "link", "show", "wanshape0"): "5: wanshape0: <UP>",
        })
        live = inspect_live(backend, "eth0")
        self.assertTrue(live.egress)
        self.assertTrue(live.ingress)
        self.assertTrue(live.clsact)

    def test_same_pref_from_another_tool_is_not_ours(self):
        backend = FakeBackend({
            ("tc", "qdisc", "show", "dev", "eth0"): "qdisc noqueue 0: root refcnt 2\n",
            ("tc", "filter", "show", "dev", "eth0", "ingress"): (
                "filter protocol ip pref 49152 u32 chain 0\n"
            ),
            ("ip", "link", "show", "wanshape0"): "",
        })
        live = inspect_live(backend, "eth0")
        self.assertFalse(live.our_filter)
        self.assertFalse(live.ingress)


if __name__ == "__main__":
    unittest.main()

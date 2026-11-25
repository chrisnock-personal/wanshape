import unittest

from wanshape.errors import WanshapeError
from wanshape.inspector import LiveState
from wanshape.planner import build_apply_plan, build_clear_plan
from wanshape.profiles import Direction, Profile


def profile(reorder=True) -> Profile:
    return Profile(
        name="test",
        reorder_on_jitter=reorder,
        up=Direction(delay="150ms", jitter="30ms", distribution="normal", rate="768kbit"),
        down=Direction(delay="150ms", jitter="30ms", distribution="normal", rate="1600kbit"),
    )


def clean() -> LiveState:
    return LiveState(root_kind="noqueue", root_handle="0:", clsact=False,
                     our_filter=False, ifb=False)


class PlannerTests(unittest.TestCase):
    def test_egress_only(self):
        plan = build_apply_plan(profile(), "eth0", "egress", clean())
        self.assertEqual(len(plan.egress), 1)
        self.assertEqual(plan.ingress, [])
        self.assertEqual(plan.egress[0][:7],
                         ["tc", "qdisc", "replace", "dev", "eth0", "root", "handle"])

    def test_reordering_adds_pfifo_child(self):
        plan = build_apply_plan(profile(reorder=False), "eth0", "egress", clean())
        self.assertEqual(len(plan.egress), 2)
        self.assertIn("pfifo", plan.egress[1])

    def test_both_directions(self):
        plan = build_apply_plan(profile(), "eth0", "both", clean())
        self.assertEqual(len(plan.egress), 1)
        self.assertEqual(len(plan.ingress), 5)
        self.assertIn("mirred", plan.ingress[3])
        self.assertIn("wanshape0", plan.ingress[3])

    def test_refuses_foreign_qdisc(self):
        live = LiveState(root_kind="htb", root_handle="1:", clsact=False,
                         our_filter=False, ifb=False)
        with self.assertRaises(WanshapeError):
            build_apply_plan(profile(), "eth0", "egress", live)
        plan = build_apply_plan(profile(), "eth0", "egress", live, force=True)
        self.assertEqual(len(plan.egress), 1)

    def test_refuses_foreign_clsact(self):
        live = LiveState(root_kind="noqueue", root_handle="0:", clsact=True,
                         our_filter=False, ifb=False)
        with self.assertRaises(WanshapeError):
            build_apply_plan(profile(), "eth0", "ingress", live)
        plan = build_apply_plan(profile(), "eth0", "ingress", live, force=True)
        self.assertEqual(len(plan.ingress), 5)

    def test_reapply_reuses_existing_ifb(self):
        live = LiveState(root_kind="netem", root_handle="5753:", clsact=True,
                         our_filter=True, ifb=True)
        plan = build_apply_plan(profile(), "eth0", "both", live)
        self.assertNotIn(["ip", "link", "add", "wanshape0", "type", "ifb"], plan.ingress)
        self.assertEqual(plan.ingress[0], ["ip", "link", "set", "wanshape0", "up"])
        self.assertIn("mirred", plan.ingress[2])

    def test_bursty_loss_args_are_split(self):
        p = Profile(
            name="gm",
            up=Direction(delay="50ms", loss_model="gemodel 1% 25% 100% 0%"),
            down=Direction(delay="50ms", loss_model="gemodel 1% 25% 100% 0%"),
        )
        plan = build_apply_plan(p, "eth0", "egress", clean())
        command = plan.egress[0]
        index = command.index("loss")
        self.assertEqual(command[index:index + 6],
                         ["loss", "gemodel", "1%", "25%", "100%", "0%"])

    def test_clear_restores_fq_codel(self):
        live = LiveState(root_kind="netem", root_handle="5753:", clsact=True,
                         our_filter=True, ifb=True)
        plan = build_clear_plan("eth0", "both", live, True, "fq_codel 0:")
        self.assertEqual(
            plan.commands,
            [
                ["tc", "qdisc", "del", "dev", "eth0", "root"],
                ["tc", "qdisc", "replace", "dev", "eth0", "root", "fq_codel"],
                ["tc", "filter", "del", "dev", "eth0", "ingress", "pref", "49152"],
                ["tc", "qdisc", "del", "dev", "eth0", "clsact"],
                ["ip", "link", "del", "wanshape0"],
            ],
        )

    def test_clear_leaves_foreign_clsact_alone(self):
        live = LiveState(root_kind="noqueue", root_handle="0:", clsact=True,
                         our_filter=False, ifb=False)
        plan = build_clear_plan("eth0", "both", live, False, "noqueue 0:")
        self.assertTrue(plan.empty)


if __name__ == "__main__":
    unittest.main()

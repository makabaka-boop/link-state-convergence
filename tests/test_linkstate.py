"""端到端测试（标准库 unittest，无需 pytest）。

核对方式：测试文件内自带一份**独立**的最短路实现 —— 枚举所有简单路径
再按 (费用, 完整路径字典序) 取最优 —— 与路由器的 Dijkstra 不共享代码，
适用于小图的独立核对。

覆盖：
* 2~8 节点小图最终收敛（含并列费用的字典序裁决）；
* 旧序号通告：直接构造旧 LSA、编排传输让旧通告迟到、分区滞留旧通告；
* 链路涨价后改走更优路径；
* 分区期间明确不可达，重连且消息停止丢失后收敛；
* 传输层延迟、复制、乱序、丢弃下的最终收敛。
"""

from __future__ import annotations

import itertools
import unittest
from collections import defaultdict

from linkstate import (
    ACCEPT_DUPLICATE,
    ACCEPT_INSTALLED,
    ACCEPT_STALE,
    LSA,
    RuleTransport,
    Router,
    Simulation,
    Transport,
)


# ---------------------------------------------------------------------------
# 独立参考真值：暴力枚举所有简单路径（仅用于小图）


def brute_force_routes(edges, down=frozenset()):
    """返回 {src: {dest: {"cost", "path", "reachable", "next_hop"}}}。"""
    adj = defaultdict(dict)
    nodes = set()
    for (a, b), cost in edges.items():
        nodes.update((a, b))
        if frozenset((a, b)) in down:
            continue
        adj[a][b] = cost
        adj[b][a] = cost
    nodes = sorted(nodes)

    best = {}
    for src in nodes:
        best[src] = {}
        for dest in nodes:
            if src == dest:
                best[src][dest] = {
                    "reachable": True, "cost": 0, "path": [src],
                    "next_hop": None}
                continue
            winner = None
            # 枚举简单路径：按中间节点个数生成排列，检查相邻边是否存在。
            for length in range(0, len(nodes) - 1):
                for middle in itertools.permutations(
                        [n for n in nodes if n not in (src, dest)], length):
                    path = (src,) + middle + (dest,)
                    cost = 0
                    ok = True
                    for u, v in zip(path, path[1:]):
                        if v not in adj[u]:
                            ok = False
                            break
                        cost += adj[u][v]
                    if ok:
                        cand = (cost, path)
                        if winner is None or cand < winner:
                            winner = cand
            if winner is None:
                best[src][dest] = {"reachable": False, "cost": None,
                                   "path": [], "next_hop": None}
            else:
                cost, path = winner
                best[src][dest] = {
                    "reachable": True, "cost": cost, "path": list(path),
                    "next_hop": path[1]}
    return best


def assert_routes_equal(testcase, sim, expected):
    actual = sim.current_routes()
    testcase.assertEqual(set(actual), set(expected))
    for src in expected:
        for dest, want in expected[src].items():
            got = actual[src][dest]
            if not want.get("reachable", True):
                testcase.assertFalse(
                    got["reachable"], f"{src}->{dest} 应不可达，实际 {got}")
                continue
            testcase.assertTrue(got["reachable"], f"{src}->{dest} 应可达")
            testcase.assertEqual(got["cost"], want["cost"],
                                 f"{src}->{dest} 费用错误")
            testcase.assertEqual(got["path"], want["path"],
                                 f"{src}->{dest} 路径/字典序裁决错误")
            testcase.assertEqual(got["next_hop"], want["next_hop"])


# ---------------------------------------------------------------------------
# 单元测试：序号规则与独立路由计算


class StaleSequenceUnitTest(unittest.TestCase):
    def test_old_sequence_never_overwrites(self):
        r = Router("R", all_router_ids=("R", "X", "Y"))
        fresh = LSA.create("X", 3, {"Y": 1})
        status, _ = r.receive(fresh, "X")
        self.assertEqual(status, ACCEPT_INSTALLED)

        stale = LSA.create("X", 2, {"Y": 9})
        status, _ = r.receive(stale, "X")
        self.assertEqual(status, ACCEPT_STALE)
        self.assertIs(r.lsdb["X"], fresh)  # 新状态未被覆盖

        older = LSA.create("X", 1, {})
        status, _ = r.receive(older, "X")
        self.assertEqual(status, ACCEPT_STALE)
        self.assertEqual(r.lsdb["X"].seq, 3)
        self.assertEqual(r.lsdb["X"].links, (("Y", 1),))

    def test_same_sequence_is_duplicate_not_forwarded(self):
        r = Router("R", all_router_ids=("R", "X", "Z"))
        r.local_links = {"X": 1, "Z": 1}
        lsa = LSA.create("X", 5, {"R": 1})
        self.assertEqual(r.receive(lsa, "X")[0], ACCEPT_INSTALLED)
        status, forwards = r.receive(lsa, "X")
        self.assertEqual(status, ACCEPT_DUPLICATE)
        self.assertEqual(forwards, [])

    def test_router_only_uses_its_lsdb(self):
        # LSDB 里 X 自称连 Y，但 Y 的当前通告没有 X —— 边不成立。
        r = Router("X", all_router_ids=("X", "Y"))
        r.originate({"Y": 1})
        r._install(LSA.create("Y", 1, {}))
        self.assertFalse(r.routes["Y"].reachable)

        # 当 Y 也确认 X 且费用一致时，边才成立。
        r._install(LSA.create("Y", 2, {"X": 1}))
        self.assertTrue(r.routes["Y"].reachable)
        self.assertEqual(r.routes["Y"].cost, 1)


class TieBreakUnitTest(unittest.TestCase):
    def test_lexicographic_full_path_on_brute_force_graphs(self):
        # 一个存在两条等费用路径的菱形：S-A-T 与 S-B-T
        edges = {("S", "A"): 1, ("S", "B"): 1,
                 ("A", "T"): 2, ("B", "T"): 2}
        r = Router("S", all_router_ids=tuple(sorted(
            set().union(*edges))))
        r.originate({"A": 1, "B": 1})
        r._install(LSA.create("A", 1, {"S": 1, "T": 2}))
        r._install(LSA.create("B", 1, {"S": 1, "T": 2}))
        r._install(LSA.create("T", 1, {"A": 2, "B": 2}))
        want = brute_force_routes(edges)["S"]["T"]
        got = r.routes["T"]
        self.assertEqual(got.cost, want["cost"])
        self.assertEqual(list(got.path), want["path"])
        self.assertEqual(want["path"], ["S", "A", "T"])  # 字典序裁决


# ---------------------------------------------------------------------------
# 集成：小图收敛（2 ~ 8 节点）


class ConvergenceTest(unittest.TestCase):
    def _check_graph(self, edges, ticks=60, transport=None):
        sim = Simulation(edges, transport=transport or Transport())
        expected = brute_force_routes(edges)
        sim.drain_until_match(expected, max_ticks=ticks)
        assert_routes_equal(self, sim, expected)
        # 再跑若干 tick（含一次周期补发），路由表必须保持稳定。
        for _ in range(8):
            sim.step()
        assert_routes_equal(self, sim, expected)
        return sim

    def test_two_routers(self):
        self._check_graph({("A", "B"): 3})

    def test_three_node_line(self):
        self._check_graph({("A", "B"): 2, ("B", "C"): 4})

    def test_diamond_with_tie(self):
        edges = {("S", "A"): 1, ("S", "B"): 1,
                 ("A", "T"): 2, ("B", "T"): 2,
                 ("A", "B"): 5}
        self._check_graph(edges)

    def test_eight_node_graph_under_chaotic_transport(self):
        edges = {
            ("n1", "n2"): 2, ("n1", "n3"): 5,
            ("n2", "n3"): 1, ("n2", "n4"): 4,
            ("n3", "n5"): 3, ("n4", "n5"): 2,
            ("n4", "n6"): 6, ("n5", "n7"): 1,
            ("n6", "n7"): 2, ("n6", "n8"): 1,
            ("n7", "n8"): 3,
        }
        t = Transport(drop_p=0.2, dup_p=0.25, delay_min=1, delay_max=4,
                      reorder_bias=0.3, seed=42)
        sim = self._check_graph(edges, ticks=220, transport=t)
        # 有损阶段确实发生过丢弃与复制。
        self.assertGreater(t.dropped_count, 0)
        self.assertGreater(t.duplicated_count, 0)
        # 复制的副本到达后只能是 duplicate（收敛后）。
        late_dups = [d for rec in sim.history for d in rec.deliveries
                     if d.duplicated and d.status == ACCEPT_DUPLICATE]
        self.assertTrue(late_dups)


# ---------------------------------------------------------------------------
# 链路涨价


class CostIncreaseTest(unittest.TestCase):
    def test_link_price_increase_reroutes(self):
        edges = {
            ("A", "B"): 1, ("A", "C"): 1,
            ("B", "D"): 4, ("C", "D"): 4,
        }
        t = Transport(drop_p=0.2, dup_p=0.15, delay_min=1, delay_max=3,
                      seed=11)
        sim = Simulation(edges, transport=t, reflood_period=4)

        before = brute_force_routes(edges)
        sim.drain_until_match(before, max_ticks=80)
        assert_routes_equal(self, sim, before)
        # 并列时字典序走 B。
        self.assertEqual(sim.current_routes()["A"]["D"]["path"],
                         ["A", "B", "D"])

        # B-D 涨价：4 -> 10，最优改为经 C。
        sim.schedule_cost(sim.tick, "B", "D", 10)
        sim.schedule_transport_reliable(sim.tick + 1, True)
        changed_edges = dict(edges)
        changed_edges[("B", "D")] = 10
        after = brute_force_routes(changed_edges)
        sim.drain_until_match(after, max_ticks=120)
        assert_routes_equal(self, sim, after)
        self.assertEqual(sim.current_routes()["A"]["D"]["path"],
                         ["A", "C", "D"])
        self.assertEqual(sim.current_routes()["A"]["D"]["cost"], 5)

    def test_decrease_then_increase_sequence_monotonic(self):
        edges = {("A", "B"): 5, ("A", "C"): 2, ("B", "C"): 2}
        sim = Simulation(edges)
        sim.drain_until_match(brute_force_routes(edges))
        seq_before = sim.routers["A"].lsdb["A"].seq
        sim.schedule_cost(sim.tick, "A", "B", 1)
        sim.step()
        self.assertEqual(sim.routers["A"].lsdb["A"].seq, seq_before + 1)
        cheaper = dict(edges); cheaper[("A", "B")] = 1
        sim.drain_until_match(brute_force_routes(cheaper))
        sim.schedule_cost(sim.tick, "A", "B", 9)
        sim.step()
        self.assertEqual(sim.routers["A"].lsdb["A"].seq, seq_before + 2)
        dearer = dict(edges); dearer[("A", "B")] = 9
        sim.drain_until_match(brute_force_routes(dearer))
        assert_routes_equal(self, sim, brute_force_routes(dearer))


# ---------------------------------------------------------------------------
# 旧序号通告：显式编排乱序迟到


class StaleInTransitTest(unittest.TestCase):
    def test_old_advertisement_arrives_after_new_one(self):
        # A--B 是 A 侧通往 {F,...} 的桥，确保 F 只能经 A-B 收到 A 的 LSA。
        edges = {
            ("A", "B"): 1, ("A", "F"): 1,
            ("B", "C"): 1, ("C", "D"): 1, ("D", "E"): 1,
            ("B", "E"): 3,
        }
        # tick 8 涨价后，A 已经是序号 2；把此时补发的旧 A#1（->F）
        # 扣到 tick 22。after_tick=8 保证 tick 0 的初始泛洪不受影响。
        rules = {("A", "F", 1): {"hold_until": 22, "after_tick": 8,
                                 "before_tick": 22}}
        t = RuleTransport(delay_min=1, delay_max=1, rules=rules)
        sim = Simulation(edges, transport=t, reflood_period=4)

        # tick 8 涨价 A-B：A 产生序号 2。
        sim.schedule_cost(8, "A", "B", 6)
        # 运行 21 步（处理完 tick 0..20，sim.tick=21）：F 应已通过
        # A-B 收到 A#2（延迟恒为 1 tick，涨价后多跳泛洪到 F 最多 4 跳）。
        sim.run(21)
        f_db = sim.history[-1].lsdb["F"]
        self.assertEqual(f_db["A"]["seq"], 2)

        # tick 22：被扣下的 A#1 迟到，必须被判为过期并拒绝。
        sim.step()  # tick 21
        rec = sim.step()  # tick 22
        self.assertEqual(rec.tick, 22)
        stale = [d for d in rec.deliveries
                 if d.origin == "A" and d.status == ACCEPT_STALE]
        self.assertTrue(stale, "迟到的旧序号通告应被明确拒绝")
        self.assertEqual(sim.routers["F"].lsdb["A"].seq, 2)

        # 最终收敛与独立暴力真值一致，且路由从未被旧状态污染。
        changed = dict(edges); changed[("A", "B")] = 6
        expected = brute_force_routes(changed)
        sim.drain_until_match(expected, max_ticks=80)
        assert_routes_equal(self, sim, expected)


# ---------------------------------------------------------------------------
# 分区与重连


class PartitionReconnectTest(unittest.TestCase):
    def test_partition_unreachable_then_reconverge(self):
        edges = {("A", "B"): 1, ("B", "C"): 1,
                 ("A", "D"): 2, ("D", "C"): 2}
        # 初始可靠，保证 tick 10 前完成收敛；规则只拦截分区开始后
        # A 补发给 B 的旧 A#1（after_tick=10），扣到重连之后的 tick 33。
        t = RuleTransport(
            drop_p=0.0, dup_p=0.0, delay_min=1, delay_max=1, seed=5,
            rules={("A", "B", 1): {"hold_until": 33, "after_tick": 10,
                                   "before_tick": 33}},
        )
        sim = Simulation(edges, transport=t, reflood_period=5)
        sim.run(9)
        assert_routes_equal(self, sim, brute_force_routes(edges))
        # tick 9 起把传输层调成有损（延迟/复制/丢弃）。
        t.drop_p, t.dup_p, t.delay_max = 0.25, 0.15, 2

        # tick 10 分区：断开两条跨区链路，{A,D} 与 {B,C} 分离。
        sim.schedule_link(10, "A", "B", False)
        sim.schedule_link(10, "D", "C", False)
        unreachable_seen = {"A->C": False, "C->A": False,
                            "D->B": False, "B->D": False}
        for _ in range(19):  # 跑到 tick 29（含重连前一刻）
            rec = sim.step()
            rt = rec.routes
            if not rt["A"]["C"]["reachable"]:
                unreachable_seen["A->C"] = True
            if not rt["C"]["A"]["reachable"]:
                unreachable_seen["C->A"] = True
            if not rt["D"]["B"]["reachable"]:
                unreachable_seen["D->B"] = True
            if not rt["B"]["D"]["reachable"]:
                unreachable_seen["B->D"] = True
        self.assertTrue(all(unreachable_seen.values()),
                        f"分区期间应出现明确不可达: {unreachable_seen}")
        # 分区中每个路由器对跨区目的都要显式标注不可达。
        for rid in ("A", "D"):
            for dest in ("B", "C"):
                self.assertIn(dest, sim.routers[rid].routes)
                self.assertFalse(sim.routers[rid].routes[dest].reachable)
        for rid in ("B", "C"):
            for dest in ("A", "D"):
                self.assertFalse(sim.routers[rid].routes[dest].reachable)
        # 分区两侧内部仍可达且费用正确。
        self.assertEqual(sim.routers["A"].routes["D"].cost, 2)
        self.assertEqual(sim.routers["B"].routes["C"].cost, 1)

        # tick 30 重连，且消息停止丢失（传输层变可靠）。
        sim.schedule_link(30, "A", "B", True)
        sim.schedule_link(30, "D", "C", True)
        sim.schedule_transport_reliable(30, True)

        # 滞留的旧 A#1 在 tick 33 到达 B，被拒绝。
        stale_seen = False
        for _ in range(20):  # 跑到 tick 49
            rec = sim.step()
            if any(d.status == ACCEPT_STALE for d in rec.deliveries):
                stale_seen = True
        self.assertTrue(stale_seen, "重连后应有滞留旧通告被拒绝")

        # 最终按独立暴力真值收敛。
        expected = brute_force_routes(edges)
        sim.drain_until_match(expected, max_ticks=60)
        assert_routes_equal(self, sim, expected)
        # 旧序号始终没有覆盖新状态（分区与重连各产生过一次新序号）。
        self.assertGreaterEqual(sim.routers["A"].lsdb["A"].seq, 2)

    def test_two_node_partition(self):
        edges = {("A", "B"): 1}
        sim = Simulation(edges)
        sim.drain_until_match(brute_force_routes(edges))
        sim.schedule_link(sim.tick + 1, "A", "B", False)
        for _ in range(6):
            sim.step()
        self.assertFalse(sim.current_routes()["A"]["B"]["reachable"])
        self.assertFalse(sim.current_routes()["B"]["A"]["reachable"])

        sim.schedule_link(sim.tick + 1, "A", "B", True)
        sim.drain_until_match(brute_force_routes(edges), max_ticks=40)
        assert_routes_equal(self, sim, brute_force_routes(edges))


# ---------------------------------------------------------------------------
# 周期性补发是最终一致的保证


class PeriodicRefloodTest(unittest.TestCase):
    def test_convergence_even_with_heavy_loss_only_via_reflood(self):
        # 高丢包 + 零复制；初始泛洪可能几乎全丢，靠周期补发收敛。
        edges = {("A", "B"): 1, ("B", "C"): 1, ("C", "D"): 1}
        t = Transport(drop_p=0.6, delay_min=1, delay_max=1, seed=99)
        sim = Simulation(edges, transport=t, reflood_period=3)
        expected = brute_force_routes(edges)
        sim.drain_until_match(expected, max_ticks=300, require_stable=5)
        assert_routes_equal(self, sim, expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)

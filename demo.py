"""命令行演示：两个独立场景，打印逐 tick 消息、LSDB 与路由表。

场景 1：链路涨价。菱形拓扑含并列费用路径（字典序裁决），
        先在有损传输层下收敛，随后一条链路涨价，全网改走另一条最短路。
场景 2：分区与重连。线性拓扑分区期间明确显示"不可达"，
        重连后分区期间被滞留的旧序号通告到达时被拒绝（不得覆盖新状态），
        消息停止丢失后重新收敛。

用法：
    python3 demo.py            # 紧凑模式（只打印变化、过期拒绝与状态快照）
    python3 demo.py --all      # 打印每个 tick 的每条消息
"""

from __future__ import annotations

import sys

from linkstate import RuleTransport, Simulation, expected_tables, render_history
from linkstate.network import _format_routes


def _print_expected(title, edges, down=None) -> None:
    print(f"\n### 参考真值（独立最短路实现）{title}")
    print(_format_routes(expected_tables(edges, down=down)))


def scenario_price_increase(verbose: bool) -> None:
    print("=" * 64)
    print("场景 1：链路涨价 + 有损/复制/乱序传输 + 并列费用字典序裁决")
    print("=" * 64)
    edges = {
        ("A", "B"): 1,
        ("A", "C"): 1,   # A->D: A-B-D 与 A-C-D 费用并列
        ("B", "D"): 4,   #      路径 (A,B,D) < (A,C,D) 字典序，走 B
        ("C", "D"): 4,
        ("B", "C"): 5,
    }
    transport = RuleTransport(
        drop_p=0.15, dup_p=0.2, delay_min=1, delay_max=3,
        reorder_bias=0.3, seed=7,
    )
    sim = Simulation(edges, transport=transport, reflood_period=5)

    print("""
拓扑（初始）:
        1       4
    A ----- B ----- D
    |       |       |
    |1      |5      |
    C ------+       |
        4 ----------+

A->D 的两条路径费用并列 (5)：按完整路径字典序 (A,B,D) < (A,C,D)，走 B。
tick 12：B-D 涨价 4 -> 8，最短路应改为 A-C-D (5)。
""")

    sim.schedule_cost(12, "B", "D", 8)
    sim.schedule_transport_reliable(14, True)
    sim.run(25)

    print(render_history(sim.history, verbose=verbose))

    _print_expected(" —— 涨价后最终应收敛到：",
                    {("A", "B"): 1, ("A", "C"): 1, ("B", "D"): 8,
                     ("C", "D"): 4, ("B", "C"): 5})
    ad = sim.current_routes()["A"]["D"]
    print(f"\n实际 A->D: cost={ad['cost']} path={ad['path']} "
          f"下一跳={ad['next_hop']}")
    print(f"传输统计: 发送 {transport.sent_count}，"
          f"丢弃 {transport.dropped_count}，复制 {transport.duplicated_count}")


def scenario_partition(verbose: bool) -> None:
    print("\n" + "=" * 64)
    print("场景 2：分区 -> 明确不可达 -> 重连 -> 旧序号通告被拒绝 -> 收敛")
    print("=" * 64)
    edges = {("A", "B"): 1, ("B", "C"): 1}
    # 分区开始的 tick 10，周期补发把 A#1 发给 B；用规则把它滞留到重连
    # 之后的 tick 28。after_tick=10 使初始泛洪（同为 A#1）不受影响。
    rules = {("A", "B", 1): {"hold_until": 28, "after_tick": 10,
                             "before_tick": 28}}
    transport = RuleTransport(drop_p=0.1, dup_p=0.15,
                              delay_min=1, delay_max=2,
                              seed=3, rules=rules)
    sim = Simulation(edges, transport=transport, reflood_period=5)

    print("""
拓扑（线性）:  A --1-- B --1-- C

tick 10：断开 A-B（分区）。A 与 {B,C} 互相不可达。
tick 22：恢复 A-B。分区开始时滞留的旧通告 A#1 在 tick 28 到达 B，
         此时 B 已持有 A#2，旧通告被拒绝；随后收敛。
""")

    sim.schedule_link(10, "A", "B", False)
    sim.schedule_link(22, "A", "B", True)
    sim.schedule_transport_reliable(22, True)
    sim.run(32)

    print(render_history(sim.history, verbose=verbose))

    _print_expected(" —— 分区期间（A-B 断开）：",
                    edges, down={frozenset(("A", "B"))})
    # 在分区期间找一个两侧都已收敛的 tick 做快照。
    snap = next(
        rec.routes for rec in sim.history
        if 12 <= rec.tick <= 20
        and not rec.routes["A"]["C"]["reachable"]
        and not rec.routes["C"]["A"]["reachable"]
    )
    print("\n实际（分区期间快照）：")
    print(f"  A->C reachable = {snap['A']['C']['reachable']}（应为 False）")
    print(f"  C->A reachable = {snap['C']['A']['reachable']}（应为 False）")

    _print_expected(" —— 重连后最终：", edges)
    ac = sim.current_routes()["A"]["C"]
    print(f"\n实际 A->C: cost={ac['cost']} path={ac['path']} "
          f"下一跳={ac['next_hop']}")

    stale = [d for rec in sim.history for d in rec.deliveries
             if d.status == "stale"]
    print(f"被拒绝的过期通告数: {len(stale)}（示例: "
          f"{[(d.from_id, d.to_id, d.origin, d.seq) for d in stale[:3]]} ...）")


if __name__ == "__main__":
    verbose = "--all" in sys.argv
    scenario_price_increase(verbose)
    scenario_partition(verbose)

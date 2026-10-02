"""模拟网络：逐 tick 驱动链路变化、通告收发与周期性补发。

时间模型（每个 tick）：
  1. 处理拓扑事件（up/down/set_cost/transport 开关）；
  2. 发送侧：周期补发（每 ``reflood_period`` 个 tick），新通告立刻进入传输层；
  3. 投递侧：到期的通告交给目的路由器处理（产生新的转发也排到以后的 tick）；
  4. 记录本 tick 的全部消息与各路由器的 LSDB、路由表快照。

事件队列里所有动作都带未来 tick，同一 tick 内的拓扑事件早于投递事件，
拓扑变化引发的通告最早下一个 tick 才能到达，杜绝同 tick 无限连锁。
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field

from .lsa import LSA
from .router import (
    ACCEPT_DUPLICATE,
    ACCEPT_INSTALLED,
    ACCEPT_STALE,
    Router,
)
from .transport import Delivery, Transport


@dataclass(frozen=True)
class SendRecord:
    tick: int
    from_id: str
    to_id: str
    origin: str
    seq: int
    status: str  # sent | dropped


@dataclass(frozen=True)
class DeliveryRecord:
    tick: int
    from_id: str
    to_id: str
    origin: str
    seq: int
    status: str  # installed | duplicate | stale
    duplicated: bool = False


@dataclass(frozen=True)
class EventRecord:
    tick: int
    kind: str          # link | cost | transport
    a: str
    b: str | None
    value: object = None
    detail: str = ""


@dataclass
class TickRecord:
    tick: int
    events: list[EventRecord] = field(default_factory=list)
    sends: list[SendRecord] = field(default_factory=list)
    deliveries: list[DeliveryRecord] = field(default_factory=list)
    lsdb: dict = field(default_factory=dict)
    routes: dict = field(default_factory=dict)


class Simulation:
    def __init__(
        self,
        edges: dict[tuple[str, str], int],
        transport: Transport | None = None,
        reflood_period: int = 5,
    ) -> None:
        self.transport = transport or Transport()
        self.reflood_period = reflood_period

        node_ids: set[str] = set()
        for (a, b), cost in edges.items():
            if a == b:
                raise ValueError(f"不允许自环: {a}")
            if not isinstance(cost, int) or cost <= 0:
                raise ValueError(f"链路费用必须为正整数: {(a, b)} -> {cost}")
            node_ids.update((a, b))
        self.node_ids = tuple(sorted(node_ids))

        # 物理链路的原费用（恢复时沿用）。
        self.link_costs: dict[frozenset[str], int] = {}
        # 当前断开的链路。
        self.down_links: set[frozenset[str]] = set()
        for (a, b), cost in edges.items():
            key = frozenset((a, b))
            if key in self.link_costs:
                raise ValueError(f"重复的链路定义: {(a, b)}")
            self.link_costs[key] = cost

        self.routers: dict[str, Router] = {
            rid: Router(rid, all_router_ids=self.node_ids)
            for rid in self.node_ids
        }
        # 路由器当前实际在用的直连邻居。
        self.active_neighbors: dict[str, dict[str, int]] = {
            rid: {} for rid in self.node_ids
        }
        for key, cost in self.link_costs.items():
            a, b = tuple(key)
            self.active_neighbors[a][b] = cost
            self.active_neighbors[b][a] = cost

        self.tick = 0
        # 事件堆：(tick, seq, kind, payload)
        self._events: list = []
        self._tie = 0
        self.history: list[TickRecord] = []

        # 让每个路由器产生初始 LSA（序号 1）并排入首 tick 泛洪。
        for rid, r in self.routers.items():
            lsa = r.originate(self.active_neighbors[rid])
            for nb in self.active_neighbors[rid]:
                self._transmit(0, rid, nb, lsa)

    # ------------------------------------------------------------------
    # 事件安排

    # 同 tick 事件的优先级：传输模式开关最早生效，其次拓扑变化，
    # 最后投递（拓扑变化引起的新通告总在之后的 tick 才到达）。
    _KIND_PRIORITY = {"transport": 0, "link": 1, "cost": 1, "deliver": 2}

    def _push(self, tick: int, kind: str, payload: object) -> None:
        heapq.heappush(
            self._events,
            (tick, self._KIND_PRIORITY[kind], self._tie, kind, payload),
        )
        self._tie += 1

    def schedule_link(self, tick: int, a: str, b: str, up: bool) -> None:
        self._push(tick, "link", (a, b, up))

    def schedule_cost(self, tick: int, a: str, b: str, cost: int) -> None:
        if not isinstance(cost, int) or cost <= 0:
            raise ValueError("新费用必须为正整数")
        self._push(tick, "cost", (a, b, cost))

    def schedule_transport_reliable(self, tick: int, reliable: bool) -> None:
        self._push(tick, "transport", reliable)

    def _transmit(self, tick: int, a: str, b: str, lsa: LSA,
                  record: TickRecord | None = None) -> None:
        deliveries = self.transport.plan(tick, a, b, lsa)
        if not deliveries:
            if record is not None:
                record.sends.append(
                    SendRecord(tick, a, b, lsa.origin, lsa.seq, "dropped"))
            return
        if record is not None:
            record.sends.append(
                SendRecord(tick, a, b, lsa.origin, lsa.seq, "sent"))
        for d in deliveries:
            self._push(d.deliver_tick, "deliver", d)

    # ------------------------------------------------------------------
    # 逐 tick 驱动

    def step(self) -> TickRecord:
        rec = TickRecord(tick=self.tick)

        # 1. 周期补发当前已知的全部状态（拓扑事件之前先发，
        #    保证一个 tick 内顺序确定；两者均在下一 tick 才到达）。
        if self.tick > 0 and self.tick % self.reflood_period == 0:
            for rid, r in self.routers.items():
                for nb, lsa in r.periodic_reflood():
                    self._transmit(self.tick, rid, nb, lsa, rec)

        # 2/3. 处理所有到期事件（同 tick 顺序：传输开关 -> 拓扑变化 -> 投递）。
        due = []
        while self._events and self._events[0][0] <= self.tick:
            due.append(heapq.heappop(self._events))
        due.sort(key=lambda e: (e[0], e[1], e[2]))
        for _, _, _, kind, payload in due:
            if kind == "link":
                self._apply_link(payload, rec)
            elif kind == "cost":
                self._apply_cost(payload, rec)
            elif kind == "transport":
                self.transport.set_reliable(bool(payload))
                rec.events.append(
                    EventRecord(self.tick, "transport", "", None,
                                bool(payload),
                                "reliable" if payload else "lossy"))
            elif kind == "deliver":
                self._apply_delivery(payload, rec)

        # 4. 快照。
        rec.lsdb = {rid: r.lsdb_snapshot() for rid, r in self.routers.items()}
        rec.routes = {rid: r.routes_snapshot()
                      for rid, r in self.routers.items()}

        self.history.append(rec)
        self.tick += 1
        return rec

    def _notify_routers(self, endpoints: set[str],
                        rec: TickRecord) -> None:
        for rid in endpoints:
            r = self.routers[rid]
            sends = r.update_local_links(self.active_neighbors[rid])
            for nb, lsa in sends:
                self._transmit(self.tick, rid, nb, lsa, rec)

    def _apply_link(self, payload, rec: TickRecord) -> None:
        a, b, up = payload
        key = frozenset((a, b))
        if key not in self.link_costs:
            raise KeyError(f"不存在的链路: {(a, b)}")
        if up:
            will_be_cost = self.link_costs[key]
            self.down_links.discard(key)
            self.active_neighbors[a][b] = will_be_cost
            self.active_neighbors[b][a] = will_be_cost
        else:
            self.down_links.add(key)
            self.active_neighbors[a].pop(b, None)
            self.active_neighbors[b].pop(a, None)
        rec.events.append(
            EventRecord(self.tick, "link", a, b, up,
                        f"{'up' if up else 'down'}"))
        self._notify_routers({a, b}, rec)

    def _apply_cost(self, payload, rec: TickRecord) -> None:
        a, b, cost = payload
        key = frozenset((a, b))
        if key not in self.link_costs:
            raise KeyError(f"不存在的链路: {(a, b)}")
        if key in self.down_links:
            raise ValueError(f"链路 {(a, b)} 当前断开，不能改费用")
        self.link_costs[key] = cost
        self.active_neighbors[a][b] = cost
        self.active_neighbors[b][a] = cost
        rec.events.append(
            EventRecord(self.tick, "cost", a, b, cost, f"cost={cost}"))
        self._notify_routers({a, b}, rec)

    def _apply_delivery(self, d: Delivery, rec: TickRecord) -> None:
        # 若链路此刻已断，投递直接失败（不再是邻居），单独标注，
        # 不与"序号过旧被拒绝"混淆。
        if d.to_id not in self.active_neighbors.get(d.from_id, {}):
            rec.deliveries.append(
                DeliveryRecord(self.tick, d.from_id, d.to_id,
                               d.lsa.origin, d.lsa.seq,
                               "link-gone", d.duplicated))
            return
        r = self.routers[d.to_id]
        status, forwards = r.receive(d.lsa, d.from_id)
        rec.deliveries.append(
            DeliveryRecord(self.tick, d.from_id, d.to_id,
                           d.lsa.origin, d.lsa.seq, status, d.duplicated))
        if status == ACCEPT_INSTALLED:
            for nb, lsa in forwards:
                self._transmit(self.tick, d.to_id, nb, lsa, rec)

    # ------------------------------------------------------------------
    # 运行与校验

    def run(self, ticks: int, quiet: bool = True) -> list[TickRecord]:
        for _ in range(ticks):
            self.step()
        return self.history[-ticks:]

    @property
    def in_flight(self) -> int:
        return len(self._events)

    def current_routes(self) -> dict[str, dict]:
        return {rid: r.routes_snapshot()
                for rid, r in self.routers.items()}

    def routes_match(self, expected: dict[str, dict[str, dict]]) -> bool:
        """expected[src][dest] = {"path": [...], "cost": c}（或不可达标记）。"""
        for src, table in expected.items():
            actual = self.routers[src].routes
            for dest, want in table.items():
                got = actual[dest]
                if not want.get("reachable", True):
                    if got.reachable:
                        return False
                    continue
                if not got.reachable or got.cost != want["cost"]:
                    return False
                if list(got.path) != list(want["path"]):
                    return False
        return True

    def drain_until_match(
        self,
        expected: dict[str, dict[str, dict]],
        max_ticks: int = 200,
        require_stable: int = 3,
    ) -> int:
        """一直跑到路由表与期望一致且连续若干 tick 稳定；返回总 tick 数。"""
        stable = 0
        last = None
        while self.tick < max_ticks:
            self.step()
            cur = self.current_routes()
            if self.routes_match(expected) and cur == last:
                stable += 1
                if stable >= require_stable:
                    return self.tick
            else:
                stable = 0
            last = cur
        raise AssertionError(
            f"在 {max_ticks} ticks 内未收敛；当前路由:\n"
            + _format_routes(self.current_routes())
        )


def _format_routes(routes: dict[str, dict]) -> str:
    lines = []
    for src, table in routes.items():
        for dest, r in sorted(table.items()):
            if r["reachable"]:
                lines.append(
                    f"  {src} -> {dest}: cost={r['cost']} "
                    f"path={r['path']} nh={r['next_hop']}")
            else:
                lines.append(f"  {src} -> {dest}: UNREACHABLE")
    return "\n".join(lines)

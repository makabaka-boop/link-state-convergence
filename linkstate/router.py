"""路由器：只基于自己的链路状态库（LSDB）独立计算下一跳。

关键规则：
* 路由器永远不知道"全局真值"。它构造的图只包含 LSDB 中
  **两端互相确认且费用一致** 的链路（单向/费用不一致的视图视为链路不可用）。
* 收到旧序号（seq 小于库中已有序号）一律拒绝，新状态不会被旧状态覆盖。
* 本地链路变化 / 收到更新的通告时，路由器立刻重算自己的路由表。
* 每个周期把当前已知的全部通告补发给所有在用邻居（分区恢复后
  最终一致性就靠它 + 序号规则保证）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .lsa import LSA

# 接收处理结果，供传输层/模拟器记录逐 tick 消息：
#   installed  旧库没有或被更新（随后会泛洪给除来源外的邻居）
#   duplicate  序号相同（不泛洪）
#   stale      序号更旧（必须拒绝，旧序号不得覆盖新状态）
ACCEPT_INSTALLED = "installed"
ACCEPT_DUPLICATE = "duplicate"
ACCEPT_STALE = "stale"


@dataclass(frozen=True)
class Route:
    dest: str
    next_hop: str | None
    cost: int
    path: tuple[str, ...]
    reachable: bool


class Router:
    def __init__(
        self,
        router_id: str,
        links: dict[str, int] | None = None,
        all_router_ids: tuple[str, ...] = (),
    ) -> None:
        self.id = router_id
        # 当前实际在用的本地链路视图。
        self.local_links: dict[str, int] = dict(links or {})
        self.all_router_ids = tuple(all_router_ids)
        # 链路状态库：origin -> LSA（每个源只保留最高序号）。
        self.lsdb: dict[str, LSA] = {}
        self.routes: dict[str, Route] = {}
        # 自己产生通告的起始序号为 1。
        self._next_seq = 1

    # ------------------------------------------------------------------
    # 通告的产生

    def originate(self, links: dict[str, int] | None = None) -> LSA:
        """产生一条新的（序号更大的）通告并装入自己的库。"""
        if links is not None:
            self.local_links = dict(links)
        lsa = LSA.create(self.id, self._next_seq, self.local_links)
        self._next_seq += 1
        self._install(lsa)
        return lsa

    def update_local_links(self, links: dict[str, int]) -> list[tuple[str, LSA]]:
        """网络层把最新的本地直连视图交给路由器；变化时才重新通告。

        返回需要发给各在用邻居的 (邻居, LSA)。无变化时返回空列表，
        避免链路涨价被错误地用相同序号重复发送。
        """
        if self.lsdb.get(self.id) is not None and self.local_links == links:
            return []
        self.local_links = dict(links)
        lsa = self.originate()
        return [(nb, lsa) for nb in self.local_links]

    # ------------------------------------------------------------------
    # 接收与泛洪

    def receive(
        self, lsa: LSA, from_neighbor: str
    ) -> tuple[str, list[tuple[str, LSA]]]:
        """处理一条来自 ``from_neighbor`` 的通告。

        返回 (结果, 需转发的(邻居, LSA)列表)。水平分割：不转回来源。
        """
        known = self.lsdb.get(lsa.origin)
        if known is not None and lsa.seq < known.seq:
            return ACCEPT_STALE, []
        if known is not None and lsa.seq == known.seq:
            return ACCEPT_DUPLICATE, []

        # 只有更高序号（或全新源）才安装。
        self._install(lsa)
        forwards = [
            (nb, lsa)
            for nb in self.local_links
            if nb != from_neighbor  # 水平分割
        ]
        return ACCEPT_INSTALLED, forwards

    def _install(self, lsa: LSA) -> None:
        self.lsdb[lsa.origin] = lsa
        self._recompute()

    def periodic_reflood(self) -> list[tuple[str, LSA]]:
        """把当前已知的全部状态补发给所有在用邻居（可靠泛洪的基础）。"""
        out: list[tuple[str, LSA]] = []
        for nb in self.local_links:
            for lsa in self.lsdb.values():
                out.append((nb, lsa))
        return out

    # ------------------------------------------------------------------
    # 只用 LSDB 自己算路由 —— 不读取任何全局真值

    def _build_graph(self) -> dict[str, dict[str, int]]:
        """根据库中通告构造无向图。

        一条无向边仅当两端的当前通告**都列出对方且费用一致**时才成立。
        分区期间会有一端已删除边、另一端尚未得知，此时该边不出现，
        因此分区两侧自然算出"不可达"；恢复并收敛后两端一致，边恢复。
        """
        graph: dict[str, dict[str, int]] = {rid: {} for rid in self.all_router_ids}
        graph[self.id]  # 确保自己存在
        links_of: dict[str, dict[str, int]] = {}
        for origin, lsa in self.lsdb.items():
            graph.setdefault(origin, {})
            links_of[origin] = dict(lsa.links)

        for u, nu in links_of.items():
            for v, cost in nu.items():
                gu = graph.setdefault(u, {})
                gv = graph.setdefault(v, {})
                # 必须互相确认且费用一致
                if links_of.get(v, {}).get(u) == cost:
                    # 去重（u-v 与 v-u 各处理一次，写入相同结果）
                    gu[v] = cost
                    gv[u] = cost
        return graph

    def _recompute(self) -> None:
        graph = self._build_graph()
        # Dijkstra：费用为第一关键字；费用并列时，按完整路由器 ID
        # 路径的字典序选择最优路径。
        best: dict[str, tuple[int, tuple[str, ...]]] = {
            self.id: (0, (self.id,))
        }
        visited: set[str] = set()
        while True:
            candidates = [(label, node) for node, label in best.items()
                          if node not in visited]
            if not candidates:
                break
            (cost, path), u = min(candidates)
            visited.add(u)
            for v, edge_cost in graph.get(u, {}).items():
                candidate = (cost + edge_cost, path + (v,))
                if v not in best or candidate < best[v]:
                    best[v] = candidate

        self.routes = {}
        for dest in self.all_router_ids:
            if dest == self.id:
                self.routes[dest] = Route(
                    dest=dest, next_hop=None, cost=0,
                    path=(self.id,), reachable=True)
            elif dest in best:
                c, path = best[dest]
                self.routes[dest] = Route(
                    dest=dest, next_hop=path[1], cost=c,
                    path=path, reachable=True)
            else:
                self.routes[dest] = Route(
                    dest=dest, next_hop=None, cost=-1,
                    path=(), reachable=False)

    # ------------------------------------------------------------------
    # 给输出/测试用的快照

    def lsdb_snapshot(self) -> dict[str, dict]:
        return {
            origin: {"seq": lsa.seq, "links": list(lsa.links)}
            for origin, lsa in sorted(self.lsdb.items())
        }

    def routes_snapshot(self) -> dict[str, dict]:
        return {
            dest: {
                "next_hop": r.next_hop,
                "cost": r.cost if r.reachable else None,
                "path": list(r.path),
                "reachable": r.reachable,
            }
            for dest, r in sorted(self.routes.items())
        }

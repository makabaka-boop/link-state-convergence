"""独立参考实现：不与路由器代码共享任何路径计算逻辑。

直接读取全局真值（边表），用朴素 Dijkstra 计算各源点的最短路；
并列费用按完整路由器 ID 路径的字典序裁决。供演示输出"期望值"，
测试文件则另有一份纯暴力枚举实现。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

Graph = Dict[str, Dict[str, int]]


def build_graph(edges: Dict[Tuple[str, str], int],
                down: Optional[set] = None) -> Graph:
    down = down or set()
    g: Graph = {}
    for (a, b), cost in edges.items():
        if frozenset((a, b)) in down:
            continue
        g.setdefault(a, {})[b] = cost
        g.setdefault(b, {})[a] = cost
    return g


def all_shortest_paths(g: Graph) -> Dict[str, Dict[str, dict]]:
    result: Dict[str, Dict[str, dict]] = {}
    nodes = sorted(g)
    for src in nodes:
        best: Dict[str, tuple] = {src: (0, (src,))}
        done: set = set()
        while True:
            todo = [(label, n) for n, label in best.items() if n not in done]
            if not todo:
                break
            (cost, path), u = min(todo)
            done.add(u)
            for v, w in sorted(g.get(u, {}).items()):
                cand = (cost + w, path + (v,))
                if v not in best or cand < best[v]:
                    best[v] = cand
        table = {}
        for dest in nodes:
            if dest == src:
                table[dest] = {"reachable": True, "cost": 0,
                               "path": [src], "next_hop": None}
            elif dest in best:
                c, p = best[dest]
                table[dest] = {"reachable": True, "cost": c,
                               "path": list(p), "next_hop": p[1]}
            else:
                table[dest] = {"reachable": False, "cost": None,
                               "path": [], "next_hop": None}
        result[src] = table
    return result


def expected_tables(edges: Dict[Tuple[str, str], int],
                    down: Optional[set] = None
                    ) -> Dict[str, Dict[str, dict]]:
    return all_shortest_paths(build_graph(edges, down))

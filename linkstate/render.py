"""逐 tick 输出：消息、各节点状态库与路由表的文本渲染。"""

from __future__ import annotations

from .network import TickRecord


def _fmt_links(links) -> str:
    return ",".join(f"{n}:{c}" for n, c in links) or "-"


def render_tick_lines(rec: TickRecord, verbose: bool = False) -> list[str]:
    """渲染一个 tick。verbose=False 时只显示变化，保持逐 tick 紧凑。"""
    lines = [f"── tick {rec.tick} ────────────────────────────────"]

    for ev in rec.events:
        if ev.kind == "link":
            lines.append(f"  [事件] 链路 {ev.a}-{ev.b} : {ev.detail}")
        elif ev.kind == "cost":
            lines.append(
                f"  [事件] 链路 {ev.a}-{ev.b} 费用变为 {ev.value}")
        elif ev.kind == "transport":
            lines.append(f"  [事件] 传输层切换为 {ev.detail}")

    dropped = [s for s in rec.sends if s.status == "dropped"]
    if verbose:
        for s in rec.sends:
            mark = "丢弃!" if s.status == "dropped" else "发送 "
            lines.append(
                f"  [消息] {mark} {s.from_id} -> {s.to_id}  "
                f"LSA({s.origin}#{s.seq})")
    elif dropped:
        for s in dropped:
            lines.append(
                f"  [消息] 丢弃! {s.from_id} -> {s.to_id}  "
                f"LSA({s.origin}#{s.seq})")

    notable = rec.events or any(d.status == "stale" for d in rec.deliveries)
    if verbose or notable:
        for d in rec.deliveries:
            if not verbose and not rec.events and d.status != "stale":
                continue
            tag = {
                "installed": "新状态",
                "duplicate": "重复",
                "stale": "过期-拒绝",
                "link-gone": "链路已断-丢弃",
            }[d.status]
            dup = " (副本)" if d.duplicated else ""
            lines.append(
                f"  [投递] {d.from_id} -> {d.to_id}  "
                f"LSA({d.origin}#{d.seq}) => {tag}{dup}")

    if verbose or notable:
        lines.append(render_state(rec, indent="  "))
    return lines


def render_state(rec: TickRecord, indent: str = "") -> str:
    """渲染本 tick 所有节点的 LSDB 与路由表。"""
    blocks: list[str] = []
    for rid in sorted(rec.lsdb):
        lsdb = rec.lsdb[rid]
        routes = rec.routes[rid]
        lsa_parts = []
        for origin, info in sorted(lsdb.items()):
            lsa_parts.append(
                f"{origin}#{info['seq']}[{_fmt_links(info['links'])}]")
        route_parts = []
        for dest, r in sorted(routes.items()):
            if dest == rid:
                continue
            if r["reachable"]:
                route_parts.append(
                    f"→{dest}: nh={r['next_hop']} cost={r['cost']} "
                    f"path={'-'.join(r['path'])}")
            else:
                route_parts.append(f"→{dest}: 不可达")
        blocks.append(
            f"{indent}路由器 {rid}\n"
            f"{indent}  LSDB: {' '.join(lsa_parts) or '(空)'}\n"
            f"{indent}  路由: {'; '.join(route_parts) or '(仅本机)'}")
    return "\n".join(blocks)


def render_history(records: list[TickRecord], verbose: bool = False) -> str:
    return "\n".join(
        "\n".join(render_tick_lines(r, verbose=verbose)) for r in records
    )

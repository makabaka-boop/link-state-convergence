"""链路状态通告（Link State Advertisement）。

每条通告由某个路由器产生，带有源 ID 与单调递增的序号；
``links`` 是该路由器此刻全部在用的直连链路（邻居, 费用）。

链路断开 = 从 links 中删除该邻居；链路涨价 = 以更大序号重新通告。
"""

from __future__ import annotations

from dataclasses import dataclass, field

Link = tuple[str, int]  # (邻居 ID, 费用)


@dataclass(frozen=True, order=True)
class LSA:
    origin: str
    seq: int
    links: tuple[Link, ...] = field(default_factory=tuple)

    @staticmethod
    def create(origin: str, seq: int, links: dict[str, int]) -> "LSA":
        """从字典构造；按邻居 ID 排序，保证表示唯一。"""
        return LSA(origin, seq, tuple(sorted(links.items())))

    def describes_same_links(self, other: "LSA") -> bool:
        """两份通告描述的链路集合是否完全一致（用于检测重传等）。"""
        return self.links == other.links

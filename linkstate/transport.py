"""传输层：对通告做延迟、复制、乱序和丢弃。

传输层不理解序号，只是不可靠信道：
* ``drop_p``            独立丢弃概率
* ``dup_p``             复制概率（额外再投一份，两份各自独立延迟）
* ``delay_min/max``     延迟 tick 区间，随机大延迟天然制造乱序
* ``reorder_bias``      若给出，则后发的消息有一定概率取到更小延迟，
                        显式制造乱序
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from .lsa import LSA


@dataclass(frozen=True)
class Delivery:
    deliver_tick: int
    from_id: str
    to_id: str
    lsa: LSA
    duplicated: bool


class Transport:
    def __init__(
        self,
        drop_p: float = 0.0,
        dup_p: float = 0.0,
        delay_min: int = 1,
        delay_max: int = 1,
        reorder_bias: float = 0.0,
        seed: int | None = None,
    ) -> None:
        assert delay_min >= 1, "延迟至少 1 个 tick，避免同 tick 无限转发"
        assert 0.0 <= drop_p < 1.0
        self.drop_p = drop_p
        self.dup_p = dup_p
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.reorder_bias = reorder_bias
        self.rng = random.Random(seed)
        self.dropped_count = 0
        self.duplicated_count = 0
        self.sent_count = 0
        # 可靠模式：不丢、不复制、1 tick 必达（用于"消息停止丢失"）。
        self.reliable = False

    def set_reliable(self, reliable: bool) -> None:
        self.reliable = bool(reliable)

    def plan(
        self, tick: int, from_id: str, to_id: str, lsa: LSA
    ) -> list[Delivery]:
        """决定一条通告在未来各 tick 的投递；空列表表示被丢弃。

        本方法不修改模拟器事件队列，只产出投递计划，由模拟器安排。
        """
        self.sent_count += 1
        if self.reliable:
            return [Delivery(tick + 1, from_id, to_id, lsa, False)]
        if self.rng.random() < self.drop_p:
            self.dropped_count += 1
            return []
        deliveries = [Delivery(tick + self._delay(), from_id, to_id, lsa, False)]
        if self.rng.random() < self.dup_p:
            self.duplicated_count += 1
            deliveries.append(
                Delivery(tick + self._delay(), from_id, to_id, lsa, True)
            )
        return deliveries

    def _delay(self) -> int:
        d = self.rng.randint(self.delay_min, self.delay_max)
        if self.reorder_bias and self.rng.random() < self.reorder_bias:
            d = self.delay_min
        return d


class RuleTransport(Transport):
    """可按 (from,to,seq) 编排的传输层，用于精确测试过期/乱序场景。

    rules: key=(from,to,seq) -> dict
        hold_until=tick：匹配的（且非复制）首份通告延迟到指定 tick 才投递，
                         从而保证它在更新的通告之后到达，必然成为过期通告。
        after_tick=tick：规则仅对该 tick 及之后产生的发送生效，
                         使初始泛洪（同序号）不被拦截。
        before_tick=tick：规则仅对该 tick 之前产生的发送生效，
                          使投递时刻之后的周期补发不再被拦截。
    """

    def __init__(self, rules: dict | None = None, **kwargs) -> None:
        kwargs.setdefault("delay_min", 1)
        kwargs.setdefault("delay_max", 1)
        super().__init__(**kwargs)
        self.rules = rules or {}

    def plan(
        self, tick: int, from_id: str, to_id: str, lsa: LSA
    ) -> list[Delivery]:
        key = (from_id, to_id, lsa.seq)
        rule = self.rules.get(key)
        # 编排规则优先于可靠模式：即使在"消息停止丢失"之后，
        # 被显式滞留的旧通告仍按计划迟到（用于演示/测试过期拒绝）。
        if (
            rule is not None
            and tick >= rule.get("after_tick", 0)
            and tick < rule.get("before_tick", 10**12)
        ):
            if "hold_until" in rule:
                assert rule["hold_until"] > tick, (
                    "hold_until 必须晚于当前 tick"
                )
                self.sent_count += 1
                return [Delivery(rule["hold_until"], from_id, to_id, lsa, False)]
        return super().plan(tick, from_id, to_id, lsa)

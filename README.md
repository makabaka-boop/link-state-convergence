# 链路状态路由模拟

一个逐 tick 驱动的最小链路状态（link-state）路由模拟网络：2～8 个路由器，
每个路由器**只依据自己的链路状态库（LSDB）独立计算下一跳**，任何节点都
读不到全局真值。链路状态以带来源与递增序号的通告（LSA）在不可靠传输层上
传播；分区期间明确显示不可达，恢复连通且消息停止丢失后必然收敛。

仅依赖 Python 3 标准库，无需安装任何包。

## 核心规则

1. **带来源与递增序号的通告**：链路变化（断开/涨价/恢复）由两端各产生一条
   新序号的 LSA；序号从 1 起单调递增。
2. **旧序号不得覆盖新状态**：库中只保留每个来源的最高序号 LSA。收到
   `seq` 更小的通告一律以 `stale`（过期）拒绝；相同序号为 `duplicate`；
   更高序号才 `installed` 并向除来源外的邻居泛洪（水平分割）。
3. **不可靠传输层**：可独立配置丢弃概率、复制概率、延迟区间与乱序倾向
   （`Transport`），也可用 `RuleTransport` 精确编排某条通告"迟到"。
4. **周期性补发**：每 `reflood_period` 个 tick，每个路由器把当前已知的
   **全部**状态补发给所有在用邻居。这是分区恢复后最终一致的保证——
   哪怕新通告曾被丢弃，补发最终会把最高序号状态送到对端，而旧序号永远
   无法回滚新状态。
5. **只认自己的库**：路由器构图时，一条无向边仅当**两端的当前 LSA
   互相列出对方且费用一致**时才成立。分区时一端先删除边，该边立即从
   各节点自算视图中消失，跨区目的明确标记为 **不可达**。
6. **路径裁决**：Dijkstra 以总费用为第一关键字；费用并列时，按
   **完整路由器 ID 路径的字典序**裁决（逐跳扩展路径并比较路径元组，
   费用为正时该松弛规则可得到全局最优并列路径）。
7. **收敛判定**：路由表与独立参考真值一致且连续若干 tick 保持稳定。
   消息停止丢失（切换到可靠模式）后，在有界 tick 内完成收敛。

## 输出内容

每个 tick 记录一条 `TickRecord`：

* `events`：拓扑事件（链路 up/down、费用变化、传输层可靠/有损切换）；
* `sends`：每条通告的发送（含被传输层丢弃的发送）；
* `deliveries`：每条到期通告的处理结果
  （`installed` 新状态 / `duplicate` 重复 / `stale` 过期拒绝 /
  `link-gone` 链路已断）；
* `lsdb`：本 tick 末每个节点的状态库（来源、序号、链路集合）；
* `routes`：本 tick 末每个节点自算的路由表
  （目的、下一跳、费用、完整路径、是否可达）。

`linkstate/render.py` 提供文本渲染：紧凑模式只打印变化、丢弃与过期拒绝，
`--all` 打印每个 tick 的每条消息与全部节点快照。

## 运行

```bash
python3 demo.py          # 两个场景的紧凑输出
python3 demo.py --all    # 逐 tick 全量消息与各节点 LSDB/路由表
python3 -m unittest tests.test_linkstate -v   # 全部测试
```

### 演示场景

* **场景 1 链路涨价**：菱形拓扑，A→D 两条费用并列路径（字典序走 B）；
  传输层延迟/复制/乱序/丢弃，tick 12 把 B–D 涨价 4→8，随后消息停止丢失，
  全网改走 A–C–D。
* **场景 2 分区与重连**：A–B–C 线性拓扑；tick 10 断开 A–B，跨区目的
  明确显示不可达；tick 22 恢复并切到可靠传输；分区开始时被滞留的旧
  A#1 在 tick 28 才到达 B，此时 B 已持有 A#3，旧通告被拒绝，随后收敛。

## 测试覆盖（14 个用例）

| 用例 | 覆盖点 |
|---|---|
| `test_two_routers` / `test_three_node_line` | 2、3 节点小图最终收敛 |
| `test_diamond_with_tie` | 并列费用按完整路径字典序裁决 |
| `test_eight_node_graph_under_chaotic_transport` | 8 节点 + 延迟/复制/乱序/丢弃，副本收敛后只能是重复 |
| `test_old_sequence_never_overwrites` | 单元：旧序号 LSA 绝不覆盖新状态 |
| `test_same_sequence_is_duplicate_not_forwarded` | 单元：相同序号判重且不转发 |
| `test_router_only_uses_its_lsdb` | 单元：单向/费用不一致视图不成立边 |
| `test_old_advertisement_arrives_after_new_one` | 编排乱序：旧通告迟到 → 过期拒绝，路由不被污染 |
| `test_link_price_increase_reroutes` | 链路涨价后改走新最短路径（有损阶段 + 停止丢失后收敛） |
| `test_decrease_then_increase_sequence_monotonic` | 降价再涨价，序号单调、路由正确 |
| `test_partition_unreachable_then_reconverge` | 分区期间跨区全部明确不可达；重连后滞留旧通告被拒绝；按真值收敛 |
| `test_two_node_partition` | 最小分区/重连 |
| `test_convergence_even_with_heavy_loss_only_via_reflood` | 60% 丢包下仅靠周期补发收敛 |

收敛核对使用 `tests/test_linkstate.py` 内一份**独立的暴力枚举实现**
（枚举所有简单路径，再按 (费用, 完整路径字典序) 取最优），与路由器内的
Dijkstra 不共享任何路径计算代码，适用于小图的独立交叉验证。

## 代码结构

```
linkstate/
  lsa.py        通告数据结构：来源、序号、链路集合
  router.py     路由器：LSA 收发、序号规则、泛洪、周期补发、Dijkstra
  transport.py  不可靠传输层（丢弃/复制/延迟/乱序）与可编排传输层
  network.py    模拟器：事件堆、逐 tick 驱动、拓扑变化、记录与收敛判定
  reference.py  独立参考 Dijkstra（读全局真值，仅用于展示期望值）
  render.py     逐 tick 消息、LSDB、路由表的文本渲染
tests/
  test_linkstate.py  unittest 用例 + 独立暴力最短路核对
demo.py         两个端到端演示场景
```

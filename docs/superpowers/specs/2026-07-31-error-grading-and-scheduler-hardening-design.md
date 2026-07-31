# C-4 错误分级 + 调度硬化设计（+ C-3 幂等审计收口）

- **日期**：2026-07-31
- **分支**：master
- **状态**：✅ spec review 通过（2026-07-31）—— 三要点已决议，下一步落 plan
- **review 决议（2026-07-31）**：
  - 要点 1（L1/L2 边界）：✅ 接受 —— 单只=L2 继续 / 整批·DB·网关=L1 停；补强：单只层面 DB 写异常升 L1，业务拒绝才 L2
  - 要点 2（停调度方式）：✅ 选 `sched.shutdown(wait=False)` —— 弃 `sched.pause()`（致命场景不留可被误恢复的口子）
  - 要点 3（C-3 cancel）：✅ 接受 —— 审计优先；补强：以对账幽灵单风险为判据决策树
- **关联**：
  - `2026-07-29-trading-state-store-redesign.md`（C-1 state_store——幂等 UNIQUE 主体已做，C-3 仅审计）
  - `2026-07-30-scheduling-orchestration-design.md`（C-2——`_alert_critical` + `sched.shutdown` 先例）
  - `2026-07-28-strategy-unify-backtest-live-design.md`（D12 decide_exit fallback——盘中不裸奔的分级先例）
- **范围**：错误处理三级分级（L1 致命硬抛+CRITICAL+停调度 / L2 警告 CRITICAL / L3 软降级）+ APScheduler `job_defaults` 硬化（`max_instances=1`/`misfire_grace_time=300`/`coalesce=True`）+ 停调度机制（`_halted` + wrapper）。**C-3 幂等仅审计收口**（C-1 已做主体，用户确认重点 C-4）。

---

## 1. 背景与现状

### 1.1 宗旨（用户原话）

> 错误处理分级：交易关键路径失败 → **硬抛 + 钉钉 CRITICAL + 停调度**（不软降级）；APScheduler 配 `max_instances=1` + `misfire_grace_time=300` + `coalesce=True`。

### 1.2 现状（master HEAD `3d959362`）

| 项 | 现状 | 证据 |
|---|---|---|
| APScheduler 配置 | **裸构造**，零 `job_defaults` | `engine.py:1685` `AsyncIOScheduler()`；五 job（pipeline_then_eod 18:00 / pre_open 9:22 / stop_loss 30s / post_close 15:30 / _health_guard 60s） |
| 错误处理 | **30+ 处软降级**（`except: logger + continue/return {}`）遍布四触发点 | grep `except Exception` engine.py 命中 30+ |
| CRITICAL 告警 | `_alert_critical` 已有，**零散用 4 处** | `engine.py:94` 定义；用在 pre_open submitted=0（:829）/ gate 失败（:638）/ 网关连续失败（:1981）/ 口径自检（:2787） |
| 停调度先例 | `sched.shutdown(wait=False)` **1 处** | `engine.py:2795` 口径自检失败 |
| 幂等 UNIQUE | **C-1 已做主体** | order `UNIQUE(account_id,trade_date,symbol,purpose)` + trade_event `UNIQUE(account_id,trade_id,action)` + fill `UNIQUE(order_id,traded_time)` + `has_order` 死态过滤（I-2，3d959362） |

**核心病灶**：交易关键路径（挂单/止损/DB 写）失败时，当前 `except + logger + continue` 软降级——engine 继续跑，状态可能已不一致（DB 写失败但继续挂单/止损），live 下 = 真金损失 + 静默漂移。配套 scheduler 零硬化，机器休眠/慢触发会 job 堆积重叠。

---

## 2. 目标与非目标

### 目标
1. **错误三级分级标准** + 四触发点路径映射（L1/L2/L3 清单）
2. **APScheduler `job_defaults`**：`max_instances=1` + `misfire_grace_time=300` + `coalesce=True`
3. **停调度机制**：`_halted` flag + `_critical_guard` wrapper + `sched.shutdown`
4. **`_alert_critical` 铺设**：所有 L1/L2 路径接 CRITICAL 钉钉
5. **C-3 幂等审计**：cancel 幂等补漏 + 统一 `(date,symbol,side,operation_type)` 约定文档化（**不重建 C-1 UNIQUE**）

### 非目标（显式 out of scope）
- **不重建 C-1 既有 UNIQUE**（order/trade_event/fill 已闭环，3d959362 验证）
- **不改策略/业务逻辑**（只改错误处理形态 + scheduler 配置）
- **side 显式进 UNIQUE**（当前 purpose 隐含方向，冗余，follow-up）
- **live 上线**（本 spec 是 live 前置，不含 `AUTO_TRADE_MODE=live`）

---

## 3. 错误三级分级标准

### L1 致命（硬抛 `_CriticalHalt` + CRITICAL + 停调度）
**定义**：交易关键路径失败，继续跑致真金损失或状态真相源失真。

| 路径 | 触发条件 | 当前处理 | 目标 |
|---|---|---|---|
| state_store 关键写入 | `insert_order`/`update_order_state`/`insert_trade_event`/`insert_fill` 抛异常 | 软降级（`engine.py:573/677/744/803` 等） | L1：DB 真相源失真，停 |
| pre_open `load_plan`/读 DB 失败 | plan 读不到/DB 查询异常 | 部分 soft | L1：无计划基准，停 |
| stop_loss monitor 查持仓/DB 失败 | `gw._fetch_broker_positions`/state_store 查异常 | 软降级返 `{checked:0}` | L1：敞口未明，停 |
| pipeline_then_eod 采集子进程失败 | `proc.wait()` 非 0 / eod 落 DB 失败 | 待查 | L1：T+1 计划失真，停 |
| 网关断线 + 重连耗尽（live） | `_health_guard` 连续失败超阈值 | 已 CRITICAL（:1981） | 不升 L1（见下方说明） |
| 口径自检失败 | （已有） | 已 shutdown（:2795） | L1（保留） |

> **`_health_guard` 特殊路径（不升 L1 · review 决议 2026-07-31）**：`_health_guard` 是「统一网关自愈入口」（M1），停调度=丧失自愈能力，与职责冲突。网关断线的致命场景由 pre_open/stop_loss 交易时点的 L1 兜底（查持仓/DB 失败已升 L1）；盘后/盘前断线时 `_health_guard` 持续 CRITICAL 告警 + 自愈尝试（客户端重启后能恢复）优于硬停等人工。故 `_health_guard` 保持 `@_critical_guard` 装饰（`_halted` 检查仍防「其他 job 已 halt 时本 job 跳过」），但函数体内不 `raise _CriticalHalt`。

### L2 警告（CRITICAL + **不**停调度）
**定义**：单只标的/单次操作失败，局部影响，整批可继续。

| 路径 | 当前 | 目标 |
|---|---|---|
| 单只挂单被拒（pre_open `_submit` RuntimeError） | warn + REJECTED 回填（I-2） | L2：补 CRITICAL（单只研究员要知情） |
| 单只止损发卖失败（stop_loss `_submit`） | warn continue | L2：补 CRITICAL（漏止损真金） |
| 单只止盈挂失败 | warn | L2 |

> **边界澄清（review 补强 · 业务拒绝 vs 基础设施失败）**：同样是"单只挂单失败"，成因不同分级不同——
> QMT 业务拒单（涨停价不接/资金不足/限频拒绝等）= **L2**（局部，整批继续）；
> 单只挂单**过程中 state_store 写异常**（`insert_order`/`insert_trade_event`/`insert_fill` 抛错）= **L1**（DB 真相源失真，优先于"单只"语义，硬抛停调度）。
> 判定线：**基础设施（DB/网关）失败 > 单只计数**——单只层面只要触及 DB 写异常即升 L1，**不**按 L2 continue（否则 DB 写失败但 engine 继续挂下一只 = 静默漂移，正是核心病灶）。

### L3 软降级（logger + 继续，**不改**）
**定义**：非交易路径，失败不影响正确性。
- `_alert_critical` 发送失败自身（`:119`）
- 持仓盈亏播报失败（eod `:583` query_asset / `_broadcast_positions_pnl`）
- docstring/配置读取异常
- 完整性 gate fail-open（`:367`）

---

## 4. APScheduler 配置

```python
# engine.py:1685 改造
self.sched = AsyncIOScheduler(job_defaults={
    "max_instances": 1,          # 每 job 同时只一个实例（防 pre_open 跑超时与下次重叠双挂）
    "misfire_grace_time": 300,   # 错过触发 5min 内仍跑，超 5min 放弃（防机器休眠堆积补跑）
    "coalesce": True,            # 堆积多次触发合并一次（与 misfire 配合，stop_loss 30s 堆积只补 1 次）
})
```

**Why 三参数**：
- `max_instances=1`：pre_open 挂单慢（QMT 限频）跑超 9:22，下次触发被挡，防重叠双挂。stop_loss 30s 跑超 30s 同理防重叠。
- `misfire_grace_time=300`：机器休眠/重启错过触发——5min 内补跑（保盘后 job 不轻易漏），超 5min 放弃（stop_loss 30s 间隔堆积 10 次只补最近 1 次，防补跑风暴）。
- `coalesce=True`：与 misfire 配合，堆积合并成一次（不补跑多次）。

---

## 5. 停调度机制（`_halted` + wrapper）

```python
class _CriticalHalt(Exception):
    """L1 致命异常：关键路径失败，wrapper 捕获后停调度。"""

def _critical_guard(coro_method):
    """L1 路径 wrapper：_halted 检查 + 捕获 _CriticalHalt → CRITICAL + 停调度。"""
    @functools.wraps(coro_method)
    async def wrapped(self, *a, **kw):
        if self._halted:
            logger.warning("引擎已停调度（_halted），跳过 %s", coro_method.__name__)
            return
        try:
            return await coro_method(self, *a, **kw)
        except _CriticalHalt as e:
            self._halted = True
            _alert_critical(f"致命停调度 [{coro_method.__name__}] {e}")
            try:
                self.sched.shutdown(wait=False)   # 停所有 job（先例 :2795）
            except Exception:
                logger.exception("sched.shutdown 失败（_halted 已置，job 顶检查兜底）")
            raise
    return wrapped
```

**应用**：五 job method（`_pre_open`/`_stoploss`/`_post_close`/`_health_guard`/`pipeline_then_eod` 经 engine）装饰 `@_critical_guard`。L1 路径内 `raise _CriticalHalt(msg)`。

**双层保障**：`sched.shutdown` 停新触发 + `_halted` flag 防 in-flight job 继续写（shutdown 异常时 flag 兜底）。

**in-flight 语义澄清（review 补强）**：
- **当前 job**：L1 路径 `raise _CriticalHalt` → 异常向上传播，**当前 job 在 raise 处立即中断**后续写（不会把半截状态 continue 写完）；wrapper `except` 捕获后置 `_halted` + shutdown + 再 `raise`（APScheduler 顶层吞掉 job 异常记日志，不影响其他 job）。
- **其他 job / 下一轮**：`_halted` flag 在 wrapper 顶 `if self._halted: return` 兜底——`max_instances=1` 下，其他被触发的 job 或堆积补跑的 job 入口即跳过，不再写。
- 即：**`raise _CriticalHalt` 中断"当前轮"，`_halted` flag 防"下一轮 / 其他 job"**——两者合起来覆盖 in-flight 全部窗口，无"最后一轮污染"死角。

---

## 6. C-3 幂等审计（收口，非重建）

C-1 已做 order/trade_event/fill UNIQUE + has_order I-2 闭环。C-3 剩余：

1. **cancel 幂等审计**：`cancel_all_open_orders` 当前查柜台撤（`io/breaker.py:89`），重跑柜台对已撤单返 noop——风险低，但**无 DB 幂等键**。审计是否需 order 表加 `purpose='CANCEL'` 行（撤单幂等记录），或柜台 noop 已足够（结论记 spec）。
   - **审计判据（review 补强 · 决策树；结论仍按要点 3 留给审计阶段）**：以**对账幽灵单风险**为唯一判据——
     - **若撤单确认最终落 DB**（trade_event `action=CANCEL` 落库，或 order.status 更新为 CANCELLED）→ order 表已能反映"挂→撤"完整生命周期 → **免** `purpose='CANCEL'` 行，审计记录即可；
     - **若撤单不落任何 DB 记录**（柜台 noop + 无 trade_event）→ pre_open 重入场景下，T0 挂单被撤但 DB 仍记 SUBMITTED → T+1 对账幽灵单（实测见 QMT 模拟盘：撤单 `CANCELLED` 主推延迟 1-2s 须轮询，连带的真相源风险）→ **必须**补：order `purpose='CANCEL'` 行 **或** `insert_trade_event(action=CANCEL)`，二选一，审计阶段拍板。
2. **统一约定文档化**：`(date, symbol, side, operation_type)` 幂等键约定写进 `state_store.py` 顶部 docstring（显式声明，非重建）。
3. **覆盖度 grep 审计**：所有交易写入路径（insert_order/trade_event/fill）都过 UNIQUE——确认无遗漏。

**不做**：side 进 UNIQUE（冗余）、重建 UNIQUE、统一幂等函数（C-1 已闭环）。

---

## 7. 风险

| # | 风险 | 缓解 |
|---|---|---|
| R1 | `max_instances=1` + stop_loss 30s interval 跑超 30s → 实际间隔拉长 | 实测 stop_loss 单次耗时（get_quotes + decide_exit）；超 30s 上调 `ENGINE_STOPLOSS_INTERVAL_SECONDS=60` |
| R2 | `misfire_grace_time=300` 盘后 job 错过 5min 不补跑 | 盘后 job（18:00 pipeline / 15:30 post_close）错过=机器故障，补跑反而危险；接受 + CRITICAL 唤醒 |
| R3 | L1/L2 边界误判（L2 当 L1）→ 单只失败停整调度，过度保守 | spec §3 列具体映射；**单只 = L2（继续），整批/DB/网关 = L1（停）**；实现严守 + review |
| R4 | `_halted` 后 engine 常驻需人工重启 | CRITICAL 钉钉唤醒人工；live 下停调度优于带病跑（真金保护） |
| R5 | `sched.shutdown` 在 job 内调（self 关闭自己的 scheduler） | APScheduler 允许（:2795 先例）；`wait=False` 不等 pending |
| R6 | `_stop_already_placed` 升 L1 在 stop_loss 30s 高频下，SQLite 偶发 `database is locked` 触发整引擎停 | 已知 live 运维风险（非逻辑 bug；升 L1 物理意图正确：不知是否挂 STOP→可能重发双倍卖）；模拟盘观察 DB 抖动频率，高频误停则 follow-up 加瞬时错误退避（仅 `OperationalError` 跳过本轮，`IntegrityError`/corruption 仍 L1） |

---

## 8. 测试策略

- **单测**：
  - `test_critical_guard_halts`：raise `_CriticalHalt` → `_halted=True` + `sched.shutdown` mock 调用 + `_alert_critical` mock
  - `test_halted_skips_job`：`_halted=True` → job 跳过（不执行被包函数）
  - `test_scheduler_job_defaults`：构造 engine 断言 `sched.job_defaults` 含三参数
- **集成**：
  - `test_pre_open_db_write_failure_halts`：mock `state_store.insert_order` 抛异常 → pre_open 触发 `_CriticalHalt` → 停调度
  - `test_l2_single_reject_continues`：单只 `_submit` RuntimeError → CRITICAL 但 **不**停调度（整批继续）
- **回归**：全量 1125 passed 零退化

---

## 9. 验收标准

1. `AsyncIOScheduler(job_defaults={...})` 含 `max_instances=1`/`misfire_grace_time=300`/`coalesce=True`
2. 五 job method 装 `@_critical_guard`
3. L1 路径（§3 表）全改 `raise _CriticalHalt` + 经 wrapper 停调度
4. L2 路径（单只失败）调 `_alert_critical` 但**不**停调度
5. `_halted` flag + `sched.shutdown` 双层停调度生效（单测 + 集成）
6. L3 软降级保留不动
7. C-3 cancel 幂等审计有结论（补或确认柜台 noop 足够）+ `(date,symbol,side,op)` 约定入 docstring
8. 全量回归零退化

---

## 10. 实现步骤（高层 · 详细 diff 见 plan）

| 阶段 | 内容 | gate |
|---|---|---|
| **U1 scheduler 硬化** | `AsyncIOScheduler(job_defaults={三参数})` | scheduler 单测 |
| **U2 停调度机制** | `_CriticalHalt` + `_critical_guard` wrapper + `_halted` + shutdown；五 job 装饰 | wrapper 单测 |
| **U3 L1 路径改造** | §3 L1 路径（state_store 写入/load_plan/monitor 查持仓/pipeline 采集）改硬抛 `_CriticalHalt` | 集成测试（DB 失败停调度） |
| **U4 L2 CRITICAL 铺设** | 单只挂单/止损/止盈失败接 `_alert_critical`（不停调度） | L2 单测 |
| **U5 C-3 审计** | cancel 幂等结论 + `(date,symbol,side,op)` docstring + 覆盖度 grep | 审计文档 |

U3 最高风险（改四触发点错误处理，逻辑零改动只改异常形态），需逐路径 + e2e 回归。

---

## 11. spec review 要点

1. **L1/L2 边界**：✅ 已接受 —— §3"单只失败=L2 继续 / 整批·DB·网关=L1 停"作为判定线。补边界澄清：单只层面**业务拒绝=L2 / DB 写异常=L1**（基础设施 > 单只计数）。
2. **停调度方式**：✅ 选 `sched.shutdown(wait=False)` —— 硬停需人工重启，带病跑不如停（CRITICAL 唤醒）；弃 `sched.pause()`（可恢复=可被误恢复，致命场景不留口子）。补 in-flight 语义：`raise _CriticalHalt` 中断当前轮 + `_halted` 防"下一轮/其他 job"。
3. **C-3 cancel**：✅ 已接受 —— 审计优先，补 `purpose='CANCEL'` 视审计结论。补判据决策树：以**对账幽灵单风险**为判据（撤单落 DB 则免 / 不落则必补 order CANCEL 行或 trade_event action=CANCEL，二选一）。

✅ spec 已通过（2026-07-31）。下一步落 plan（`docs/superpowers/plans/2026-07-31-error-grading-and-scheduler-hardening.md`）。

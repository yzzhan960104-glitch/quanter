# C-6 时间统一上下文：trading 包单一时间源 clock.py

- **日期**：2026-08-01
- **分支**：feat/c6-unified-clock（spec 阶段）
- **状态**：待审（spec review gate）
- **关联**：
  - memory [[eod-date-offbyone-fix]]（eod/pre_open key 错位病灶 + next_trading_day 修复）
  - C-5 spec §2 非目标「不做 C-6 时间上下文统一（C-6 单独 brainstorm）」
  - C-4 [[c4-error-grading-scheduler-hardening-status]]（_critical_guard/_health_guard 决议不变）
- **范围**：trading 包内所有 `datetime.now()` 收口到 `trading/clock.py` 单一时间源（now/today/trading_day 三函数）+ 触发点入口缓存防同轮跨午夜漂移。

---

## 1. 背景与现状

### 1.1 痛点
- trading 包内 `datetime.now()` 散落 20+ 处（engine.py 18 处 + orchestrate/pipeline.py 1 处 + order_state.py 2 处），每处独立调用 → 时间源不统一。
- 同一轮触发点内多次 `datetime.now()` 可能跨午夜漂移（23:59:59 → 00:00:00），key 计算不一致。
- 历史 eod/pre_open key 错位病灶（[[eod-date-offbyone-fix]]）：eod 用 today 落盘、pre_open 读 today 永远差一天 → confirmed 计划存在但 pre_open 全部 reason=「无计划」。**已修 next_trading_day**，但根因「独立时间源、无单一口子」仍在，未来类似漂移无防线。
- 测试冻结时间需 patch 多处 datetime 模块（无单一口子）。

### 1.2 现状（master HEAD 0d672823，C-5 merged）
| 项 | 现状 | 证据 |
|---|---|---|
| 时间源 | 散落 `datetime.now()` 20+ 处 | engine.py:736/779/862/970/1300/1502/1546/1623/1644/1663/1701/2173/2228/2428/2478/2589/2684/2816 + orchestrate/pipeline.py:52 + order_state.py:59/189 |
| eod 落盘 key | `next_trading_day(today)`（已修） | engine.py:506-507,590 save_plan(date) + _eod:2191 + 启动口径自检 2156-2177 |
| pre_open 读 key | `today` | _pre_open 入口 today 传 pre_open(date) |
| key 对齐 | 已修（next_trading_day） | [[eod-date-offbyone-fix]] 34 passed |
| 时间源统一 | **无** | 每处独立 datetime.now()，无单一口子 |
| 测试冻结时间 | monkeypatch 模块 datetime | test_e2e_trading_flow.py:77,850（patch position_book/pipeline.datetime） |

**核心病灶**：时间源散落 → (1) 同轮跨午夜漂移无防线；(2) 测试冻结需 patch 多处 datetime 模块；(3) 未来 eod/pre_open 类似 key 漂移无统一口子拦截。

---

## 2. 目标与非目标

### 目标
1. **单一时间源**：`trading/clock.py` 提供 `now()`/`today()`/`trading_day()` 三函数，trading 包内所有 `datetime.now()` 收口到 clock。
2. **入口缓存防漂移**：触发点（_eod/_pre_open/_stoploss/_post_close）入口算一次 `_today`/`_td`，下游 key 操作用缓存值（防同轮跨午夜漂移）。
3. **测试冻结单一口子**：测试 monkeypatch `trading.clock.now`（单一口子），eod/pre_open key 对齐可复现。
4. **eod/pre_open 语义保留**：today()=pre_open 读口径，trading_day()=eod 落盘口径（next_trading_day(today)），命名区分读写避免混淆。

### 非目标（显式 out of scope）
- **不引入 Clock 类抽象**（模块级函数扁平，Karpathy 极简；Clock 类对 order_state/pipeline 等 instance-less 调用点过度）。
- **不凝固时间**（clock.now() 每次返当前 datetime.now()，进程级缓存会让长跑服务时间凝固不真实；防漂移靠入口缓存而非 clock 内部凝固）。
- **不改 next_trading_day 实现**（clock.trading_day() 复用 calendar.next_trading_day，calendar 对 Tushare token 依赖/fallback 不变）。
- **不动 _critical_guard/_health_guard/_gw_health_gate**（C-4/C-5 决议不变；clock 只替换时间源，不改 gate 语义）。
- **不收口 trading 包外的 datetime.now**（presentation/broadcast/discovery/broker 等不改——C-6 仅 trading 包）。
- **不改 start_all 收编**（C-6 与 start_all 收编是独立 project，分别 brainstorm；start_all 随后单独评估）。

---

## 3. 架构

### 3.1 trading/clock.py（新模块）
```python
# -*- coding: utf-8 -*-
"""C-6 单一时间源口子。trading 包内所有 datetime.now() 替换为本模块函数。

物理意图（[[eod-date-offbyone-fix]] 教训）：
    时间源散落 → 同轮跨午夜漂移 + 测试冻结难 + 未来 eod/pre_open 类似 key 漂移无防线。
    本模块提供单一口子：测试 monkeypatch trading.clock.now 即冻结全包时间。

三函数命名区分读/写口径（避免 eod/pre_open 混淆）：
    - today()       = 今日（pre_open 读 plan key 口径）
    - trading_day() = 次交易日（eod 落盘 plan key 口径 = next_trading_day(today)）
    - now()         = 当前 datetime（事件时间戳 submitted_at/order_id/written_at）
"""
from __future__ import annotations
from datetime import datetime
from trading.calendar import next_trading_day


def now() -> datetime:
    """当前 datetime（单一时间源口子，事件时间戳用）。"""
    return datetime.now()


def today() -> str:
    """今日 YYYY-MM-DD（pre_open 读 plan key 口径）。"""
    return now().strftime("%Y-%m-%d")


def trading_day() -> str:
    """次交易日（eod 落盘 plan key 口径 = next_trading_day(today)）。

    物理意图：eod（T 日盘后）落 plan_T+1，pre_open（T+1 开盘前）读 plan_T+1。
    today() 与 trading_day() 命名区分读/写口径——避免 eod/pre_open key 错位
    （[[eod-date-offbyone-fix]] 病灶：原 eod 用 today 落盘，pre_open 读 today 永远差一天）。
    """
    return next_trading_day(today())
```

### 3.2 替换规则（按用途分三类）
| 用途 | 替换为 | 代表命中点 |
|---|---|---|
| 业务日期 key（读 plan / is_trading_day / holding_days） | `clock.today()` | engine.py _pre_open/_stoploss/_post_close 入口 today；pipeline.py:52；engine.py:736/779/970/1502/1546/... |
| eod 落盘 key | `clock.trading_day()` | _eod 入口（替代 next_trading_day(today)）；engine.py:590 save_plan(date) 的 date 来源 |
| 事件时间戳（submitted_at / order_id / written_at） | `clock.now()` | engine.py:862/1300/2684；order_state.py:59/189 |

### 3.3 触发点入口缓存（防同轮跨午夜漂移）
- _eod/_pre_open/_stoploss/_post_close 入口各算一次：
  - _eod: `_td = clock.trading_day()` → 传 eod_plan(date=_td)（替代 next_trading_day(today)）
  - _pre_open/_stoploss/_post_close: `_today = clock.today()` → 下游 load_plan(_today)/is_trading_day(_today)/holding_days(..., _today)
- 下游 key 操作用缓存值，**不重复调 clock.today**（防同轮 23:59:59→00:00:00 漂移）。
- 事件时间戳（submitted_at 等）**不缓存**，直接 `clock.now()`（记录性，每次当前无 key 风险）。

### 3.4 不变量
- `today()` 与 `trading_day()` 命名区分（读 vs 写），禁止混用（eod 必用 trading_day，pre_open 必用 today）。
- clock 无状态（每次调 datetime.now），lifespan 不装配（不凝固时间）。
- clock.trading_day() 继承 calendar.next_trading_day 的 Tushare token 依赖（缺则 weekday fallback，既有行为）。

---

## 4. 测试策略

- **test_clock.py（新）**：三函数单测——now() 返 datetime；today() 格式 YYYY-MM-DD；trading_day() == calendar.next_trading_day(today())。
- **e2e clock freeze**：monkeypatch `trading.clock.now` 返固定 datetime → eod 落 plan_T+1 + pre_open 读 plan_T+1 key 对齐（回归 [[eod-date-offbyone-fix]]，单一口子冻结可复现）。
- **既有测试 patch 迁移（R2）**：
  - test_e2e_trading_flow.py:77 `monkeypatch.setattr(position_book, "datetime", _FrozenDT)` → 改 patch `trading.clock.now`（若 position_book 改用 clock）或保留（若 position_book 保留 datetime 非 key 用途）；plan 阶段确认 position_book 是否有业务日期 key 用途。
  - test_e2e_trading_flow.py:850 `monkeypatch.setattr(pipeline_mod, "datetime", _FrozenDT)` → pipeline.py:52 改 clock.today 后 patch `trading.clock.today`。
  - 量级：2 处 patch 锚点迁移（机械）。
- **全量回归**：C-5 后 1158 passed 基线零退化。

---

## 5. 风险

| # | 风险 | 缓解 |
|---|---|---|
| R1 | clock.py import calendar.next_trading_day → clock.trading_day() 继承 Tushare token 依赖（缺则 weekday fallback） | 既有行为（eod 已用 next_trading_day），clock 不改实现；fallback 仅识周末不识节假日是 calendar 既有降级 |
| R2 | 既有测试 patch datetime 锚点迁移（test_e2e_trading_flow.py:77,850） | 量级小（2 处），plan 阶段确认 position_book/pipeline 是否改 clock；改则 patch clock，不改则保留 patch datetime |
| R3 | order_state ORDER_id 唯一性（clock.now 到微秒） | clock.now() == datetime.now()，多次调用到微秒仍唯一，不冲突 |
| R4 | clock 无状态 → 同轮多次 clock.today() 仍漂移 | 入口缓存（§3.3）：触发点入口算一次 _today 传下游，下游不重复调 clock.today |
| R5 | today/trading_day 命名混淆（eod 误用 today 落盘） | 启动口径自检（engine.py:2156-2177 _verify_eod_calendar_alignment）已验 next_trading_day != today；C-6 后验 clock.trading_day() != clock.today()；命名区分 + 自检双保险 |

---

## 6. 验收标准

1. `trading/clock.py` 提供 now()/today()/trading_day() 三函数（today=读口径，trading_day=写口径=next_trading_day(today)，命名区分）。
2. trading 包内所有 `datetime.now()` 收口到 clock（grep `datetime\.now\(\)` 在 trading/ 仅命中 clock.py 内部）。
3. 触发点入口缓存（_eod/_pre_open/_stoploss/_post_close 入口算一次 _today/_td 传下游）。
4. eod 落盘 key = clock.trading_day()，pre_open 读 key = clock.today()（key 对齐回归通过，_verify_eod_calendar_alignment 自检绿）。
5. 既有测试 patch 迁移（test_e2e_trading_flow.py:77,850）+ 新 test_clock.py + e2e clock freeze 通过。
6. 全量回归 1158 passed 基线零退化。
7. _critical_guard/_health_guard/_gw_health_gate 语义不变（C-4/C-5 决议）。

---

## 7. 实现步骤（高层 · 详细 diff 见 plan）

| 阶段 | 内容 | gate |
|---|---|---|
| **V1 clock.py + 单测** | 新建 trading/clock.py（now/today/trading_day）+ test_clock.py | clock 单测 |
| **V2 engine.py 收口** | engine.py 18 处 datetime.now → clock（key→today/timestamp→now/eod→trading_day）+ 触发点入口缓存 | engine 全套测试 |
| **V3 orchestrate+order_state 收口** | pipeline.py:52 + order_state.py:59/189 → clock | 这些模块测试 |
| **V4 测试 patch 迁移 + 全量** | test_e2e_trading_flow.py:77,850 patch 迁移 + e2e clock freeze + 全量 1158/0 | smoke + 1158/0 |

---

## 8. spec review 要点

1. **clock.py 三函数 + 不封 Clock 类**（模块级扁平，Karpathy 极简）——接受？
2. **today/trading_day 命名区分读/写口径**（避免 eod/pre_open 混淆，[[eod-date-offbyone-fix]] 教训）——接受？
3. **入口缓存防同轮漂移**（clock 无状态不凝固，靠触发点入口算一次）——接受？
4. **仅 trading 包收口**（presentation/broadcast/discovery/broker 不改）——接受？

spec 通过后落 plan（`docs/superpowers/plans/2026-08-01-c6-unified-clock.md`）。

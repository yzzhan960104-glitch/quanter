# 后端分层重构设计 · 1/2/3 步（先正名 / 立防腐层 / 拆 caisen 上帝包）

- **日期**：2026-07-15
- **分支**：`refactor/backend-layering`（从 `master` 切出，与在途的 `feat/tushare-data-collection` 物理隔离）
- **推进策略**：渐进 strangler（新结构优先 + 旧入口 re-export 转发 + 每步测试绿可中断）
- **状态**：设计已获批，待转实现计划（writing-plans）

---

## 1. 背景与动机

当前后端按「数据层 / 模型层 / 接口层」三层心智模型组织。该划分壳层正确，但存在 **一个致命概念错误** 与 **三个结构性坍塌**，已实际拖慢三轮重构（工业级蜕变 / 宏观 CTA / 去玩具化 6 层）。本设计针对其中**可低风险先行**的前三步收敛动作出方案：

1. **先正名**：拆 `config.py`（819 行上帝配置）、解散 `core/`（杂物间）、声明 `data/` vs `data_lake/` 边界。
2. **立防腐层**：给 `caisen/` 建应用门面 `facade.py`，让 `server/services` 不再穿透到模型内部 6 个子模块。
3. **拆 caisen 上帝包**：把 5891 行的 `caisen/` 按职责分成 `engines / optimize / infra / advisor` 四子包。

> 执行编排层（`trading/` + `caisen/execution.py` + 双 risk 合并）与"训练/模拟/真实三种执行态"的隔离属**第 4 步**，本设计明确不含。

---

## 2. 现状诊断（证据）

| 症状 | 证据 |
|---|---|
| 上帝配置 | `config.py` 819 行，10 个顶层常量段挤在一个文件 |
| 上帝模型包 | `caisen/` 5891 行（24 文件），同时承担 模型+存储+执行+训练+可视化 五类职责 |
| 空壳目录 | `backtest/`、`factors/`、`strategies/` 三个目录为空 —— 规划的抽象层未落地，逻辑全挤进 `caisen/` |
| 杂物间 | `core/` 装了 `indicator.py`（因子，44 行）、`macro_regime.py`（宏观模型，194 行）、`notifier.py`（通知，297 行）三种不相干职责 |
| 零门面穿透 | `caisen/__init__.py` 仅一行 docstring、零导出；`server/services/caisen_service.py` 直接 `import` 了 `caisen.plan` / `caisen.backtest_replay` / `caisen.replay_runs` / `caisen.replay_tasks_db` / `caisen.storage` / `caisen.patterns.screener` / `caisen.risk` 共 6 个内部子模块 |
| 概念错误 | 原始三层把「训练/模拟/真实」当作数据层分类。实际三者差异在撮合与账本真实性（执行语义），行情数据应统一一份 |

---

## 3. 目标分层

```
┌──────────────────────────────────────────────────────┐
│ 接口层  server/api/v1 → services(应用服务)           │
│         ↑ 只依赖门面，不摸模型内脏                    │
├──────────────────────────────────────────────────────┤
│ 模型层  caisen/ (门面 facade.py)                     │
│   ├ engines/ (patterns+factors+plan+risk) 纯逻辑     │
│   ├ optimize/ (training_*) 参数优化                   │
│   ├ advisor/ (AI 决策,预留)                           │
│   └ infra/ (storage/execution/replay/viz 待迁→后续层) │
├──────────────────────────────────────────────────────┤
│ 数据层  data/(取数) + data_lake/(存储) + registry     │
│         config/ (按层拆分,非819行上帝文件)            │
└──────────────────────────────────────────────────────┘
   (执行编排层 = 第4步,本次1/2/3 不动)
```

### 3.1 最终完成态架构详图（1/2/3 步后）

> 下图为第 1/2/3 步全部完成后的后端全貌。执行编排层与双 risk 合并为**第 4 步**，本次仅以 `caisen/infra/` 标注待迁。

```
═══════════════════════════════════════════════════════════════
            最终预期架构 · 第 1/2/3 步全部完成后
═══════════════════════════════════════════════════════════════

┌─ 接口层 Presentation ─────────────────────────────────────┐
│  web/            CaisenScreen · ParamLab · Dashboard ·     │
│                  LiveCockpit · DataLake · Review  (6视图)  │
│  server/api/v1/  caisen · data · macro · review · trading  │
│                  · logs                      (HTTP 路由)   │
│  server/services 应用服务：编排用例 【只调门面】           │
└───────────────────────────┬─────────────────────────────────┘
                            │ 唯一依赖 CaisenFacade
                            ▼
┌─ 模型层 Strategy ── 门面包 caisen/ ───────────────────────┐
│  facade.py   ◄ CaisenFacade：10 用例，唯一对外契约        │
│  ───────────────────────────────────────────────────────  │
│  engines/    策略本体（纯逻辑·无 IO）                    │
│    ├ patterns/  w_bottom·head_shoulder·triangle_bottom·   │
│    │            neckline·zigzag_causal·screener·registry  │
│    ├ factors/   atr（+ 后续因子）                         │
│    └ plan.py · risk.py · config.py(StrategyConfig)        │
│  optimize/   参数优化（可异步·可重跑）                   │
│    └ training_analyzer · training_loops_db                │
│  advisor/    AI 决策（预留占位）                          │
│  infra/      待迁项（第 4 步移出 caisen）                │
│    └ storage·execution·backtest_replay·replay_*·viz_*     │
└───────────────────────────┬─────────────────────────────────┘
                            │ 读数据
                            ▼
┌─ 数据层 Data ─────────────────────────────────────────────┐
│  data/       取数：clients/*·fetcher·cleaner·resilience·  │
│               tushare_sync·lake_reader                    │
│  data_lake/  存储：parquet shards·jq_shards·macro·        │
│               cyq_perf·fundamentals                       │
│  config/     credentials·market·data·macro·viz·broker·    │
│               celery·registry（数据集唯一真相源）         │
└───────────────────────────────────────────────────────────┘

┌─ 横切 Cross-cutting ──────────────────────────────────────┐
│  infra/notifier.py（← core 解散迁出）· viz/（双套合并）   │
└───────────────────────────────────────────────────────────┘

┌ ─ ─ 执行编排层 Execution · 第 4 步（本次不做）─ ─ ─ ─ ─ ┐
   trading/   emt·qmt·mock gateway · order_state · risk_shield
   ◆ caisen/infra 的 execution·replay 待第 4 步迁入此层，
     并合并 caisen/risk 与 trading/risk_shield 双 risk
└ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┘
```

**依赖总则（最终态铁律）**：

```
  ① 接口层 ──► facade ──► engines      facade 为唯一对外契约
  ② optimize ──► engines               单向·优化器不污染本体
  ③ advisor  ──► engines               单向·AI 顾问不污染本体
  ④ infra    ──► engines               单向·执行/存储调本体
  ⑤ 数据层(config/data_lake) 被各层读，无反向依赖
  ✕ engines 绝不反向 import optimize / advisor / infra
```

**读图要点**：
- `facade.py` 是模型层**唯一对外契约** → 接口层零穿透（Step2 交付）。
- `engines/` 是策略本体 → `optimize / advisor / infra` **单向依赖**它，互不污染（Step3 交付）。
- `config/` 从 819 行单文件拆为 8 子文件包；`core/` 解散（`indicator→factors`、`notifier→infra`）（Step1 交付）。
- `caisen/infra/` 标注**待迁**：第 4 步迁入执行编排层，并合并 `caisen/risk` 与 `trading/risk_shield` 双 risk。

---

## 4. 总体策略：渐进 strangler

**三铁律**（贯穿三步）：
1. **新结构优先，旧入口 re-export 转发** —— 每步结束，所有现存 `import` 语句零改动可用。
2. **每步收尾 = 全量 `pytest` 绿 + 一个 commit + 一个可中断点**。任一步做完都可停手合并，系统仍完整可用。
3. **只动结构，不动逻辑** —— 前三步不重写算法、不改参数、不动风控阈值，纯文件移动 + 包边界 + re-export 垫片。

**演进时间线**：
```
master ──●── feat/tushare-data-collection   (在途,本次完全不动)
         │
         └── refactor/backend-layering      (新建)
              ├─ Step1 先正名    [commit] ✅绿 ─ 可停
              ├─ Step2 立facade  [commit] ✅绿 ─ 可停
              └─ Step3 拆caisen  [commit×2] ✅绿 ─ 可停
```

**全局红线**：三步完成时 `git diff master --stat` 应几乎只有文件 rename + 新增 `__init__.py` 垫片，**不应有实质算法 diff**。若出现大段逻辑改动即偏离原则，必须回退。

---

## 5. 第 1 步：先正名（零逻辑改动 · re-export 垫片）

### 5.1 `config.py`（819 行）→ `config/` 包

按 10 个顶层常量段、按**归属层**拆分：

| 新文件 | 装什么（原 `config.py` 行段） | 归属层 |
|---|---|---|
| `config/credentials.py` | `DATA_SOURCE_CREDENTIALS`（27–61） | 横切·密钥 |
| `config/market.py` | `MARKET_HOURS`（62–69） | 数据层 |
| `config/data.py` | `DATA_CONFIG`（70–76）`LAKE_CONFIG`（113–121）`MACRO_CLIENT_CONFIG`（122–129）`JQDATA_CONFIG`（139–231） | 数据层 |
| `config/macro.py` | `MACRO_CONFIG`（77–86） | 模型层·宏观 |
| `config/viz.py` | `VIZ_CONFIG`（87–94） | 横切·可视化 |
| `config/broker.py` | `MOCK_TRADING_CONFIG`（95–101） | 执行层 |
| `config/celery.py` | `CELERY_CONFIG`（130–135） | 执行编排 |
| `config/registry.py` | `DATASET_REGISTRY` `TUSHARE_DATASETS` `SYNCING_DIR`（234–819） | 数据层·元数据 |

**兼容垫片** `config/__init__.py`：
```python
# 兼容垫片：保持 `from config import DATASET_REGISTRY` 等全部旧用法零改动
from .credentials import DATA_SOURCE_CREDENTIALS
from .market import MARKET_HOURS
from .data import DATA_CONFIG, LAKE_CONFIG, MACRO_CLIENT_CONFIG, JQDATA_CONFIG
from .macro import MACRO_CONFIG
from .viz import VIZ_CONFIG
from .broker import MOCK_TRADING_CONFIG
from .celery import CELERY_CONFIG
from .registry import DATASET_REGISTRY, TUSHARE_DATASETS, SYNCING_DIR
```

> **风控红线**：`JQDATA_CONFIG` 的配额闸门（`quota_manual_limit=950_000` 硬停、`quota_warn_spare=50_000` 告警、`calibrate_every=10` 校准）是防 JQData 按次计费超额扣费的命门。拆分时**原样搬运、一个字符不动**，仅在文件头补归属注释，绝不借重构之名调参数。

### 5.2 `data/` vs `data_lake/` 边界声明（不重命名目录）

命名是认知陷阱最深处：`data/` = **取数代码包**（clients/fetcher/cleaner/resilience/tushare_sync），`data_lake/` = **parquet 存储文件**。重命名 Python 包会波及海量 import、违反 strangler 低风险铁律，故第 1 步只做边界声明：

- `data/__init__.py` 补醒目 docstring：`"数据访问层：取数/清洗/客户端。只放代码，不放数据文件。"`
- `data_lake/` 顶层加 `.README`：`"物理存储：只放 parquet + .syncing 状态，禁止放 .py"`
- `config/registry.py` 成为数据集**唯一真相源**；14 个 `scripts/sync_*` 的入口逐步收口到 `data/tushare_sync.py::sync_dataset(key)`（收口为 5.2 可选尾巴，不阻塞 Step1 完成）。

### 5.3 `core/` 杂物间解散

| 原文件 | 目标位置 | 兼容垫片 |
|---|---|---|
| `core/indicator.py`（atr） | `factors/atr.py`（**填上空目录**） | `core/__init__.py` re-export `atr` |
| `core/macro_regime.py`（CreditRegime） | Step1 暂留 `core/`（仅加归属注释）；最终归模型层·宏观域，迁移随 Step3/4 推进 | —— |
| `core/notifier.py`（三通道通知） | `infra/notifier.py`（横切） | `core/__init__.py` re-export |

> `factors/` 空目录被填上是 Step1 最直观的「债务清零」信号 —— 原本规划的抽象层开始落地。

---

## 6. 第 2 步：立防腐层 facade

### 6.1 根因与目标

**根因**：`caisen/__init__.py` 零导出，逼得 `caisen_service.py` 直接 import 6 个内部子模块。一旦 `caisen` 内部重组，这 6 处 import 全炸。

**目标**：新建 `caisen/facade.py`，把 `caisen_service` 现有 10 个用例封装为稳定门面，server 只依赖门面。

### 6.2 facade 用例契约（与现有 `caisen_service` 一一对应）

```python
# caisen/facade.py —— 唯一对外契约，内部重组对 server 不可见
class CaisenFacade:
    def scan(req)              -> list[CandidatePlan]      # 编排 screener→plan→storage
    def list_plans(status)     -> list[CandidatePlan]
    def approve(plan_id, rvw)  -> CandidatePlan
    def activate(plan_id)      -> CandidatePlan
    def get_plan(plan_id)      -> CandidatePlan | None
    def replay(req)            -> ReplayReport
    def replay_async(req)      -> str
    def list_replay_runs()     -> list[ReplayRunSummary]
    def get_replay_run(run_id) -> ReplayRunDetail | None
    def delete_replay_run(id)  -> bool
```

### 6.3 依赖方向反转

```
【改造前·穿透】                      【改造后·门面】
caisen_service                        caisen_service
 ├ caisen.plan            ┐            └ caisen.facade.CaisenFacade  ← 唯一依赖
 ├ caisen.backtest_replay │                  │ (内部编排,对外不可见)
 ├ caisen.replay_runs     ├── 摸内脏×6        ├ engines.patterns.screener
 ├ caisen.replay_tasks_db │                  ├ engines.plan / risk
 ├ caisen.storage         │                  ├ optimize.training_*
 └ caisen.patterns.screener┘                 └ infra.storage / replay_*
   caisen 内部一重组 → 6处全炸               caisen 内部任意重组 → server 零改动
```

> **异常透传契约**：`activate_plan` 触发 `_sync_to_active`（持仓激活），`replay_async` 落库异步任务 —— 两者均有副作用。facade 必须完整透传异常，对齐 `server/api/v1/caisen.py::_map_service_exception`：`KeyError→404` / `ValidationError→422` / `ValueError→422`，**不得在封装层吞异常**。

---

## 7. 第 3 步：拆 `caisen/` 上帝包

第 3 步只做**模型层内部分包**（执行编排层属第 4 步，本次不动）。

### 7.1 目标子包结构

```
caisen/  (门面包,保留包名)
├ __init__.py        # re-export 兼容旧路径
├ facade.py          # Step2 建
│
├ engines/           ← 策略本体(纯逻辑,无 IO)
│  ├ patterns/       # ← 原 caisen/patterns/(w_bottom/head_shoulder/triangle_bottom/neckline/zigzag_causal/screener/registry)
│  ├ factors/        # ← core/indicator.py(Step1 已移) + 原 factors/
│  ├ plan.py         # ← 原 caisen/plan.py
│  ├ risk.py         # ← 原 caisen/risk.py(模型级风控,与 trading/risk_shield 区分,Step4 再处理)
│  └ config.py       # ← 原 caisen/config.py(StrategyConfig)
│
├ optimize/          ← 参数优化(可异步、可重跑,与引擎解耦)
│  ├ training_analyzer.py   # ← 原 caisen/training_analyzer.py
│  └ training_loops_db.py   # ← 原 caisen/training_loops_db.py
│
├ advisor/           ← AI 决策(预留,本次仅占位;caisen-ai-training-loop 落地处)
│
└ infra/             ← 待迁项(第4步移出 caisen 包)
   ├ storage.py             # → 最终 数据层·account域
   ├ execution.py           # → 最终 执行层
   ├ backtest_replay.py + replay_runs/tasks_db/scheduler/worker.py
   └ viz_static.py / viz_interactive.py   # → 最终 横切·viz(与顶层 viz/ 合并)
```

### 7.2 单向依赖红线（Step3 的灵魂）

```
engines  ◄──── optimize   (优化器读引擎输出,绝不反向)
engines  ◄──── advisor    (AI 顾问读引擎输出,绝不反向)
engines  ◄──── infra      (执行/存储调引擎,绝不反向)
```

> 这条红线直接回答"AI 决策往哪放"：它是 `advisor/`，单向依赖 `engines`，不会与策略本体死锁耦合。

### 7.3 子阶段（各自测试绿）

- **3a. 建子包骨架 + re-export**：创建 `engines/ optimize/ infra/ advisor/` 四个空包，`__init__.py` 从原位置 re-export（文件暂不动）。此时旧路径 `from caisen.patterns.screener import PatternScreener` 与新路径 `from caisen.engines.patterns import PatternScreener` 并存可用。
- **3b. 物理移动文件**：把文件真正搬进子包，更新 re-export 指向新位置，删除原文件。每移一类跑一次测试。`infra/` 内文件标注「Step4 迁出 caisen 包」。

> **风控拷问**：`caisen/__main__.py`（623 行 CLI 入口）引用几乎所有内部模块，是 3b 的最大波及面 —— 必须作为 3b 的**最后**移动项，移完后单独跑 `python -m caisen` 冒烟 + `scripts/smoke_caisen.py`。

---

## 8. 验证与回退

| 步骤 | 验证手段 | 回退点 |
|---|---|---|
| Step1 | `pytest` 全绿 + `grep -rn "from config import" \| wc -l` 数量不降 + `from core.indicator import atr` 仍可用 | revert 单 commit |
| Step2 | `pytest` 全绿 + `caisen_service` 不再出现 `from caisen.{plan,storage,backtest_replay,replay_runs,replay_tasks_db,patterns}` 直接 import | revert facade，service 还原 |
| Step3a/3b | `pytest` 全绿 + `python -m caisen` 冒烟 + `smoke_caisen.py` + 新旧 import 路径并存验证 | revert 对应 commit |

---

## 9. 范围边界（明确不做）

- ❌ 不动 `trading/`（emt/qmt gateway）、不合并双 risk（`caisen/risk.py` vs `trading/risk_shield.py`）—— 属第 4 步执行编排层。
- ❌ 不动 `data_lake/` 物理存储结构、不改 parquet schema。
- ❌ 不改任何策略算法、参数、风控阈值（含 `JQDATA_CONFIG` 配额闸门）。
- ❌ 不碰 `feat/tushare-data-collection` 分支与 `bridge/` 钉钉桥。
- ❌ 不重命名 `data/` / `data_lake/` 目录（仅边界声明）。

---

## 10. 开放决策记录

| 决策点 | 选择 | 理由 |
|---|---|---|
| config 拆分粒度 | 8 个子文件（按归属层） | 用户确认「没问题」；与目标四层归属一致，认知清晰 |
| `infra/` 处理 | Step3 暂留 caisen 内、标注 Step4 迁出 | 避免与第 4 步执行编排层重叠，保持 Step3 聚焦模型层内部分包 |
| 推进策略 | 渐进 strangler + 独立 refactor 分支 | 用户选定；每步可中断、与 tushare 物理隔离、风险最低 |
| 目录重命名 | 不重命名 `data/` `data_lake/` | 重命名波及海量 import、违反 strangler 低风险铁律；用边界声明替代 |

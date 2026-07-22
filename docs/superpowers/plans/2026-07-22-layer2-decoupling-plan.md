# Layer 2 五模块解耦实现计划（strangler 六阶段）

> **For agentic workers:** REQUIRED SUB-SKILL: 用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 按 Task 逐条实现。步骤用 checkbox（`- [ ]`）跟踪，每 Task 收尾 = 全量 `pytest` 绿 + 一个 commit + 一个可中断点。

**Goal:** 用渐进 strangler 把 Layer 2（实验/外部券商/策略/交易/回测）从「上帝包 caisen + 3 循环依赖 + 32 垫片 + 回测寄生执行层」收敛为「五模块边界清晰、单向依赖、无循环」，全程零逻辑改动（纯结构迁移 + 死码清除）、每阶段测试绿可中断，最终 `caisen` 上帝包解散。

**Architecture:** 七决策刻碑（见 spec §2）——① 五模块按职责解耦；② **compute 方案 A**（纯决策留 `trading.compute` 子包，回测单向依赖，回测/实盘共用杀双源真理）；③ 行情按是否存储分流（实时→交易直调券商，历史→Layer3 数据机器人编排落湖）；④ 回测独立（稳定性隔离）；⑤ experiment 纯配置中心（零依赖叶子）；⑥ caisen 形态退役；⑦ 拓扑订正 `trading→experiment` 拉权重。六阶段 strangler：阶段0 清 EMT 死码 → 阶段1 断环+策略收口 → 阶段2 抽 trading.compute → 阶段3 剥 broker → 阶段4 拆 execution+回测独立 → 阶段5 交易内部五层定型 → 阶段6 清垫片+caisen 解散。

**Tech Stack:** Python 3.10（`.venv310`，pytest 基线 949 测试=918 passed/3 failed）、纯标准库 re-export 垫片（无新依赖）、import-linter（阶段6 立守护）。

**Spec 来源:** `docs/superpowers/specs/2026-07-22-layer2-decoupling-design.md`（设计总纲已起草）。本 plan 是其「转实现计划」产物。

---

## Global Constraints

- **语言**：所有新增/修改代码注释、docstring、commit message 用标准中文（CLAUDE.md 全中文协议）。
- **🩸 零回归红线（用户硬约束）**：每阶段收尾跑全量 `pytest`，**failed 必须仍是同样的 3 个预先债**（见下「已知基线失败」），**不得新增任何失败**。passed 数随死码/测试删除自然递减是允许的，failed 不许变多。
- **已知基线失败（3 个，与 Layer 2 解耦无关，阶段外另修）**：
  1. `tests/test_execution_layer_compat.py::test_check_exit_single_source` — `execution.backtest_replay` 已无 `_simulate_one_trade`（回测重构遗留）
  2. `tests/test_execution_layer_compat.py::test_caisen_business_modules_no_reverse_dependency` — `caisen/facade.py:60 from server`（白名单停在 59 没跟上）
  3. `tests/test_sync_data_lake.py::test_load_universe_filters_st` — `load_universe` 没剔 *ST 股
- **strangler 红线**：每步 = 先建新路径 → 切消费点 → 删旧路径（垫片兜底过渡），`git diff` 应**只有结构迁移 + re-export 垫片**，出现大段逻辑/参数改动即偏离，必须回退。
- **杀手不变量红线**：`check_exit`（`caisen/engines/exit_logic.py:78`）回测/实盘已共用（Step4b 就位），阶段2 抽 `trading.compute` 时**必须保持 is 同源**（搬迁非复制）。
- **范围边界（明确不做）**：不改策略算法/参数/风控阈值；不动 Layer 1 数据物理存储；Layer 3 后台防腐层（`caisen↔server` 循环）不在本 plan（另立）；不修 3 个已知基线失败（除非该阶段天然触及）。
- **执行环境**：Windows + Git Bash。测试用 `.venv310/Scripts/python.exe -m pytest`（vnemttrader 已删，但 xtquant 仍绑 3.10，统一用 .venv310）。文件移动用 `git mv`（保留历史）。
- **每步 TDD 形态**：纯结构重构无新逻辑，TDD 体现为——每个 Task 先在 `tests/test_execution_layer_compat.py` 或新契约测试追加**兼容性断言**（旧路径 + 新路径同源/可用），迁移后跑该断言 + 全量 pytest 绿，再 commit。

---

## E2E 验证矩阵（🩸零回归红线的可执行兑现 · 每阶段必跑）

> pytest 全绿 ≠ 整车能开。本重构是**纯结构迁移**，E2E 锐化为「迁移前后黑盒输出逐数值 / 逐字节一致」。
> 四层 T0–T3（详见 spec §8），每阶段按下表必跑。**任一层失败 = 立即停，revert 排查，不带新失败进下阶段。**

### 四层速查（命令 + 预期）

| 层 | 命令 | 预期 | 成本 |
|---|---|---|---|
| **T0** 单元/契约 | `.venv310/Scripts/python.exe -m pytest tests/ -q --tb=short -p no:cacheprovider 2>&1 \| tail -5` | `918 passed, 3 failed`（failed 必须恒为「已知基线失败」3 个，不得新增） | 秒级 |
| **T1** 数值回归·快版 | `.venv310/Scripts/python.exe scripts/regression_neckline_golden.py`（阶段 1 Task 1.0 建） | golden kelly 年化逐位一致（`==` 或 `pytest.approx(abs=1e-9)`） | 秒级 |
| **T1** 数值回归·全版 | `.venv310/Scripts/python.exe -u scripts/param_iter.py --time-budget 28800` | 最优年化稳定在基线区间（全市场 28.4% / 创板科创口径 99.7%） | 8h · 仅里程碑 |
| **T2** 交易编排冒烟 | `.venv310/Scripts/python.exe scripts/smoke_trading_engine.py` | 影子 eod_plan 全链路 dry_run 返回 `{n_orders:0, mode:dry_run}`，`plan_<today>.json` 落盘 | 分钟级 |
| **T2** 券商真实柜台 | `.venv310/Scripts/python.exe scripts/qmt_live_smoke_headless.py` | T1–T6 共 14 项全 pass（需 miniQMT 客户端登录 + 开盘时段） | 分钟级 · 需柜台 |
| **T3** 浏览器 E2E | `.venv310/Scripts/python.exe -m pytest tests/e2e/lab_param_lab.py -q` | Parameter Lab 页面加载 + 交互断言全绿 | 分钟级 |

### 每阶段必跑门（✓ = 必跑，✓✓ = 核心强校验，— = 该阶段不触及可跳）

| 阶段 | T0 pytest | T1 快版 | T1 全版 | T2 编排冒烟 | T2 券商冒烟 | T3 浏览器 |
|---|---|---|---|---|---|---|
| 0 清 EMT（已完成） | ✓ | — | — | — | ✓（确认 QMT-only） | — |
| 1 断环 + 颈线法收口 + Signal | ✓ | ✓✓ **前置捕获 golden** | — | ✓（signal 字段变） | — | ✓（signal 字段变） |
| 2 抽 trading.compute | ✓ | ✓✓ **必跑·守 check_exit 同源** | — | ✓ | — | — |
| 3 剥 broker | ✓ | — | — | — | ✓✓ **必跑 14 项** | — |
| 4 拆 execution + 回测独立 | ✓ | ✓✓ **必跑·守 driver 收口** | — | — | — | — |
| 5 交易五层定型 | ✓ | — | — | ✓✓（+ post_close 熔断新功能独立验） | — | — |
| 6 清垫片 + caisen 解散 | ✓ | ✓ | ✓✓ **里程碑全量 8h** | ✓ | ✓ | ✓ |

**执行说明**：
- T1 快版脚本 `regression_neckline_golden.py` 由**阶段 1 Task 1.0 Step 1b** 创建并捕获 golden；之后每阶段触及策略 / 回测 / compute 都跑它对比。纯重构下数值必须 `==` 一致，任何漂移 = 偷改逻辑。
- T2 券商冒烟需 miniQMT 客户端 + 开盘时段，**不能进 CI**；作为阶段 3 / 5 的**人工 pre-merge 门**（headless 版半自动）。
- `smoke_caisen.py` + `tests/e2e/caisen_*.py` 验的是 caisen 形态（阶段 1 随 D6 删），**不计入稳定基线**，删除时一并清。
- `post_close` 熔断连线（阶段 5）是**新功能补缺非纯重构**，单独 Task + 单独 E2E（构造 equity 数据源触达 `check_daily_loss_limit`），不计入数值回归基线。
- broadcast / server 无独立 smoke（Layer 3 范围），本 plan 用 `tests/broadcast` + `tests/server` pytest 兜底；正式 smoke 留 Layer 3 防腐层 plan（spec §10 遗留#5）。

---

## 关键事实基线（勘察订正点 · 执行者必读）

四路勘察 agent 逐行核实（2026-07-22），spec 中与代码现状不符处已订正，执行以本表为准：

| 项 | spec/常识说法 | 实际（已核对） | 阶段处理 |
|---|---|---|---|
| sys.modules 垫片数 | 20 个 | **32 个**（17 个服务交易域） | 阶段6 分批清 |
| 回测 driver 位置 | caisen/infra（编排） | 真身在 **`execution/backtest_replay.py`**（Step4 已迁出），caisen/infra 是垫片 | 阶段4 拆 execution 时迁 backtest/ |
| experiment 依赖方向 | 实验→交易（注入） | **trading→experiment**（`trading/engine.py:575` 拉 `resolve_active`），experiment 零外部依赖（纯标准库叶子） | 阶段3/4 守边界 |
| experiment→回测 依赖 | 存在（参数训练） | **不存在**，参数训练在 `caisen/optimize/training_loop`，与 experiment 无代码通路（人工 CLI 录入） | 阶段4 training_loop 归 backtest/ |
| check_exit 单源 | 待建立 | **已就位**（Step4b），回测/实盘共用 | 阶段2 保持同源搬迁 |
| 颈线法策略本体 | 在 strategies/ | 算法在 **`scripts/neckline_method_v0.py`+`neckline_backtest.py`**，`strategies/neckline_method.py` 用 sys.path hack 挂载（算法已零 caisen 依赖） | 阶段1 收口进 strategies/neckline/ |
| caisen↔strategies 循环 | 存在 | 坐实 `caisen/facade.py:46`（模块级）↔ `strategies/caisen_pattern.py:20-25`（反向 import caisen 7 符号） | 阶段1 删 caisen_pattern+facade:46 断环 |
| caisen 形态活跃度 | 多形态策略 | 颈线法是**唯一活跃策略**，caisen 形态（W底/头肩/三角形）已不用 | 阶段1 退役（D6） |
| Signal 契约 | 有 dataclass | **无**，两套 dict 字段（回测 `TRADE_REQUIRED_KEYS` vs 实盘 `scan_live`） | 阶段1 建 Signal dataclass 收敛 |

---

## File Structure（六阶段文件蓝图）

- **阶段0（已完成）**：删 `trading/emt_gateway.py`+EMT 测试/脚本+`emt_api_python/`；改 `trading_service`/`execution/__init__`/`conftest`/`test_execution_layer_compat`/`.env.example`/`.gitignore`+4 注释清理。
- **阶段1**：删 `strategies/caisen_pattern.py`+`caisen/engines/patterns/`+`caisen/patterns/` 垫片；`git mv scripts/neckline_*.py → strategies/neckline/`；建 `strategies/signal.py`（Signal dataclass）。
- **阶段2**：建 `trading/compute/{exit,risk,position,plan,stop,breaker,reconcile}.py`；13+ 纯函数归位（保 check_exit 同源）。
- **阶段3**：建 `broker/{base,qmt,qmt_quote,mock}.py`；`git mv trading/{execution_gateway,qmt_gateway,qmt_market_data}.py → broker/`（留 trading 垫片）；补 `broker.base` 的 `query_asset`/`get_quote` 抽象。
- **阶段4**：建 `backtest/{replay,worker,scheduler,tasks_db,runs,optimize}/`；`git mv execution/replay_*.py + caisen/optimize/ → backtest/`；`execution/{engine,storage}.py → trading/state/`；解散 `execution/` 包。
- **阶段5**：`trading/{types,state,io,orchestrate}/` 定型；拆 `stop_loss_monitor` 四缠热点；`post_close` 熔断连线；状态机 reducer 化。
- **阶段6**：清剩余 sys.modules 垫片；立 `import-linter`/`tests/test_layer_contract.py`；caisen 顶层解散。

---

## Task 阶段0：清 EMT 死代码 ✅【已完成 2026-07-22】

**验证数据**：全量 pytest `918 passed, 3 failed`（基线 `946 passed, 3 failed` → 删 2 个 EMT 测试文件 -28 用例，failed 同 3 个零新增）；生产代码 EMT 残留 Grep 零命中；import 冒烟 `get_gateway()` QMT-only 正确。

**改动**（14 文件，净删 1278 行）：
- Delete：`trading/emt_gateway.py`(652) · `tests/test_emt_gateway.py`(328) · `tests/test_emt_reconnect.py`(75) · `scripts/emt_smoke.py`(120) · `emt_api_python/`(SDK) · `backtest/__pycache__`
- Modify：`trading_service.get_gateway`（QMT 唯一）· `execution/__init__`（去 EMT re-export）· `conftest`（删 vnemttrader 注入，保留 xtquant）· `test_execution_layer_compat`（摘 5 处 EMT 断言）· `.env.example`/`.gitignore`（去 EMT 配置）· `circuit_breaker`/`qmt_gateway`/`test_notifier`/`test_circuit_breaker`（注释清理）

**遗留标注**：`scripts/smoke_caisen.py::run_emt_dry_run_smoke` 不直接 import emt_gateway（走 `ts.get_gateway`），删 EMT 后不炸且不在 tests/，**随阶段1 caisen 形态退役整体删**。

---

## Task 阶段1：断 caisen↔strategies 循环 + 颈线法收口 + Signal dataclass【当前焦点】

**Goal:** 消灭 `caisen↔strategies` 循环（删 `caisen_pattern` 适配器 + `facade.py:46`），退役 caisen 形态（D6），颈线法 `scripts/` 收口进 `strategies/neckline/`，建 Signal dataclass 收敛两套字段。完成后颈线法成为唯一活跃策略，策略层零交易/回测/caisen 依赖。

**风险**：本阶段动 caisen 形态退役 + 颈线法迁移，是六阶段中风险最高的。**Task 1.0 必须先勘察颈线法对 caisen 的隐式依赖**（尤其 `RiskManager` 的 micro_filter/liquidity_filter/macro_position_coef、`zigzag_causal.compute_atr`），确认退役边界后再删。

### Task 1.0：基线快照 + 颈线法依赖勘察

**Files:** 无源码改动；产出勘察结论记入 commit message。

- [ ] **Step 1: 锁阶段1 基线快照**

Run: `.venv310/Scripts/python.exe -m pytest tests/ -q --tb=no -p no:cacheprovider 2>&1 | tail -5`
Expected: `918 passed, 3 failed`（与阶段0 收尾一致；3 failed 是已知基线债）。记入 commit。

- [ ] **Step 1b: 捕获颈线法数值 golden 基线（T1 锚点 · 阶段 1/2/4 数值回归全靠此对比）**

建 `scripts/regression_neckline_golden.py`：固定 3 标的（从 `data_lake` 取创板科创代表，如 `300750.SZ / 688981.SH / 301269.SZ`）+ `neckline_method.DEFAULTS` + `EXEC_DEFAULTS`，调 `scan_symbol` 全链路（识别→执行→凯利），汇总输出 → 落 `tests/_golden/neckline_baseline.json`（含标的清单 + DEFAULTS 哈希 + 各标的 kelly 年化 + trades 计数）。
Run: `.venv310/Scripts/python.exe scripts/regression_neckline_golden.py --capture`
Expected: 生成 golden json 并 commit「捕获颈线法 T1 golden 基线」。**此数值是阶段 1/2/4 迁移后逐位对比的锚**——纯结构重构下迁移后重跑 `--verify` 必须 `==` 一致（`pytest.approx(abs=1e-9)`），任何漂移 = 偷改逻辑，立即 revert。
⚠ 本步是「新增测试脚手架」非业务逻辑，不违反 strangler「纯结构迁移」红线；`scan_symbol` 阶段 1 收口进 `strategies/neckline/` 后，脚本 import 路径随之改，**golden 数值不变**（这正是要守的不变量）。

- [ ] **Step 2: 勘察颈线法对 caisen 的依赖（决定 caisen 形态退役边界）**

Run: 用 codegraph/grep 核实 `strategies/neckline_method.py` + `scripts/neckline_method_v0.py` + `scripts/neckline_backtest.py` 是否 import `caisen.engines.risk`（micro_filter/liquidity_filter/macro_position_coef）、`caisen.patterns.zigzag_causal`、`caisen.engines.exit_logic`、`caisen.config.StrategyConfig`。
Expected: 产出"颈线法依赖 caisen 的符号清单"。**若颈线法依赖 `caisen.engines.risk` 或 `zigzag_causal`，这些不能随 caisen 形态删，需在 Task 1.2 保留并标注归阶段2 compute**（对应 spec §10 遗留#1）。

- [ ] **Step 3: 勘察 caisen 形态消费点 + facade.py 全文**

Run: grep `caisen.patterns|caisen.engines.patterns|from caisen import patterns` 全仓；读 `caisen/facade.py` 全文确认删 `:46` 的 `CaisenPatternStrategy` import 后 facade 是否还用该类（若 facade 体内调用 CaisenPatternStrategy，需一并删调用）。
Expected: 产出"caisen 形态的内部/外部消费点清单"+"facade.py 删 import 的影响面"。

### Task 1.1：断 caisen↔strategies 循环（最低成本路径）

**Files:**
- Delete: `strategies/caisen_pattern.py`（自述阶段E删）
- Modify: `caisen/facade.py`（删 `:46` 模块级 `from strategies.caisen_pattern import CaisenPatternStrategy` + 体内任何调用）
- Modify: `strategies/__init__.py`（删触发 `caisen_pattern` 注册的 import，行 15-16 区域）

- [ ] **Step 1: 删 caisen_pattern 适配器 + facade import**

删 `strategies/caisen_pattern.py`；改 `caisen/facade.py` 去 `:46` import 及体内引用；改 `strategies/__init__.py` 去 `import caisen_pattern`。
Run: `.venv310/Scripts/python.exe -m pytest tests/ -q --tb=short -p no:cacheprovider 2>&1 | tail -15`
Expected: failed 仍 3 个（同基线），不新增。若 `caisen↔strategies` 循环导致 collection error，说明有遗漏的模块级 import，回退排查。
- [ ] **Step 2: 验证循环已断**

Run: `grep -rn "from strategies.caisen_pattern\|import caisen_pattern" --include=*.py .` 应零命中；`grep -rn "from strategies" caisen/` 应零命中（caisen 不再反向 import strategies）。
Expected: 双向零命中 = 循环断。commit「断 caisen↔strategies 循环」。

### Task 1.2：退役 caisen 形态（patterns）

**Files:**
- Delete: `caisen/engines/patterns/`（`w_bottom`/`head_shoulder`/`triangle_bottom`/`zigzag_causal`/`screener`/`registry`/`neckline` 基元）
- Delete: `caisen/patterns/`（转发垫片包）
- Delete: `tests/caisen/` 下形态测试（`test_head_shoulder`/`test_w_bottom`/`test_triangle_bottom`/`test_zigzag_causal`/`test_screener`/`test_registry`/`test_neckline`）
- ⚠️ **保留**：若 Task 1.0 Step 2 发现颈线法依赖 `caisen.engines.risk` 或 `zigzag_causal`，**这几个文件不删**，改为标注「待阶段2 抽 compute」。

- [ ] **Step 1: 删 caisen 形态代码 + 垫片 + 测试**

`git rm` 上述文件（保留 Task 1.0 标注的颈线法依赖文件）。
Run: `.venv310/Scripts/python.exe -m pytest tests/ -q --tb=short -p no:cacheprovider 2>&1 | tail -15`
Expected: passed 数下降（删了形态测试），**failed 仍 3 个同基线**，不新增。若有 import error 指向遗漏的消费点，逐个清理。
- [ ] **Step 2: 清理残留消费点**

Run: `grep -rn "caisen.patterns\|caisen.engines.patterns" --include=*.py . | grep -v "^docs"`
Expected: 零命中（或仅剩 Task 1.0 标注保留的文件）。commit「退役 caisen 形态 patterns」。

### Task 1.3：颈线法收口进 strategies/neckline/

**Files:**
- `git mv`: `scripts/neckline_method_v0.py` → `strategies/neckline/method_v0.py`
- `git mv`: `scripts/neckline_backtest.py` → `strategies/neckline/backtest.py`
- Create: `strategies/neckline/__init__.py`
- Modify: `strategies/neckline_method.py`（删 `:22-29` sys.path hack，改 `from .neckline.method_v0 import ...`；或整体并入 `strategies/neckline/`）

- [ ] **Step 1: 搬迁颈线法算法 + 删 sys.path hack**

`git mv` 两个 scripts 文件进 `strategies/neckline/`；改 `neckline_method.py` 去掉 `sys.path.insert(_PROJ_ROOT/scripts)` hack，改本包相对 import。
Run: `.venv310/Scripts/python.exe -m pytest tests/test_neckline_core.py tests/test_neckline_recognition.py tests/strategies/ -q --tb=short -p no:cacheprovider 2>&1 | tail -15`
Expected: 颈线法相关测试全绿（识别层一致性守护测试 `test_neckline_recognition.py::test_*` 通过 = 研究侧 scan_symbol 与编排侧 scan_at 不分叉）。
- [ ] **Step 2: 全量回归**

Run: 全量 pytest。
Expected: failed 仍 3 个。commit「颈线法收口 strategies/neckline/」。

### Task 1.4：建 Signal dataclass 收敛两套字段

**Files:**
- Create: `strategies/signal.py`（`@dataclass(frozen=True) class Signal`，收敛 `TRADE_REQUIRED_KEYS` 回测口径 + `scan_live` 实盘口径）
- Modify: `strategies/neckline_method.py`（`scan_at`/`scan_live` 返回 `list[Signal]`）
- Modify: `trading/signal_runner.py`（`build_orders_from_signals` 改读 Signal dataclass 字段，去字符串键 `s["symbol"]` 等）

- [ ] **Step 1: 建 Signal dataclass + 双口径收敛**

定义 `Signal`（含 symbol/signal_type/formed_at/entry_price/atr/neckline/bottom + 可选 exit 字段）；`scan_at`/`scan_live` 改返 `list[Signal]`；`signal_runner` 改读 dataclass。
Run: `.venv310/Scripts/python.exe -m pytest tests/test_signal_runner.py tests/test_signal_runner_attribution.py tests/trading/test_engine_eod_injection.py tests/strategies/ -q --tb=short -p no:cacheprovider 2>&1 | tail -15`
Expected: 信号消费链测试全绿。
- [ ] **Step 2: 全量回归 + 阶段1 收尾**

Run: 全量 pytest。
Expected: failed 仍 3 个。commit「建 Signal dataclass 收敛双口径」。阶段1 完成。

### Task 1.5：阶段1 收尾验证

- [ ] **Step 1: 循环终检**

Run: `grep -rn "from strategies\|import strategies" caisen/`（零命中）+ 跑 `tests/test_execution_layer_compat.py`。
Expected: caisen 零反向 import strategies；compat 测试除 2 个已知基线失败外全绿。
- [ ] **Step 2: 产出阶段1 总结**

记录：删了多少形态文件/测试、颈线法是否复用 caisen RiskManager（Task 1.0 结论如何影响阶段2）、Signal dataclass 字段最终集。

---

## Task 阶段2：抽 trading.compute 子包【执行前细化】

**Goal:** 把 13+ 纯决策函数归位进 `trading/compute/`，**保 check_exit is 同源**，让回测可改依赖 `trading.compute`。回测/实盘共用决策内核从「事实就位」升级为「结构显式」。

**关键文件:** `trading/compute/{exit,risk,position,plan,stop,breaker,reconcile}.py`（见 spec §4 归位表）。
**关键步骤:** ① 建 `trading/compute/` + `__init__` re-export；② 逐函数 `git mv` + 保同源（check_exit 从 caisen/engines/exit_logic → trading/compute/exit，caisen 侧留垫片）；③ 颈线法出场双源收口（回测 simulate_exit 与实盘出场共用纯函数，spec §10 遗留#2）；④ 回测改依赖 trading.compute。
**验证:** compute 子包零外部依赖（`grep -rn "import broker\|import data\|from trading.io\|from trading.orchestrate" trading/compute/` 零命中）；全量 pytest failed 仍 3 个；`test_check_exit_single_source` 在搬迁后**仍指向同一函数对象**（is 断言）。**【E2E】T1 快版 `regression_neckline_golden.py --verify` 必须 `==` golden**（守 check_exit 同源 = 数值零漂移）；T2 `smoke_trading_engine` 绿。
**风险:** check_exit 搬迁若破坏同源，回测/实盘双源真理复发——必须保 is 同源 + 垫片兜底。

---

## Task 阶段3：剥出 broker 模块【执行前细化】

**Goal:** `trading/{execution_gateway,qmt_gateway,qmt_market_data}.py` → `broker/`，补 `broker.base` 的 `query_asset`/`get_quote` 统一抽象，留 `trading.*` re-export 垫片（20+ 处消费）。

**关键文件:** `broker/{base,qmt,qmt_quote,mock}.py`；`trading/execution_gateway.py` 等降级为垫片。
**关键步骤:** ① 建 `broker/` 包 + base 抽象（补 query_asset/get_quote）；② `git mv` 三文件；③ 全仓消费点改指 `broker.*`（或留 trading 垫片过渡）；④ `reconcile()` 留 trading（风控语义）。
**验证:** broker 零反向依赖（`grep -rn "from trading\|import trading" broker/` 除 order_state 枚举外零命中）；`test_qmt_gateway` 20 测试 + `test_trading_service` 全绿；import 冒烟 get_gateway 仍返 QmtExecutionGateway。**【E2E】T2 `qmt_live_smoke_headless.py` 14 项全 pass**（人工 pre-merge 门·需 miniQMT 客户端登录；剥 broker 后下单/查持仓/查行情/撤单链路真实柜台可用）。
**风险:** 20+ 处 `from trading.execution_gateway import` 若不留垫片会大面积炸——strangler 铁律①留 re-export。

---

## Task 阶段4：拆 execution + 回测独立成 backtest/【执行前细化】

**Goal:** 回测 driver 与盘中执行状态机物理分离（稳定性隔离）。回测 5 文件 + `caisen/optimize`（参数训练，D5）→ `backtest/`；盘中状态机 `execution/{engine,storage}.py` → `trading/state/`；解散 `execution/` 包。

**关键文件:** `backtest/{replay,worker,scheduler,tasks_db,runs,optimize}/`；`trading/state/{engine,storage}.py`。
**关键步骤:** ① `git mv execution/replay_*.py → backtest/`；② `git mv caisen/optimize/ → backtest/optimize/`；③ `git mv trading/mock_broker.py → backtest/`；④ param_iter/identify_param_scan 收口走 driver（消灭双源路径，spec §10 隐患）；⑤ `execution/{engine,storage}.py → trading/state/`；⑥ 解散 execution/ + 清 caisen/infra 垫片。
**验证:** `backtest/` 只 import `trading.compute`/`strategies`/`data`（`grep -rn "from trading.engine\|from trading.orchestrate\|import execution" backtest/` 零命中）；回测 driver 仍只依赖 `strategies.base.Strategy`；全量 pytest failed 仍 3 个。**【E2E】T1 快版 `regression_neckline_golden.py --verify` 必须 `==` golden**（守 param_iter/identify_param_scan 收口走 driver = 双源路径消灭，数值零漂移）。
**风险:** param_iter 直调 scan_symbol 绕路 driver 是双源隐患，收口时识别层一致性守护测试（`test_neckline_recognition.py`）必须绿。

---

## Task 阶段5：交易内部五层定型【执行前细化】

**Goal:** `trading/{types,compute,state,io,orchestrate}/` 定型（functional core / imperative shell）；拆 `stop_loss_monitor` 四缠热点；`post_close` 熔断连线（live 前必修）；状态机 reducer 化。

**关键文件:** `trading/{types,state,io,orchestrate}/__init__.py`；拆 `trading/engine.py:324-425`。
**关键步骤:** ① 建 types/（Order/Position/PlannedOrder/ExitDecision/OrderState dataclass）；② state/ reducer 化（`(state,event)→(state',commands)`，event 清洗归 io，state/ 只吃干净 event——spec §3.5 决策A）；③ io/（下单/查持仓/查行情只调 broker+data，只搬运不判定）；④ orchestrate/（四触发点 + __main__）；⑤ 拆 stop_loss_monitor 为 compute 判定 + io 查价查仓 + orchestrate 调度；⑥ post_close 熔断连线（补 equity 数据源，spec §10 遗留#3）。
**验证:** `grep` io/orchestrate 内无业务判定（只错误处理分支）；compute/state 零外部依赖；状态机可纯单测；全量 pytest failed 仍 3 个。**【E2E】T2 `smoke_trading_engine` 绿**（四触发点影子编排未坏）；**post_close 熔断连线属新功能**，单独 Task + 单独 E2E（构造 equity 数据源触达 `check_daily_loss_limit` → `cancel_all_open_orders` → `emergency_halt` 三步串联），不计入数值回归基线。
**风险:** post_close 熔断连线需 equity 数据源（broker 无 get_equity）——属功能补缺非纯重构，可能引入新逻辑，单独 Task 谨慎处理。

---

## Task 阶段6：清垫片 + 立 import-linter + caisen 解散【执行前细化】

**Goal:** 清剩余 sys.modules 垫片（32 个中已无消费的），立依赖铁律 CI 守护，caisen 顶层解散。

**关键文件:** `import-linter` 配置或 `tests/test_layer_contract.py`；删 `caisen/` 顶层垫片。
**关键步骤:** ① 逐垫片确认无消费后删（`grep` 每个垫片路径）；② 立 `tests/test_layer_contract.py` 守 spec §7 依赖铁律（compute/state 禁碰 broker/data/io；experiment 零兄弟依赖；strategies 禁碰 trading/broker；backtest 只许 trading.compute）；③ caisen/engines 剩余（config/plan/risk/exit_logic，阶段2 已迁 compute 后）随 caisen 顶层解散；④ viz 归属定（Layer3 或独立，spec §10 遗留#4）。
**验证:** `import-linter` 全绿；`grep -rn "sys.modules\[" caisen/` 零命中；caisen 包可删；全量 pytest failed 仍 3 个（或已另修）。**【E2E】里程碑全量回归**：T1 全版 `param_iter.py --time-budget 28800`（8h）最优年化稳定在基线区间 + T1 快版 `==` golden + T2 全链路冒烟（编排 + 券商）+ T3 `lab_param_lab` 绿。收尾后 Layer 2 五模块解耦 + 零回归双重落锤。

---

## Rollback 策略

- 每个 Task 一个 commit，回滚 = `git revert <commit>`。strangler 垫片保证任一阶段回退后旧路径仍可用。
- **零回归门**：任一阶段收尾若 ① pytest failed 新增（非 3 个已知基线）或 ② 该阶段必跑的 E2E 层（见「E2E 验证矩阵」）失败 / T1 数值快版漂移（`!=` golden），**立即停**，revert 该阶段最后一个 commit 排查，不许带新失败 / 新漂移进下一阶段。
- 阶段1 风险最高（caisen 退役 + 颈线法迁移），若 Task 1.0 勘察发现颈线法深度依赖 caisen 形态（超出 RiskManager/compute_atr），**暂停阶段1 升级评估**，可能需先抽依赖到 compute 再退役形态。

---

## 进度跟踪

- [x] 阶段0：清 EMT 死代码（918p/3f，零回归 ✅）
- [ ] **阶段1：断环 + 颈线法收口 + Signal dataclass** ← 当前焦点，从 Task 1.0 起步
- [ ] 阶段2：抽 trading.compute
- [ ] 阶段3：剥 broker
- [ ] 阶段4：拆 execution + 回测独立
- [ ] 阶段5：交易内部五层定型
- [ ] 阶段6：清垫片 + import-linter + caisen 解散

**机器人续聊入口**：读本 plan「进度跟踪」+ 当前阶段 Task，按 Task 步骤执行，每步 Run+Expected 验证后勾 checkbox + commit。

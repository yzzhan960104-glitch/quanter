# Layer 2 解耦 Follow-up 设计 · 三项 DEFER 收尾

> **状态：🟢 已实施（2026-07-23，commits 见 progress.md Task 1-7）**
> 本文件是 `feat/layer2-decouple` 主线（19 commits、零回归、净删 13728 行）合并前的**三项 DEFER follow-up 收尾设计**。
> 上游总纲：`docs/superpowers/specs/2026-07-22-layer2-decoupling-design.md`；账本：`.superpowers/sdd/progress.md`。
> 续聊入口：从「§5 验证策略」+「§6 实施顺序」读起。

---

## §0 背景与范围

Layer 2 五模块解耦主线已完成（caisen 上帝包 + execution 包双双消失、五模块单向依赖、trading 五层 functional-core、check_exit 回测实盘 is 同源、test_layer_contract 固化 spec§7 六铁律）。合并前剩三项 DEFER follow-up（非阻塞，但合主線前收口更干净）：

| # | follow-up | 性质 | 风险 |
|---|---|---|---|
| **#3** | `training_analyzer` → server 反向依赖 | 架构反转（修真实 spec§7 违规） | 低 |
| **#4** | 3 保留垫片（signal_runner / execution_gateway / order_state） | strangler 收尾清理 | 中（execution_gateway 20+ 消费） |
| **#2** | `param_iter`/`identify` 收口走 driver | **边界重新定义**（spec 预设被实证挑战） | 中（动研究基础设施） |

**分支策略**：三项全在 `feat/layer2-decouple` 上做，按 **#3 → #4 → #2**（低→高风险）推进，最后与主线统一 merge（用户 2026-07-23 拍板）。

---

## §1 #3 · training_analyzer 反向依赖反转

### 1.1 病灶（逐行核实）

`backtest/optimize/training_analyzer.py:18` 模块级：

```python
from server.services.review_service import _call_glm
```

backtest（Layer 2）反向 import server（Layer 3），违反 spec§7「`backtest ─► trading.compute / strategies / data`，禁止 import server」。`analyze_round`/`parse_review` 两处调 `_call_glm`（行 70、130）。

### 1.2 方案 · infra/llm 子包 + 接口实现（ports & adapters）

**定位**：勘察证实项目已有 `infra/` 外部依赖适配层（`core/__init__.py` 明确 notifier → `infra/notifier.py`，钉钉外部 API 适配在此）。LLM 调用（z.ai 外部 API）与 notifier 同性质，归 `infra/` 一致；**不新建 `integration/`**（避免与 infra 职责重叠，形成两个外部依赖层）。

**子包结构**（接口+实现，未来切供应商仅加实现类 + 配置）：

```
infra/llm/
├─ __init__.py   # get_llm_client() 工厂（按 LLM_PROVIDER env 选实现）+ re-export LLMClient
├─ base.py       # LLMClient Protocol（端口）+ LLMConfigError
└─ glm.py        # GlmClient（z.ai Anthropic 兼容端点实现·当前唯一）
```

**端口（base.py）**——业务语义接口，屏蔽供应商细节：

```python
class LLMClient(Protocol):
    def call(self, prompt: str, *, max_tokens: int = 4096,
             temperature: float = 0.3) -> str: ...
```

凭证/端点/HTTP 细节**内化到实现类**，接口只暴露「给 prompt 出文本」。调用失败抛 `LLMConfigError`（凭证缺失）或网络异常，由调用方捕获走各自降级（与现状「`_call_glm` 异常向上抛、`diagnose`/`analyze_round` 捕获降级」语义一致）。

**实现（glm.py）**：`GlmClient` 封装原 `_call_glm` 的 urllib 逻辑（z.ai `/api/anthropic` 端点 + x-api-key/Bearer 双投鉴权 + anthropic-version 头），构造时读 `GLM_API_KEY`/`ZHIPU_API_KEY`/`GLM_MODEL` env。

**工厂（__init__.py）**：`get_llm_client()` 按 `LLM_PROVIDER` env（默认 `glm`）选实现。未来切 Claude Code：仅新增 `infra/llm/claude.py` + 工厂加一分支，**调用方零改动**（用户可扩展性诉求落地）。

**调用方改造**：
- `server/services/review_service.py`：`diagnose` 改 `get_llm_client().call(prompt)`；凭证缺失/失败降级改异常驱动（合并现有 `if not api_key` 前置检查 + `try/except` 为统一 `try: ... except (LLMConfigError, Exception): 降级`）。保留 review 专用 `_assemble_prompt`/`_degraded_report`/`diagnose`。
- `backtest/optimize/training_analyzer.py`：`analyze_round`/`parse_review` 同改；两处 `os.getenv("GLM_API_KEY")` 凭证检查移除（内化到 GlmClient）。

**测试同步**：`tests/caisen/test_training_analyzer.py` monkeypatch 改指——注入实现 Protocol 的 fake `LLMClient` 到 `get_llm_client`，或 patch `infra.llm.glm.GlmClient.call`。

### 1.3 依赖方向与铁律更新

- **方向**：`server → infra.llm`；`backtest → infra.llm`。infra 与 data 同级（基础设施叶子，纯标准库 + env），被所有需要外部依赖的模块依赖。
- **spec§7 加**：「`backtest ─► trading.compute / strategies / data / infra`」—— infra 作外部依赖适配，与 data 并列。
- **test_layer_contract**：backtest 允许依赖集补 `infra`（现只许 trading.compute/strategies/data）。

### 1.4 不取方案

- **Protocol 注入到 parse_review**（仿 `TrainingNotifier`）：parse_review 同步函数 + 被 loop 多处调，注入侵入面大；infra 工厂已足够解耦，不取。
- **training_analyzer 内联复制 `_call_glm`**：违反 DRY，不取。
- **下沉到 `backtest/optimize/llm.py`**（初版方案 A）：LLM 是外部依赖非 backtest 业务；`server→backtest` 依赖不如 `server→infra` 干净。否决。

---

## §2 #4 · 三垫片清理（能清全清）

勘察后真相：三垫片形态差异大，**order_state 非「纯债」**。

### 2.1 signal_runner.py（23 行 · 纯垫片 · 清）

- 真身：`trading/compute/plan.py`（`build_orders_from_signals` + `PlannedOrder`）。
- 消费点：4 个 tests（`tests/trading/test_signal_runner.py` / `test_signal_runner_attribution.py` / `test_engine_eod_injection.py` / `tests/experiment/test_e2e_eod_to_plan.py`）。
- 动作：**删 `trading/signal_runner.py`**；4 tests 改 `from trading.signal_runner import ...` → `from trading.compute.plan import ...`。
- 验证：grep 全仓 `trading.signal_runner` / `trading/signal_runner` 零残留。

### 2.2 execution_gateway.py（62 行 · 纯垫片 · 清）

- re-export 三源四目标：

| 符号 | 真身目标 |
|---|---|
| `BaseExecutionGateway` / `OrderResult` | `broker.base` |
| `MockExecutionGateway` | `broker.mock` |
| `reconcile` / `PositionDrift` / `ReconciliationResult` | `trading.compute.reconcile` |
| `OrderRequest` | `trading.compute.types` |

- 消费点：20+ 处全仓 `from trading.execution_gateway import ...`。
- 动作：**删 `trading/execution_gateway.py`**；逐消费点按所用符号改指对应目标（非一对一，须逐处核实符号集）。
- 验证：grep 全仓 `trading.execution_gateway` / `trading/execution_gateway` 零残留 + 全量 pytest + smoke_trading_engine（实盘下单/撤单链路不变）。

### 2.3 order_state.py（271 行 · hybrid · 清 re-export、保文件）

- **真身留此**：`OrderStateMachine`（有状态 imperative shell 类，28–254 行）—— 合法保留，文件**不删**。
- **清两个 re-export 垫片**：
  - 行 25 `from trading.types.order_state import OrderState` → 消费点改 `from trading.types.order_state import OrderState`。
  - 行 267–271 `from trading.compute.stop import check_stop_loss, check_take_profit, update_trailing_stop` → 消费点改 `from trading.compute.stop import ...`。
- 动作：保 `OrderStateMachine` + 更新 module docstring（去掉 re-export 描述，只述状态机）；删两段 re-export；消费点改指。
- 验证：grep 消费点改完 + 全量 pytest。`from enum import auto`（行 20）若 `OrderStateMachine` 不再引用则一并清。

### 2.4 共性纪律

- 每垫片独立 commit（三 commit），各自跑全量 pytest + golden 守零回归。
- 消费点改指后，垫片文件（signal_runner / execution_gateway）物理删除；order_state 保留瘦身。
- `test_layer_contract.py` 不需改（它扫的是模块间 import 方向，垫片删除使方向更纯净）。

---

## §3 #2 · param_iter 收口（边界重新定义）

### 3.1 根本性发现（对 spec§3.6/§8.4 预设的实证挑战）

勘察证实：param_iter 与 replay driver **不是「同一段逻辑的两条路径」，而是两套不同统计模型**：

| 路径 | 识别+模拟内核 | 统计聚合层 | 业务用途 |
|---|---|---|---|
| **param_iter** | `scan_symbol`→`simulate_exit`→`detect_neckline_method` | `risk_metrics`/`kelly_metrics`（凯利 + pos_cap=0.05 + freq_cap=150 按年封顶 **实盘年化**） | **调参目标函数**（基线年化 28.4% / 创板科创 99.7%） |
| **replay driver** | `scan_at`→`simulate_exit`→`detect_neckline_method` | `_compute_stats`（固定 RISK_FRAC=0.01 **CAGR** + equity_curve） | **展示统计**（前端 ReplayReport） |

**关键**：两侧**识别+模拟内核已同源**（`detect_neckline_method` + `simulate_exit` 同一份，Task 1.6 Signal dataclass 收口 + `test_scan_symbol_matches_strategy` 守护 `scan_symbol`≡`scan_at`）。

**若硬执行 spec「param_iter 走 driver」** → param_iter 须放弃 kelly 目标函数、改用 driver CAGR → **调参语义彻底改变、既有年化基线全废**（正是 spec 自标的「语义风险」）；且 driver 须反向塞 kelly 才能满足 param_iter，污染展示层。

**架构评审结论（风控官人格）**：「双源」真义 = 「双识别内核」，Task 1.6 已收口；统计聚合层是「调参 vs 展示」两个不同业务诉求，**有意分轨是正确设计**，强行统一是反 YAGNI。spec§3.6/§8.4「收口走 driver」的预设需订正。

### 3.2 方案 · 重新定义边界 + 清全局 mutation 债（方案 A）

**收口边界订正**：内核同源 = 已收口；统计层有意分轨 = 设计非债。

**清真技术债 · 全局状态 mutation**：
`scripts/param_iter.py::run_one`（行 159–160）用 `DEFAULTS.update(id_params)` / `EXEC_DEFAULTS.update(exec_params)` **mutation 全局可变状态**传参（靠 try/finally 恢复，不安全）。而 `scan_symbol(sym_df, window, exec=None, id_cfg=None)` **已支持显式传参**，param_iter 未用。

动作：
1. **param_iter**：去 `DEFAULTS.update`/`EXEC_DEFAULTS.update` + try/finally，改构造 `id_cfg`/`exec_cfg` dict 显式传 `scan_symbol(sym_df, window, exec=exec_cfg, id_cfg=id_cfg)`。
2. **identify_param_scan.py / kbkg_trailing_verify.py / regression_neckline_golden.py**：同类全局 mutation（如 `kbkg_trailing_verify.py:70` `DEFAULTS.update(id_p); EXEC_DEFAULTS.update(exec_p)`）一并清，改传参。
3. **实现时核实**：`detect_neckline_method` 内部是否仍直读全局 `DEFAULTS`（若读，需进一步参数化，否则传参不彻底）—— 这是实现阶段的核实点，不达同源则升级处理。

**补契约测试**：固化「param_iter 内核 = driver 内核」同源，防未来分叉。强化/新增断言：param_iter 调的 `scan_symbol` 与 replay driver 调的 `scan_at` 走同一 `simulate_exit`/`detect_neckline_method` 函数对象（is 同源）。

**spec 订正**（§4 详列）。

### 3.3 不取方案

- **硬收口**（param_iter 走 driver + driver 扩 kelly）：破坏基线、反 YAGNI、污染展示层，否决。
- **仅补文档**：留全局 mutation 债，不彻底，否决。

---

## §4 对上游总纲的订正清单

`docs/superpowers/specs/2026-07-22-layer2-decoupling-design.md`：

| 节 | 原文 | 订正 |
|---|---|---|
| §3.6 | 「`param_iter.py`/`identify_param_scan.py` 绕开 driver 直调 `neckline_backtest.scan_symbol`（双源路径隐患）」 | 「识别+模拟内核已同源（Task 1.6）；统计层有意分轨（调参 kelly vs 展示 CAGR），非债。全局 mutation 传参债见 follow-up spec §3.2」 |
| §8.4 | 「阶段4 · driver 收口 → T1 数值一致」 | 「内核同源由 `test_scan_symbol_matches_strategy` 守护；统计层分轨是设计。T1 golden 守 param_iter 改传参后数值零漂移」 |
| §10 待裁决#2 | 「颈线法出场双源收口方式…阶段 2 设计」 | 标记「已由 Task 1.6 Signal dataclass + scan_symbol 参数化收口内核；统计层分轨定案」 |

---

## §5 验证策略（零回归红线）

| 层 | 跑什么 | 验收 |
|---|---|---|
| **T0 单元/契约** | 全量 pytest | failed 恒为 1（universe\*ST 预存基线），**不新增**；test_layer_contract 7 passed |
| **T1 数值回归** | `scripts/regression_neckline_golden.py --verify` | exit 0、golden kelly 年化零漂移（#2 改传参后数值不变） |
| **T2 链路冒烟** | `scripts/smoke_trading_engine.py` | PASS（#4 execution_gateway 迁后实盘编排链路不变） |
| **grep 残留** | 全仓扫 `trading.signal_runner` / `trading.execution_gateway` / 垫片 re-export | 零残留（order_state 除真身 OrderStateMachine） |
| **依赖方向** | `server→infra.llm` / `backtest→infra.llm` 合法；backtest 不 import server；infra 与 data 同级 | grep 证 |

**诚实缺口**：
- T1 golden 只覆盖 3 固定标的；param_iter 全市场 8h 跑不进 CI（成本），靠「传参等价性 + golden 局部守护」。
- #4 execution_gateway 20+ 消费点机械替换的回归保证 = 全量 pytest + smoke（无独立端到端冒烟，由 T0 兜底）。

---

## §6 实施顺序（commit 粒度）

每 commit 跑 T0 + 受影响 T1/T2，零回归方可进下一个。

1. **#3**（1 commit）：新建 `infra/llm/` 子包（`base.py` LLMClient Protocol + `glm.py` GlmClient 实现 + `__init__.py` 工厂）+ review_service/training_analyzer 改用 `get_llm_client()` + test monkeypatch 改指 + spec§7/test_layer_contract 补 backtest→infra。
2. **#4a** signal_runner（1 commit）：删文件 + 4 tests 改指。
3. **#4b** execution_gateway（1 commit）：删文件 + 20+ 消费点改指。
4. **#4c** order_state（1 commit）：删两 re-export + 消费点改指 + docstring 瘦身（保 OrderStateMachine）。
5. **#2a**（1 commit）：param_iter / identify / kbkg / golden 去全局 mutation → 传参 + golden 验零漂移。
6. **#2b**（1 commit）：补契约测试固化内核同源。
7. **#2c**（1 commit）：spec§3.6/§8.4/§10 订正。

完成后 `feat/layer2-decouple` 与主线统一 merge。

---

## §7 待确认 / 边实现边定

1. **detect_neckline_method 是否直读全局 DEFAULTS**：若读，#2a 传参不彻底，需进一步参数化（实现时核实，不达同源则升级）。
2. **execution_gateway 20+ 消费点的符号分布**：实现时 grep 列全量清单，按符号集分批改指（避免漏迁）。
3. **order_state `from enum import auto` 去留**：OrderStateMachine 若不引用则清，否则保 noqa。

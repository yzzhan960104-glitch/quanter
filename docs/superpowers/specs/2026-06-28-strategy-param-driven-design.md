# 设计文档：策略参数 Schema 前端驱动（OSkhQuant 模块① 扩展层）

- **日期**：2026-06-28
- **状态**：设计已获批，待生成实现计划
- **范围**：在 OSkhQuant 模块①（策略插件系统）之上，叠加「策略参数可发现 / 可下发 / 可渲染」机制，实现"前端 100% 驱动回测调参"
- **与既有文档的关系**：
  - **继承** `2026-06-28-oskhquant-absorb-design.md`（总 spec）与 `docs/superpowers/plans/2026-06-28-module1-strategy-system.md`（模块① 计划）的 BaseStrategy / StrategyLoader / 删 Series 调用 / 迁 HMM 等骨架设计，**不重做**
  - **补白**：上述计划里 service 以 `strategy_cls(universe=[...])` 零参数硬编码实例化策略（计划第 946 行），策略可调参数对前端不可见——本扩展正是补这一层

---

## 1. 背景与目标

### 1.1 痛点（经事实审查核实）

用户主诉"核心参数硬编码在后端，每次调参都要改代码重启"。逐行核查后，**真实的硬编码病灶**集中在服务层（而非用户最初以为的 `config.py` 或引擎构造函数）：

```
backtest_service.py:104   moving_average_cross(short_window=5, long_window=20)   # MA 周期写死
backtest_service.py:107   volume_price_trend(window=20)                          # VPT 窗口写死
backtest_service.py:110   (ma_signal + vpt_signal) / 2                           # 技术内融合等权写死
backtest_service.py:114   macro_anchor_signal(threshold=0.02, window=3)          # 宏观阈值写死
portfolio_service.py:92   align_macro_data(release_lag=5, max_fill_days=90)      # HMM 对齐写死
portfolio_service.py:88   covariance_type/n_iter/random_state ← PORTFOLIO_DEFAULTS  # 半硬编码
```

同时澄清两处与用户心智模型的偏差：`CostModel` / `BacktestEngine` 构造函数**早已显式依赖注入、不 import config**；`BacktestRequest` 已含完整的 `CostModelParams`（6 字段）与 `tech_weights` 校验，并非"简陋"。

### 1.2 目标

所有"策略私有超参数"必须：
1. 在策略类上**显式声明**（Pydantic 参数模型，单一真相源）
2. 经 API **以 JSON Schema 下发**前端
3. 前端**按 schema 动态渲染**表单（无硬编码字段）
4. 运行时**由请求显式注入**已校验参数，每次请求参数上下文绝对隔离
5. **严禁 `**kwargs` 黑盒**——参数是已校验的 Pydantic 对象，显式传递

### 1.3 非目标（YAGNI）

- 不做风控参数 / 回撤熔断（`max_drawdown_limit`）——属引擎横切风控，留待模块③ `RiskManager` 统一设计
- 不做策略参数的热持久化 / 预设保存（前端本地即可）
- 不做策略版本管理
- 不物理删除 `BacktestEngine.run()`（沿用模块① 计划决策：仅停止调用，物理删除归模块③，避免与 `tests/test_backtest.py::TestBacktestEngine` 牵连）

---

## 2. 关键决策记录（brainstorming 阶段已对齐）

| 决策项 | 选定方案 | 理由 |
|---|---|---|
| 架构路线 | 沿 OSkhQuant spec 走（非另起炉灶） | 与已获批方向一致，零返工 |
| 落地范围 | 完整模块① + 参数 schema 扩展 | 一次性把策略骨架与参数驱动都做掉 |
| 参数声明机制 | Pydantic `params_model` + `model_json_schema()` + `Field(json_schema_extra={"ui":...})` | 与现有 Pydantic-first 架构一致；单一真相源；后端校验复用；新策略零成本接入 |
| 参数分层 | 引擎级（横切，请求顶层）vs 策略级（私有，schema 下发）严格二分 | 符合"参数的物理归属"，避免巨型嵌套对象 |
| 风控/回撤 | 留模块③ | 避免与未实现的 RiskManager 打架 |
| HMM 矩阵 | `state_weights` + `n_hmm_states` 留请求级（驱动矩阵形状），HMM 训练标量进策略 params | 矩阵列数依赖 universe、行数依赖 n_hmm_states，无法静态 schema 化 |
| 单资产默认策略 | 由现有计划的 `MaCrossStrategy`(纯 MACD) 改为 `TechMacroFusionStrategy`(保留原 tech+macro 融合行为) | 消除现有计划 line 861 的"默认策略行为变更"隐患，且其参数正中用户调参诉求 |
| `tech_weights` | 从 `BacktestRequest` 顶层迁入 `TechMacroFusionParams.tech_weight` | 融合权重是策略私有逻辑，归位策略参数（破坏性但更内聚，前端同步改） |

---

## 3. 参数分层原则（核心心智模型）

| 层级 | 归属 | 持有者 | 举例 | 下发方式 |
|---|---|---|---|---|
| **引擎级（横切）** | 所有策略共享 | `BacktestRequest` / `PortfolioRequest` 顶层 | symbol(s)、dates、initial_capital、signal_freq、cost_model | 固定字段（现状已对） |
| **策略级（私有）** | 单个策略特有 | 策略类 `params_model` | MACD 周期、HMM 协方差类型、宏观发布滞后、技术/宏观融合权重 | **JSON Schema 动态下发**（本次新增） |

红线：**两类参数绝不混入同一个嵌套大对象**。引擎级参数是"回测怎么跑"，策略级参数是"信号怎么算"，物理边界不同。

---

## 4. 参数 Schema 机制（本次核心）

### 4.1 BaseStrategy 扩展（对模块① 计划 `strategies/base.py` 的 delta）

模块① 计划的 base.py 仅有 `name` / `universe` ClassVar 与 `fit` / `generate_target_weights`。本扩展**追加**：

```python
from pydantic import BaseModel

class BaseStrategy(ABC):
    name: ClassVar[str]
    universe: ClassVar[List[str]]
    # 【新增】策略参数的 Pydantic 模型——JSON Schema 的唯一真相源
    params_model: ClassVar[type[BaseModel]]

    def __init__(self, universe: List[str], params: BaseModel):
        """
        显式依赖注入：params 必须是已由 service 层用 params_model 校验过的对象。
        禁止 **kwargs 黑盒——策略内部以 self.params.<field> 显式读取。
        """
        self.universe = list(universe)
        self.params = params
```

`StrategyContext` 保持模块① 计划原样（timestamp / current_weights / cash / aum），不再额外塞 params（params 走 ctor 注入，与 ctx 只读快照职责分离）。

### 4.2 参数模型规范（json_schema_extra 携带 UI 元数据）

每个策略定义一个 `params_model`，字段用 `Field` 声明类型约束 + 中文 `description` + `json_schema_extra={"ui": {...}}` 携带前端渲染提示：

```python
class MaCrossParams(BaseModel):
    fast: int = Field(
        12, ge=2, le=60,
        description="MACD 快线周期（EMA）",
        json_schema_extra={"ui": {"control": "slider", "group": "MACD均线", "step": 1}},
    )
    slow: int = Field(
        26, ge=10, le=120,
        description="MACD 慢线周期（EMA）",
        json_schema_extra={"ui": {"control": "slider", "group": "MACD均线"}},
    )
    signal: int = Field(
        9, ge=3, le=30,
        description="MACD 信号线周期（对 DIF 再求 EMA）",
        json_schema_extra={"ui": {"control": "slider", "group": "MACD均线"}},
    )
```

**`ui` 约定**（前端渲染器协议）：

| ui 键 | 含义 | 取值 |
|---|---|---|
| `control` | 前端控件类型 | `slider` / `input-number` / `select` |
| `group` | 分组（映射为 Tab 标签） | 任意中文串 |
| `step` | 步进（slider/input-number） | 数值，可选 |
| `options` | 下拉项（select） | `[{label, value}]`，可选 |

`select` 类型字段在 params_model 中用 `Literal[...]` 声明，JSON Schema 自动产出 `enum`；前端据此渲染 `el-select`。

### 4.3 已验证的技术事实

- 项目为 **Pydantic v2**（`ConfigDict` / `model_validator` / `field_validator` / `model_json_schema()` 均为 v2 特性，`server/schemas/backtest.py` 印证）
- Pydantic v2 的 `Field(json_schema_extra={...})` 会将额外键原样合并进该字段的 JSON Schema 输出，故 `ui` 会出现在 `model_json_schema()["properties"][<field>]` 中

### 4.4 下发与回传闭环

```
① 前端启动    GET /api/v1/strategies
              → [{name, label, universe, params_schema_url}, ...]
② 选策略      GET /api/v1/strategies/{name}/schema
              → params_model.model_json_schema()  （含 properties[*].ui）
③ 前端渲染    按 schema.properties[*].ui.group 分 Tab
              按 ui.control 选控件，min/max/step/enum 直接取自 schema（与后端 Field 同源）
④ 运行回测    POST /api/v1/backtest/run
              body = {symbol, start_date, ..., strategy_name, strategy_params:{fast:5,...}}
⑤ 后端校验    params = strategy_cls.params_model(**req.strategy_params)
              ← Pydantic 自动校验类型/范围，失败由 FastAPI 返 422
⑥ 后端注入    strategy = strategy_cls(universe=[symbol], params=params)
              ← 每请求 new，绝不跨请求复用
```

**请求隔离红线**：`params` 是请求级局部变量；strategy 与 engine 每请求全新实例化（模块① 计划已遵守，本扩展不破坏）。

---

## 5. 策略参数集定义

### 5.1 MaCrossStrategy（单资产，纯 MACD）

`params_model = MaCrossParams`（见 4.2）。策略逻辑沿用模块① 计划 Task 2：MACD 金叉→满仓、死叉→空仓、DIF>DEA→维持。仅 `__init__` 签名由 `(universe, fast, slow, signal)` 改为 `(universe, params: MaCrossParams)`，内部 `self._fast = params.fast` 等。

### 5.2 TechMacroFusionStrategy（单资产，**新默认**，保留原融合行为）

逻辑整体搬迁自现 `backtest_service.run_single_backtest` 步骤 3-5：MA 双均线 + VPT 等权融合成技术信号 → 与宏观锚点信号按 `tech_weight` 加权融合。`params_model` 正中用户调参诉求：

```python
class TechMacroFusionParams(BaseModel):
    ma_short: int = Field(
        5, ge=1, le=60,
        description="短均线周期（SMA，天）",
        json_schema_extra={"ui": {"control": "slider", "group": "均线", "step": 1}},
    )
    ma_long: int = Field(
        20, ge=5, le=250,
        description="长均线周期（SMA，天）",
        json_schema_extra={"ui": {"control": "slider", "group": "均线"}},
    )
    vpt_window: int = Field(
        20, ge=5, le=120,
        description="量价趋势(VPT)窗口（天）",
        json_schema_extra={"ui": {"control": "slider", "group": "量价"}},
    )
    macro_threshold: float = Field(
        0.02, ge=0.0, le=0.2,
        description="宏观锚点阈值（M2 环比增速）",
        json_schema_extra={"ui": {"control": "slider", "group": "宏观", "step": 0.005}},
    )
    macro_window: int = Field(
        3, ge=1, le=12,
        description="宏观连续超阈值期数",
        json_schema_extra={"ui": {"control": "slider", "group": "宏观"}},
    )
    tech_weight: float = Field(
        0.7, ge=0.0, le=1.0,
        description="技术信号融合权重（宏观权重 = 1 − tech_weight）",
        json_schema_extra={"ui": {"control": "slider", "group": "融合", "step": 0.05}},
    )
```

策略 `generate_target_weights` 产出单标的 `{symbol: fused_weight}` 的 `TargetWeightSignal` 序列（依赖模块① 计划 Task 1 放宽的"权重和 ≤ 1"约束）。该策略成为单资产回测**默认策略**（`strategy_name` 缺省时使用），保留与现网一致的 tech+macro 融合行为。

### 5.3 HMMMacroStrategy（组合）

**标量训练参数进策略 params（新下发）**：

```python
class HmmMacroParams(BaseModel):
    covariance_type: Literal["diag", "full", "tied", "spherical"] = Field(
        "diag",
        description="HMM 协方差类型（diag 稳定 / full 灵活易过拟合）",
        json_schema_extra={"ui": {"control": "select", "group": "HMM训练"}},
    )
    n_iter: int = Field(
        100, ge=10, le=500,
        description="EM 算法最大迭代次数",
        json_schema_extra={"ui": {"control": "input-number", "group": "HMM训练"}},
    )
    release_lag: int = Field(
        5, ge=0, le=30,
        description="宏观数据发布滞后（天，防未来函数）",
        json_schema_extra={"ui": {"control": "slider", "group": "数据对齐"}},
    )
    max_fill_days: int = Field(
        90, ge=10, le=365,
        description="宏观前向填充最大天数（超此标记 NaN）",
        json_schema_extra={"ui": {"control": "slider", "group": "数据对齐"}},
    )
```

**结构性参数留请求级**（`PortfolioRequest` 顶层，契约不变）：`symbols`、`n_hmm_states`、`state_weights`、`buffer_threshold`、`dates`、`initial_capital`。理由：`state_weights` 是 `State_N × symbols` 矩阵，行列分别依赖 `n_hmm_states` 与 `universe`，无法静态 schema 化；`buffer_threshold` 现已在 `PortfolioRequest` 且前端已有滑块，迁移属破坏性且无收益，保留。

**策略 ctor 显式签名**（无 `**kwargs`，schema 参数与结构配置显式分离）：

```python
class HMMMacroStrategy(BaseStrategy):
    name: ClassVar[str] = "hmm_macro"
    params_model: ClassVar[type[BaseModel]] = HmmMacroParams

    def __init__(
        self,
        universe: List[str],
        params: HmmMacroParams,
        n_hmm_states: int,
        state_weights: Dict[str, Dict[str, float]],
        buffer_threshold: float,
    ):
        super().__init__(universe, params)
        # 结构性配置（请求级，非 schema）
        self._n_states = n_hmm_states
        self._state_weights = state_weights
        self._buffer = buffer_threshold
        # HMM 模型用 params 的标量训练参数
        self._hmm = MacroRegimeHMM(
            n_components=n_hmm_states,
            covariance_type=params.covariance_type,
            n_iter=params.n_iter,
            random_state=42,  # 随机种子保持服务层管控，不下发（可复现性）
        )
```

`fit` 内 `align_macro_data` 用 `params.release_lag` / `params.max_fill_days`（替换现 portfolio_service 的硬编码 5 / 90）。

---

## 6. 后端改动清单（相对模块① 计划的增量）

| 文件 | 模块① 计划已含 | 本扩展新增 / 修改 |
|---|---|---|
| `strategies/base.py` | name/universe/fit/generate + StrategyContext | **+ `params_model` ClassVar + `__init__(universe, params)`** |
| `strategies/ma_cross_strategy.py` | MACD 策略（kwargs ctor） | **改 ctor 收 `params: MaCrossParams` + 声明 `params_model`** |
| `strategies/tech_macro_fusion_strategy.py` | — | **新建**（含 `TechMacroFusionParams`，搬迁现 service 步骤 3-5） |
| `strategies/hmm_macro_strategy.py` | HMM 策略（kwargs ctor） | **改 ctor 收 `params: HmmMacroParams` + 结构配置 + 声明 `params_model`**；`release_lag`/`max_fill_days` 取自 params |
| `server/schemas/backtest.py` | +`strategy_name` | **+ `strategy_params: dict = {}`；− `tech_weights`**（迁入融合策略） |
| `server/schemas/portfolio.py` | — | **+ `strategy_params: dict = {}`**（契约其余不变） |
| `server/api/v1/strategies.py` | GET 列表 | **+ `GET /{name}/schema` 返回 `params_model.model_json_schema()`；列表项 +`label`** |
| `server/services/backtest_service.py` | 走策略 + run_portfolio | **默认策略改 `TechMacroFusionStrategy`；实例化时校验注入 `params`**（替换零参数实例化） |
| `server/services/portfolio_service.py` | HMM 迁策略 | **实例化时校验注入 `HmmMacroParams`**（covariance/n_iter/release_lag/max_fill_days 来自请求） |

**service 实例化协议（两服务统一）**：

```python
# 1. 取策略类
strategy_cls = loader.get(req.strategy_name or DEFAULT_STRATEGY)
# 2. 用 params_model 校验请求参数（Pydantic 自动范围/类型校验，失败抛 ValueError → 路由 422/500）
params = strategy_cls.params_model(**(req.strategy_params or {}))
# 3. 显式注入（单资产）
strategy = strategy_cls(universe=[req.symbol], params=params)
# 或（组合）HMMMacroStrategy(universe=req.symbols, params=params, n_hmm_states=..., state_weights=..., buffer_threshold=...)
```

`DEFAULT_STRATEGY = "tech_macro_fusion"`（单资产）；组合固定走 `hmm_macro`。

---

## 7. 前端：JSON Schema → Element Plus 动态渲染器

### 7.1 ParamForm.vue 重构为两段式

- **上半（引擎级，固定控件）**：标的 / 日期 / 初始资金 / 信号频率 / 成本模型——保留现有控件
- **下半（策略级，动态）**：`<StrategyParamForm>` 子组件，watch `strategy_name` → 拉取 schema → 按 schema 渲染

组合模式下，schema 渲染器与**现有 `state_weights` 矩阵控件并存**（矩阵不走 schema，保留现有动态矩阵实现）。

### 7.2 StrategyParamForm 渲染协议

```ts
// 1. 按 schema.properties[*].ui.group 分组 → 每组一个 el-tab-pane
const groups = groupBy(Object.entries(schema.properties), ([, v]) => v.ui?.group ?? '其他')
// 2. 组内按 ui.control 渲染：
//    slider      → el-slider   (min/max/step 取自 schema；0-1 浮点字段如 tech_weight
//                                前端可 ×100 以百分比展示并回传时 ÷100，整数字段原值展示)
//    input-number→ el-input-number
//    select      → el-select   (候选值取自 schema.enum[Literal 推导]；
//                                ui.options 可选提供 enum 值的中文 label)
// 3. v-model 绑定到 reactive(strategyParams)[fieldKey]
//    提交时 strategyParams 作为请求体 strategy_params 字段
```

约束（min/max/step/enum）**直接取自后端 schema**，前端不重复定义——单一真相源，前后端约束永不漂移。

### 7.3 api/backtest.ts 改动

```ts
// 新增
export function getStrategies(): Promise<StrategyMeta[]>
export function getStrategySchema(name: string): Promise<JSONSchema>
// SingleBacktestParams 增 strategy_name?: string; strategy_params?: Record<string, unknown>
// PortfolioParams 增 strategy_params?: Record<string, unknown>
// 删除 SingleBacktestParams.tech_weights（迁入融合策略 params）
```

---

## 8. API 契约变更与向后兼容

| 变更 | 兼容性 | 处理 |
|---|---|---|
| `BacktestRequest` +`strategy_params`(默认`{}`) | ✅ 向后兼容 | 缺省走 params_model 默认值 |
| `BacktestRequest` −`tech_weights` | ❌ 破坏性 | 同步改前端 + api.ts；融合权重迁入 `TechMacroFusionParams.tech_weight` |
| `PortfolioRequest` +`strategy_params`(默认`{}`) | ✅ 向后兼容 | HMM 训练标量可选下发 |
| `GET /strategies/{name}/schema` | ✅ 纯新增 | — |
| 策略 ctor 签名变更 | ✅ 内部 | API 不暴露构造，仅 service 调用 |
| 单资产默认策略 MaCross→TechMacroFusion | ⚠️ 行为修正 | 反而**消除**模块① 计划的"默认策略行为变更"隐患（保留原融合行为） |

---

## 9. 交叉关注点

### 9.1 错误处理
- `strategy_params` 校验失败 → Pydantic `ValidationError` → 路由层捕获转 422（FastAPI 内建）或 500 中文信息（沿用现 `backtest.py` 路由异常策略）
- `strategy_name` 未注册 → `loader.get` 抛 `KeyError` → 路由层 400/500 中文"未注册的策略"
- schema 端点对无 `params_model` 的策略返回空 schema（`{"type":"object","properties":{}}`），前端渲染空表单

### 9.2 前视偏差 / 数据质量
- `TechMacroFusionStrategy` 搬迁现 service 逻辑，沿用 `factors/technical.py` / `macro.py` 既有的 `shift(1)` 防前视与 NaN 处理，**不改变计算语义**
- HMM `release_lag` 下发后仍经 `align_macro_data` 的发布滞后机制防未来函数

### 9.3 测试策略（TDD）
- **params_model 单测**：每个策略 params_model 构造默认值合法、超范围/非法类型被拒
- **schema 端点单测**：`GET /strategies/{name}/schema` 返回含 `ui` 的合法 JSON Schema
- **service 注入单测**：`strategy_params` 缺省→默认值；传入→生效（如改 `ma_short` 后信号变化）；非法值→抛错
- **回归**：`python -m pytest tests/ -v` 全绿；`tests/test_backtest.py::TestBacktestEngine` 仍测 `run()`（未物理删除）

### 9.4 反黑盒审查
- 无 `**kwargs`：params 是 Pydantic 对象，ctor 显式命名参数
- 无新重型依赖：仅复用 Pydantic（已在依赖中）、Element Plus（前端已在用）
- 单一真相源：参数约束只在 `params_model` 的 `Field` 定义一次，前后端共享

---

## 10. 风险与未决项

| 风险 | 处理 |
|---|---|
| `tech_weights` 删除破坏现有前端调用 | 同步改 `ParamForm.vue` + `api.ts`；该字段本就只在 single 模式用，影响面可控 |
| HMM `random_state` 是否下发 | **不下发**，保持服务层管控（=42）确保回测可复现；若后续需调，再纳入 params |
| 前端 JSON Schema 渲染器复杂度 | 一次性投入；后续新策略零前端成本。控件类型限定 slider/input-number/select 三种，范围可控 |
| `Literal` 类型在旧 Pydantic 版本的 schema 表现 | 项目已 v2，`Literal` → `enum` 表现稳定；TDD 阶段以断言验证 |
| 矩阵与 schema 渲染器并存的双轨 UI | 文档明确：组合模式矩阵走专用控件、标量走 schema；前端用 mode 区分，不混淆 |

---

## 11. 实现顺序（依赖驱动，供 writing-plans 拆解）

1. `base.py` 增 `params_model` + ctor（地基，TDD：params 注入单测）
2. `MaCrossParams` + 改 `MaCrossStrategy` ctor（最简策略验证机制）
3. `TechMacroFusionParams` + 新建 `TechMacroFusionStrategy`（搬迁现 service 逻辑）
4. `HmmMacroParams` + 改 `HMMMacroStrategy` ctor（组合策略，含结构配置分离）
5. schema 端点 `GET /strategies/{name}/schema` + 列表 +`label`
6. `BacktestRequest`/`PortfolioRequest` 增 `strategy_params`、删 `tech_weights`；两 service 注入改造
7. 前端 `StrategyParamForm` 渲染器 + `ParamForm` 两段式重构 + `api.ts`
8. 全量回归 `pytest tests/ -v`

---

## 12. 验收标准

- [ ] 每个策略类声明 `params_model`（ClassVar），ctor 收 `params: <ParamsModel>`
- [ ] `GET /api/v1/strategies/{name}/schema` 返回含 `ui` 提示的合法 JSON Schema
- [ ] 前端按 schema 动态渲染 Tab + 控件，无任何硬编码策略字段
- [ ] 运行回测时 `strategy_params` 经 Pydantic 校验后注入策略；缺省用默认值
- [ ] `tech_weights` 已从 `BacktestRequest` 移除，融合权重经 `TechMacroFusionParams` 下发
- [ ] 单资产默认策略为 `TechMacroFusionStrategy`，保留原 tech+macro 融合行为
- [ ] 组合模式 HMM 训练标量（covariance/n_iter/release_lag/max_fill_days）前端可调
- [ ] `python -m pytest tests/ -v` 全绿

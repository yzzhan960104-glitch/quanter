# 三大支柱全栈闭环迭代设计（Explorer / Backtest / Live）

> 设计日期：2026-07-05
> 范围：quanter 全栈（FastAPI + Celery + Vue3 + ECharts），聚焦三大业务支柱
> 基线：在 `2026-07-01-quanter-industrial-design.md` / `2026-07-01-macro-cta-refactor-design.md` 之上增量演进，不重写既有设施

---

## 1. 背景与目标

系统已完成工业级蜕变（per-run SSE 回测、多数据湖、Celery 因子沙盒骨架、QMT 实盘网关本体）。本轮迭代把已具备但未暴露的能力**接通到前端**，形成三大业务闭环：

| 支柱 | 目标 | 核心交付 |
|------|------|---------|
| 🧪 因子探索（Explorer） | 让研究员在前端直观"证伪"因子 | `ExplorerView.vue` 两图（多空分层 + IC 时序/分布）；Celery 暴露分层收益与 IC 序列 |
| 📈 回测可视化（Backtest） | 重构图表，直观看"系统何时崩溃"与"买卖点细节" | `ProChart.vue` 双 Y 轴（log 净值 + inverse 回撤红填充）+ 买卖点 scatter；`ParamForm.vue` 池子只读化；基准净值接入 |
| 🚨 实盘中控（Live） | 接管 QMT 通道，上帝视角风控 | `LiveCockpitView.vue`（熔断按钮 + 心跳 + 持仓 Treemap）；新建 `server/api/v1/trading.py` |

### 1.1 设计原则（继承 CLAUDE.md）
- **极简显式**：因子数学用纯 Pandas/NumPy，ECharts option 用 `markRaw` 纯对象，不引重型黑盒。
- **优雅降级**：每个外部依赖（Redis / QMT / 数据湖 / 标普 ETF）挂掉时，对应面板降级为空态而非白屏/崩溃。
- **绝不虚假繁荣**：Cockpit 状态真实反映后端单例状态机，绝不允许前端展示"已连接"而实际断线。

---

## 2. 现状基线（已核实事实）

| 设施 | 现状 | 缺口 |
|------|------|------|
| `FactorAnalyzer` | 已有 `compute_ic`（ic_series/ic_mean/ic_ir/t_stat）+ `fractile_analysis`（group_returns + long_short） | Celery `run_factor_grid_impl` 仅暴露 ic_mean/ic_ir，**未吐 Q1-Q5 累计收益与 IC 时序/分布** |
| `server/api/v1/explorer.py` | 已有 `POST /explorer/grid`（CPU 探针+降级）+ `GET /explorer/result/{task_id}` | 响应数据契约不含分层/IC 时序，前端无 ExplorerView |
| `BacktestResponse` | 含 metrics/nav_series/drawdown_series/trades/ohlcv/positions | **不含 benchmark_series**，ProChart 无法画基准 |
| `ProChart.vue` | K 线 candlestick + 风控 markPoint | 无双 Y 轴净值/回撤、无买卖点 scatter、无基准 |
| `NavChart.vue` | 已有"左净值/右回撤 inverse + 红填充"双轴范式 | 可作为 ProChart 重构的参考蓝本 |
| `ParamForm.vue` | Epic2 已隐藏 symbol 输入框，提交劫持为 `'dynamic_top50'` | 池子仍是隐式劫持，未做成显式只读卡片 |
| `trading/qmt_gateway.py` | `QmtExecutionGateway` 完备（connect/disconnect/submit_order/cancel_order/`_fetch_broker_positions`/`is_locked`/状态契约校验） | **未挂任何 FastAPI 路由**，`/api/v1/trading/*` 全缺 |
| `router/index.ts` | 仅 `/`(TerminalView) + `/dashboard`(DashboardView) | 无 `/explorer` 与 `/live` |
| 沪深300 基准 | `fetch_daily_hist` 能拉 510300.SH（ETF 属个股）；daily 湖走 stock_basic 不含指数 000300.SH | 基准取数路径需明确（ETF 直取） |

---

## 3. 支柱一：因子探索沙盒（Explorer）

### 3.1 后端：扩展因子网格任务产物

**改动文件**：`server/celery_app.py`（`run_factor_grid_impl`）、`server/api/v1/explorer.py`（响应透传）

`run_factor_grid_impl` 当前只算 `cross_sectional_momentum` + `compute_ic`。扩展为：在算完 IC 后，额外调用 `FactorAnalyzer.fractile_analysis(factor, fwd_returns, n_groups=5)`，把分层累计收益与 IC 时序一并落进结果 dict，供前端直接消费。

**产物结构（落 `reports/explorer/{task_id}.json` + Celery result）**：

```python
{
  "ok": True,
  "factor": "cross_sectional_momentum",   # 因子名（前端图标题）
  "dates": ["2024-01-02", ...],            # 评估区间交易日（IC/分层共享 x 轴）
  "ic_series": [0.082, -0.031, ...],       # 逐期 Rank IC（前端柱状图，正红负绿）
  "ic_mean": 0.041, "ic_ir": 0.52, "t_stat": 2.31,
  # 分层累计收益：每组从 1.0 起步的累计净值，前端多空折线图
  "quantile_nav": {
    "Q1": [1.0, 1.002, ...],   # 最低组
    "Q2": [...], "Q3": [...], "Q4": [...],
    "Q5": [1.0, 1.011, ...],   # 最高组
    "LS": [0.0, 0.009, ...]    # Q5-Q1 纯净 Alpha（高亮曲线）
  },
  # IC 分布直方图：把 ic_series 分箱后的频次
  "ic_hist": {"bin_edges": [-0.1, -0.05, ...], "counts": [3, 12, ...]}
}
```

**数学实现要点（显式，禁 Alphalens）**：
- `quantile_nav`：`fractile_analysis` 已返回每日各组远期收益 `group_returns: dict[g, Series]`；对每组 Series 做 `(1 + r).cumprod()` 得累计净值；`LS = Q5 累计 - Q1 累计`。
- `ic_hist`：`np.histogram(ic_series, bins=20)` 直出 bin_edges + counts。
- **前视红线**：`fractile_analysis` 内部已逐日 qcut（不跨日），`fwd_returns = returns.shift(-1)`，与现 `run_factor_grid_impl` 一致，无新增前视风险。
- **NaN 守卫**：IC 序列里偶发 NaN（横截面样本不足被 dropna）会在 `np.histogram` 触发；落盘前 `pd.Series(ic).dropna().tolist()`，直方图 bin_edges 为空时前端走空态。

**降级契约（沿用既有）**：数据湖未加载 / universe 全空 → `{"ok": False, "reason": "..."}`，前端 ExplorerView 显示空态水印，不崩。

### 3.2 前端：`web/src/views/ExplorerView.vue`（新增）

**路由**：`/explorer` → `ExplorerView`（在 `router/index.ts` 追加，App.vue 顶部导航增加一项「因子沙盒」）。

**布局**：顶部参数条（因子下拉 + 日期范围 + 提交）+ 两图竖排。

**图表 1：多空分层累计收益（Multi-line）**
- 5 条 Q1-Q5 折线（灰阶渐变，Q1 最浅 Q5 最深），叠加 1 条 `LS`（Q5-Q1）粗线高亮（Quant 蓝 `#2962ff`，2.5px）。
- Y 轴线性（累计净值从 1.0 起），x 轴共享 dates。
- tooltip 显示当日各组净值 + LS 价差。

**图表 2：IC 时序与分布（Bar + Line + Histogram 组合）**
- 主图（70% 高）：逐期 IC 柱状图，`visualMap` 按 IC 正负染色（正→红 `#ef5350`，负→绿 `#26a69a`，A 股配色）；叠加 20 日滚动 IC 均值折线（黄 `#d29922`）。
- 副图（30% 高）：IC 分布直方图（bar），x 轴为 IC bin，y 轴为频次。
- 顶部摘要卡：`ic_mean` / `ic_ir` / `t_stat`（|t|>2 显著性提示）。

**数据流**：提交 → `POST /explorer/grid` 拿 task_id → 轮询 `GET /explorer/result/{task_id}`（ready 后取 result）→ `markRaw(result)` 写入 shallowRef → ECharts setOption。
- **轮询纪律**：500ms 间隔，上限 120 次（60s）；degraded=True（同步降级）时直接拿 result 不轮询。
- **清理**：组件 `onBeforeUnmount` 清轮询定时器，避免离开页面后定时器空跑。

### 3.3 因子下拉数据源
- 暂用固定列表（`cross_sectional_momentum` / `vol_adjusted_momentum` / `north_flow_momentum` / `dragon_signal` / `valuation_cross_section`），与 `factors/` 模块导出的函数名对齐。
- universe 固定为 `dynamic_top50` 活跃池（与 ParamForm 同一去主观化口径），从 `daily_active` 湖解析，前端不暴露标的输入框。

---

## 4. 支柱二：工业级回测可视化（Backtest）

### 4.1 后端：`BacktestResponse` 扩展基准净值

**改动文件**：`server/schemas/backtest.py`、`server/services/backtest_service.py`

```python
class BenchmarkPoint(BaseModel):
    """基准累计净值节点（归一化，起点=1.0）"""
    date: str
    nav: float

class BacktestResponse(BaseModel):
    # ... 既有字段 ...
    benchmark_series: List[BenchmarkPoint] = []   # 沪深300 ETF 累计净值，缺数据时空数组
```

**基准计算（在 `backtest_service` 内）**：
1. 取 `510300.SH` 在 `[start_date, end_date]` 的前复权 close：优先 `LakeDataFetcher`（daily 湖 `get_timeseries("510300.SH", ...)`）；湖缺 → `AKShareClient.fetch_daily_hist("510300.SH", ...)` 在线兜底（带熔断）；全空 → `benchmark_series=[]`（ProChart 不画基准线，降级）。
2. **归一化**：基准 close 与策略 nav 都按 `nav / nav[0]` 归一化到起点 1.0（ProChart log 轴可比的前提）。
3. **对齐**：基准按策略 nav_series 的 date 列 reindex，缺失日前向填充（基准停牌日沿用前收，避免基准线断裂误导）。
4. **NaN 守卫**：归一化前 `dropna`；任何 NaN 进入序列 → 严格 JSON 早抛防线（既有 `StrictJSONResponse` 兜底）。

**Why ETF 510300.SH 而非指数 000300.SH**：`AKShareClient` 无指数接口（仅 `stock_zh_a_hist` 个股）；510300 是 A 股 ETF 属个股，`fetch_daily_hist` 直取；与策略同币种/同时区/同交易日历，年跟踪误差 <0.5%，作为相对收益基准偏差可忽略。

### 4.2 前端：`web/src/components/ProChart.vue` 重构

**新结构**：双 Y 轴主图 + 买卖点 scatter 叠加，**移除 K 线**（信息密度过高与净值/回撤主图冲突，K 线需求可在后续迭代作为可切换 tab）。

| 轴 | 配置 | 数据 |
|----|------|------|
| 左 Y 轴 | `type: 'log'`，刻度 1.0 起 | 策略累计净值（Quant 蓝）+ 基准累计净值（灰 `#787b86` 虚线） |
| 右 Y 轴 | `type: 'value', inverse: true`，0% 顶部 | 回撤百分比（红 `#ef5350`，`areaStyle` 半透明红填充，"水下憋气"视觉） |
| X 轴 | category（dates） | dataZoom inside + slider |

**买卖点 scatter（叠在左轴净值线上）**：
- 解析 `trades`，buy → 红 `#ef5350`，sell → 绿 `#26a69a`；每个点 `(date, 当日 nav)`。
- **防御堆叠（数据量自适应）**：
  - `trades.length <= 50`：`symbolSize=10`，显示 direction label。
  - `50 < length <= 500`：`symbolSize=6`，隐 label。
  - `length > 500`：`symbolSize=3`，隐 label，`progressive=400` 开启 ECharts 大数据渐进渲染。
- **tooltip**：展示 date / direction / shares / price / **cost（手续费）**；若 trade 带 `reason`（止损/止盈/移动止损）一并显示。滑点用 `(price - 当日 nav 对应标的收) / 收` 近似标注（可选，数据契约带 slippage 时直取）。

**性能红线**：option 用 `markRaw({})`；ohlcv/nav_series/trades 由父组件 `shallowRef` 持有，ProChart 仅做 `computed` 派生不深拷贝。

### 4.3 前端：`ParamForm.vue` 池子只读化

把当前"隐式劫持 symbol"升级为**显式只读卡片**：
- 顶部插一张 `UniverseCard`（卡片化只读组件），标题 `⚡ 宏观动能 Top 50 活跃池`，副标说明"自动从融资增速 top 板块 + 动量评分选取，不可手动修改"。
- 移除残留的 `formData.symbol` 默认值 `'600000.SH'` 等主观代码（仅保留作 portfolio 权重矩阵占位，UI 不暴露）。
- 提交负载契约不变（仍 `symbol: 'dynamic_top50'`），只是 UI 上彻底无输入框。

---

## 5. 支柱三：实盘中控大屏（Live）

### 5.1 后端：新建 `server/api/v1/trading.py`

**优雅降级真接入**：路由真连 `QmtExecutionGateway` 单例；无 xtquant / 未连接时返回明确降级态（前端真实反映），绝不假数据。

**网关单例装配**（`server/main.py` lifespan）：
- 启动期尝试 `get_qmt_gateway()` 单例：环境变量 `QMT_USERDATA_PATH`/`QMT_ACCOUNT_ID` 齐全 → 构造 `QmtExecutionGateway`（**不**自动 connect，避免启动期阻塞）；缺凭证 → 单例为 None（降级模式）。
- 单例挂 `app.state.qmt_gateway`。

**端点**：

| 方法 | 路径 | 行为 |
|------|------|------|
| GET | `/api/v1/trading/status` | 返 `{connected, locked, mode}`：从 gateway 单例读 `_connected`/`is_locked`；无单例 → `connected=False, mode="unavailable"`；单例存在但未 connect → `mode="disconnected"`；已连接 → `mode="live"`；断线锁定 → `mode="vetoed_by_risk"` |
| GET | `/api/v1/trading/positions` | 聚合底层持仓：调 `gateway._fetch_broker_positions()` 返 `{symbol, qty, market_value, pnl_today, pnl_pct, sector}`；未连接/锁定 → 409 + 中文说明；无单例 → 503 |
| POST | `/api/v1/trading/emergency_halt` | 一键熔断：① 触发 `gateway` 进入 `lock_down=True`（断线锁定，禁后续发单）；② 撤所有活跃订单（`cancel_order`）；③ 钉钉最高级别告警。未连接 → 仅置 lock_down 标志 + 告警（仍返成功，因熔断语义是"拒绝后续发单"已达成）；无单例 → 503 |

**风控红线**：
- `emergency_halt` 必须幂等——重复调用不重复撤单（按 `_orders` 状态过滤仅撤未终态）。
- 持仓 `pnl_today` / `sector` 字段：当日浮动盈亏来自 last_price vs 持仓成本；`sector` 从 `fetch_sector_fund_flow` 的板块映射查（缺映射 → "未知"）。颜色由前端按 pnl 正负染色。
- 所有端点 `run_in_threadpool` 包裹同步 QMT 调用，避免阻塞事件循环（与既有 portfolio 路由同纪律）。

### 5.2 前端：`web/src/views/LiveCockpitView.vue`（新增）

**路由**：`/live` → `LiveCockpitView`（App.vue 导航加「实盘中控」）。

**布局**：顶部状态条 + 左侧熔断控制 + 右侧 Treemap。

**① 一键熔断按钮**：
- 全宽红色大按钮 `【🚨 紧急熔断】`，`el-popconfirm` 二次确认（避免误触）。
- 点击 → `POST /trading/emergency_halt` → 成功后按钮置灰 + 全屏红色脉冲告警条。
- 防御：请求中 loading 态、失败 ElMessage 提示、503 时提示"网关未装配"。

**② 网关心跳灯**：
- 三态圆点：`live`（绿，"已连接"）/ `disconnected`（灰，"未连接"）/ `vetoed_by_risk`（红，"风控否决"）/ `unavailable`（黄，"网关未装配"）。
- **轮询纪律**：`setInterval(fetchStatus, 2000)`；`onBeforeUnmount` 清定时器。
- **绝不虚假繁荣**：状态完全镜像后端 `/status` 返回值，前端不缓存推断。

**③ 持仓 Treemap（多空敞口热力图）**：
- ECharts `treemap`：面积 = 仓位市值占比，颜色 = 当日浮动盈亏（A 股红涨 `#ef5350` 绿跌 `#26a69a`）。
- 一眼扫出哪个板块拖累净值：按 sector 一级分组、个股二级叶子。
- 数据：`GET /trading/positions`；未连接 → 空态水印"网关未连接"。

---

## 6. 架构红线（Grill Me 验收清单）

| 红线 | 落地点 | 验收 |
|------|--------|------|
| 万级时序不递归代理 | ExplorerView/ProChart/Cockpit 全用 `shallowRef` + `markRaw` | F12 检查 reactive 无深度代理；万级 nav setOption < 500ms |
| SSE 清理 | ExplorerView 轮询定时器、Cockpit 轮询定时器均 `onBeforeUnmount` 清 | 切换路由后 Network 面板无残留请求 |
| 状态同步安全 | Cockpit 状态严格镜像后端 `/status` | 断网/重启后端后心跳灯 2s 内变灰 |
| NaN 早抛 | 后端所有新端点经 `StrictJSONResponse`（既有）；IC 序列/基准序列 dropna | 故意注入 NaN → 500 + 中文错，不白屏 |
| 优雅降级 | 数据湖缺/Redis 挂/QMT 未装/基准无数据 → 各面板独立空态 | 全部依赖摘掉，三个视图仍可访问不崩 |
| QMT 幂等熔断 | `emergency_halt` 重复调用不重复撤单 | 连续两次调用，第二次返"已处于熔断态" |

---

## 7. 数据契约变更清单（前后端对齐）

| 文件 | 变更 |
|------|------|
| `server/schemas/backtest.py` | + `BenchmarkPoint`；`BacktestResponse` + `benchmark_series: List[BenchmarkPoint] = []` |
| `server/services/backtest_service.py` | + 基准净值计算 + 归一化 + reindex 对齐 |
| `server/celery_app.py` | `run_factor_grid_impl` 扩返 quantile_nav / ic_series / ic_hist / dates |
| `server/api/v1/trading.py` | **新建**：status / positions / emergency_halt 三端点 |
| `server/main.py` | lifespan 装 QMT 单例；include trading_router |
| `web/src/api/backtest.ts` | + `BenchmarkPoint` interface；`SingleBacktestResponse` + `benchmark_series` |
| `web/src/api/trading.ts` | **新建**：getStatus/getPositions/emergencyHalt |
| `web/src/api/explorer.ts` | **新建**：submitGrid/getResult + 类型 |
| `web/src/views/ExplorerView.vue` | **新建** |
| `web/src/views/LiveCockpitView.vue` | **新建** |
| `web/src/components/ProChart.vue` | 重构双 Y 轴 + scatter |
| `web/src/components/ParamForm.vue` | + UniverseCard 只读卡片 |
| `web/src/components/UniverseCard.vue` | **新建**（小，纯展示） |
| `web/src/router/index.ts` | + `/explorer` + `/live` |
| `web/src/App.vue` | 导航加「因子沙盒」「实盘中控」两项 |

---

## 8. 测试策略

### 8.1 后端
- `test_factor_grid_payload.py`：mock DataLakeReader，断言 `run_factor_grid_impl` 返回结构含 quantile_nav.Q1..Q5/LS、ic_series 长度 = dates 长度、ic_hist.counts 之和 = 有效 IC 样本数。
- `test_backtest_benchmark.py`：注入 fake 510300 close，断言归一化起点=1.0、reindex 后长度=nav_series 长度；湖缺+在线降级全空时 benchmark_series=[]。
- `test_trading_status.py`：monkeypatch gateway 单例三种态（unavailable/disconnected/live/vetoed_by_risk），断言 `/status` 各分支。
- `test_trading_emergency_halt.py`：断言幂等（连续两次不重复撤单）；未连接时仍置 lock_down。
- 既有 `test_strict_json_response.py` 覆盖 NaN 早抛。

### 8.2 前端
- 沿用既有无前端单测的现状；本次以人工验收 + 后端契约测试为主。
- 关键路径脚本化验证：提交 Explorer → 轮询 → 图渲染；Cockpit 轮询心跳不残留。

---

## 9. 实施顺序（建议）

1. **后端契约先行**：`schemas/backtest.py` + `backtest_service` 基准 → `celery_app` 因子扩返 → `trading.py` 三端点。每步配契约测试。
2. **前端 API facade**：`api/explorer.ts` + `api/trading.ts` + `api/backtest.ts` 扩 BenchmarkPoint。
3. **前端视图**：ProChart 重构（影响既有 `/`）→ ExplorerView 新增 → LiveCockpitView 新增 → ParamForm 只读化。
4. **路由与导航接线**：router + App.vue。
5. **端到端验收**：三视图分别跑通，再验证红线清单第 6 节全项。

> 测试与实施细化交由 writing-plans 阶段拆成可执行 task。

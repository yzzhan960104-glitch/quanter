# 全栈重构设计规格：quanter 纯 Python → FastAPI + Vue 3

## 1. 架构决策

- **后端**：FastAPI，分层服务架构（api / services / schemas / core）
- **前端**：Vue 3 Composition API + Vite + Element Plus + ECharts + Axios
- **核心引擎**：保留现有 `backtest/engine.py` 和 `factors/fusion.py`，绝对不修改计算逻辑
- **两种回测模式**：单资产 `run()` + 组合 `run_portfolio()`

## 2. 目录结构

```
server/
├── api/v1/backtest.py        # 单资产路由
├── api/v1/portfolio.py       # 组合路由
├── services/backtest_service.py
├── services/portfolio_service.py
├── schemas/backtest.py
├── schemas/portfolio.py
├── core/config.py
└── main.py

web/
├── src/api/backtest.ts
├── src/views/SingleBacktest.vue
├── src/views/PortfolioBacktest.vue
├── src/components/ParamForm.vue
├── src/components/NavChart.vue
├── src/components/MetricCards.vue
├── src/router/index.ts
├── src/App.vue
├── src/main.ts
├── vite.config.ts
└── package.json
```

## 3. API 接口

### POST /api/v1/backtest/run
- 请求：symbol, start_date, end_date, initial_capital, signal_freq, tech_weights, cost_model
- 响应：metrics, nav_series, drawdown_series, trades

### POST /api/v1/portfolio/run
- 请求：symbols, start_date, end_date, initial_capital, n_hmm_states, buffer_threshold, state_weights
- 响应：metrics, nav_series, drawdown_series, weight_series, trades

## 4. 序列化精简策略

- nav_series：仅 date/nav/return/cumulative_return
- drawdown_series：仅 date/drawdown
- trades：仅 date/direction/shares/price/cost
- 不传输 OHLCV 原始 K 线数据

## 5. 异常处理

- 参数校验失败 → 422 + 明确中文错误信息
- 引擎内部异常 → 500
- 回测超时 → 504
- 前端 Axios 拦截器统一 Toast 提示

## 6. 前端组件

- ParamForm：模式切换，表单校验，权重和=1 实时校验
- NavChart：ECharts 双 Y 轴（净值面积图 + 回撤填充图）
- MetricCards：4×2 网格，涨跌色
- PortfolioBacktest 额外：权重堆叠面积图 + 动态矩阵表单

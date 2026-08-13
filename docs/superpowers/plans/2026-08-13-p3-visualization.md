# P3 可分析性→后台可视化实施计划（敏感性分析 + 热力图 + DiscoveryLab 视图）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 按 spec §4（2026-08-12-overall-optimization-design.md）把参数敏感性分析/热力图融入 presentation 后台「直接看」：`discovery/sensitivity.py` 纯函数读 trial 语料（现成 88 新引擎 + 366 legacy 可作语料）→ 3 个只读端点（去 require_write）→ Vue 新视图 DiscoveryLabView（echarts 5.5 已在前端依赖，零新依赖）。

**Architecture:** 分层红线（spec §4.1）：discovery 包零 presentation 依赖——analysis 纯函数在 discovery/sensitivity.py；research 桥（读库+调纯函数）在 presentation/server/api/v1/discovery.py（新只读 router，不挂 require_write——research_router 保持写鉴权，proposal POST 不动）。

**Tech Stack:** Python 3.10 + numpy/pandas + FastAPI；前端 vue-echarts（DashboardView 同款按需引入模式）。零新增依赖。

## Global Constraints

- 全中文注释，像素级说明 Why（CLAUDE.md）。零新依赖。
- 只读红线：新端点不写 discovery DB（sensitivity 纯读）；`fill=true` 补格依赖 P1 已落地（35s/组）但默认关，不在本计划实现（留 P4 后）。
- 渐进式：每 Task 独立 commit。
- 测试：后端 `.venv310/Scripts/python.exe -m pytest`（PYTHONIOENCODING=utf-8 PYTHONUTF8=1）；前端 `npm run typecheck` + `npm test`。

## File Structure

| 文件 | 职责 | 改动 |
|---|---|---|
| `discovery/sensitivity.py`（新建） | 敏感性分析纯函数（读 trial rows，零 DB 连接——调用方注入） | 新建 |
| `presentation/server/api/v1/discovery.py`（新建） | 只读 router：/research/discovery/{sensitivity,heatmap,params} | 新建 |
| `presentation/server/main.py` | 挂载 discovery_router（**不挂** require_write） | 1 行 |
| `tests/discovery/test_sensitivity.py`（新建） | 敏感性纯函数单测（合成 trial 语料；死参数 min_rr 低方差结论） | 新建 |
| `tests/server/test_discovery_api.py`（新建） | 端点契约（只读 DB 模式，沿 tests/server 惯例） | 新建 |
| `presentation/web/src/api/discovery.ts`（新建） | 前端类型化 facade（沿 caisen.ts 模式） | 新建 |
| `presentation/web/src/views/DiscoveryLabView.vue`（新建） | 三块：敏感性仪表板 + 热力图 + 搜索进展 | 新建 |
| `presentation/web/src/router/index.ts` | +`/discovery` 懒加载路由 | 1 行 |
| `presentation/web/src/App.vue` | 导航 +「搜索实验室」入口（研究区段） | 数行 |
| `presentation/web/src/views/__tests__/DiscoveryLabView.spec.ts`（新建） | vitest 冒烟（渲染 + API mock） | 新建 |

## Tasks

### Task 1：discovery/sensitivity.py 纯函数（TDD）

**接口：**
- `marginal_effects(trials, param_keys, metric="calmar") -> dict`：每维每档 {mean, n}；trial 内 params dict + inner_metrics dict（calmar/ann/max_dd/sharpe/kelly/n/curve）
- `main_effect_ranking(marginals) -> list[(key, spread, n_levels)]`：档间均值极差降序（方差分解的一阶近似——主效应排序）
- `dead_param_flags(marginals, ranking, min_n=2) -> list[str]`：低 spread（<阈值，如 calmar 档间极差 < 全局 5%）且各档有样本 → 死参数候选
- `coverage_blind_spots(marginals, param_space) -> dict[key -> 未采样档]`
- `heatmap_data(trials, x_key, y_key, metric) -> {x_axis, y_axis, grid, n_obs}`：网格均值 + 样本量同行返回（防单点热区误导）
- 验收锚：合成语料中 min_rr 恒 2.0（死参）→ 低方差标记；主效应排名窗口 > min_rr

### Task 2：只读端点 + 挂载

- `GET /api/v1/research/discovery/sensitivity`：marginal + ranking + dead + blind（读最新 snapshot 非 legacy trial）
- `GET /api/v1/research/discovery/heatmap?x=&y=&metric=&fill=false`（fill 恒 False，未实现则 400 或忽略——**忽略并固定 False**，spec §4.2）
- `GET /api/v1/research/discovery/params`：PARAM_SPACE + 耦合约束元数据（前端维度选择器）
- 降级：discovery DB 缺失/空 → 空结构（沿 macro 路由「离线降级」惯例）

### Task 3：前端（api facade + 视图 + 路由 + 导航 + spec）

- `api/discovery.ts`：三端点类型 + getSensitivity/getHeatmap/getParams/getStatus（status 复用现有 /research/discovery/status）
- `DiscoveryLabView.vue`：三块布局（qt-card 样式）——①敏感性仪表板（el-table 按主效应排名 + 死参数徽标 + 盲区警告）②热力图（x/y/metric 三维选择器 → VChart heatmap + 样本量角标）③搜索进展（trial 数/最新 run/新冠军 + 覆盖度）
- router + App.vue 导航（研究区段，nav-label-icon 纪律：带文字标签）

## 实测结果（2026-08-13 填充）

- 后端：sensitivity/heatmap/params 三端点对真实 DB（88 trial）冒烟通过——主效应 top5：
  window 12.81 > breakout_vol_mult 7.16 > max_h_atr 7.09 > tp1_h_mult 6.86 >
  buy_limit_atr_mult 6.67（window 主导符合先验）；死参数标记：stop_atr_mult/
  cancel_thresh_mult/min_touches/trailing_floor；盲区 0 维（88 trial 覆盖全部候选档）。
- 前端：vue-tsc typecheck 绿 + vitest 42 passed（含 DiscoveryLabView 2 例 + 路由表 +1 项）。
- **发现（P4 输入）**：min_rr「死参数」断言已过时——constraints.py 注释称「结构恒 rr=2.0」
  是 R3 前（几何 rr 2H/H）口径；R3 改实际口径 (tp2−entry)/(entry−stop_price) 后 min_rr
  是活参数。且 TPE 采样绕过 normalize_params（Sobol 强制 min_rr=2.0，TPE 自由 1.0/1.5/2.0）
  → 边际效应显示 min_rr 三档均值 5.16/10.24/7.76（n=6/5/77，TPE 档样本小噪声大但非零）。
  → 移交 P4：min_rr 复活 + TPE normalize 收口。

## 风险

| 风险 | 缓解 |
|---|---|
| 88 trial 语料薄 → 敏感性噪声大 | 边际效应表带 n 列 + 盲区提示；不据此强行改参（P4 决策协议不变） |
| research_router require_write 误伤只读 | 新 router 单独挂载不挂写鉴权（spec §4.2 明确） |
| 前端 echarts 引入面 | 按需引入（DashboardView 模式），禁全量 import |

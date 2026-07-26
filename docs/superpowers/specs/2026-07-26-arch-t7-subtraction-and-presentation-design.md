# T7 架构治理 · 减法清零 + presentation 伞盖 + server 内部正名

- **日期**:2026-07-26
- **分支**:`refactor/arch-governance-2026-07`
- **前序**:`2026-07-22-layer2-decoupling-design.md`(trading/broker/backtest 三层定型)、`b1c1ba26` 架构治理 v1(根清理 + README 对齐 + core 解散 + 宏观下线)
- **状态**:设计稿(待用户复审 → 转 writing-plans)

---

## 1. 背景与动机

`b1c1ba26`(架构治理 v1)已完成顶层清理、README 对齐、`core/` 解散与宏观 CTA 下线。复审 README §2「后端分层架构」后,研究员对 5 个目录归属提出讨论。本设计是讨论的实证结论——**只做有证据支撑的减法与最小结构整理,拒绝装饰性重组**。

核心动机:
- **清零死代码**:`core/`(空壳)、`factors/`(孤儿)、`viz/`(仅自测试引用)三相均在 README 标注为"strangler 收尾中/横切",但实证已无生产引用,继续保留只增加认知噪声与命名歧义。
- **消除 core 命名歧义**:顶层 `core/` 与 `server/core/` 同名不同物,新手极易混淆。顶层删 + server 侧正名,一举清零。
- **presentation 伞盖**:研究员决策(覆盖 AI 助手的成本/收益反对)将 `web/` + `server/` 物理收编进 `presentation/`,让接口层 folder 字面对齐 README §2 的分层语义。

---

## 2. 决策矩阵(实证驱动)

| # | 议题 | 决策 | 决定性证据 |
|---|------|------|-----------|
| 1 | `presentation/` 收编 `web/`+`server/` | ✅ **采纳**(用户决策,覆盖 AI 反对) | 概念对齐 README §2 接口层;成本=72 import 机械改写,可控 |
| 2 | `server/core/` 正名 | ✅ → `server/http/`(用户选) | 顶层 core/ 删除后,清除"两个 core"命名尾巴;`http/` 字面贴合内容(auth+CORS+JSON 响应都是 HTTP 概念) |
| 3 | 删 `core/` | ✅ 删除 | 仅 `__init__.py`(12 行空壳说明文档);顶层 `core` 零真实 import(grep 命中的 `server.core.*` 是 server 内部子包,同名不同物) |
| 4 | 删 `factors/` | ✅ 删除 | 颈线法自带 `compute_atr`(`strategies/neckline/method_v0.py:62`),不依赖 `factors/atr`;唯一消费者 `/macro/factors` 端点本身无前端调用方(孤儿) |
| 5 | 删 `viz/` | ✅ 删除 | 全项目仅 `tests/test_viz.py` 引用;`ReportGenerator`/`viz_static`/`viz_interactive` 均已删,只剩 Plotly Jupyter 探索图 |
| 6 | 合并 `data/`+`data_lake/` | ❌ **拒绝** | 类别错误:`data/`=源码(入库),`data_lake/`=parquet 存储(`.gitignore` 第 86 行不入库)。合并破坏 git 追踪与 ruff/mypy 扫描语义 |
| 7 | 合并 backtest/discovery/experiment | ❌ **拒绝** | 三者不同生命周期(web 触发 / 离线 CLI / 实盘热路径);耦合极弱:`backtest` 零引用另两者,`discovery→experiment` 单向 publish,`experiment` 零反向 |
| 8 | `factors` 归策略层 | ❌ **拒绝**(原则) | 策略应**消费**因子,不应拥有因子;反向依赖。且 factors 已死,问题是删不是搬 |
| 9 | `viz` 归 Presentation | ❌ **拒绝**(改为删) | 概念虽属 presentation,但已是死代码;搬动死代码无意义,删更彻底 |

---

## 3. 目标架构

```
quanter/
├─ presentation/                   ← 新增伞盖(接口层)
│  ├─ web/                         (Vue 3 原样迁入,工具链不变)
│  └─ server/                      (FastAPI 应用原样迁入)
│     ├─ api/v1/                   HTTP 路由(接口面)
│     │  ├─ data.py  logs.py  macro.py  review.py  trading.py  training.py  _sse.py
│     ├─ services/                 应用层(用例编排,聚合各域)
│     │  └─ data_service.py  review_service.py  trading_service.py
│     ├─ schemas/                  请求/响应 DTO
│     ├─ http/                     ← 原 server/core/ 正名(HTTP 运行时基建)
│     │  ├─ auth.py                require_write 鉴权装饰器(81 行)
│     │  ├─ config.py              PROJECT_ROOT / CORS_ORIGINS / LOG_CONFIG / sys.path(64 行)
│     │  └─ _responses.py          StrictJSONResponse(38 行)
│     └─ main.py                   FastAPI app 装配
│
├─ ❌ core/                         删除(空壳)
├─ ❌ factors/                      删除(孤儿)
├─ ❌ viz/                          删除(死代码)
│
├─ data/  data_lake/  config/      数据层(不动)
├─ trading/  broker/               执行编排 + 网关(不动)
├─ strategies/                     策略层(不动)
├─ backtest/  discovery/  experiment/   三副线(不动)
├─ infra/  broadcast/              横切(不动)
└─ scripts/  tests/  docs/  ops/   (不动)
```

**分层语义标注**(README §2 同步更新):`api/v1` = 接口面(HTTP 路由);`services` = 应用层(用例编排);`schemas` = DTO;`http` = HTTP 运行时基建(鉴权/配置/响应塑形)。folder 位置不拆散 server 内聚,但 README 显式写清边界。

---

## 4. 改动清单(4 批,每批可独立验证)

### 批 1 · 减法清零(零~低风险,先做)

| 动作 | 文件 | 说明 |
|------|------|------|
| 删 `core/` 整包 | `core/__init__.py` | 12 行空壳,零真实 import |
| 删 `factors/` 整包 | `factors/atr.py` `factors/__init__.py` | 颈线法自带 compute_atr,不依赖 |
| 删 `viz/` 整包 | `viz/interactive.py` `viz/__init__.py` | 仅 test_viz.py 引用 |
| 删 `/macro/factors` 端点 | `server/api/v1/macro.py` | 删 `factors()` 函数(L100-)+ 端点内 `from factors.atr import atr`(L117);保留 `/macro/sector/flow`(DashboardView 在用) |
| 删 `tests/test_viz.py` | 全文件 | `from viz import InteractiveChart`,随 viz/ 删除 |
| 删 `tests/test_final_fixes.py` | 全文件 | 该文件唯一测试 `test_atr_preserves_warmup_nan_not_fake_value` 验证 factors.atr warm-up NaN 行为,import `from factors.atr import atr`;factors 删则测试无意义 |
| 删 `tests/test_layering_compat.py::test_factor_atr_legacy_and_new_path` | 删该测试函数(保留文件其余测试) | 该函数三行 `from factors.atr import atr`(atr_legacy/atr_new/atr_pkg)是真 import,非"过时断言文字";factors 删则 ImportError。文件内 `test_config_package_reexports_legacy_names`/`test_config_credentials_dotenv_loaded`/`test_notifier_legacy_and_new_path` 保留 |
| 清前端 `web/src/api/macro.ts` | 删 `getRegime`/`getCredit`/`getFactors` 孤儿函数 + 对应 Response 接口 | 后端 regime/credit 端点早已删,factors 端点本次删;仅保留 `getSectorFlow`(DashboardView 在用) |

**关键论证**:`/macro/factors` 端点的前端调用方 grep 实证为空(`getFactors` 定义于 macro.ts:118 但无任何 `.vue`/`.ts` import),属孤儿端点。删 factors/ 后它失去唯一依赖,删除比内联 ATR 更彻底,符合 YAGNI。若未来需 ATR 端点,4 行 `high-low` 滚动均值随时可重写(CLAUDE.md 显式实现原则)。

### 批 2 · server/ 内部正名(低风险)

| 动作 | 说明 |
|------|------|
| `git mv server/core/ server/http/` | 保 rename 历史;`__init__.py` 内容不变 |
| 改 import `server.core` → `server.http` | 6 处:`server/main.py`(3 处)、`server/services/trading_service.py`(1 处)、`tests/test_auth.py`、`tests/test_strict_json_response.py`、`tests/test_check_contracts.py`(注释)、`ops/check_ports.py`(注释) |

### 批 3 · presentation/ 伞盖(最大批量,机械改写)

| 动作 | 命中数 | 说明 |
|------|--------|------|
| `git mv web/ presentation/web/` | - | 前端原样,vite 按端口代理不依赖后端路径 |
| `git mv server/ presentation/server/`(此时已是 http/ 正名) | - | 保 rename 历史 |
| `from server` / `import server` → `from presentation.server` / `import presentation.server` | **72 处 / 31 文件** | sed 机械改写,逐文件 `python -c "import"` 烟测 |
| 入口路径 `server.main:app` → `presentation.server.main:app` | 4 处 | README §6.1、`scripts/start_dingtalk_bots.md` ×2、`web/vite.config.ts` 注释 ×1 |

### 批 4 · README + 文档对齐

- README §2 架构图重画(反映 presentation/ 伞盖 + http/ 正名 + core/factors/viz 删除 + macro 端点现状)
- README §6.1 启动命令更新为 `uvicorn presentation.server.main:app --reload`
- README §7 业务模块速览:删 `/macro/factors` 行(若存在)
- 显式标注 `api/v1` / `services` / `http` 三层边界

---

## 5. 风险与缓解

| 风险点 | 等级 | 缓解 |
|--------|------|------|
| 72 处 import 改写遗漏导致 ImportError | 中 | 改完跑 `python -c "import presentation.server.main"` + 全量 pytest;`grep -r "from server\b\|import server\b" --include="*.py"` 返回 0 命中确认无残留 |
| `git mv` 跨目录历史断裂 | 低 | 全程用 `git mv`(非 cp/rm),rename 检测保历史;批 3 先 mv 再 sed 改 import,顺序锁定 |
| 前端代理破裂 | 低 | vite.config.ts proxy 按端口 8000,不依赖后端文件夹路径,零改动(仅注释更新) |
| 删 `/macro/factors` 破前端契约 | 低 | 实证无前端调用方;`getFactors` 客户端函数本就是孤儿,同步删 |
| 删 factors/ 破颈线法 | 零 | 颈线法 `strategies/neckline/method_v0.py:62` 自带 `compute_atr`,grep 实证不 import factors |
| 删 viz/ 破生产 | 零 | 全项目仅 `tests/test_viz.py` 引用,生产零调用 |
| 删 factors/ 破测试 | 低 | 3 处 import 随批 1 同步清:macro.py(随端点删)、test_final_fixes.py(删文件)、test_layering_compat.py(删 1 函数) |

---

## 6. 验证清单(Definition of Done)

每批完成后跑对应验证,全部通过才算该批完成:

**批 1 后**:
- [ ] `pytest tests/ -k "not (backtest or discovery or experiment or broadcast)"` 全绿(核心层测试)
- [ ] `python -c "from server.api.v1.macro import router"` 通过(剩 `/macro/sector/flow` 端点健康)
- [ ] `grep -r "factors\.\|from factors\|import factors" --include="*.py"` 返回 0 命中
- [ ] `grep -r "from viz\|import viz" --include="*.py"` 返回 0 命中
- [ ] `grep -r "from core\b\|import core\b" --include="*.py"` 返回 0 命中(顶层 core)

**批 2 后**:
- [ ] `python -c "from server.http.config import PROJECT_ROOT"` 通过
- [ ] `pytest tests/test_auth.py tests/test_strict_json_response.py` 全绿

**批 3 后**:
- [ ] `python -c "from presentation.server.main import app; print('OK')"` 通过
- [ ] `uvicorn presentation.server.main:app` 能起,`http://127.0.0.1:8000/docs` 可达
- [ ] `grep -rn "from server\b\|import server\b" --include="*.py" --exclude-dir=presentation` 返回 0 命中(全量旧路径清零)
- [ ] `pytest` 全套(含 backtest/discovery/experiment/broadcast)全绿
- [ ] `cd web && npm run build` 前端构建通过

**批 4 后**:
- [ ] README §2 架构图与实际目录树一致(人工对齐)
- [ ] README §6.1 启动命令可直接复制运行

---

## 7. 不做的事(YAGNI 红线)

- ❌ 不合并 `data/` + `data_lake/`(类别错误:代码 vs 存储,`.gitignore` 差异)
- ❌ 不合并 backtest/discovery/experiment(不同生命周期,弱耦合)
- ❌ 不把 `server/services/` 升为顶层应用层(破坏 FastAPI app 内聚,main.py 装配无处安放)
- ❌ 不内联 ATR 保 `/macro/factors` 端点(端点无消费方,删比保更彻底)
- ❌ 不为"未来策略/未来宏观重建"预留抽象(CLAUDE.md 极简原则;宏观重建时另立 `macro/` 包)
- ❌ 不在顶层 `core/` 留 re-export 垫片(已是空壳,垫片无意义)

---

## 8. 迁移影响速览(import 矩阵)

| 旧路径 | 新路径 | 命中 |
|--------|--------|------|
| `from server.xxx` | `from presentation.server.xxx` | 72 处 / 31 文件 |
| `from server.core.xxx` | `from presentation.server.http.xxx` | 6 处(批 2+批 3 复合) |
| `from factors.atr import atr` | (删除) | 3 处(`server/api/v1/macro.py:117` 随端点删、`tests/test_final_fixes.py:15` 删文件、`tests/test_layering_compat.py:42-44` 删函数) |
| `from viz import InteractiveChart` | (删除) | 1 处(`tests/test_viz.py`,随删) |
| `uvicorn server.main:app` | `uvicorn presentation.server.main:app` | 4 处文档/配置 |
| `import core` / `from core` | (删除) | 0 处(已空壳) |

---

## 9. 后续(本设计范围外)

- `trading → server.services` 5 处倒挂 + `trading/protocols.py` 孤儿契约(README §2 已记架构债):属 Layer2 follow-up #4c,本设计不处理。
- `broker → trading.compute.types` 疑似反向:同上,Layer2 follow-up 范畴。
- 宏观逻辑重建:若未来恢复 CreditRegime,另立 `macro/` 包,不复活 `core/`。
- `scripts/` 清空:见 `scripts-dir-retained-decision.md`,后续显式任务。

---

## 变更历史

| 日期 | 版本 | 作者 | 说明 |
|------|------|------|------|
| 2026-07-26 | v1 | AI 助手 + 研究员 | 初稿;5 问题讨论定论 + server 正名 + 4 批改动清单 |

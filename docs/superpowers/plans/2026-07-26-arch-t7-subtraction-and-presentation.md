# T7 架构治理 · 减法清零 + presentation 伞盖 + server 正名 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 删除 core/factors/viz 三相死代码,server/core/ 正名为 server/http/,web/+server/ 收编进 presentation/ 伞盖,同步对齐 README——把 README §2 的分层架构从"注释里"落到"文件夹里"。

**Architecture:** 纯结构重构,零业务逻辑改动。验证手段 = 既有测试套件保持全绿 + 结构 grep 断言 + import 烟测。5 个 Task 按依赖顺序串行(每个 Task 产出独立可验证的交付物,前一个 Task 是后一个的前提)。所有文件移动用 `git mv` 保 rename 历史。

**Tech Stack:** Python 3.10(FastAPI/uvicorn)、Vue 3(Vite)、pytest、Git Bash(Windows)。

**对应 spec:** `docs/superpowers/specs/2026-07-26-arch-t7-subtraction-and-presentation-design.md`

## Global Constraints

- **分支**:在 `refactor/arch-t7-2026-07` 上执行(已建好,spec commit `6f613c45` 在此)。严禁动 master。
- **中文协议**(CLAUDE.md):所有 commit message、新增/修改注释用专业中文。
- **显式实现**(CLAUDE.md):不引入新依赖,纯结构搬运。
- **git mv 保历史**:所有目录/文件移动用 `git mv`,非 cp+rm。
- **TDD 适配**:这是重构不是功能开发,无新测试。"测试先行"= 每个 Task 先跑当前 pytest 建基线绿,改完再跑确认仍绿 + grep 结构断言。
- **commit 粒度**:每个 Task 末尾一个 commit,信息含"Co-Authored-By: Claude <noreply@anthropic.com>"尾签。
- **平台**:Git Bash(POSIX sh),路径用正斜杠,sed 用 `-i`(Git Bash 支持)。
- **不 push**:本计划只本地 commit,不 push、不开 PR(等用户发话)。

---

## File Structure(改动总览)

| Task | 动作 | 文件 |
|------|------|------|
| 1 | 删 core/ + viz/ + 关联测试 | `core/`(整包)、`viz/`(整包)、`tests/test_viz.py`、`tests/test_layering_compat.py`(删 1 函数) |
| 2 | 删 factors/ + /macro/factors 端点 + 前端孤儿 | `factors/`(整包)、`server/api/v1/macro.py`(删端点)、`tests/test_final_fixes.py`(删文件)、`web/src/api/macro.ts`(删 3 函数+5 类型) |
| 3 | server/core/ → server/http/ 正名 | `git mv server/core server/http`、6 处代码 import + 6 处注释 |
| 4 | presentation/ 伞盖 | `git mv web/ server/`→`presentation/`、72 处 import 改写、4 处入口路径 |
| 5 | README + 文档对齐 | `README.md` §2/§6/§7、`scripts/start_dingtalk_bots.md` |

---

## Task 1: 删除 core/ + viz/ + 关联测试(纯死代码清零)

**Files:**
- Delete: `core/__init__.py`(整包 core/)
- Delete: `viz/__init__.py`、`viz/interactive.py`(整包 viz/)
- Delete: `tests/test_viz.py`(全文件)
- Modify: `tests/test_layering_compat.py`(仅删 `test_factor_atr_legacy_and_new_path` 函数,文件其余保留)

**Interfaces:**
- Consumes: 无(纯删除)
- Produces: `core/`、`viz/` 目录消失;`factors.atr` 引用清零的前置(本 Task 不动 factors,但 test_layering_compat 里的函数会因 Task 2 删 factors 而提前暴露,故本 Task 先删该函数避免悬空)

> **注意**:`test_layering_compat.py::test_factor_atr_legacy_and_new_path` 的三行 `from factors.atr import atr`(L42-44)是真 import。本 Task 删它(因 viz/core 死代码清理时一并清过时契约),Task 2 再删 factors/ 本体。顺序锁定:先删测试函数,再删 factors 包,避免任何中间态 ImportError。

- [ ] **Step 1: 建立测试基线(确认改前全绿)**

Run:
```bash
pytest tests/ -x -q 2>&1 | tail -20
```
Expected: 全绿(或有与本 Task 无关的预存失败——记录基线,改后不能新增失败)。若 `test_viz.py`/`test_layering_compat.py` 本就在失败集合,记下基线。

- [ ] **Step 2: 删 core/ 整包**

Run:
```bash
git rm -r core/
```
Verify: `core/__init__.py` 的 12 行空壳说明文档随包删除。零真实 import(顶层 `core` 无引用)。

- [ ] **Step 3: 删 viz/ 整包**

Run:
```bash
git rm -r viz/
```
Verify: 仅 `tests/test_viz.py` 引用 `viz.InteractiveChart`,生产零调用。

- [ ] **Step 4: 删 tests/test_viz.py**

Run:
```bash
git rm tests/test_viz.py
```

- [ ] **Step 5: 删 test_layering_compat.py 里的 factors 契约函数**

Modify: `tests/test_layering_compat.py`

删除整个 `test_factor_atr_legacy_and_new_path` 函数(含其上方的 `# ====...` 分隔注释块与 `# Step 1 契约:core/indicator → factors/atr` 标题)。保留该文件中:
- `test_config_package_reexports_legacy_names`
- `test_config_credentials_dotenv_loaded`
- `test_notifier_legacy_and_new_path`
- 文件头 docstring 与 `from __future__ import annotations`

删后该处只留一个空行分隔上下两个保留函数。

- [ ] **Step 6: 结构断言 —— 死代码清零**

Run:
```bash
# core/ 与 viz/ 目录不存在
test ! -d core && echo "core/ deleted OK" || echo "FAIL: core/ still exists"
test ! -d viz && echo "viz/ deleted OK" || echo "FAIL: viz/ still exists"
# 无 viz 残留 import
grep -rn "from viz\|import viz" --include="*.py" --exclude-dir=.venv310 --exclude-dir=__pycache__ || echo "viz imports: 0 (OK)"
# 顶层 core 无 import(grep 命中的应为 server.core.*,不是顶层 core)
grep -rn -E "^[[:space:]]*(from|import) core[. ]" --include="*.py" --exclude-dir=.venv310 --exclude-dir=__pycache__ || echo "top-level core imports: 0 (OK)"
```
Expected: 三条全 OK。

- [ ] **Step 7: 跑测试确认无新增失败**

Run:
```bash
pytest tests/ -x -q 2>&1 | tail -20
```
Expected: 与 Step 1 基线一致(不新增失败)。`test_viz.py` 已删故其失败(若有)消失。

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "refactor(arch): T7 批1——删 core/+viz/ 死代码清零

core/ 仅剩 12 行空壳 __init__.py(已解散),顶层零真实 import。
viz/ 仅 test_viz.py 引用 InteractiveChart,生产零调用,ReportGenerator
等已先行删除。同步删 test_viz.py 与 test_layering_compat.py 里
test_factor_atr_legacy_and_new_path(为批2 删 factors/ 铺路)。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 2: 删除 factors/ + /macro/factors 端点 + 前端孤儿函数

**Files:**
- Delete: `factors/__init__.py`、`factors/atr.py`(整包 factors/)
- Delete: `tests/test_final_fixes.py`(全文件,其唯一测试验证 factors.atr)
- Modify: `server/api/v1/macro.py`(删 `/macro/factors` 端点 + 其 `from factors.atr import atr`)
- Modify: `web/src/api/macro.ts`(删 `getMacroRegime`/`getMacroCredit`/`getMacroFactors` + 5 个孤儿类型,保留 `getSectorFlow`)

**Interfaces:**
- Consumes: Task 1(已删 test_layering_compat 的 factors 引用,本 Task 删 factors 本体不会再被测试挂住)
- Produces: `factors/` 目录消失;macro.py 仅剩 `/macro/sector/flow` 端点;前端 macro.ts 仅剩 sector 相关导出。

> **论证**:颈线法 `strategies/neckline/method_v0.py:62` 自带 `compute_atr`,grep 实证不 import factors。`/macro/factors` 端点的前端调用方为空(DashboardView 仅 import `getSectorFlow`),删 factors 后端点失去唯一依赖,删端点比内联 ATR 更彻底(YAGNI)。

- [ ] **Step 1: 删 factors/ 整包**

Run:
```bash
git rm -r factors/
```

- [ ] **Step 2: 删 tests/test_final_fixes.py**

Run:
```bash
git rm tests/test_final_fixes.py
```
Verify: 该文件唯一测试 `test_atr_preserves_warmup_nan_not_fake_value` 验证 factors.atr warm-up NaN 行为,factors 删则无意义。

- [ ] **Step 3: 删 macro.py 的 /macro/factors 端点**

Modify: `server/api/v1/macro.py`

删除从「分隔注释 + `@router.get("/factors/{symbol}"...`」到文件末尾(`return {"atr": float(v)}`)的整段——即 `factors()` 异步端点函数及其上的 `# ----...` 分隔线。

具体:删除文件中 L98 起的 `# ------------------------------` 分隔线 + `@router.get("/factors/{symbol}", ...)` 装饰器 + `async def factors(...)` 整个函数体(到文件末尾 L139)。

删后 macro.py 应结束于 `/macro/sector/flow` 的 `sector_flow()` 函数末尾。文件顶部 docstring 的「端点清单」也要同步去掉 `/macro/factors/{symbol}` 那一行(把"端点清单"从两行改为一行,只留 sector/flow)。

**macro.py 顶部 docstring 修改**:把
```
端点清单：
    - GET /api/v1/macro/sector/flow        板块资金流排名 + 活跃股池
    - GET /api/v1/macro/factors/{symbol}   单标的 ATR 波动率（微观定权）
```
改为
```
端点清单：
    - GET /api/v1/macro/sector/flow        板块资金流排名 + 活跃股池
```

- [ ] **Step 4: 结构断言 —— factors 清零**

Run:
```bash
test ! -d factors && echo "factors/ deleted OK" || echo "FAIL"
grep -rn "from factors\|import factors\|factors\.atr" --include="*.py" --exclude-dir=.venv310 --exclude-dir=__pycache__ || echo "factors imports: 0 (OK)"
```
Expected: 两条 OK(macro.py 的 `from factors.atr` 已随端点删除)。

- [ ] **Step 5: macro router 烟测 —— 剩余端点健康**

Run:
```bash
python -c "from server.api.v1.macro import router; print('routes:', [r.path for r in router.routes])"
```
Expected: 输出仅含 `/macro/sector/flow`(prefix `/macro` + `/sector/flow`),无 `/factors/{symbol}`。

- [ ] **Step 6: 删前端 macro.ts 孤儿函数与类型**

Modify: `web/src/api/macro.ts`

删除以下 3 个函数 + 5 个孤儿类型(它们的后端端点已删或本次删,且 DashboardView 不引用):
- 类型:`RegimeValue`、`RegimeHistoryPoint`、`MacroRegimeResponse`、`SeriesPoint`、`MacroCreditResponse`、`MacroFactorsResponse`
- 函数:`getMacroRegime`、`getMacroCredit`、`getMacroFactors`

**保留**:`SectorRecord`、`SectorFlowResponse`、`getSectorFlow`(DashboardView.vue:43 在用)。

删后 macro.ts 应只剩:文件头 docstring(需同步修订,把"四个 GET 端点"改为"单个 GET 端点")+ `import { apiClient }` + `SectorRecord`/`SectorFlowResponse` 类型 + `getSectorFlow()` 函数。

**macro.ts 头部 docstring 修订**:把"对应后端 server/api/v1/macro.py 的四个 GET 端点"改为"对应后端 server/api/v1/macro.py 的单个 GET 端点(/macro/sector/flow)"。

- [ ] **Step 7: 前端构建确认**

Run:
```bash
cd web && npm run build 2>&1 | tail -15
```
Expected: 构建成功(若 TypeScript 报某处仍引用已删类型,说明 grep 漏了调用方,按报错定位补删;实证 DashboardView 仅用 getSectorFlow)。

- [ ] **Step 8: 跑后端测试**

Run:
```bash
pytest tests/ -x -q --ignore=tests/test_viz.py 2>&1 | tail -15
```
Expected: 无新增失败。

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "refactor(arch): T7 批2——删 factors/ 与 /macro/factors 死端点

factors/atr 唯一消费者是 /macro/factors 端点,该端点前端零调用方
(DashboardView 仅用 getSectorFlow)。颈线法自带 compute_atr 不依赖
factors。删 factors/ 整包 + 端点 + test_final_fixes.py + 前端
getMacroRegime/getMacroCredit/getMacroFactors 三个孤儿函数及关联类型。
保留 /macro/sector/flow(在用)。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 3: server/core/ → server/http/ 正名

**Files:**
- Rename: `git mv server/core server/http`
- Modify(6 处代码 import):
  - `server/main.py:25,26,29`
  - `server/services/trading_service.py:27`
  - `tests/test_auth.py:16`
  - `tests/test_strict_json_response.py:10`
- Modify(6 处注释/文档,保一致性):
  - `ops/check_contracts.py:133`
  - `ops/check_ports.py:12`
  - `server/api/v1/_sse.py:11`
  - `server/http/config.py:20`(原 server/core/config.py 自引用注释)
  - `server/http/__init__.py:2`(原 server/core/__init__.py docstring "server.core 包初始化")
  - `tests/test_check_contracts.py:12`

**Interfaces:**
- Consumes: Task 1、Task 2(无依赖,但顺序在前两个之后保持工作区稳定)
- Produces: `server.http` 子包(含 `auth`/`config`/`_responses`),`server.core` 命名彻底消失。

- [ ] **Step 1: git mv 重命名(保历史)**

Run:
```bash
git mv server/core server/http
```
Verify:`server/http/` 下应有 `__init__.py`、`auth.py`、`config.py`、`_responses.py`。

- [ ] **Step 2: 改 6 处代码 import —— sed 批量**

Run:
```bash
# 精确匹配 server.core 的代码 import(server.core 后跟 . 或空格或 import)
files=$(grep -rln -E "from server\.core[. ]|import server\.core[. ]" --include="*.py" --exclude-dir=.venv310 --exclude-dir=__pycache__)
echo "命中文件: $files"
for f in $files; do
  sed -i -E 's/from server\.core\./from server.http./g; s/import server\.core\./import server.http./g' "$f"
done
```
Expected: 命令 `server/main.py`、`server/services/trading_service.py`、`tests/test_auth.py`、`tests/test_strict_json_response.py` 四个文件被改(main.py 3 处、其余各 1 处)。

- [ ] **Step 3: 改 server/http/__init__.py docstring**

Modify: `server/http/__init__.py`

把第 2 行 `"""server.core 包初始化"""` 改为 `"""server.http 包初始化(HTTP 运行时基建:auth/config/_responses)"""`。

- [ ] **Step 4: 改 server/http/config.py 自引用注释**

Modify: `server/http/config.py:20`

把 `# server/core/config.py → server/ → 项目根目录` 改为 `# server/http/config.py → server/ → 项目根目录`。

- [ ] **Step 5: 改其余 4 处注释引用**

Modify(把注释里的 `server.core` / `server/core` 路径改为 `server.http` / `server/http`):
- `ops/check_contracts.py:133` —— `server/core/config.py` → `server/http/config.py`
- `ops/check_ports.py:12` —— `server.core.config` → `server.http.config`
- `server/api/v1/_sse.py:11` —— `server/core/_responses.py` → `server/http/_responses.py`
- `tests/test_check_contracts.py:12` —— `server.core.config` → `server.http.config`

Run(批量替换注释中的路径):
```bash
sed -i -E 's|server/core/|server/http/|g; s|server\.core\.|server.http.|g' \
  ops/check_contracts.py ops/check_ports.py server/api/v1/_sse.py tests/test_check_contracts.py
```
> ⚠ 注意:此 sed 会把上述文件里**所有** `server/core/` 与 `server.core.` 字面量替换。执行后逐文件 `git diff` 确认只动了注释、未误伤逻辑。

- [ ] **Step 6: 结构断言 —— server.core 清零**

Run:
```bash
# 代码 import 清零
grep -rn -E "from server\.core[. ]|import server\.core[. ]" --include="*.py" --exclude-dir=.venv310 --exclude-dir=__pycache__ || echo "server.core code imports: 0 (OK)"
# 目录确认
test -d server/http && echo "server/http exists OK" || echo "FAIL"
test ! -d server/core && echo "server/core gone OK" || echo "FAIL"
```
Expected: 三条 OK。

- [ ] **Step 7: import 烟测**

Run:
```bash
python -c "from server.http.config import PROJECT_ROOT, CORS_ORIGINS; from server.http.auth import require_write; from server.http._responses import StrictJSONResponse; print('server.http import OK')"
python -c "from server.main import app; print('server.main import OK')"
```
Expected: 两条均 OK。

- [ ] **Step 8: 跑测试**

Run:
```bash
pytest tests/test_auth.py tests/test_strict_json_response.py tests/test_layering_compat.py -v 2>&1 | tail -20
```
Expected: 全绿。

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "refactor(arch): T7 批3——server/core/ 正名为 server/http/

清除顶层 core/(批1已删)后,server/core/ 与历史文档 core/ 仍同名易混。
正名为 server/http/(字面贴合内容:auth+CORS+JSON 响应都是 HTTP 概念)。
git mv 保历史;6 处代码 import + 6 处注释同步更新。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 4: presentation/ 伞盖 —— git mv web/+server/ + 72 处 import 改写

**Files:**
- Move: `git mv web presentation/web`
- Move: `git mv server presentation/server`(此时已是 http/ 正名后)
- Modify: 72 处 `from server` / `import server` → `from presentation.server` / `import presentation.server`(31 个 .py 文件)
- Modify(入口路径,4 处):
  - `README.md` §6.1 —— `uvicorn server.main:app` → `uvicorn presentation.server.main:app`
  - `scripts/start_dingtalk_bots.md`(2 处)—— 同上
  - `web/vite.config.ts`(注释 1 处)—— 同上

**Interfaces:**
- Consumes: Task 3(server.core 正名完成,避免本 Task 的 sed 同时处理 core→http 与 server→presentation.server 两个变换)
- Produces:`presentation/web/`、`presentation/server/` 两个子树;`from server` 旧路径在全项目清零;uvicorn 入口变为 `presentation.server.main:app`。

> **执行顺序铁律**:先 `git mv`(目录就位)→ 再 sed 改 import(此时 .py 文件路径已变,但内容里的 `from server` 仍是旧路径,sed 把它们改成 `from presentation.server`)→ 最后改入口文档路径。顺序不能乱。

- [ ] **Step 1: 建 presentation/ 并 git mv web/**

Run:
```bash
mkdir -p presentation
git mv web presentation/web
```
Verify:`presentation/web/src/views/` 等结构存在。

- [ ] **Step 2: git mv server/**

Run:
```bash
git mv server presentation/server
```
Verify:`presentation/server/main.py`、`presentation/server/http/`、`presentation/server/api/v1/` 等存在。

> ⚠ 此刻项目处于"中间态":.py 文件内容里的 `from server` / `from server.http` 全部 broken(因 server/ 已搬到 presentation/server/)。接下来 sed 必须一次性改完才能恢复 import。

- [ ] **Step 3: sed 批量改 72 处 import**

Run:
```bash
# 收集所有命中文件(排除 .venv310 / __pycache__ / .git / node_modules)
files=$(grep -rln -E "from server[. ]|import server[. ]" --include="*.py" --exclude-dir=.venv310 --exclude-dir=__pycache__ --exclude-dir=node_modules)
echo "命中文件数: $(echo $files | wc -w)"
# 逐文件改:from server.X → from presentation.server.X;from server import → from presentation.server import;import server.X → import presentation.server.X
for f in $files; do
  sed -i -E 's/from server\./from presentation.server./g; s/from server import/from presentation.server import/g; s/import server\./import presentation.server./g' "$f"
done
echo "改写完成"
```
Expected: 命中文件数与 grep 一致(改前用 `grep -rln -E "from server[. ]|import server[. ]" --include="*.py" ... | wc -l` 应得 31 左右)。

> **sed 安全性论证**:模式 `from server\.` 只匹配字面 `from server.`(空格+server+点),不会误伤 `from presentation.server.`(那是 `from presentation.`,非 `from server.`);不会误伤 `from my_server.`(无 `server.` 紧跟 from)。同理 `import server\.`。

- [ ] **Step 4: 结构断言 —— 旧 server 路径清零**

Run:
```bash
grep -rn -E "from server[. ]|import server[. ]" --include="*.py" --exclude-dir=.venv310 --exclude-dir=__pycache__ --exclude-dir=node_modules || echo "residual server imports: 0 (OK)"
```
Expected: `residual server imports: 0 (OK)`(全项目无任何 `from server`/`import server` 残留)。

- [ ] **Step 5: 改 4 处入口路径**

Modify(把 `server.main:app` 改为 `presentation.server.main:app`):
- `README.md` §6.1 —— `uvicorn server.main:app --reload` → `uvicorn presentation.server.main:app --reload`
- `scripts/start_dingtalk_bots.md` —— 2 处 `uvicorn server.main:app` → `uvicorn presentation.server.main:app`
- `web/vite.config.ts`(此时已迁至 `presentation/web/vite.config.ts`)—— 注释里的 `uvicorn server.main:app` → `uvicorn presentation.server.main:app`

Run(在 markdown/vite 文件里改入口字符串):
```bash
# README 与 start_dingtalk_bots.md
sed -i -E 's|server\.main:app|presentation.server.main:app|g' README.md scripts/start_dingtalk_bots.md
# vite.config.ts(已迁位)
sed -i -E 's|server\.main:app|presentation.server.main:app|g' presentation/web/vite.config.ts
```

- [ ] **Step 6: import 烟测 —— app 可装配**

Run:
```bash
python -c "from presentation.server.main import app; print('presentation.server.main import OK, routes:', len(app.routes))"
```
Expected: 输出 OK + 路由数(非零)。

- [ ] **Step 7: uvicorn 启动烟测(后台起 + curl /docs + 杀)**

Run:
```bash
# 后台起服务
.venv310/Scripts/python.exe -m uvicorn presentation.server.main:app --port 8765 &
SERVER_PID=$!
sleep 6
# 探活 /docs
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8765/docs
echo ""
# 杀进程
kill $SERVER_PID 2>/dev/null
wait $SERVER_PID 2>/dev/null
echo "server stopped"
```
Expected: curl 返回 `200`(`/docs` 可达)。

> **降级**:若 `.venv310` 路径不对,改用 `python` 或 `python3`;若端口 8765 被占,换一个;若 curl 不在,用 `python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8765/docs').status)"`。

- [ ] **Step 8: 跑全量后端测试**

Run:
```bash
pytest tests/ -q 2>&1 | tail -25
```
Expected: 全绿(或仅基线预存的无关失败)。重点关注是否有 `ModuleNotFoundError: No module named 'server'`——若有,说明 sed 漏改某文件,回 Step 3 补。

- [ ] **Step 9: 前端构建烟测**

Run:
```bash
cd presentation/web && npm run build 2>&1 | tail -10
```
Expected: 构建成功(vite 按端口代理,不依赖后端文件夹路径,前端零逻辑改动)。

- [ ] **Step 10: Commit**

```bash
git add -A
git commit -m "refactor(arch): T7 批4——presentation/ 伞盖收编 web/+server/

把 README §2 的接口层语义落到文件夹:presentation/{web,server}。
git mv 保历史;72 处 from server/import server → from presentation.server
(31 文件,机械改写 + grep 断言清零);4 处 uvicorn 入口路径同步。
前端按端口代理零改动。app 可装配 + /docs 200 + 全量 pytest 绿。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 5: README + 文档对齐

**Files:**
- Modify: `README.md`(§2 架构图重画、§6.1 启动命令已在 Task 4 改、§7 业务模块速览去 /macro/factors)
- Modify: `scripts/start_dingtalk_bots.md`(已在 Task 4 改入口,本 Task 复核)

**Interfaces:**
- Consumes: Task 1-4 全部完成(目录结构已定型)
- Produces: README §2 架构图与磁盘真实目录一致;§7 无已删端点残留。

- [ ] **Step 1: 重画 README §2 架构图**

Modify: `README.md` §2 的 ASCII 架构图(``` ``` 围起来的目录树)。

把现有目录树替换为下面的目标树(反映 presentation/ 伞盖 + http/ 正名 + core/factors/viz 删除 + macro 端点现状):

```
quanter/
├─ 接口层 presentation/         web/+server/ 收编(README §2 语义落地)
│  ├─ web/                      前端 6 视图(CaisenScreen/ParamLab/Dashboard/LiveCockpit/DataLake/Review)
│  └─ server/                   FastAPI 应用
│     ├─ api/v1/                HTTP 路由(caisen·data·macro·review·trading·training·logs + sse)
│     ├─ services/              应用服务(编排用例,聚合各域)
│     ├─ schemas/               请求/响应 DTO
│     ├─ http/                  HTTP 运行时基建(auth/config/_responses,原 server/core/ 正名)
│     └─ main.py                app 装配
│
├─ 执行编排层 trading/          实盘执行引擎 + 状态机(compute/io/orchestrate/state/types + engine)
├─ 网关层 broker/               券商网关抽象(base/mock/qmt/qmt_quote)
├─ 策略层 strategies/           纯叶子(base/registry/signal + neckline/)
├─ 回测副线 backtest/           策略中立回测器(replay/worker/scheduler/optimize/mock_broker)
├─ 发现副线 discovery/          参数发现引擎(Plan 1-4 · L0-L5 闭环)
├─ 实验中心 experiment/         实盘版本配置中心(models/resolver/store)
├─ 数据层
│  ├─ data/                     取数(clients/fetcher/cleaner/lake_reader,只放 .py)
│  ├─ data_lake/                parquet 存储(只放数据,禁放 .py,不入库)
│  └─ config/                   按层拆配置(8 子文件包)
└─ 横切
   ├─ infra/                    通知(notifier)+ LLM(llm/glm)
   └─ broadcast/                行情播报(brief/push/name_resolver)
```

**同步修订 §2 的"依赖铁律"段**:删除含 `factors`、`viz`、`core` 的依赖描述行(如"strategies / factors / viz / infra / experiment / config 为纯叶子"改为"strategies / infra / experiment / config 为纯叶子");把"server 经 services 扇出"改为"presentation.server 经 services 扇出";删除"⚠ strangler 垫片收尾中"的 core 描述行。

- [ ] **Step 2: README §6.1 启动命令复核**

Verify: `README.md` §6.1 的启动命令应为 `uvicorn presentation.server.main:app --reload`(Task 4 已 sed 改过)。Run:
```bash
grep -n "presentation.server.main:app" README.md
```
Expected: 至少 1 处命中。

- [ ] **Step 3: README §6.2 前端启动命令复核**

Verify: `cd web && npm run dev` 需改为 `cd presentation/web && npm run dev`。Run:
```bash
grep -n "cd web" README.md
```
把所有 `cd web` 改为 `cd presentation/web`(§3.2 与 §6.2 等处)。

Run:
```bash
sed -i -E 's|cd web\b|cd presentation/web|g' README.md
```
> ⚠ `\b` 在某些 sed 不支持,若不生效改用 `s|cd web |cd presentation/web |g`(带尾空格)手动复核。

- [ ] **Step 4: README §7 业务模块速览去 macro/factors**

Modify: `README.md` §7 表格。检查是否有 `/macro/factors` 相关行(若有则删);`宏观驾驶舱~~` 那行保留(已标注下线)。§6.2 的 `/dashboard` 视图说明里若提到 factors 端点也一并修订。

Run:
```bash
grep -n "macro/factors\|getMacroFactors\|factors\.atr" README.md || echo "README 无 factors 残留 (OK)"
```
Expected: 无残留。

- [ ] **Step 5: README §2 顶部"架构演进注"补一条**

Modify: `README.md` §1 末尾或 §2 顶部的"架构演进注",补一句反映 T7:

在现有演进注后追加:
```
> **T7 架构治理(2026-07-26)**:core/factors/viz 三相死代码删除;server/core/ 正名 server/http/(消除 core 命名歧义);web/+server/ 收编进 presentation/ 伞盖(README §2 接口层语义落地文件夹)。详见 `docs/superpowers/specs/2026-07-26-arch-t7-subtraction-and-presentation-design.md`。
```

- [ ] **Step 6: 人工对齐复核**

逐条核对 README §2 架构图每一行与磁盘真实目录:
```bash
# 真实顶层目录
ls -d presentation trading broker strategies backtest discovery experiment data data_lake config infra broadcast scripts tests docs 2>/dev/null
# 确认 core/factors/viz 不在
test ! -d core -a ! -d factors -a ! -d viz && echo "dead dirs gone OK"
# 确认 server/http 存在、server/core 不在
test -d presentation/server/http && test ! -d presentation/server/core && echo "http rename OK"
```
Expected: 全部存在 + 两条 OK。

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "docs(arch): T7 批5——README §2 架构图对齐 + 启动命令路径同步

§2 目录树重画:presentation/ 伞盖 + server/http/ 正名 + core/factors/viz
删除。§6.1/§6.2 启动命令改 presentation.server.main:app 与
cd presentation/web。§2 依赖铁律与 §1 架构演进注同步更新 T7 记录。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## 完成判定(全部 Task 后的最终验证)

- [ ] `test ! -d core -a ! -d factors -a ! -d viz`(三相死代码目录消失)
- [ ] `test -d presentation/web -a -d presentation/server/http`(伞盖 + 正名到位)
- [ ] `grep -rn -E "from server[. ]|import server[. ]|from factors|from viz|from core[. ]" --include="*.py" --exclude-dir=.venv310 --exclude-dir=__pycache__` 返回 0 命中(旧路径全清零)
- [ ] `python -c "from presentation.server.main import app"` 通过
- [ ] `pytest tests/ -q` 全绿(或仅与本重构无关的基线预存失败)
- [ ] `cd presentation/web && npm run build` 成功
- [ ] `uvicorn presentation.server.main:app` 能起、`/docs` 返 200
- [ ] README §2 架构图与 `ls` 真实目录逐行一致

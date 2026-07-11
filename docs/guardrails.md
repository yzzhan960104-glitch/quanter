# 全栈验证护栏 SOP

把「后端独木桥」升级为「端口 + 契约 + 后端单测 + 前端类型 + 前端组件/单测 + E2E」多层护栏，防止前后端漂移、
类型回归、鉴权缺口这类**后端测试抓不到**的问题溜进主干。

---

## 一、护栏总览

| 层 | 工具 | 速度 | 何时跑 | 抓什么 |
|---|---|---|---|---|
| 端口一致性 | `scripts/check_ports.py` | 秒级 | 每次 `npm run dev`（predev 自动）+ CI | vite proxy 与后端 API_PORT 漂移（ECONNREFUSED） |
| 前后端契约 | `scripts/check_contracts.py` | 秒级 | CI | `api/*.ts` 调用了后端 openapi 不存在的端点（404/契约漂移） |
| 后端单测 | `pytest tests` | ~30s | CI | 533 项后端逻辑/路由/状态机/鉴权回归 |
| 前端类型 | `npm run typecheck`（vue-tsc） | ~30s | CI | TS 类型回归 |
| 前端组件/单测 | `npm run test`（vitest + @vue/test-utils） | ~3s | CI | facade 契约姿势（URL/method/timeout）+ 组件渲染/emit 回归 |
| E2E | `tests/e2e/*.py`（Playwright） | 分钟级 | 本地手动（首版不入 CI） | 真实浏览器交互、token 注入、渲染白屏 |

---

## 二、一键 fast gate（CI 与本地同源）

```bash
python scripts/run_checks.py
```

串跑端口/契约/后端单测/前端类型/前端单测 5 项，逐项中文报告，任一失败 exit 1。**这是 push 前的标准动作**——
CI 跑的就是这一条，本地过了 CI 必过。

---

## 三、单项跑（定位用）

```bash
python scripts/check_ports.py            # 端口一致性（前端 predev 也自动跑这个）
python scripts/check_contracts.py        # 前后端契约（exit 0=一致，1=漂移，2=解析失败）
python -m pytest tests -q                # 后端全量单测
python -m pytest tests/test_check_contracts.py -v   # 单个测试文件
npm --prefix web run typecheck           # 前端类型检查（vue-tsc --noEmit）
npm --prefix web run test               # 前端组件/单测（vitest：caisen.spec facade 契约 + DatasetTable.spec 组件渲染）
```

契约护栏的纯函数也有单测：`tests/test_check_contracts.py`（15 项）、`tests/test_check_ports.py`。

---

## 四、E2E（Playwright，真实浏览器）

E2E 验证「在页面上」的真实可用性——token 是否在浏览器实际注入、页面是否白屏、交互是否通。
首版只在本地手跑（慢 + 需起前后端服务编排），不入 CI。

**前置**：
- 已 `pip install playwright && python -m playwright install chromium`
- `web/.env.local` 含 `VITE_API_TOKEN=e2e-token`（已被 gitignore 忽略，不入库）

**跑首条 E2E**（蔡森 token 注入 + 链路）：
```bash
# 用 webapp-testing 技能的 with_server.py 起后端 uvicorn + 前端 vite，再跑 E2E
# （WITH_SERVER 替换为 with_server.py 的实际路径；VENV 替换为 .venv310 的 python）
VENV=.venv310/Scripts/python.exe
QUANTER_API_TOKEN=e2e-token python "$WITH_SERVER" \
  --server "$VENV -m uvicorn server.main:app --port 8000" --port 8000 \
  --server "npm --prefix web run dev" --port 5173 --timeout 120 \
  -- "$VENV" tests/e2e/caisen_token_path.py
```

断言三件：`GET /caisen/plans` 带 `Authorization: Bearer` 头、响应非 401、`POST /scan` 同样带 token。
截图留 `tests/e2e/_caisen_e2e.png`。

> **解释器坑**：务必用 `.venv310` 绝对 python 跑 E2E 脚本——`with_server.py` 用 subprocess 时
> 可能绕过 venv 激活落到系统 python（无 playwright）。`run_checks.py` 用 `sys.executable` 已规避此坑。
>
> **Windows 残留坑**：`with_server.py` 在 Windows 上停服时可能 kill 不掉 uvicorn 子进程，残留进程
> 占着 8000 → 下次直接 `uvicorn server.main:app` 会报 `[WinError 10013] 访问套接字权限不允许`。
> 排查清理：
> ```bash
> netstat -ano | findstr ":8000 "                                        # 找占用 PID
> powershell -NoProfile -Command "Stop-Process -Id <PID> -Force"         # 强杀残留
> ```

---

## 五、CI（GitHub Actions）

`.github/workflows/ci.yml`：`push`/`pull_request` 到 master/main 自动触发（也支持网页 `workflow_dispatch` 手动触发）。
ubuntu runner 装 Python 3.10 + Node 20 → `pip install -r requirements.txt` → `npm ci --prefix web` → `python scripts/run_checks.py`。

- 同 ref 重复 push 自动取消旧 run（省额度）；
- fast gate 全过才放行合并；
- E2E 不在首版 CI（后续可加独立 e2e job / nightly）。

---

## 六、开发流程建议

1. **改代码后**：`python scripts/run_checks.py`（约 1min，5 项门禁）。
2. **碰前端交互/鉴权/契约后**：加跑 E2E（第四节）。
3. **push/PR**：CI 自动复跑同一套，本地过则 CI 必过。
4. **加新端点/改 facade**：契约护栏会在 CI 暴露漂移；加新交互则在 E2E 补一条路径。

---

## 七、已知 follow-up（非本次范围）

- 契约护栏目前只比「端点路径 + 方法」，不比响应字段（如 `CandidatePlan` 字段级）——
  字段级契约可走 openapi→TS codegen（更重，下一层）。
- E2E 首条聚焦 token/链路；完整状态机（扫描→审核→激活）需注入 fixture 数据。
- 12 个既有 warning（`pct_change` FutureWarning、`emt_gateway.py` coroutine never awaited）非阻断，
  建议择机清理（后者是真实潜在 bug）。

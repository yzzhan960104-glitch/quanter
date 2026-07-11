# 端口一致性护栏（Port Consistency Guard）— 设计稿

- 日期：2026-07-11
- 状态：已批准（待实现）
- 关联事件：`vite.config.ts` proxy target 被手改为 `localhost:8001`，与后端 `--port 8000` 错位，前端 `/api/v1/caisen/plans` 报 `ECONNREFUSED`。

## 1. 背景与问题

前后端端口约定**没有任何机器校验**：

- 后端端口仅在 `server/main.py` docstring 与 README 文字描述，实际由 `uvicorn --port` 命令行决定。
- 前端代理端口硬编码在 `web/vite.config.ts` 的 `proxy['/api'].target`。

两侧各写各的，改一边不会报错，漂移只能靠运行时 `ECONNREFUSED` 暴露——反馈滞后、定位成本高（本次即手误改成 8001，静默存活到前端首次请求才炸）。

## 2. 目标 / 非目标

**目标**

- 让「前端代理端口 ≠ 后端实际端口」的漂移在 `npm run dev` 启动时被**前置拦截**，并以中文错误指明两处真值。
- 后端端口收敛到单一真相源（`server/core/config.py` 的 `API_PORT`），可被环境变量覆盖。

**非目标（YAGNI）**

- 不校验前端 dev 端口（5173）与 `CORS_ORIGINS` 白名单的联动（见 §7 Future）。
- 不做前后端端口「物理单源共享文件」（跨语言 TS↔Python 共享复杂度高于收益，双写 + 比对已足够拦截漂移）。
- 不改 CORS / 不引入配置框架。

## 3. 设计

### 3.1 文件改动

| 文件 | 改动 | 性质 |
|---|---|---|
| `scripts/check_ports.py` | preflight 校验脚本，零依赖纯正则 | 🆕 新增 |
| `server/core/config.py` | 加 `API_HOST` / `API_PORT` 常量，`os.getenv` 可覆盖 | 改动（沿用 `LOG_LEVEL` 同款） |
| `server/main.py` | 加 `if __name__ == "__main__": uvicorn.run(...)`，端口读 config | 改动（补 `__main__` 块） |
| `web/package.json` | 加 `"predev": "python ../scripts/check_ports.py"` | 改动（npm 自动钩子） |

### 3.2 数据流

```
npm run dev
 → predev 自动先跑: python ../scripts/check_ports.py
    backend_port = os.getenv("API_PORT") or 正则提取 config.py 的 API_PORT 默认值
    vite_port    = 正则提取 vite.config.ts 的 proxy target 端口
    backend_port != vite_port → 打印中文错误 + sys.exit(1)
    相等                          → 静默放行（exit 0）
 → vite dev server 启动
```

### 3.3 关键决策

1. **零依赖正则提取，不 `import server.core.config`** —— 避免 import 链拉起 fastapi/uvicorn 重依赖，脚本启动快、CI 友好、不会因 venv 未装全而崩。两处正则：
   - config 侧：`API_PORT\s*=\s*int\(os\.getenv\(["']API_PORT["']\s*,\s*["'](\d+)["']\)`
   - vite 侧：`target:\s*['"]https?://[^:/]+:(\d+)`（兼容 `localhost` 与 `127.0.0.1`）
2. **环境变量覆盖诚实化** —— 真相端口优先取 `os.getenv("API_PORT")`，故 env 覆盖同样进入校验。错误信息会提示「若以 env 覆盖了端口，须同步 `vite.config.ts`」，不假装覆盖所有路径。
3. **`predev` 的 python 解释器走 PATH** —— 不硬编码 `.venv310` 路径，保跨机器/CI 通用；代价是要求开发者先激活 venv 再 `npm run dev`（README 注明）。CI 另配显式解释器。
4. **失败硬度 `sys.exit(1)`** —— 强制阻断 `npm run dev`，不留静默漂移。

## 4. preflight 脚本契约（`scripts/check_ports.py`）

- 入口：`python scripts/check_ports.py`
- 输入：无参。脚本以自身文件位置锚定项目根（`Path(__file__).resolve().parent.parent`），定位 `server/core/config.py` 与 `web/vite.config.ts`。
- 输出：
  - 一致：静默，`exit 0`。
  - 不一致：stderr 打印中文错误（含 backend_port、vite_port 两值与修复指引），`exit 1`。
  - 任一文件缺失或正则无匹配：stderr 打印「无法解析端口（文件/格式异常）」，`exit 2`（与「不一致」区分，便于排错）。
- 依赖：仅标准库（`os` / `sys` / `re` / `pathlib`）。

## 5. 测试策略

`tests/test_check_ports.py`（pytest）：

- **一致用例**：构造临时 config.py（`API_PORT=8000`）+ vite.config.ts（`target: 'http://127.0.0.1:8000'`），脚本 `exit 0`。
- **不一致用例**：vite 侧改 `:8001`，脚本 `exit 1` 且 stderr 含中文提示。
- **正则兼容性**：vite target 分别用 `localhost` / `127.0.0.1`、单引号 / 双引号，端口都能抽准。
- **解析失败用例**：config.py 缺 `API_PORT` 行 → `exit 2`。

脚本通过 `if __name__ == "__main__"` 暴露可被 `subprocess.run` 调用的 CLI；测试用 `tmp_path` 造隔离 fixtures，不碰真实仓库文件。

## 6. 落地验证清单

- [ ] `python scripts/check_ports.py` 当前仓库（已修为 8000）→ `exit 0`。
- [ ] 手动把 vite.config.ts 临时改回 8001 → `exit 1` + 中文错误；改回。
- [ ] `cd web && npm run dev` → predev 自动触发，通过后 vite 起来。
- [ ] `pytest tests/test_check_ports.py` 全绿。
- [ ] `python -m server.main` 能起（验证 `__main__` 块读 config 端口）。

## 7. Future（不在本次范围）

- 前端 dev 端口（5173）与 `CORS_ORIGINS` 白名单的联动校验——同型漂移风险，可在下一护栏迭代里加，复用本脚本的正则 + 比对骨架。
- 把护栏挂进 CI（`.github/workflows`）与 pre-commit，兜底「直接 vite / 单起后端」的漏网路径。

# bridge→dws 统一迁移 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: 用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现。步骤用 `- [ ]` 复选框跟踪。

**Goal:** 把 bridge 对话机器人（自研 dingtalk-stream + ClaudePool + Alarmer）迁移到 dws dev connect，退役 `bridge/` ~1000 行 + dingtalk-stream SDK，两个机器人统一一套 dws 接入逻辑。

**Architecture:** bridge 机器人 → `dws dev connect --channel claudecode`（@→Claude Code 续聊 + 审批闸事前确认替代 Alarmer 事后告警）。审查机器人保持 `dws dev connect --agent-cmd bridge脚本→/review`（已就绪）。`bridge/` 全删，training_dingtalk 死代码删，dingtalk-stream 依赖移除。

**Tech Stack:** dws CLI（Go 二进制，已装 v1.0.52）、Claude Code（--channel claudecode）、Python 3.10（uvicorn + training loop）、pytest。

## Global Constraints

- **语言红线**：所有对话/注释/文档/commit 全中文（CLAUDE.md）。
- **凭证红线**：绝不硬编码 token/secret，dws 用 `--robot-client-id`/`--unified-app-id`（运行时取凭证，不入 argv）。
- **dingtalk-stream SDK 移除前提**：bridge/ + training_dingtalk 死代码是唯二用户，删净后才能移除依赖（webhook 推走 urllib，不用 SDK）。
- **strangler 垫片**：`caisen/training_dingtalk.py`（17 行）+ `caisen/training_loops_db.py`（16 行）是 `sys.modules` 别名垫片，转发到 `caisen/optimize/`；改真实实体在 `optimize/`，垫片保留（除非整体退役）。
- **DingTalkNotifier 保留**：training loop 推报告走 webhook（urllib），不用 dingtalk-stream SDK，不删。
- **dws 工作目录坑**：`--agent-cmd` 用相对路径时 dws cwd 找不到（已踩），必须绝对路径或 `--agent-workdir`。

---

## Task 1: 实测 dws dev connect 能力（定 fallback 走向）

**目标：** 用 bridge 应用起 `dws dev connect --channel claudecode` demo，实测 spec §5 三个待定点（审批闸粒度 / ClaudePool 崩溃恢复 / 进度推送），结论写入 progress 文件，决定后续直接用 dws 还是 fallback wrapper。

**Files:**
- Create: `.superpowers/sdd/progress.md`（记录实测结论，供后续任务读）

- [ ] **Step 1: 确认 bridge 应用 dingyyzdjpl6… 类型 + dws 接入凭证方式**

查 `~/.claude/settings.json` 或 `.env` 的 `DINGTALK_APP_KEY`。在钉钉开放平台确认 `dingyyzdjpl6tojlz2mn` 是老式应用还是统一应用（有无 App ID UUID）。
- 老式 → Task 2 走 `dws dev app robot submit` 建号拿 robot 凭证
- 统一应用 → 直接 `--unified-app-id`

把结论（应用类型 + 凭证方式）记到 `.superpowers/sdd/progress.md`。

- [ ] **Step 2: 起 dws dev connect --channel claudecode demo（bridge 应用）**

据 Step 1 凭证方式起：
```bash
# 老应用（robot 凭证，Step 1 建号后）
dws dev connect --robot-client-id <id> --robot-client-secret <sec> --channel claudecode --agent-memory --allowed-users <DINGTALK_ALLOWED_STAFF_IDS>
# 或统一应用
dws dev connect --unified-app-id <id> --channel claudecode --agent-memory --allowed-users <DINGTALK_ALLOWED_STAFF_IDS>
```
后台起（run_in_background），读日志确认 `stream connect success`。

- [ ] **Step 3: 实测审批闸粒度（核心）**

起带审批闸的版本：
```bash
dws dev connect <凭证> --channel claudecode --agent-memory \
  --approval-card-template <模板id> --owner-user-id <owner> --agent-approval-mode ask
```
（模板 id 在钉钉开放平台·本应用·AI 卡片设置获取；无模板则跳过 --approval-card-template 只测 --agent-approval-mode ask）
在钉钉 @bridge 机器人发"列一下当前目录文件"，观察：
- claude 调工具（如 LS/Bash）时，是否推确认卡片？是每个工具调用都确认，还是只高危？
- 把"审批闸粒度"结论（全确认 / 可配置高危 / 无确认）记 progress

- [ ] **Step 4: 实测 ClaudePool 崩溃恢复等价（--agent-memory + --alwayson）**

在钉钉 @bridge 机器人多轮对话（第1轮"记住我叫测试"，第2轮"我叫什么"），验 `--agent-memory` 续聊。
然后 Ctrl-C 停 dws dev connect，重起，再 @bridge"我叫什么"，验崩溃后上下文是否恢复（dws 内部 session 持久化）。
把"ClaudePool 崩溃恢复覆盖度"结论记 progress。

- [ ] **Step 5: 实测进度推送**

在钉钉 @bridge 机器人发一个复杂问题（如"读 README.md 并总结"），观察 dws 是否推"思考中 Ns / 工具 M 次"实时进度，还是只有 thinking/done表态。
把"进度推送覆盖度"结论记 progress。

- [ ] **Step 6: 据实测结论定 fallback 决策（记 progress）**

在 `.superpowers/sdd/progress.md` 写明：
- 审批闸：直接用 dws（粒度够）/ 需要 fallback wrapper（全确认太烦）
- ClaudePool：dws 覆盖够 / 需要 wrapper 补 session 持久化
- 进度推送：接受 dws 默认 / 需要 wrapper
→ 决定 Task 3 是否做、做什么。

- [ ] **Step 7: 停 demo + commit progress**

停 Step 2/3 的 dev connect。`git add .superpowers/sdd/progress.md && git commit -m "chore(dws-migration): Task1 实测 dws 能力结论（审批闸/ClaudePool/进度推）"`。

---

## Task 2: bridge 应用 dws 凭证就绪

**目标：** bridge 机器人能用 dws dev connect 接入（凭证齐）。

**Files:**
- Modify: `.env`（补 dws 凭证；.gitignore 已忽略）

- [ ] **Step 1: 据 Task 1 Step 1 结论准备凭证**

- 老应用：`dws dev app robot submit --app-key dingyyzdjpl6tojlz2mn --app-secret <DINGTALK_APP_SECRET>` 建号 → `dws dev app robot result` 轮询拿 robot clientId/clientSecret
- 统一应用：在开放平台拿 unified-app-id

- [ ] **Step 2: 凭证落 .env（dws 不读 .env，但记录便于脚本用）**

`.env` 补（不打印值）：
```
BRIDGE_DWS_ROBOT_CLIENT_ID=<robot clientId 或留空用 unified>
BRIDGE_DWS_ROBOT_CLIENT_SECRET=<robot clientSecret>
BRIDGE_DWS_UNIFIED_APP_ID=<若统一应用>
```

- [ ] **Step 3: 验证 dws dev connect 能连 bridge 机器人**

起 `dws dev connect <据凭证> --channel claudecode`，读日志确认 `stream connect success`，@bridge 机器人发"测试"确认 Claude Code 回复。

- [ ] **Step 4: 停 demo（不 commit，.env 不入库）**

---

## Task 3:（条件）审批闸 fallback wrapper —— 仅当 Task 1 Step 6 决定需要

**前置：读 `.superpowers/sdd/progress.md`，仅当"审批闸 fallback 需要"才做本任务；否则跳过。**

**目标：** dws 审批闸全确认太烦时，用 `--agent-cmd` wrapper 内部跑 Claude Code + 只对 `_DANGER_PATTERNS` 命中工具触发确认。

**Files:**
- Create: `scripts/dingtalk_claude_wrapper.py`（dws agent-cmd 桥：收问题 → 起 claude --print + 流式监听 tool_use → 高危命中暂停等钉钉确认）
- Test: `tests/test_dingtalk_claude_wrapper.py`

**Interfaces:**
- Consumes: `bridge/alarmer.py:_DANGER_PATTERNS`（移植，bridge 删前先抄出来）+ Claude Code CLI
- Produces: `scripts/dingtalk_claude_wrapper.py`（stdin/argv 收问题，stdout 回复；高危工具命中写确认信号到约定文件供 dws 审批闸读）

- [ ] **Step 1: 抄出 _DANGER_PATTERNS（bridge 删前）**

从 `bridge/alarmer.py` 抄 `_DANGER_PATTERNS`（5 类正则：实盘/凭证/破坏/外传/下单）到 wrapper 顶部常量。

- [ ] **Step 2: 写失败测试（_DANGER_PATTERNS 命中判定）**

`tests/test_dingtalk_claude_wrapper.py`：
```python
from scripts.dingtalk_claude_wrapper import is_dangerous_tool_use
def test_danger_trading_path():
    assert is_dangerous_tool_use("Bash", {"command": "cat trading/emt_gateway.py"})
def test_danger_env():
    assert is_dangerous_tool_use("Read", {"file_path": "/proj/.env"})
def test_safe_read():
    assert not is_dangerous_tool_use("Read", {"file_path": "/proj/README.md"})
```

- [ ] **Step 3: 跑测试确认失败**

`pytest tests/test_dingtalk_claude_wrapper.py -x` → FAIL（模块不存在）

- [ ] **Step 4: 实现 wrapper（is_dangerous_tool_use + claude 调用骨架）**

`scripts/dingtalk_claude_wrapper.py`：移植 `_DANGER_PATTERNS`，实现 `is_dangerous_tool_use(tool_name, tool_input) -> bool`。claude 调用 + 流式高危拦截的主体若复杂，先实现 `is_dangerous_tool_use` 让测试过，claude 调用部分用 `subprocess` 起 `claude --print` + 解析 stream-json（参考 bridge/claude_pool.py 的 _spawn 思路）。

- [ ] **Step 5: 跑测试确认通过**

`pytest tests/test_dingtalk_claude_wrapper.py -v` → PASS

- [ ] **Step 6: Commit**

`git add scripts/dingtalk_claude_wrapper.py tests/test_dingtalk_claude_wrapper.py && git commit -m "feat(dws-migration): Task3 审批闸 fallback wrapper(高危工具确认)"`

---

## Task 4: 删 optimize/training_dingtalk.py 死代码 + main.py review_bot 装配块

**目标：** 审查机器人 @接收已改 dws 桥，删老 stream 收审核死代码（ReviewChatbotHandler/start_review_bot/_run_stream/import dingtalk_stream），保留 DingTalkNotifier。

**Files:**
- Modify: `caisen/optimize/training_dingtalk.py`（删 line 174-288：`# 4. ReviewChatbotHandler` 到文件末尾的 start_review_bot；删 line 178-179 `import dingtalk_stream` + `from dingtalk_stream import ...`；保留 line 1-170 ReviewBotConfig/DingTalkNotifier/_NoopNotifier）
- Modify: `server/main.py`（lifespan 的 `start_review_bot` 调用块：删 `if _review_cfg is not None: app.state.review_bot_task = start_review_bot(...)` 分支 + shutdown 的 `_rbtask.cancel()`；保留 DingTalkNotifier 装配）
- Modify: `tests/caisen/test_training_dingtalk.py`（删 ReviewChatbotHandler/start_review_bot 相关测试，保留 DingTalkNotifier 推送测试）

- [ ] **Step 1: 读 optimize/training_dingtalk.py 确认删改边界**

`Read caisen/optimize/training_dingtalk.py` 确认 line 173-288 是 `# 4. ReviewChatbotHandler` 到末尾，line 178-179 是 dingtalk_stream import。

- [ ] **Step 2: 删 optimize/training_dingtalk.py 死代码**

删 line 174 起到文件末尾（ReviewChatbotHandler/_run_stream/start_review_bot）+ line 178-179 import。保留 line 1-170（含 _NoopNotifier，line 163-170）。改完文件应以 _NoopNotifier 收尾。顶部 docstring 若提及"stream 收审核"也更新为"webhook 推报告（@接收改 dws 桥）"。

- [ ] **Step 3: 删 main.py lifespan 的 review_bot_stream 装配 + shutdown**

`Read server/main.py` 找 lifespan 的：
- startup：`app.state.review_bot_task = start_review_bot(...)` 那个 `if _review_cfg is not None:` 分支（含 else 的 info 日志）→ 删（保留 `app.state.training_orchestrator = TrainingLoopOrchestrator(_notifier)` + `start_daemon()`）
- startup import：`from caisen.training_dingtalk import (... start_review_bot ...)` 去掉 `start_review_bot`
- shutdown：`_rbtask = getattr(app.state, "review_bot_task", None); if _rbtask: _rbtask.cancel()` → 删

- [ ] **Step 4: 删 test_training_dingtalk.py 的 ReviewChatbotHandler/start_review_bot 测试**

`Read tests/caisen/test_training_dingtalk.py`，删 `ReviewChatbotHandler`/`start_review_bot`/`_run_stream` 相关测试函数（保留 DingTalkNotifier push/token 缓存测试）。删 `test_chatbot_handler_*` 类。

- [ ] **Step 5: 跑 training 全套测试确认绿**

`pytest tests/caisen/test_training_dingtalk.py tests/caisen/test_training_loop.py tests/test_training_api.py -q`
Expected: PASS（DingTalkNotifier + /review 端点 + loop 状态机不受影响）。

- [ ] **Step 6: 启动 smoke（lifespan 装配不阻断）**

`python -c "from server.main import app; print([r.path for r in app.routes if '/training' in getattr(r,'path','')])"`
Expected: 打印 training 路由 + 无异常（review_bot_stream 不再装配，DingTalkNotifier 还在）。

- [ ] **Step 7: Commit**

`git add caisen/optimize/training_dingtalk.py server/main.py tests/caisen/test_training_dingtalk.py && git commit -m "refactor(dws-migration): Task4 删审查机器人老stream死代码(@接收改dws桥)"`

---

## Task 5: 删 bridge/ + tests/bridge/

**目标：** bridge 自研全退役（前提：Task 2/3 已验证 dws dev connect 能接管 bridge 机器人）。

**Files:**
- Delete: `bridge/`（__init__/__main__/alarmer/claude_events/config/replier/safety/session_store/claude_pool/stream_client.py，10 文件）
- Delete: `tests/bridge/`（__init__ + 9 测试）
- Delete: `scripts/dingtalk_claude_bridge.py`（bridge 入口包装，若有）

- [ ] **Step 1: 确认 bridge/ 无外部引用（除 tests/bridge/）**

`grep -rn "from bridge\|import bridge" --include=*.py .` 排除 tests/bridge/，确认无生产代码引用（training_dingtalk Task 4 已删死代码，应无引用）。

- [ ] **Step 2: 删 bridge/ + tests/bridge/ + 入口脚本**

```bash
git rm -r bridge/ tests/bridge/
git rm -f scripts/dingtalk_claude_bridge.py 2>/dev/null || true
```

- [ ] **Step 3: 跑全套测试确认无残留引用**

`pytest tests/ -q --ignore=tests/bridge`
Expected: 全绿（bridge 测试已删，其余不引用 bridge）。

- [ ] **Step 4: Commit**

`git commit -m "refactor(dws-migration): Task5 退役 bridge/ 自研(dws dev connect 接管)"`

---

## Task 6: 移除 dingtalk-stream SDK 依赖

**目标：** bridge + training_dingtalk 死代码是唯二用户，删净后移除依赖。

**Files:**
- Modify: `requirements.txt`（删 line 90 `dingtalk-stream>=0.20.0`）

- [ ] **Step 1: 确认全项目无 dingtalk_stream import**

`grep -rn "dingtalk_stream\|dingtalk-stream" --include=*.py .`
Expected: 无匹配（Task 4/5 已删净）。若有残留，先删。

- [ ] **Step 2: 删 requirements.txt 的 dingtalk-stream**

删 `requirements.txt:90` `dingtalk-stream>=0.20.0`。

- [ ] **Step 3: 验证服务起得来（dingtalk-stream 不在也不报错）**

`python -c "from server.main import app; print('OK')"`
Expected: OK（无 ImportError）。

- [ ] **Step 4: Commit**

`git add requirements.txt && git commit -m "chore(dws-migration): Task6 移除 dingtalk-stream SDK 依赖"`（注：.venv310 里包还在，不重装不影响；下次重建 venv 生效）

---

## Task 7: 启动编排 + 文档

**目标：** 两个 dws dev connect 常驻 + uvicorn 的启动方式固化（脚本或文档）。

**Files:**
- Create: `scripts/start_dingtalk_bots.md`（启动说明，两 dev connect + uvicorn 命令）
- Modify: `CLAUDE.md` 或 `docs/`（钉钉桥章节更新为 dws）

- [ ] **Step 1: 写启动说明**

`scripts/start_dingtalk_bots.md`：
```markdown
# 钉钉机器人启动（dws 统一接入）

## 1. bridge 对话机器人（yzzhan量化）
dws dev connect <据Task2凭证> --channel claudecode --agent-memory \
  --allowed-users <DINGTALK_ALLOWED_STAFF_IDS> \
  [--approval-card-template <id> --owner-user-id <id> --agent-approval-mode ask]  # 据Task1结论
  # 或 Task3 fallback: --agent-cmd "<venv>/python.exe scripts/dingtalk_claude_wrapper.py"

## 2. 审查训练机器人（yzzhan参数优化）
dws dev connect --unified-app-id e2695383-6fe9-4617-9439-2a8538af3107 \
  --channel custom --agent-cmd "<venv>/python.exe scripts/dingtalk_review_bridge.py"

## 3. uvicorn 服务（training loop + webhook 推 + /review 端点）
<venv>/python.exe -m uvicorn server.main:app --host 127.0.0.1 --port 8000
```

- [ ] **Step 2: 更新 CLAUDE.md / docs 钉钉桥章节**

把"钉钉桥 bridge/ 自研"描述更新为"dws dev connect 统一接入"。

- [ ] **Step 3: Commit**

`git add scripts/start_dingtalk_bots.md CLAUDE.md && git commit -m "docs(dws-migration): Task7 钉钉机器人 dws 启动编排说明"`

---

## Task 8: 真实 @冒烟（两机器人都通）

**目标：** 端到端验证迁移后 bridge 对话 + 审查训练两机器人 @闭环都通。

- [ ] **Step 1: 起 uvicorn + 两 dev connect（据 Task 7 说明）**

三个后台进程：uvicorn + bridge dev connect + 审查 dev connect。

- [ ] **Step 2: bridge 对话机器人 @冒烟**

@yzzhan量化 发"现在几点"，确认 Claude Code 回复 + 审批闸（若配）行为符合 Task 1 结论。

- [ ] **Step 3: 审查训练机器人 @闭环冒烟**

POST /training/start 提交小 universe loop（max_rounds=1）→ 等回测→AI→AWAITING_REVIEW → @yzzhan参数优化 发"停" → 确认 dev connect→bridge→/review→CONFIRMING → "确认"→STOPPED。复用 commit 54b4c6c 验证过的链路。

- [ ] **Step 4: 记录冒烟结论 + Commit（若有改动）**

`.superpowers/sdd/progress.md` 记两机器人冒烟结果。若有 fix，commit。

---

## Self-Review

**Spec coverage:** spec §1架构→Task1/2/7；§2退役→Task4/5/6；§5待实测→Task1；§6 fallback→Task3；§7实施顺序→Task1-8。✓
**Placeholder:** 启动命令的 `<凭证>`/`<模板id>` 是 Task1/2 实测后填的具体值（非 plan 占位），已标注来源。✓
**Type consistency:** ReviewBotConfig/DingTalkNotifier/_NoopNotifier 跨任务保留；_DANGER_PATTERNS Task3 从 alarmer 抄。✓

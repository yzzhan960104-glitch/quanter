# 钉钉桥 dws 统一迁移设计（bridge 退役 → dws dev connect）

> **日期**：2026-07-16
> **状态**：设计（待 writing-plans 出实现计划）
> **关联**：训练 loop 闭环实测通过（commit `54b4c6c`）；审查机器人已于 2026-07-16 改 dws 桥（统一应用老 SDK 不兼容）

---

## 1. 背景

项目里两个钉钉机器人，两套接入逻辑：

| 机器人 | 应用 | 应用类型 | 现接入方式 |
|---|---|---|---|
| **bridge 对话**（yzzhan量化） | `dingyyzdjpl6tojlz2mn` | 老式企业内部应用 | 自研 `dingtalk-stream` SDK + `ClaudePool` + `Alarmer` + `safety` + 审计 + 频控（`bridge/` 9 模块 ~1000 行） |
| **审查训练**（yzzhan参数优化） | `dingbabujxcelmssmdpn` | 统一应用（App ID UUID 铁证） | `dws dev connect` + `scripts/dingtalk_review_bridge.py` → `POST /api/v1/training/review`（2026-07-16 改） |

**根因（2026-07-16 实测）**：审查应用是统一应用，老 `dingtalk-stream` SDK 的 `ChatbotHandler`/`ChatbotMessage.TOPIC` 代际不兼容（stream 连得上但 @不推，只收 SYSTEM ping）。bridge 老应用用老 SDK 兼容（能收 @）。

## 2. 目标 / 非目标

**目标**：两个机器人统一到 **dws dev connect**（一套接入逻辑），**退役 bridge 自研**（`bridge/` ~1000 行 + `dingtalk-stream` SDK 依赖），减负。Alarmer 事后告警换 **dws 审批闸事前确认**（安全范式升级，用户 2026-07-16 拍板）。

**非目标**：
- 审查机器人 @桥（已 dws，不动）
- training loop 业务逻辑（不动）
- `DingTalkNotifier`（webhook 推报告，保留）

## 3. 现状 bridge 定制（迁移要替代的）

| 定制 | 物理意图 | 量化场景价值 |
|---|---|---|
| **Alarmer** | claude 调工具命中 `_DANGER_PATTERNS`（实盘 `trading/`/`emt_gateway`/`qmt_`/`xtquant`、凭证 `.env`、破坏 `rm`/`git push`/`git reset`/`--force`、外传 `curl/wget/scp/nc`、下单 `place_order` 等）→ 实时推钉钉告警 | bypassPermissions 全放行下，实盘/凭证误操作不可逆，事后审计变事中知情 |
| **ClaudePool** | 每会话一进程 + 崩溃 `--resume` 重建（session_id 落盘）+ 空闲回收 | 多会话并发 + 长会话上下文不丢 + 资源控制 |
| **进度推送** | claude 思考节流 15s 推「思考中 Ns · 工具 M 次 · 输出 X 字」 | 长任务用户知情 |
| **上游错误诚实化** | claude 返回实为 529/429/503 → 改发诚实失败 | 不让用户误判为 claude 结论 |
| safety 白名单 / 频控 / 审计 jsonl / replier 清洗 / `/new` `/status` `/help` | 身份闸 + 防刷 + 追溯 + 钉钉 Markdown 清洗 + 会话指令 | 通用 |

## 4. 设计

### §1 架构 + 组件映射

**新数据流**（bridge 机器人 yzzhan量化）：
```
@yzzhan量化(钉钉)
  → dws dev connect 收@（--robot-client-id dingyyzdjpl6… 建号 或 升级统一应用 --unified-app-id）
  → Claude Code（--channel claudecode --agent-memory 续聊，替代 ClaudePool）
  → 审批闸（--approval-card-template + --agent-approval-mode ask，替代 Alarmer：事前确认）
  → 回复钉钉
  + 审计 --audit-sheet（替代 jsonl）+ dws 内置频控 20条/分（替代 bridge 频控）
```

**组件映射**：
| bridge 功能 | dws 替代 | 置信度 |
|---|---|---|
| stream_client @接收 | dws dev connect 收@ | ✓ 已实测（审查机器人验证） |
| ClaudePool 续聊 | `--agent-memory` | △ 崩溃 `--resume`/session 持久化待实测 |
| **Alarmer 高危告警** | **审批闸（事前确认）** | ⚠️ 粒度待实测（全确认太烦 → fallback: `--agent-cmd` wrapper 只拦高危） |
| safety 白名单 | `--allowed-users` / `--allowed-groups` | ✓ |
| 频控 | dws 内置（20条/分/人） | ✓ |
| 审计 jsonl | `--audit-sheet` | ✓ |
| replier 清洗 | dws 内置 markdown 清洗 | ✓ |
| 进度推送 | dws thinking/done 表态 | △ 实时进度数待实测 |
| `/new` `/status` `/help` | dws 内置 `/new` `/clear` | ✓（`/status` `/help` 无等价，可接受） |

### §2 退役清单 + 启动方式 + 测试策略

**退役清单**：
- `bridge/` **全删**（`stream_client.py`/`claude_pool.py`/`safety.py`/`alarmer.py`/`replier.py`/`session_store.py`/`claude_events.py`/`config.py`/`__main__.py`/`__init__.py`）+ `tests/bridge/` 全删
- `caisen/training_dingtalk.py` **死代码删**：`ReviewChatbotHandler` + `start_review_bot` + `_run_stream`（@接收已改 dws 桥，不再用）；**保留** `DingTalkNotifier`/`_NoopNotifier`/`ReviewBotConfig`（webhook 推报告 + 凭证装配仍用）
- `server/main.py` lifespan 的 `review_bot_stream` 装配块删（`start_review_bot` 不再调，`DingTalkNotifier` 保留）
- **`dingtalk-stream` SDK 从 `requirements.txt` 移除**——bridge + training_dingtalk 是唯二用户，都退役/改 dws 后无人用（webhook 推走 urllib）
- 审批闸 fallback（若粒度不够）→ 新增 `scripts/dingtalk_claude_wrapper.py`（`--agent-cmd` 用，内部跑 Claude Code + 沿用 Alarmer `_DANGER_PATTERNS` 只拦高危触发确认）

**启动方式**（两个 dws dev connect 常驻 + uvicorn）：
```bash
# bridge 对话机器人（常驻）—— bridge 老应用凭证两种方式（plan 阶段实测定）：
#   A. --robot-client-id/--robot-client-secret（dws dev app robot submit 建号拿 robot 凭证）
#   B. 升级 bridge 应用为统一应用 → --unified-app-id <新 unified id>
dws dev connect [--robot-client-id <id> --robot-client-secret <sec> | --unified-app-id <id>] \
  --channel claudecode --agent-memory --allowed-users <DINGTALK_ALLOWED_STAFF_IDS> \
  --approval-card-template <模板id> --owner-user-id <owner> --audit-sheet <axls表>

# 审查训练机器人（现状保留）
dws dev connect --unified-app-id e2695383-6fe9-4617-9439-2a8538af3107 \
  --channel custom --agent-cmd "<venv>/python.exe scripts/dingtalk_review_bridge.py"

# uvicorn 服务不变（training loop + webhook 推 + /review 端点）
```
> 启动编排（两 dev connect + uvicorn）：plan 阶段定（scripts/start_bots 脚本或文档）。

**测试策略**：
- `tests/bridge/` 删（bridge 退役）
- `tests/caisen/test_training_dingtalk.py`：`ReviewChatbotHandler`/`start_review_bot` 相关测试删；`DingTalkNotifier` 推送测试保留
- `tests/test_training_api.py`（含 `/review` 端点）保留
- dws dev connect 是外部 Go 工具，不写 Python 单测，靠实测（@→dws→Claude Code / @→dws→bridge→/review）
- fallback wrapper（若用）写单测：`_DANGER_PATTERNS` 命中/不命中

## 5. 待实测（writing-plans 阶段收敛）

1. **审批闸粒度**：`--agent-approval-mode ask` + `--approval-card-template` 是「每个 agent 操作都确认」还是可配置「只高危」？→ 决定直接用 dws 审批闸，还是走 fallback `--agent-cmd` wrapper（只拦 `_DANGER_PATTERNS`）。
2. **ClaudePool 崩溃恢复**：dws `--agent-memory` + `--alwayson` 是否覆盖 session_id 持久化 + 崩溃后续上下文（bridge 的 `--resume <sid>` 链）？dws 内部会话存储粒度？
3. **进度推送**：dws thinking/done 表态能否给用户「思考中 Ns / 工具 M 次 / 输出 X 字」的实时进度？或接受 dws 默认表态。

## 6. 风险 + fallback

| 风险 | fallback |
|---|---|
| dws 审批闸全确认（每步确认太烦） | `--agent-cmd scripts/dingtalk_claude_wrapper.py`，wrapper 内部跑 Claude Code + 只对 `_DANGER_PATTERNS` 命中工具触发确认卡片 |
| dws 崩溃恢复/session 持久化不够 | 同 wrapper 补 session 落盘 + `--resume`（移植 `session_store.py` 思路） |
| 凭证：bridge 老应用 dws 建号失败 | 升级 bridge 应用为统一应用，用 `--unified-app-id` |
| dws dev connect 本身 bug/不稳 | bridge 暂留（不删），双轨过渡，dws 稳定后再退役 |

## 7. 实施顺序（plan 细化）

1. **实测 dws 能力**（§5 三点）：用 bridge 应用起 `dws dev connect --channel claudecode` demo，验审批闸/续聊/崩溃恢复
2. 据 §5 结果定：直接用 dws 审批闸 vs fallback wrapper
3. bridge 机器人切 dws dev connect（新旧并存验证）
4. 删 `bridge/` + `tests/bridge/` + training_dingtalk 死代码 + main.py 装配块
5. 移除 `dingtalk-stream` 依赖
6. 全套测试绿 + 真实 @冒烟（bridge 对话 + 审查训练）

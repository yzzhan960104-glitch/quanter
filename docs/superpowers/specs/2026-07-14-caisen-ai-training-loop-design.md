# Spec 3 · 蔡森 AI 人审训练 Loop 设计（参数训练平台 · Spec 3）

> 2026-07-14 · brainstorm 阶段产出 · 待用户审阅

## 1. 背景与定位

参数训练平台 4-spec（详见项目记忆 `quanter-param-training-platform`）：Spec1 回测异步化 ✅、Spec2 Parameter Lab 前端 ✅ 已实现。**Spec 3 = 人审闭环训练**（重新定位：原「单次 AI 分析」价值有限，扩成含闭环的人审训练）。

**终态动线**：提交训练任务 → 自动连续跑 回测→AI分析→钉钉推报告→你手机审核→据审核调参续跑下一轮 → ……N 轮或你喊停。

**Spec 3 vs Spec 4 分界**：Spec 3 每轮卡一个**人审关卡**（钉钉 AWAITING_REVIEW 阻塞等你审核）；Spec 4 是把该关卡去掉的全自动 loop。Spec 3 的人审天然兜底 AI 幻觉（命门 3），Spec 4 才需硬护栏。

## 2. 设计决策（brainstorm Q&A 结论）

| 决策点 | 选定 | 理由 |
|---|---|---|
| 定位 | 人审闭环训练（非单次 AI 分析） | 单次分析价值低（≈/review 换数据源），闭环价值在 loop |
| 钉钉机器人 | 真单独机器人（新建第二个企业内部应用，独立 app_key/app_secret + 独立 stream） | 职责隔离；同 app_key 不能两个独立 stream，故新建应用 |
| 机器人专门性 | 专门审核，所有 @它消息=审核；别的用途走 bridge 机器人 | 不在同机器人路由分流，简单 |
| 审核通道 | 钉钉回复（stream 双向） | webhook 单向收不到审核；要双向必须 stream |
| loop 编排 | caisen 后台自动连续 + 每轮 AWAITING_REVIEW 阻塞等钉钉审核 | 「几天闭环训练」=自动连续，但每轮人审关卡防 AI 乱跑 |
| 交互动线 | 方案 B：AI 自然语言报告 + 你自由文本审核 + GLM 解析 + 回显确认 | 自由表达不想记指令格式；自由文本必须 GLM 解析 + 回显防误解析 |
| 终止条件 | N 轮上限（默认 20）+ 你随时钉钉「停」；不做达标自动停 | 达标阈值难定义、易锁过拟合 cfg，YAGNI |
| cfg 演进 | 累积：上一轮 cfg + 你这轮审核改动 = 下一轮 cfg；可「重置」回基准 | 「训练」=每轮基于上轮改进 |
| AI 输入 | 带历史几轮 cfg→统计摘要（不带完整 trades）给 GLM 看趋势 | 看趋势比只看当前一轮更准；完整 trades 撑爆 context |
| 部署形态 | 寄生 caisen 服务进程（uvicorn lifespan 起 stream），非独立守护进程 | 合「零守护进程」哲学；async stream 可寄生 uvicorn loop |

## 3. 架构（全寄生 uvicorn）

```
uvicorn 进程（caisen 服务）
 ├─ [Spec1 已有] 回测 ProcessPoolExecutor(max_workers=1) + 调度器（每轮 loop 复用跑回测）
 ├─ [Spec3 新] 参数审查机器人（dingtalk-stream ChatbotHandler，独立 app_key/app_secret，寄生 loop）
 │    ├─ 主动推：loop 到 AWAITING_REVIEW → 把 AI 报告 Markdown 推你钉钉
 │    └─ 收审核：你 @机器人 回复 → 唤醒 loop（CONFIRMING：GLM 解析 + 回显）
 ├─ [Spec3 新] loop 编排器（daemon 线程，跑训练状态机，concurrency=1 同时只一个活跃 loop）
 └─ [Spec3 新] AI 分析/解析服务（复用 review_service._call_glm，零新依赖）
      ├─ 分析：ReplayReport + 当前 cfg + 历史几轮统计 → GLM → 自然语言报告
      └─ 解析：你的审核文本 + 当前 cfg → GLM → {cfg_override, action: rerun/stop/reset}
```

## 4. loop 状态机

```
IDLE
  └─ POST /training/start(初始cfg+区间+max_rounds) ──> RUNNING(round=1)

RUNNING        复用 Spec1 提交一个 replay task(当前cfg, 区间)，等其终态
  ├─ replay SUCCESS ──> ANALYZING
  ├─ replay FAILED/CANCELLED ──> AWAITING_REVIEW(告知你本轮回测失败，问是否重试/改/停)
  └─ 你 stop ──> STOPPED

ANALYZING      GLM 分析(报告 + 当前cfg + 历史统计) → 报告
  └─ ──> AWAITING_REVIEW(钉钉推报告，等你回复)

AWAITING_REVIEW  阻塞等你 @机器人 回复审核（自然语言）
  ├─ 你回复 ──> CONFIRMING
  └─ 超时(可配，如 24h 无回复) ──> STOPPED(标注「等你审核超时」)

CONFIRMING     GLM 解析你的审核文本 → {cfg_override, action}；回显「上轮cfg→本轮改动→本轮cfg+动作」问你确认
  ├─ 你「确认」 + action=rerun + round<max ──> RUNNING(round+1, 累积cfg)
  ├─ 你「确认」 + action=reset ──> RUNNING(round+1, cfg=基准)
  ├─ 你「确认」 + action=stop 或 round≥max ──> STOPPED
  ├─ 你「不对，重新说」──> AWAITING_REVIEW(重新等你审核)
  └─ GLM 解析失败/非法 ──> 回显报错，回 AWAITING_REVIEW 重等

STOPPED        loop 结束（终态）
```

## 5. 存储模型（SQLite，复用 Spec1 的 `data/replay_tasks.db`）

新建 `training_loops` 表（loop 级状态）：
```sql
CREATE TABLE training_loops (
  loop_id        TEXT PRIMARY KEY,
  created_at     TEXT NOT NULL,
  status         TEXT NOT NULL,        -- IDLE/RUNNING/ANALYZING/AWAITING_REVIEW/CONFIRMING/STOPPED
  current_round  INTEGER DEFAULT 0,
  max_rounds     INTEGER NOT NULL,
  start          TEXT, end TEXT,
  universe_json  TEXT,
  base_cfg_json  TEXT,                  -- 基准 cfg（重置用，= 提交时的初始 cfg）
  current_cfg_json TEXT,                -- 当前轮生效 cfg（累积演进）
  history_json   TEXT,                  -- [{round, cfg, n_hits, win_rate, avg_rr, max_dd, annualized}] 历史每轮统计摘要
  pending_review TEXT,                  -- AWAITING_REVIEW/CONFIRMING 时缓存的待确认信息（回显草稿等）
  error          TEXT,
  started_at TEXT, finished_at TEXT
);
CREATE INDEX idx_loops_status ON training_loops(status);
```
- 每轮回测 = Spec1 `replay_tasks` 的一行（loop 编排器提交、轮询、读 report）。`training_loops.history_json` 只存**统计摘要**（不带 trades），喂 GLM 做趋势分析。
- 重启恢复：uvicorn 起来 `UPDATE training_loops SET status='STOPPED', error='进程重启中断' WHERE status IN ('RUNNING','ANALYZING')`（不自动续跑，你决定重提，同 Spec1 语义）。

## 6. AI 分析与解析（复用 `review_service._call_glm`，零新依赖）

### 6.1 分析 prompt（喂历史看趋势）
输入：当前轮 `ReplayReport`（统计 + trades 头部样本）+ 当前 cfg + `history_json`（前几轮 cfg→统计摘要）。
输出：自然语言 Markdown 报告（表现评估 / 问题诊断 / 调参建议，含具体数值方向但自由文本）。
新建 `caisen/training_analyzer.py`：`analyze_round(report, cfg, history) -> str`（组装 prompt + 调 `_call_glm` + 降级，仿 `review_service.diagnose` 三级降级）。

### 6.2 解析 prompt（你的审核文本 → 结构化）
输入：你的审核文本（如「min_rr 提到 2.0，止损放宽点，重跑」）+ 当前 cfg + 33 字段 schema（字段名+值域，防幻觉改非法字段）。
输出：JSON `{cfg_override: {field: value}, action: "rerun"|"stop"|"reset"}`。
`caisen/training_analyzer.py`：`parse_review(text, cfg) -> dict`。

### 6.3 值域护栏 + 回显确认
- `parse_review` 输出的 `cfg_override` 经 `StrategyConfig.model_copy(update=...)` 校验（非法字段/超 ge/le 抛 ValidationError → 回显报错回 AWAITING_REVIEW）。
- 回显：CONFIRMING 时把「上轮 cfg → 本轮改动字段 → 本轮完整 cfg + 动作」格式化推钉钉，你回「确认」才执行。

### 6.4 历史 context 控制
`history_json` 每轮只存统计摘要（~7 字段），N 轮历史 ≈ N×百字符，远小于 GLM context 上限。当前轮才喂完整 report（trades 截断头部样本，复用 `review_service._MAX_CSV_CHARS` 思路）。

## 7. 钉钉参数审查机器人

- **独立企业内部应用**：用户在钉钉开放平台新建第二个应用，拿独立 `REVIEW_APP_KEY`/`REVIEW_APP_SECRET`（环境变量，绝不硬编码）。与 bridge 的 `DINGTALK_APP_KEY` 物理隔离。
- **寄生 uvicorn**：lifespan 起 `dingtalk-stream` ChatbotHandler 作为 background async task（独立 stream 连接，独立 app 凭证）；shutdown 优雅断开。复用 `bridge/stream_client.py` 的连接范式（但不复用 bridge 进程）。
- **专门审核**：所有 @此机器人的消息都按当前活跃 loop 的审核处理（不路由分流）。同一时刻只有一个活跃 loop（concurrency=1）。
- **白名单**：复用 `bridge/safety.py` 思路——只有白名单 staffId 能驱动（环境变量 `REVIEW_ALLOWED_STAFF_IDS`），防他人触发训练消耗算力。
- **消息收发**：主动推（报告/回显）用 stream 机器人主动发消息 API；接收审核用 ChatbotHandler 回调。Markdown 清洗复用 `bridge/replier.py` 的 `clean_markdown_for_dingtalk`（钉钉 Markdown 限制多）。

## 8. API 端点

| 端点 | 方法 | 作用 |
|---|---|---|
| `/training/start` | POST | 提交训练 `{start,end,universe,base_cfg_override,max_rounds}` → loop_id |
| `/training/{loop_id}` | GET | loop 状态 + 历史轮次摘要 |
| `/training/{loop_id}/stop` | POST | 停止 loop |
| `/training` | GET | loop 列表（降序） |

钉钉审核回调：机器人收到 @消息 → 内部唤醒 loop 编排器（不走外部 HTTP，进程内调用）。

## 9. 边界与错误处理
- **GLM 解析误解析**：回显确认是兜底（你看一眼回显再确认），+ 值域护栏（model_copy 校验）。
- **GLM 不可用**（缺凭证/调用失败）：分析/解析走降级（仿 review_service），分析降级→推「AI 不可用，附原始统计让你手动判」；解析降级→回「没能理解，请按 `改 字段=值 重跑` 格式」。
- **钉钉 stream 断线**：robot 自动重连（dingtalk-stream 内置）；断线期间 loop 卡在 AWAITING_REVIEW（不影响回测已落盘数据）。
- **回测 FAILED**：不直接停 loop，进 AWAITING_REVIEW 告知你本轮失败、问是否重试/改/停。
- **审核超时**：AWAITING_REVIEW 超 24h（可配）无回复 → STOPPED 标注。
- **并发 loop**：concurrency=1，提交第二个 loop 时若已有活跃 loop → 拒绝（422）。一次只一个活跃 loop（避免回测 worker 争用 + 钉钉审核混乱）。

## 10. 测试策略
- loop 状态机全路径：IDLE→RUNNING→ANALYZING→AWAITING_REVIEW→CONFIRMING→RUNNING/STOPPED。
- `analyze_round` / `parse_review`：mock `_call_glm`（不真调），断言 prompt 组装 + 降级。
- 回显确认：parse → 回显 → 确认 → 下一轮 cfg 累积正确。
- 值域护栏：非法字段/超域 cfg_override 被拒。
- 历史喂入：history_json 增长 + 喂 GLM 的 prompt 含历史。
- 重启恢复：RUNNING/ANALYZING 残留 → STOPPED。
- 钉钉消息流：mock ChatbotHandler，断言推报告/收回显/唤醒 loop。

## 11. 已确认决策（用户 2026-07-14 拍板）
见 §2 全表。N 轮上限默认 **20**（审 spec 可改）。初始 cfg = 提交时 `base_cfg_override`（缺省用 `StrategyConfig` 默认）。回测区间每轮固定（提交时定 start/end，每轮同区间不同 cfg）。

## 12. 待用户提供
- 第二个企业内部应用的 `REVIEW_APP_KEY` / `REVIEW_APP_SECRET`（钉钉开放平台新建）。
- `REVIEW_ALLOWED_STAFF_IDS`（你的 staffId 白名单）。

---

状态：待用户审阅 → 认可后 commit → 转 writing-plans 分解实现任务。

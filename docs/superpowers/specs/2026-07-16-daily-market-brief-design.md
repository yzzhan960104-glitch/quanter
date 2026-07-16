# 每日行情播报机器人设计（daily market brief）

> **日期**：2026-07-16
> **状态**：设计（待用户认可 → 转 writing-plans 出实现计划）
> **关联**：钉钉出站通道 `infra/notifier.py`、数据湖 `data/lake_reader.py`、daemon 先例 `execution/replay_scheduler.py`
> **分支（待建）**：`feat/daily-market-brief`

---

## 1. 背景

每日 A 股收盘后，研究员第二天才能开电脑复盘。本功能在 **每日 19:00** 自动把当日行情摘要（大盘 / 板块 / 资金 / 龙虎榜）拼成一条钉钉 Markdown，推到群里——手机端一眼看完昨日全景，不用开终端。

时机选 19:00 的物理意图：A 股 15:00 收盘，Tushare 日终批量通常 16:00–18:00 落盘，19:00 触发能吃到**已稳定落盘的全量当日数据**，避开收盘瞬间数据半成品窗口（今天实测 `moneyflow/ths_daily/mkt_daily` 一批同步失败停在 07-14，正是日终未稳的反例）。

## 2. 目标 / 非目标

**目标**
- 每日 19:00（可配置）取 `data_lake` 行情 → 纯 pandas 聚合 → 模板拼 Markdown → 推钉钉群。
- **纯函数 + CLI** 为核心：`build_daily_brief(date) -> str` 可单测，`python -m broadcast` 可手动跑 + 外部调度器触发。
- 数据陈旧 / 湖缺失 → 优雅降级文案，绝不抛异常中断。
- **幂等去重**：同一交易日不重复播报（周末/节假日天然不触发）。

**非目标（YAGNI）**
- 不做 LLM 文案生成（纯模板拼装，可复现、零幻觉、零成本；LLM 解读留后期增强）。
- 不接蔡森形态信号（默认参数全市场 0 命中，`caisen/engines/config.py:103` 注释明说被拦；Phase 2 再接）。
- 不做交互式查询 / 回答 / SSE 推送（这是一条单向播报管道，不是对话机器人）。
- 不引任何新依赖（pandas / 标准库 urllib 已够）。

## 3. 现状基线（能复用 / 缺失）

### 3.1 能复用（已实测）

| 能力 | 现成件 | 证据 |
|---|---|---|
| 多湖查询 | `DataLakeReader.get_instance()` 单例，`get_cross_section(date, lake=)` / `get_timeseries(symbol,start,end,lake=)` | `data/lake_reader.py:243` / `:264` |
| 行情数据 | 5 湖已落湖（见 §3.3），MultiIndex(date,symbol) | `data_lake/*.parquet` |
| 钉钉加签 + errcode 校验 | `DingTalkChannel._sign` / `_validate_response`（静态方法，已验证可靠） | `infra/notifier.py:205` / `:216` |
| 钉钉 Markdown 清洗 | `clean_markdown_for_dingtalk`（剥 `<font>`/表格/`---`） | `caisen/optimize/training_dingtalk.py:70` |
| daemon 线程先例 | `ReplayScheduler._loop`（`Event.wait` + 异常吞掉续跑） | `execution/replay_scheduler.py:92` |

### 3.2 缺失（需新建）

1. **文案生成器** `build_daily_brief(date) -> str`：取数 + 聚合 + 渲染，无现成件。
2. **播报出站函数** `push_brief(title, markdown)`：subprocess 调 `dws chat message send-by-bot`（应用机器人出站，原生 Markdown + 自由 `--title`，绕开 `DingTalkChannel.send` 写死的「风控告警」壳）。
3. **幂等去重**：`logs/.last_broadcast` 记上次播报日期。
4. **触发层**：CLI 入口 + 外部调度（见 §5.4）。

### 3.3 数据湖实测 schema（2026-07-16 跑 parquet 确认）

| 湖 | 列 | 索引 | 最新日期 | 播报用途 |
|---|---|---|---|---|
| `index_daily` | ts_code/open/high/low/close/vol/amount | MultiIndex(date,symbol) | **07-15** ✅ | 大盘 8 宽基收盘 + 涨跌幅（**无 pct，近 2 日 close 现算**） |
| `ths_daily` | close/high/low/open/**pct_change**/pre_close/vol | MultiIndex(date,symbol) | 07-14 ⚠️ | 板块 Top/Bottom（**pct 直接排序**） |
| `moneyflow` | buy_sm/sell_sm/buy_elg/sell_elg_amount/**net_mf_amount** | MultiIndex(date,symbol) | 07-14 ⚠️ | 主力净流入榜（net_mf_amount 排序） |
| `dragon_list` | **仅 `hit`** | MultiIndex(date,symbol) | **07-15** ✅ | 龙虎榜（**只能列上榜标的代码，无原因/金额**） |
| `mkt_daily` | ts_name/com_count/total_mv/float_mv/pe/exchange | MultiIndex(date,symbol) | 07-14 ⚠️ | 市场宽度（总市值/PE，可选） |

> 龙虎榜字段浅（仅 hit）是硬约束——播报龙虎榜只能给"今日上榜 N 只：000001/300xxx/..."，无上榜原因与净买入明细。明细需 Phase 2 接 Tushare `top_list`/`top_inst` 全字段接口（当前落湖的是裁剪版）。

## 4. 已确认决策

| # | 决策点 | 选定 | 理由 |
|---|---|---|---|
| 1 | 触发时刻 | **每日 19:00** | 用户拍板；收盘后日终数据已稳定落盘，顺带缓解数据新鲜度风险 |
| 2 | 触发方式 | **CLI `python -m broadcast` + 外部调度（schtasks 推荐）** | "每日一次"用常驻 daemon 线程是浪费；CLI 可手动跑/可被任意调度器调；daemon 寄生 uvicorn 列为可选 fallback（§5.4） |
| 3 | 内容 MVP | 大盘 8 宽基 + 板块 Top5/Bottom5；主力净流入 Top5 + 龙虎榜为可选节（数据在则报） | 数据新鲜度分层：index/龙虎榜新鲜是核心，板块/资金停 07-14 是降级容忍区 |
| 4 | 文案生成 | 纯模板拼装（f-string + 列表），不上 LLM | 可复现 / 零幻觉 / 零成本 / 可单测；符合「显式实现、拒绝黑盒」 |
| 5 | 出站通道 | **dws `chat message send-by-bot`**（应用机器人：`--robot-code`+`--group`+`--title`+`--text`，原生 Markdown） | 零自写加签——dws 全权处理凭证/加签/errcode；原生 Markdown+自由标题，绕开 `DingTalkChannel` 写死的「风控告警」壳 |
| 6 | 幂等去重 | `logs/.last_broadcast` 存上次播报日期，同日不重发 | 周末/节假日 `index_daily` 不更新天然跳过；多次触发不重复推送，比"维护交易日历表"极简 |
| 7 | 分支 | `feat/daily-market-brief`（从 master） | — |
| 8 | 部署自动化 | **dws 全自动**：`dev app robot submit` 建专用播报机器人 + `chat group members add-bot` 拉进 yzzhan量化群 + robot-code 写 .env | 用户要求（2026-07-16）；dws 全链一等支持；自定义群 webhook 机器人创建 dws 未封装，故选应用机器人路线（非 webhook） |
| 9 | 播报机器人 | **新建专用「每日行情播报」应用机器人**（A 方案） | 用户 2026-07-16 拍板；职责隔离，与 yzzhanCli通用(对话) / yzzhan参数优化(训练人审) 三机器人各司其职；不复用现有机器人避免身份混淆 |

> 决策 2 修正了 brainstorm 阶段"寄生 uvicorn daemon 线程"的初判——`ReplayScheduler` 的复杂度（Manager/Pool/Queue）是为"持续 poll 任务流"服务的，对"每日定点一次"是过度设计。CLI + schtasks 更贴合。
>
> 决策 5/8 同理演进：原计划自写 `push_dingtalk`（urllib+加签）走自定义 webhook 机器人，但 webhook 机器人的**创建** dws 未封装、无法自动化；改走**应用机器人**——`dev app robot submit` 全自动建号 + `group members add-bot` 全自动拉群 + `send-by-bot` 全自动出站，三段 dws 一等支持，出站零自写网络代码。

## 5. 设计

### §5.1 管道架构（单向、扁平、无框架）

```
[外部调度 19:00] python -m broadcast
      │
      ├─ 取数   DataLakeReader.get_cross_section(date, lake=...)   ← 纯读 parquet，无网络
      │         index_daily / ths_daily / moneyflow / dragon_list
      │
      ├─ 分析   纯 pandas：pct_change 现算 / sort_values 取 Top N / groupby 计数
      │
      ├─ 渲染   build_daily_brief(date) → Markdown 字符串（模板 f-string）
      │         clean_markdown_for_dingtalk 清洗钉钉不支持的语法
      │
      ├─ 去重   读 logs/.last_broadcast：若 == date → 跳过（已播）
      │
      └─ 推送   push_dingtalk(title, markdown)
                DingTalkChannel._sign 加签 → urllib POST → _validate_response 校验 errcode
                → 写 logs/.last_broadcast = date
```

### §5.2 模块落点：新 `broadcast/` 包（平级 caisen/backtest/execution）

```
broadcast/
├─ __init__.py
├─ brief.py      # build_daily_brief(date, *, reader=None) -> BriefResult  核心·纯函数·可单测
├─ push.py       # push_brief(title, markdown, *, robot_code, group_id, dry_run=False) -> bool  subprocess 调 dws send-by-bot 出站
└─ __main__.py   # CLI：--date / --dry-run / --force（忽略去重）  装配 + 去重 + 调 brief+push
```

**分层铁律**：`brief.py` 只读 `DataLakeReader` + 算 pandas，**零 IO 副作用**（不碰网络/不写文件），返回纯数据结构 + Markdown 字符串，可单测；`push.py` 只管投递；`__main__.py` 是唯一编排点（去重/装配/异常兜底）。三者各司其职，brief 永不 import push。

### §5.3 出站 + 部署自动化（dws 应用机器人，全链零自写网络代码）

**出站**：应用机器人进群后，每日推送直接 subprocess 调 dws（dws 全权处理 OAuth 凭证/加签/errcode 校验，播报代码零网络逻辑）：

```python
# broadcast/push.py
import subprocess

def push_brief(title: str, markdown: str, *, robot_code: str, group_id: str,
               dry_run: bool = False) -> bool:
    """调 dws send-by-bot 推一条 Markdown 到群。dws 处理凭证/加签/errcode。"""
    cmd = ["dws", "chat", "message", "send-by-bot",
           "--robot-code", robot_code, "--group", group_id,
           "--title", title, "--text", markdown, "-y"]
    if dry_run:
        print(markdown); return True          # 样例审阅：只打印不发
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        logger.error("dws send-by-bot 失败: %s", r.stderr); return False
    return True
```

凭证：`robot_code` ← `.env` `DINGTALK_CHAT_ROBOT_CODE`；`group_id` ← `.env` `BROADCAST_GROUP_ID`（yzzhan量化群 openConversationId `ciduznBwLLiWKcMewBOF4+kWQ==`，2026-07-16 已实测拿到）。**不再需要 `DINGTALK_WEBHOOK/SECRET`**（那是自定义 webhook 机器人的；应用机器人走 robot-code）。

> 为什么不用 `infra/notifier.py` 的 `DingTalkChannel`：① `_render_markdown`（`notifier.py:293`）标题写死「风控告警」；② 它是 webhook 机器人通道，与"自动化建机器人"不同源。应用机器人出站用 dws `send-by-bot` 更原生，且部署+出站同走 dws。

**部署自动化**（一次性，`scripts/setup_broadcast_bot.sh`）——dws 全链一等支持：

```bash
# 0. 前置：dws auth login（浏览器授权，仅一次）
# 1. 建专用应用「每日行情播报」；已有可跳过
dws dev app create --name "每日行情播报" ...
# 2. 配机器人能力 + 异步建机器人 → 轮询 result 拿 robot-code
dws dev app robot submit  --app-key <key> --name "行情播报" ...
dws dev app robot result  --task-id <tid>
# 3. 拉进 yzzhan量化群（add-bot）
dws chat group members add-bot --id "ciduznBwLLiWKcMewBOF4+kWQ==" --robot-code <code>
# 4. 凭证写 .env
DINGTALK_CHAT_ROBOT_CODE=<code>
BROADCAST_GROUP_ID=ciduznBwLLiWKcMewBOF4+kWQ==
```

> 三段命令（`dev app robot submit` / `group members add-bot` / `send-by-bot`）均为 dws 一等子命令，2026-07-16 查 help 实测存在。`submit` 异步、支持失败重试；`add-bot` 是 `chat group members` 子命令。setup 脚本幂等（机器人已存在则跳过）。**部署是 SDD 的 Task 0**（先于 brief/push 编码，因为要拿 robot-code 验证出站）。

### §5.4 触发层：CLI + 外部调度（推荐 schtasks）

**CLI（必做，核心）**：
```bash
# 正常播（带去重）
python -m broadcast

# 指定日期 + 只打印不发（样例审阅）
python -m broadcast --date 2026-07-15 --dry-run

# 强制重发（忽略去重，调试用）
python -m broadcast --force
```

**外部调度（推荐 Windows 任务计划）**：
```bash
# 每日 19:00 跑（系统级，不依赖任何常驻 Python 进程）
schtasks /Create /SC DAILY /TN "QuanterDailyBrief" /TR "<venv310>\python.exe -m broadcast" /ST 19:00
```
- 优点：机器开机即触发，不需要 uvicorn 24h 常驻；崩溃不影响交易主进程。
- daemon 寄生 uvicorn（仿 `ReplayScheduler`）列为**可选 fallback**：若用户已在跑常驻 uvicorn 且不想配 schtasks，可在 `server/main.py` lifespan 起一个 `BriefScheduler` daemon 线程。MVP 不做，YAGNI。

### §5.5 数据查询（落到真实列名）

| 内容 | 查询 | 计算 |
|---|---|---|
| 大盘 8 宽基收盘+涨跌幅 | `reader.get_cross_section(date, lake="index_daily")` | 涨跌幅 = 取每指数近 2 日 close：`get_timeseries(sym, d-1, d).close.pct_change().iloc[-1]`（index_daily 无 pct 列） |
| 板块 Top5/Bottom5 | `reader.get_cross_section(date, lake="ths_daily")` | `df.sort_values("pct_change")` 取首尾 5（ths_daily 自带 pct_change） |
| 主力净流入 Top5 | `reader.get_cross_section(date, lake="moneyflow")` | `df.sort_values("net_mf_amount", ascending=False).head(5)` |
| 龙虎榜上榜标的 | `reader.get_cross_section(date, lake="dragon_list")` | `df[df["hit"]==1].index`（仅代码列表，无明细） |

**缺数据降级**：任一 `get_cross_section` 返空 DF（湖缺失或该日无数据，`lake_reader.py:252` 离线降级契约）→ 该节渲染「（数据未落湖，跳过）」，其余节照常拼。文案末尾标注实际数据截止日。

### §5.6 文案模板（钉钉 Markdown 子集：#/列表/粗体/引用，禁表格/`<font>`/`---`）

```markdown
### 📈 Quanter · 每日行情播报
> 2026-07-15（周二）收盘

**大盘宽基**
- 沪深300：3856.21 ▲1.23%
- 上证指数：3128.45 ▲0.87%
- 创业板指：2105.33 ▼0.42%
- 中证1000 / 上证50 / 科创50 / 深证成指 / 中证500 ...

**板块涨幅榜（同花顺概念）**
- 🔺 Top：CPO概念 +5.2% / 华为汽车 +4.1% / ...
- 🔻 Bottom：白酒 -2.3% / 房地产 -1.8% / ...

**主力资金净流入 Top5**
- 平安银行 +3.2亿 / ... （或：数据未落湖，跳过）

**龙虎榜**
- 今日上榜 12 只：000001 / 300xxx / ...

> 数据来源 Tushare data_lake · 数据截至 2026-07-15 · 下次播报明日 19:00
```

### §5.7 幂等去重（核心鲁棒性机制）

`logs/.last_broadcast` 存上次成功播报的 date 字符串。`__main__` 流程：
1. `date = reader.get_cross_section(..., lake="index_daily")` 的最新日期（即"最近交易日"，天然跳过周末/节假日）。
2. 读 `.last_broadcast`；若 `== date` 且非 `--force` → 日志「今日已播，跳过」并退出。
3. 推送成功 → 写 `.last_broadcast = date`。

> 比"维护 A 股交易日历表"极简得多：`index_daily` 有数据的日期就是交易日，最新日期即应播日。周末 daemon/schtasks 醒来，`index_daily` 最新日期仍是周五，`.last_broadcast` 已是周五 → 自动跳过，不发废报。幂等：多次触发不重复。

## 6. 风险 + fallback + 边界拷问（量化风控三连）

| 场景 | 处置 |
|---|---|
| **数据整体滞后**（如今天 moneyflow/ths_daily 停 07-14） | index_daily 最新日 = 应播日；其余湖若停在更早 → 该节降级「（数据截至 07-14）」；**绝不拿昨天的板块榜冒充今天的** |
| **index_daily 本身未更新**（19:00 日终同步还没跑） | 应播日 < 自然今天 → 文案标注「数据尚未落湖，最新 07-14」+ 正常推（让用户知道数据滞后，而非机器人哑了） |
| **湖完全缺失**（开发机无 parquet） | `DataLakeReader.get_cross_section` 返空 DF（离线降级契约）→ 全节降级，推一条「数据湖未就绪」 |
| **推送失败**（dws send-by-bot returncode≠0 / errcode≠0 / 频控） | `push_brief` 返 False → `__main__` 捕获记 `logs/broadcast.log`；**不写 `.last_broadcast`**（下次触发重试） |
| **dws 未登录 / auth 过期** | setup 前 `dws auth login`（浏览器授权，仅一次）；运行期过期 → send-by-bot 失败记日志，不影响交易主进程 |
| **网络超时** | urllib timeout 10s → 同上，不写 last_broadcast，下次重试 |
| **周末/节假日触发** | index_daily 最新日不变 == last_broadcast → 自动跳过，零废报 |
| **--force 重发** | 跳过去重门，强制推（调试/补播用）；正常调度不用 |
| **时区** | `datetime.now()` Windows 本地时区（东八）；schtask `/ST 19:00` 本地时间；不涉跨时区机器 |
| **NaN 守护** | 涨跌幅 pct_change 首日 NaN → 取 `.iloc[-1]` 前判 `pd.notna`，NaN 渲染「—」；除以零（pre_close=0）用 `try/except` 兜 |
| **Markdown 注入** | 板块/标的名称含 `*`/`#`/`>` 等会破坏 Markdown 结构 → 经 `clean_markdown_for_dingtalk` 清洗 |

## 7. 测试策略（E2E 为金标准，记忆 `default-e2e-after-ui`）

- **brief 纯函数单测**（`tests/test_broadcast_brief.py`）：
  - mock `DataLakeReader`（注入伪造截面 DF）→ 断言 Markdown 含预期指数行/板块行/涨跌幅符号 ▲▼。
  - 缺数据降级：某湖返空 DF → 该节含「数据未落湖」，其余节正常，**不抛异常**。
  - NaN 守护：注入含 NaN 的 close → 渲染「—」不崩。
- **push 单测**：monkeypatch `DingTalkChannel._post`（脱网）→ 断言 payload title/markdown 正确、errcode!=0 抛 RuntimeError、凭证缺失返 False 不抛。
- **去重单测**：写 `.last_broadcast=today` → 二次调用跳过；`--force` 不跳过。
- **CLI 冒烟（手动）**：`python -m broadcast --date 2026-07-15 --dry-run` → 打印样例文案贴群审阅（**不发真钉钉**），文案过关再去掉 `--dry-run`。
- **真钉钉 E2E**（文案审过后）：`python -m broadcast --force`（小范围/测试群）→ 验证群里收到卡片 + errcode=0。
- **既有 pytest 全套**：本次纯新增 `broadcast/` 包 + 测试，不动 server/caisen 核心，应保持全绿（零回归）。

## 8. 未确认项（plan 阶段实测收敛）

1. **板块 symbol → 中文名映射**：`ths_daily` 的 symbol 是同花顺概念代码（如 `885538`），播报需可读中文名（"CPO概念"）。需确认 `ths_daily` 是否含名称列（schema 只有 close/open/pct_change/pre_close/vol，**无名称**）→ 需从 `sector.parquet`（申万）或另建 `ths_code→name` 映射表。**这是文案可读性的关键缺口，plan 必须先解**。
2. **指数 symbol → 中文名**：同理，`index_daily` symbol（如 `000300.SH`）需映射"沪深300"。可能复用 `config/registry.py` 已有的标的元信息，plan 阶段查。
3. **`moneyflow` 的 symbol 也是代码**：净流入榜需标的中文名，同上映射问题。
4. **触发方式最终选定**：schtasks vs 寄生 uvicorn daemon——先按 schtasks 落 CLI，用户实测调度便利性后再定要不要补 daemon。
5. **应用机器人进群权限**：`send-by-bot` 推群消息是否需额外 `data-auth` / `chmod` 授权？plan 阶段 `--dry-run` 后小范围真发验证。

---

状态：待用户认可 → commit → 转 writing-plans 分解 Task（建议：**Task0 dws 建专用播报机器人 + 拉群 setup 脚本** / Task1 brief 纯函数+单测 / Task2 push(dws send-by-bot)+去重 / Task3 CLI+schtasks 文档 / Task4 真钉钉 E2E）。

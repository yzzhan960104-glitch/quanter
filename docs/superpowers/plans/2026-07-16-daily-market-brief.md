# 每日行情播报机器人 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: 用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现。步骤用 `- [ ]` 复选框跟踪。

**Goal:** 每日 19:00 自动取 `data_lake` 行情 → 纯 pandas 聚合 → 模板 Markdown → `dws send-by-bot` 推 yzzhan量化群。新建扁平 `broadcast/` 包 + dws 全自动建专用播报机器人（Task 0 前置）。

**Architecture:** 纯函数 `build_daily_brief(date)→BriefResult`（读 `DataLakeReader` 截面 + pandas，零 IO 副作用）+ `push_brief`（subprocess 调 `dws chat message send-by-bot`，零自写加签）+ `__main__`（CLI + `logs/.last_broadcast` 幂等去重 + 异常兜底）。部署：`dws dev app robot submit` 建机器人 + `group members add-bot` 拉群。触发：CLI + Windows `schtasks` 19:00。

**Tech Stack:** Python 3.10（`.venv310`）、pandas、dws CLI（已装 `~/.local/bin/dws`）、pytest。

**Spec:** [`docs/superpowers/specs/2026-07-16-daily-market-brief-design.md`](../specs/2026-07-16-daily-market-brief-design.md)

## Global Constraints

- **语言红线**：所有对话/注释/文档/commit 全中文（CLAUDE.md）。
- **凭证红线**：`robot-code` 走 `.env` 的 `DINGTALK_CHAT_ROBOT_CODE`，**绝不硬编码**；`.env` 已 .gitignore。
- **纯函数铁律**：`brief.py` 零 IO 副作用（不碰网络/不写文件），只读 `DataLakeReader` + 算 pandas，注入式可单测。
- **出站零自写加签**：`push.py` 只 subprocess 调 dws，**不写 urllib/HMAC/errcode 校验**（dws 全权处理）。
- **数据真相源**：`DataLakeReader.get_cross_section(date, lake=)`（MultiIndex 湖），离线/缺数据返空 DF 不抛（`lake_reader.py:252` 契约）。
- **幂等**：`logs/.last_broadcast` 存上次播报 date，同日不重发（除非 `--force`）；周末/节假日 `index_daily` 最新日不变 → 天然跳过。
- **零回归**：本次纯新增 `broadcast/` 包 + 测试，不动 server/caisen 核心；每 Task 收尾跑相关测试。

---

## Task 0: dws 建专用播报机器人 + 拉群（前置，拿 robot-code 验出站）

**目标：** 用 dws 全自动建「每日行情播报」应用机器人，拉进 yzzhan量化群，拿 robot-code 写 `.env`，实测 `send-by-bot` 出站通。**本 Task 是后续所有编码的前置**（要先有 robot-code 才能验出站）。

**Files:**
- Create: `scripts/setup_broadcast_bot.md`（部署说明，幂等：机器人已存在则跳过）
- Modify: `.env`（补 `DINGTALK_CHAT_ROBOT_CODE` + `BROADCAST_GROUP_ID`；.gitignore 已忽略）

- [ ] **Step 1: 确认 dws 已登录**

`dws auth whoami`（或 `dws doctor`）。未登录则 `dws auth login`（浏览器授权，仅一次）。把"已登录 profile"结论记 `progress.md`。

- [ ] **Step 2: 建专用应用「每日行情播报」**

```bash
dws dev app create --name "每日行情播报"   # 拿 appKey/appSecret；若已有同名应用则复用
```
查 `dws dev app list` 确认。结论（应用类型 + appKey）记 `progress.md`。

- [ ] **Step 3: 建机器人 → 轮询拿 robot-code**

```bash
dws dev app robot submit --app-key <key> --name "行情播报" ...   # 异步建号，拿 task-id
dws dev app robot result --task-id <tid>                         # 轮询到 SUCCESS，拿 robot-code
```
（`submit` 异步、支持失败重试；`robot-code` 即 `DINGTALK_CHAT_ROBOT_CODE`。）

- [ ] **Step 4: 拉进 yzzhan量化群（add-bot）+ 实测进群权限**

```bash
dws chat group members add-bot --id "ciduznBwLLiWKcMewBOF4+kWQ==" --robot-code <code>
```
Expected: 成功加入。**若报权限错**（spec §8 #5）→ `dws chat data-auth` / `dws chat chmod` 授权后重试，把"进群是否需额外授权"结论记 `progress.md`。群 id `ciduznBwLLiWKcMewBOF4+kWQ==` 是 2026-07-16 实测的 yzzhan量化群 openConversationId。

- [ ] **Step 5: 验出站（send-by-bot 真发一条测试）**

```bash
dws chat message send-by-bot --robot-code <code> \
  --group "ciduznBwLLiWKcMewBOF4+kWQ==" --title "播报通道验证" --text "每日行情播报机器人上线测试" -y
```
Expected: 群内收到卡片（errcode=0）。**这是真实发送**（`send-by-bot` 不可 mock 测试），内容已最小化。

- [ ] **Step 6: 凭证写 `.env`**

```
DINGTALK_CHAT_ROBOT_CODE=<Step3 拿到的 robot-code>
BROADCAST_GROUP_ID=ciduznBwLLiWKcMewBOF4+kWQ==
```

- [ ] **Step 7: 固化部署文档 + commit**

`scripts/setup_broadcast_bot.md` 记 Step 1-6 命令（幂等注明"机器人/应用已存在则跳过"）。
`git add scripts/setup_broadcast_bot.md && git commit -m "feat(daily-brief): Task0 dws建专用播报机器人+拉群setup（robot-code就绪）"`（`.env` 不入库）。

---

## Task 1: 标的代码→中文名映射（name_resolver）+ 单测

**目标：** 解 spec §8 #1-3——`index_daily`/`ths_daily`/`moneyflow` 的 symbol 都是代码（`000300.SH`/`885538`），文案需中文名。建 `name_resolver`，文案可读性硬前置。

**Files:**
- Create: `broadcast/name_resolver.py`
- Create: `tests/test_broadcast_name_resolver.py`

**Interfaces:**
- Consumes: `config/registry.py`（标的元信息）+ `sector.parquet`（板块名，列名 GBK 待处理）+ 硬编码兜底
- Produces: `resolve_index_name(code)` / `resolve_ths_name(code)` / `resolve_stock_name(code)`；未知返原 code

- [ ] **Step 1: 实测名称数据源**

- 指数：`config/registry.py` 是否有 `ts_code→中文名`（沪深300/上证指数…）映射？`index_daily` 的 8 个 symbol 是哪些？
- 板块：`sector.parquet` 列名 GBK 乱码（spec §3.3 注），能否解析出 `ths_code→概念名`？或 registry 是否登记？
- 个股：`moneyflow` symbol（`000001.SZ`）→ 平安银行，名称从哪来（registry/tushare stock_basic）？
把"名称数据源结论"记 `progress.md`。

- [ ] **Step 2: 写失败测试**

```python
# tests/test_broadcast_name_resolver.py
from broadcast.name_resolver import resolve_index_name, resolve_ths_name
def test_index_known():
    assert resolve_index_name("000300.SH") == "沪深300"
def test_index_unknown_passthrough():
    assert resolve_index_name("999999.XX") == "999999.XX"   # 未知返原 code
```

- [ ] **Step 3: 跑测试确认失败**

`pytest tests/test_broadcast_name_resolver.py -x` → FAIL（模块不存在）。

- [ ] **Step 4: 实现 name_resolver**

优先 registry 动态查；fallback 硬编码常用 8 指数（沪深300/上证指数/中证500/中证1000/上证50/科创50/创业板指/深证成指）+ 主要板块映射 dict；未知返原 code（**绝不抛**，文案至少有 code 兜底）。

- [ ] **Step 5: 跑测试通过 + commit**

`pytest tests/test_broadcast_name_resolver.py -v` → PASS。
`git add broadcast/name_resolver.py tests/test_broadcast_name_resolver.py && git commit -m "feat(daily-brief): Task1 标的代码→中文名映射(name_resolver)"`。

---

## Task 2: brief.py 文案生成器 + 单测

**目标：** `build_daily_brief(date, *, reader, resolver) -> BriefResult`（含 `markdown: str`）。大盘 8 宽基 + 板块 Top5/Bottom5 + 主力净流入 Top5 + 龙虎榜（可选节）；缺数据降级；NaN 守护。

**Files:**
- Create: `broadcast/brief.py`
- Create: `tests/test_broadcast_brief.py`

**Interfaces:**
- Consumes: `DataLakeReader.get_cross_section` + `broadcast.name_resolver` + `clean_markdown_for_dingtalk`（`caisen/optimize/training_dingtalk.py:70`）
- Produces: `build_daily_brief(date, *, reader, resolver) -> BriefResult`（`BriefResult`: `date`/`data_cutoff`/`markdown`）

- [ ] **Step 1: 写失败测试（正常路径）**

mock `DataLakeReader`（注入伪造 `index_daily`/`ths_daily` 截面 DF）+ mock `resolver` → 断言 `markdown` 含：指数行 + ▲/▼ 符号 + 板块 Top5 行。`BriefResult.date == 传入 date`。

- [ ] **Step 2: 写缺数据降级测试**

某湖 `get_cross_section` 返空 DF（`lake_reader.py:252` 离线契约）→ 该节 markdown 含「（数据未落湖，跳过）」，**其余节正常，不抛异常**。

- [ ] **Step 3: 写 NaN 守护测试**

注入含 NaN 的 close（`pct_change` 首日 NaN / pre_close=0 除零）→ 涨跌幅渲染「—」，不崩。

- [ ] **Step 4: 跑测试确认失败**

`pytest tests/test_broadcast_brief.py -x` → FAIL（模块不存在）。

- [ ] **Step 5: 实现 brief.py**

- 大盘：`reader.get_cross_section(date, lake="index_daily")` → 8 宽基 close；涨跌幅取每指数近 2 日 close 现算 `get_timeseries(sym,d-1,d).close.pct_change().iloc[-1]`（`index_daily` 无 pct 列，spec §5.5）。
- 板块：`get_cross_section(date, lake="ths_daily")` → `sort_values("pct_change")` 取首尾 5（ths_daily 自带 pct_change）。
- 资金：`get_cross_section(date, lake="moneyflow")` → `sort_values("net_mf_amount", ascending=False).head(5)`（可选节，空则跳过）。
- 龙虎榜：`get_cross_section(date, lake="dragon_list")` → `df[df["hit"]==1].index`（仅代码列表，无明细；spec §3.3）。
- 渲染：f-string 拼 Markdown 列表（钉钉子集：`#`/列表/粗体/引用，禁表格/`<font>`/`---`），经 `clean_markdown_for_dingtalk` 清洗。
- 名称：全部经 `name_resolver` 转中文。
- 若嫌 `clean_markdown_for_dingtalk` 跨包（broadcast→caisen.optimize）耦合，可内联到 brief.py（~15 行），plan 阶段定。

- [ ] **Step 6: 跑测试通过 + commit**

`pytest tests/test_broadcast_brief.py -v` → PASS。
`git add broadcast/brief.py tests/test_broadcast_brief.py && git commit -m "feat(daily-brief): Task2 文案生成器brief(大盘+板块+资金+龙虎榜·降级·NaN守护)"`。

---

## Task 3: push.py（dws send-by-bot）+ __main__.py（CLI + 去重）+ 单测

**目标：** `push_brief` subprocess 调 dws 出站；`__main__` 装配 + 幂等去重 + 异常兜底。

**Files:**
- Create: `broadcast/push.py`
- Create: `broadcast/__main__.py`
- Create: `tests/test_broadcast_push.py`

**Interfaces:**
- Consumes: `BriefResult`（Task 2）+ `.env`（`DINGTALK_CHAT_ROBOT_CODE`/`BROADCAST_GROUP_ID`）+ `logs/.last_broadcast`
- Produces: `push_brief(title, markdown, *, robot_code, group_id, dry_run) -> bool`；CLI `python -m broadcast [--date --dry-run --force]`

- [ ] **Step 1: 写 push 失败测试**

monkeypatch `subprocess.run`：returncode=1 → `push_brief` 返 False + 记日志；`dry_run=True` → 打印 markdown、不调 subprocess、返 True。

- [ ] **Step 2: 跑确认失败 → 实现 push.py**

`push_brief(title, markdown, *, robot_code, group_id, dry_run=False) -> bool`：组 `dws chat message send-by-bot --robot-code ... --group ... --title ... --text ... -y` 命令，`subprocess.run(timeout=30)`，returncode≠0 返 False。**零自写加签**。

- [ ] **Step 3: 写去重测试**

写 `logs/.last_broadcast=<today>` → `__main__` 检测到已播 → 跳过（不调 push）；`--force` → 忽略去重，照推。

- [ ] **Step 4: 实现 __main__.py**

- argparse：`--date`（缺省 = `index_daily` 最新日）/ `--dry-run` / `--force`。
- 流程：取 date → 读 `.last_broadcast`（==date 且非 force → 跳过）→ `build_daily_brief` → `push_brief` → 成功才写 `.last_broadcast=date`。
- 异常：`try/except` 包整体，失败记 `logs/broadcast.log`，**不写 `.last_broadcast`**（下次重试）。
- 凭证：`robot_code=os.getenv("DINGTALK_CHAT_ROBOT_CODE")`、`group_id=os.getenv("BROADCAST_GROUP_ID")`，缺则降级 `--dry-run` 提示。

- [ ] **Step 5: 跑测试 + dry-run 样例 + commit**

`pytest tests/test_broadcast_push.py -v` → PASS。
`python -m broadcast --date 2026-07-15 --dry-run` → 打印样例 Markdown（**不发真钉钉**），人工审文案。
`git add broadcast/push.py broadcast/__main__.py tests/test_broadcast_push.py && git commit -m "feat(daily-brief): Task3 push(dws send-by-bot)+CLI幂等去重"`。

---

## Task 4: schtasks 启动文档 + 真钉钉 E2E + 全套零回归

**目标：** 固化 19:00 触发；真发验证；零回归；README 更新。

**Files:**
- Create: `scripts/setup_broadcast_schtasks.md`（或并入 `setup_broadcast_bot.md` 部署章节）
- Modify: `README.md`（§9 钉钉机器人章节加第三个：每日行情播报）
- Modify: `.superpowers/sdd/progress.md`（上线结论）

- [ ] **Step 1: 写 schtasks 注册/卸载文档**

```bash
# 注册（每日 19:00）
schtasks /Create /SC DAILY /TN "QuanterDailyBrief" /TR "<venv310>\python.exe -m broadcast" /ST 19:00
# 卸载
schtasks /Delete /TN "QuanterDailyBrief" /F
```
入 `scripts/setup_broadcast_schtasks.md`，注明依赖 Task 0 的 robot-code + `.venv310`。

- [ ] **Step 2: 真钉钉 E2E（小范围/测试群或 yzzhan量化群）**

`python -m broadcast --force`（用当日真实数据）→ 群内收到播报卡片，**内容核对**（指数涨跌幅/板块榜/数据截止日正确）。

- [ ] **Step 3: 全套 pytest 零回归**

`pytest tests/ -q` → 全绿（broadcast 新增测试通过，server/caisen 余不动）。

- [ ] **Step 4: 更新 README §9 钉钉机器人章节**

加第三个机器人「每日行情播报」（dws 应用机器人，19:00 自动播报，区别于 yzzhanCli通用对话 / yzzhan参数优化训练人审）。

- [ ] **Step 5: 记 progress + commit**

`.superpowers/sdd/progress.md` 记播报上线结论（Task0 robot-code / Task4 真发截图描述 / schtasks 已注册）。
`git add scripts/setup_broadcast_schtasks.md README.md .superpowers/sdd/progress.md && git commit -m "feat(daily-brief): Task4 schtasks触发+真钉钉E2E+README(播报上线)"`。

---

## Self-Review

**Spec coverage:**
- 决策1（19:00）→ Task4 Step1；决策2（CLI+schtasks）→ Task3/4；决策3（内容 MVP）→ Task2；决策4（纯模板）→ Task2；决策5/8（dws 出站+部署）→ Task0/3；决策6（幂等去重）→ Task3 Step3-4；决策7（分支）→ 已建 `feat/daily-market-brief`；决策9（专用机器人）→ Task0。✓
- spec §8 未确认项：#1-3 名称映射 → Task1；#4 触发方式 → Task4；#5 进群权限 → Task0 Step4。✓

**Placeholder:** `<key>`/`<tid>`/`<code>` 是 Task0 实测后填的具体值（非 plan 占位）；群 id `ciduznBwLLiWKcMewBOF4+kWQ==` 是 2026-07-16 实测值（spec 已给）。✓

**Type consistency:** `DataLakeReader` 单例跨 Task0-3 共享；`name_resolver`（Task1 产）→ `brief.py`（Task2 消费）；`BriefResult`（Task2 产）→ `__main__`（Task3 消费）；`robot_code`/`group_id`（Task0 产 .env）→ `push.py`（Task3 消费）。✓

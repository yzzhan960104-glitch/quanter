# 项目全景与启动 SOP（2026-08-29 定稿）

> 一句话定位：**A 股颈线法形态突破策略（B3 冠军）的一体化量化系统**——本地研究/回测/数据管道
> + 掘金终端（Goldminer3）仿真双腿实盘 + 钉钉播报与 cockpit 监控。
> 实盘当前形态：NECK 主腿 = B3 内核（指纹 7fe3d5b3）+ amihud keep-top-5 信号过滤 + 单日 5 单 × 7.5% + 20 万资金。

---

## 一、全景：五层架构

```
┌─ 数据层 ── data_lake/（40+ parquet 表；主湖 a_shares_daily 前复权 10.5M 行 1997 起，
│             daily_basic 估值/换手、moneyflow 资金流、stock_basic 行业 110 类）
│   管道：17:00 data_pipeline supervisor = ①T1 检查 → ②增量采集(sync_daily_incremental)
│         → ③T2 检查(熔断) → ④amihud 分位写入(ops/amihud_pct_writer → 主腿 state/)
│   已知史：2024-Q1/2025 多段/2026-07 amount 断层已修（repair_amount_hole，711,620 格）
│
├─ 策略层 ── strategies/neckline/（识别内核 method_v0+signal，C2 冻结逐字节不动）
│             backtest/models.py（PositionModel：整手/min5/冻结挂单/queue_order 四态/
│             quality_alloc——组合模拟单源，回测与实盘对齐的锚）
│
├─ 执行层 ── emquant/（掘金终端单文件产物）
│   build_pilot.py 组装器（内核逐字块+§0 快照+pilot_body §2-§7 → emquant_neckline_pilot.py）
│   NECK 主腿 d9324346（仿真账户 67334fef，20 万）；NECK-EXP 实验腿 5557c9cb（c4ba3b2e，20 万）
│   策略目录 C:\Users\yzzhan\.emgm3\projects\<id>\：main.py + config/runtime.json(token)+
│   state/state.pkl + audit/audit_YYYYMMDD.csv + relaunch_strategy.ps1
│   当前版本 stamp 2987dc20（版本链 main.py.bak_* 五连可回退：03582953→3ec45db3→3dcb1e3d
│   →e0a1204c→2987dc20）
│
├─ 服务/运维层 ── presentation/server（FastAPI：research 只读 + cockpit 双腿观测代理）
│   ops/：emquant_morning_check(09:40 晨检) / emquant_audit_ingest(15:40 台账) /
│   gm_terminal_guard(5min 看护) / brief_all(18:00 钉钉播报) / data_pipeline(17:00)
│   schtasks 注册面见 ops/manage_ops_schtasks.py（C-7 收编后引擎 cron 为主）
│
└─ 研究层 ── diag/（探索脚本族：因子动物园/淘汰赛/quality_* 系列）
              discovery/（freeze 快照、objective 组合口径、split 分段）
              logs/quality/factor_zoo/（306 测试登记册=因子研究资产）
```

## 二、NECK 主腿当前规格（2026-08-29 定稿，全部经两窗验证）

| 项 | 值 |
|---|---|
| 识别 | B3 24 参数，指纹 7fe3d5b3（C2 冻结，永不改） |
| Universe | 创业板/科创板 300 只（§0 内嵌） |
| 信号过滤 | amihud keep-top-5：当日信号 >5 时留 amihud60 最高的 5 只（最不流动=质量侧）；≤5 或有效值 <5 全保留豁免 |
| 挂单 | 单日 ≤5 单，单票 7.5%×equity，整手 100 股，涨停价钳位 |
| 调度 | **09:31 早盘跑**（扫 T-1 数据、当日挂单）+ **15:36 尾盘跑**（扫 T 数据、次日挂单）+ 13:31/14:31 探针 + tick 自愈 |
| 资金 | 两腿各 20 万（cash-inout 入金；主腿拦截线 150 元/股，keep5 信号覆盖 ~96%） |
| 审计 | SIGNAL / SIGNAL_FILTERED_AMIHUD / AMIHUD_FILTER_EXEMPT / ORDER_PLACED / ORDER_BLOCKED / INIT（stamp+账户+策略三锚） |

## 三、启动 SOP

### A. 每交易日常规（全自动，人只看不动）

| 时刻 | 事件 | 检查点 |
|---|---|---|
| 09:31 | NECK 早盘跑（扫上一交易日） | audit 有 SIGNAL/EXEMPT；挂单 ≤5 |
| 09:40 | 晨检钉钉推 | 异常摘要 |
| 13:31/14:31 | 定时探针 | SCHEDULE_TICK 行=定时服务活着 |
| 15:36 | NECK 尾盘跑（扫当日） | 同上 |
| 15:40 | audit 台账入库 | experiments.db |
| 17:00 | data_pipeline（采集+amihud 分位写入） | 管道日志四步 rc=0 |
| 18:00 | 钉钉播报 | — |

### B. 机器重启后冷启动（顺序敏感）

1. **掘金终端**：`F:\dcjj\Eastmoney Goldminer3\emgm3.exe`——重启必过登录人闸（自动登录须已勾选；未勾需人工输密码）。终端是 7001/7002 服务与行情交易通道的宿主，**它不活一切免谈**。
2. **两腿策略进程**（终端不自动拉起 agent 部署的进程）：
   ```powershell
   powershell -ExecutionPolicy Bypass -File "C:\Users\yzzhan\.emgm3\projects\d9324346-9d1b-11f1-ae25-7c10c93fcb7d\relaunch_strategy.ps1"
   # 实验腿同理 5557c9cb-a308-11f1-915e-52560acd7b8c\relaunch_strategy.ps1
   ```
   验证：`audit/audit_当日.csv` 出现 INIT 行且 **build_stamp=2987dc20 / 账户 67334fef / 策略 id 三锚**；agent_run.log 有"连接行情/交易服务成功"。
3. **server**（cockpit/research + **数据管道宿主**）：⚠️ 2026-08-29 实测发现——
   ①`QuanterServer` ONSTART schtasks **从未注册**（自启从未武装）；②遗留
   `start_server_research_only.bat` 的 `python -m trading` 已随 W6 引擎退役变成死路径。
   **正确启动命令**（bat 已修正为 uvicorn 形态）：
   ```
   .venv310/Scripts/python.exe -m uvicorn presentation.server.main:app --host 127.0.0.1 --port 8000
   ```
   验证：8000 监听 + `curl http://127.0.0.1:8000/api/v1/gm/legs` 返 401（鉴权正常即活；
   启动期加载数据湖约 30-60s，期间连接拒绝是正常的，勿当死）。**server 的 lifespan 托管
   ops_sched：pipeline_then_eod 18:00 周一至周五**——server 死=管道死=湖断供
   （08-28/29 两天湖断供的根因即 W6 后 server 一直没起）。开机自启仍待武装：
   注册 QuanterServer ONSTART（注意 manage_ops_schtasks --register-server 里的命令
   也是死路径 `python -m trading`，注册前须改）。
4. **ops 定时**：gm_terminal_guard(5min)/晨检/台账/管道/播报均 schtasks 或引擎 cron 注册过，机器重启自动恢复；用 `ops/manage_ops_schtasks.py --list` 核对。
5. 账户若报 `account not exist`：`POST /v3/account-trade/login/<id>` 激活（skill goldminer-terminal 有全文档）。

### C. 策略代码更新流（改动必经，两段提交保 stamp）

1. 改 `emquant/pilot_body.py`（§2-§7 执行编排）或 `build_pilot.py`（§0 常量）——**内核与识别参数永不在产物侧手改**；
2. `python emquant/build_pilot.py --leg main`（+ `--leg exp`）重建产物；
3. `pytest tests/emquant/ -q` 全绿；
4. 提交源码 → 重建产物（stamp=新提交）→ 提交产物（两段，stamp 才稳定）；
5. 部署：备份旧 main.py → 覆盖 → relaunch → 验 INIT 三锚。

### D. 常见故障速查

| 症状 | 处置 |
|---|---|
| audit 无 INIT | 进程死：跑 relaunch ps1；gm_terminal_guard 应已自动重试 |
| INIT stamp ≠ git log 最新 | 部署错版：Ctrl+F 搜 PILOT_BUILD_STAMP 比对，重走 C 流 |
| 信号日全豁免且 n_valid=0 | amihud 拉取路径坏：查 fetch_amihud60（fields=eob,close,amount） |
| 湖尾滞后/坏行 | 跑 `data/tools/sync_daily_incremental.py`；amount 断层用 `data/tools/repair_amount_hole.py --dry-run` 先验 |
| 分位文件过期(>3 天) | 管道④步没跑：`python -m ops.amihud_pct_writer`；策略侧 fail-open 旁路不误事 |
| 入金/资金操作 | 云端 `POST 61.129.248.86:7102/v3/accounts/{id}/cash-inout {accountId,amount}`（encryptedToken 三头；细节在 skill） |
| 高价股 ORDER_BLOCKED 定尺不足一手 | 正常业务拒绝（>150 元/股超拦截线），非故障 |

### E. 回退

策略目录 `main.py.bak_<stamp>` 版本链五连（每版 INIT 验证过的）——`cp main.py.bak_<目标> main.py` + relaunch 即回退任意一步；识别参数指纹 7fe3d5b3 全程未变，回退只影响执行编排层。

## 四、知识资产索引

- 操作手册：`.agents/skills/goldminer-terminal/SKILL.md`（7002 API/入金/启停/深链，API 优先 GUI 仅观察）
- 因子研究：`logs/quality/factor_zoo/`（306 测试登记册 + 淘汰赛 + liveuni 系列）；方法论=结构闸（同日置换）必带
- 验证纪律：任何过滤/参数改动须过四闸（G1/G2/G3 + 可部署口径 21 种子中位）+ universe 约束重验（08-29 教训：回测池≠实盘 universe）
- 方案文档：`docs/superpowers/plans/`（信号质量/淘汰赛/双腿/cockpit 全档）

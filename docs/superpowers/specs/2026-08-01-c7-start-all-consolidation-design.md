# C-7 start_all 收编 + discovery 启动补跑

- **日期**：2026-08-01
- **分支**：feat/c7-start-all-consolidation（spec 阶段）
- **状态**：待审（spec review gate）
- **关联**：
  - C-5 spec（`__main__.run_server` uvicorn 薄壳，端口 8000 天然单例）
  - C-6 spec（clock 单一时间源）
  - memory [[discovery-engine-status]]（discovery daemon 串行 + 轮次 seed 派生）/ [[broadcast-robot-manager-status]]（connect 5 钉钉机器人）/ [[qmt-connect-1-rootcause]]（双进程抢 session 教训）
- **范围**：start_all 收编（broadcast connect + discovery 进 uvicorn lifespan）+ schtasks ONSTART（删 start_all.py）+ discovery 启动补跑（offline 容错，收编自洽必需）。

---

## 1. 背景与现状

### 1.1 痛点
1. `ops/start_all.py` 编排 3 件事（detach uvicorn + connect 5 钉钉机器人 + schtasks），与 lifespan 职责重叠（lifespan 已装 engine/采集/brief，C-2/C-5）。
2. **生产机不 7x24**（会关机/断电）—— 当前开机自启靠「启动」文件夹快捷式（ONLOGON，用户登录才跑，logoff/断 RDP 则 server 死）。
3. discovery 当前 schtasks DAILY 02:00（OS 级），收编 lifespan APScheduler 后，offline 跨 02:00 则夜跑漏跑（策略迭代断链）。

### 1.2 现状（master HEAD ff371db8，C-6 merged）
| 项 | 现状 | 证据 |
|---|---|---|
| start_all.py | 编排 3 件事 | `ops/start_all.py`：① subprocess.Popen DETACHED uvicorn ② `python -m broadcast connect --start all` ③ `discovery.schtasks --register` + `manage_ops_schtasks --unregister-pipeline-brief` |
| broadcast connect | 5 DETACHED 子进程 | `broadcast/__main__.py` CONNECT_BOTS（cli/trading_q/data_q/strategy_q/review）+ `connect_manager.start(bot,cfg,defaults)`/`stop(bot)` 完整生命周期 |
| discovery | schtasks DAILY 02:00 | `discovery/schtasks.py` register QuanterDiscoveryDaemon @02:00，触发 run_daemon.bat（重型全市场扫描） |
| lifespan 已装 | engine/采集/brief + replay_scheduler + training_orchestrator | `main.py` lifespan（C-2/C-5） |
| 开机自启 | 「启动」文件夹快捷式（ONLOGON） | `scripts/start_all.bat` 自安装快捷式到 `%APPDATA%\...\Startup` |

**核心病灶**：start_all 与 lifespan 职责重叠 + 开机自启 ONLOGON 不可靠（logoff 死）+ discovery 收编后需补跑防漏。

---

## 2. 目标与非目标

### 目标
1. **broadcast connect 进 lifespan**：lifespan startup 拉 5 CONNECT_BOTS（`connect_manager.start`），shutdown 树杀（`connect_manager.stop`）。软降级（装配失败不阻断 uvicorn）。
2. **discovery 进 lifespan APScheduler**：cron 02:00 → ProcessPoolExecutor 跑 `run_daemon`（重型子进程隔离不阻塞事件循环）。
3. **discovery 启动补跑**（offline 容错）：lifespan startup 检查 discovery 上次完成时间，错过昨晚 02:00 → ProcessPoolExecutor 异步补跑（幂等，收编自洽必需）。
4. **schtasks ONSTART 删 start_all.py**：开机 session 0 后台起 `python -m trading`（不依赖用户登录/logoff），替代 start_all.py 的 subprocess.Popen DETACHED + 「启动」文件夹 ONLOGON。
5. 退历史 schtasks（discovery QuanterDiscoveryDaemon + pipeline/brief）。

### 非目标（显式 out of scope）
- **不做全 job 最终一致性**（采集/eod/pre_open/brief 启动补跑 = **C-8 独立 project**，后续 brainstorm）。C-7 仅 discovery 补跑（收编自洽必需——discovery 从 schtasks 收到 lifespan 后必须有补跑防漏）。
- **不改 connect_manager 实现**（复用既有 start/stop/PID 管理）。
- **不改 discovery run_daemon 实现**（复用，仅改触发机制 schtasks→APScheduler + 补跑）。
- **不动 C-4/C-5/C-6 决议**（gate / clock / 三入口 gate 不变）。
- **不改 push 播报 schtasks**（trading/data/strategy 三个 push bot 的 schtasks 保留，C-7 只收编 connect/discovery）。

---

## 3. 架构

### 3.1 broadcast connect 进 lifespan
`main.py` lifespan（TradingEngine 装配块后，shutdown 段补 stop）：
```python
# startup（TradingEngine 装配后）
try:
    from broadcast.__main__ import CONNECT_BOTS, CONNECT_DEFAULTS
    from broadcast import connect_manager
    started = []
    for bot in CONNECT_BOTS:  # cli/trading_q/data_q/strategy_q/review
        try:
            connect_manager.start(bot, CONNECT_BOTS[bot], CONNECT_DEFAULTS)
            started.append(bot)
        except RuntimeError:
            pass  # 配置缺失跳过（同 _connect_start 语义）
        except Exception:
            logging.exception(f"connect bot={bot} 起异常（跳过，不阻断）")
    app.state.connect_bots = started
except Exception:
    logging.exception("lifespan 装 connect 异常（已忽略）")

# shutdown（断网关后）
for bot in getattr(app.state, "connect_bots", []):
    try:
        connect_manager.stop(bot)  # taskkill 树杀
    except Exception:
        logging.exception(f"connect bot={bot} stop 异常（已忽略）")
```

### 3.2 discovery 进 lifespan APScheduler
lifespan startup（connect 装配后）注册 cron 02:00 → ProcessPoolExecutor 跑 `run_daemon`：
```python
try:
    from concurrent.futures import ProcessPoolExecutor
    from discovery.daemon import run_daemon
    app.state.discovery_pool = ProcessPoolExecutor(max_workers=1, initializer=...)
    # 用既有 engine.sched（AsyncIOScheduler）或 app.state 单独 scheduler
    # cron 02:00 submit run_daemon 到 pool（重型子进程隔离，不阻塞事件循环）
    eng.sched.add_job(
        lambda: app.state.discovery_pool.submit(run_daemon, ...),
        trigger="cron", hour=2, minute=0, id="discovery_daemon",
        ...,
    )
except Exception:
    logging.exception("lifespan 装 discovery cron 异常（已忽略）")
```
**plan 确认**：engine.sched 加 cron 02:00（既有 AsyncIOScheduler）vs app.state 单独 scheduler；run_daemon 签名 + ProcessPoolExecutor initializer（同 replay_scheduler 范式）。

### 3.3 discovery 启动补跑（offline 容错）
lifespan startup（注册 cron 后）：
```python
try:
    if _discovery_missed_last_run():  # 检查上次完成时间 < 昨日 02:00
        app.state.discovery_pool.submit(run_daemon, ...)  # 异步补跑，不阻塞 uvicorn
        logging.warning("discovery 启动补跑：offline 跨 02:00，已异步补跑")
except Exception:
    logging.exception("discovery 启动补跑异常（不阻断 uvicorn）")
```
- **`_discovery_missed_last_run()`**：读 discovery 上次完成时间（discovery store/轮次，plan 确认字段），与昨日 02:00 比。
- **幂等**：discovery 轮次/seed 派生（[[discovery-engine-status]]），补跑 + 当晚 02:00 双跑靠轮次去重（discovery 既有幂等机制）。

### 3.4 schtasks ONSTART（删 start_all.py）
- **删** `ops/start_all.py` + `scripts/start_all.bat`。
- **新建** `scripts/start_server.bat`：
  ```bat
  @echo off
  chcp 65001 >nul
  cd /d "F:\quanter"
  ".venv310\Scripts\python.exe" -m trading
  ```
- **schtasks ONSTART 注册** QuanterServer：
  ```
  schtasks /Create /SC ONSTART /TN QuanterServer /TR "F:\quanter\scripts\start_server.bat" /RU <user> /RP <password> /F
  ```
  - `/SC ONSTART`：开机 session 0 后台（不依赖用户登录/logoff）。
  - `/RU /RP`：**凭证 plan 定**（当前用户+密码 vs SYSTEM——SYSTEM 无 user profile，venv/.env 路径待验证；倾向用户+密码可靠）。
- **注册入口**：`ops/manage_ops_schtasks.py` 加 `--register-server`（注册 QuanterServer ONSTART）+ 退 discovery QuanterDiscoveryDaemon + pipeline/brief（既有 `--unregister-pipeline-brief` + 新 discovery 退）。
- **开机自启**：schtasks ONSTART（开机即跑，替代「启动」文件夹 ONLOGON）。

### 3.5 不变量
- lifespan 多组件软降级（connect/discovery 装配失败不阻断 uvicorn，同 engine/training_orchestrator 范式）。
- discovery 补跑幂等（轮次/seed，不双跑重复 publish）。
- schtasks ONSTART session 0 后台（python -m trading 由 schtasks 包裹成后台，无需 start_all.py detach）。

---

## 4. 测试策略

- **lifespan 装 connect**：mock `connect_manager.start/stop`，验 5 CONNECT_BOTS 调用 + 软降级（单 bot 失败不阻断）。
- **lifespan 装 discovery cron**：mock `sched.add_job`，验 cron 02:00 注册 + ProcessPoolExecutor submit。
- **discovery 启动补跑**：mock `_discovery_missed_last_run`（错过/未错过两态），验补跑触发/跳过 + 异步不阻塞。
- **schtasks ONSTART 注册**：mock schtasks 命令，验 `/SC ONSTART /TR start_server.bat` + 退 discovery/pipeline-brief。
- **start_all.py 删**：grep 确认 `ops/start_all.py` 删 + `scripts/start_server.bat` 新建。
- **全量回归**：C-6 后 1164 passed 基线零退化。

---

## 5. 风险

| # | 风险 | 缓解 |
|---|---|---|
| R1 | connect reload 抖动（uvicorn reload 时 lifespan teardown+setup，connect 5 进程抖动） | live reload=False（C-5 V1）不 reload；开发 reload 抖动可接受；connect_manager.start 幂等（PID 文件） |
| R2 | discovery 漏跑补偿失效（补跑未触发/幂等失效） | `_discovery_missed_last_run` 单测覆盖（错过/未错过）；幂等靠 discovery 既有轮次（[[discovery-engine-status]]） |
| R3 | ProcessPoolExecutor discovery 重型阻塞 uvicorn | 子进程隔离（ProcessPoolExecutor），submit 异步不阻塞 lifespan；同 replay_scheduler 范式 |
| R4 | schtasks ONSTART 凭证（/RU 用户+密码 vs SYSTEM） | plan 定：倾向用户+密码（venv/.env 可靠）；SYSTEM 需验证 user profile 路径 |
| R5 | schtasks 迁移期双触发（discovery schtasks 残留 + lifespan APScheduler） | manage_ops_schtasks 退 discovery QuanterDiscoveryDaemon（幂等 /Delete /F）+ discovery 轮次幂等去重 |
| R6 | lifespan 多组件复杂度（connect/discovery 装配 + 补跑） | 每组件独立 try/except 软降级（装配失败不阻断 uvicorn） |
| R7 | schtasks ONSTART session 0 环境（无 tty，python -m trading 前/后台？） | schtasks session 0 起 python -m trading = uvicorn 后台（无终端，log 重定向到文件）；plan 验证 log 路径 |

---

## 6. 验收标准

1. broadcast connect 进 lifespan（5 CONNECT_BOTS start/stop，软降级单 bot 失败不阻断）。
2. discovery 进 lifespan APScheduler cron 02:00（ProcessPoolExecutor 跑 run_daemon）。
3. discovery 启动补跑（offline 跨 02:00 → 异步补跑，幂等不双跑）。
4. `ops/start_all.py` + `scripts/start_all.bat` 删；`scripts/start_server.bat` 新建 + schtasks QuanterServer ONSTART 注册。
5. 历史 schtasks 退（discovery QuanterDiscoveryDaemon + pipeline/brief）。
6. 全量回归 1164 passed 基线零退化。
7. C-4/C-5/C-6 决议不变（gate / clock / 三入口 gate）。

---

## 7. 实现步骤（高层 · 详细 diff 见 plan）

| 阶段 | 内容 | gate |
|---|---|---|
| **V1 connect 进 lifespan** | lifespan 装 5 CONNECT_BOTS（start/stop + 软降级） | connect 单测 |
| **V2 discovery 进 lifespan APScheduler** | cron 02:00 + ProcessPoolExecutor 跑 run_daemon | discovery cron 单测 |
| **V3 discovery 启动补跑** | `_discovery_missed_last_run` + 异步补跑（幂等） | 补跑单测（错过/未错过） |
| **V4 schtasks ONSTART + 删 start_all** | start_server.bat + QuanterServer ONSTART 注册 + 退 discovery/pipeline-brief + 删 start_all.py/bat | schtasks 单测 + grep |
| **V5 全量回归 + 验收** | 全量 1164+ 零退化 + spec §6 验收 1-7 | smoke + 1164/0 |

---

## 8. spec review 要点

1. **connect/discovery 进 lifespan**（软降级，装配失败不阻断 uvicorn）—— 接受？
2. **schtasks ONSTART 删 start_all.py**（/RU 凭证 plan 定：倾向用户+密码）—— 接受？
3. **discovery 启动补跑**（offline 容错，幂等轮次去重）—— 接受？
4. **全 job 最终一致性 = C-8 独立**（C-7 仅 discovery 补跑，收编自洽必需）—— 接受？
5. **push 播报 schtasks 不动**（trading/data/strategy push bot 保留）—— 接受？

spec 通过后落 plan（`docs/superpowers/plans/2026-08-01-c7-start-all-consolidation.md`）。

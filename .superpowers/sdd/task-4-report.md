# Task 4 报告：cli daemon 子命令 + 包自包含 schtasks 注册（Plan 4 L4）

## status
✅ DONE（GREEN，零回归，已 commit）

## 做了什么

### 代码（4 新增 + 3 改动）

#### 1. `discovery/schtasks.py`（新，包自包含调度）
discovery 包内自治的 Windows 任务计划程序注册器，不依赖 scripts/（后续 scripts 废弃
时不受影响），与 broadcast（scripts/manage_ops_schtasks）解耦——两者调度对象/时序
无关（broadcast 是盘后播报 15:30-17:30，daemon 是 02:00 跑批）。

- `DAEMON_TASK_NAME="QuanterDiscoveryDaemon"` / `DAEMON_TIME="02:00"` /
  `DAEMON_BAT=<pkg>/discovery/run_daemon.bat` 三常量。Why 02:00：避开 17:00-18:30
  data_check/daily_incremental 时序，选低负载深夜跑批（spec §5.2/§10）。
- `build_register_commands() -> list[dict]`：纯函数返 task/time/bat/bot 四键映射。
  Why 拆纯函数：让单测不触发 subprocess（不污染真实 Windows 任务计划程序）即可
  验证常量→命令映射回归。
- `_schtasks(args) -> int`：subprocess 封装（capture_output 防中文乱码打屏）。
- `register()`：幂等先 `/Delete /F`（不存在也返 0）再 `/Create /SC DAILY /TN /TR
  /ST /F`——保证改时间后重跑不报"任务已存在"错（复用 manage_ops_schtasks 既有纪律）。
- `unregister()` / `list_tasks()`：清退 / 查询。
- `main(argv)`：CLI 入口（`python -m discovery.schtasks --register/--unregister/--list`）。

#### 2. `discovery/run_daemon.bat`（新，包内入口，CRLF 行尾）
schtasks 触发的物理入口。`cd /d %~dp0\..`（bat 所在 discovery/ 的上一级=项目根）→
`call .venv310\Scripts\activate.bat` → `python -m discovery daemon --budget 4h`。
Why `cd /d %~dp0\..`：保证无论 schtasks 以哪个 cwd 启动都能定位 venv 与 discovery 包。
**CRLF 行尾**（Windows bat 硬要求，已校验 8 CRLF / 0 lone LF）。

#### 3. `discovery/cli.py`（改动：+ cmd_daemon + main 注册 daemon 子命令）
- `cmd_daemon(args)`：串 `build_default_manager()` → `freeze(lake_start)` →
  `holdout_split(embargo)` → `run_daemon(meta, split, db_path, budget_hours=,
  n_proc=, lake_start=, tpe_trials=, rho_threshold=, K=)`。打印 RunSummary +
  跨夜判据① + outer 去偏 + 下一步 publish 提示。
  - **⚠️ 关键职责**：开头必须 `from infra.notifier import build_default_manager;
    build_default_manager()`。Why：run_daemon 内部 `_notify_champion` 走
    `NotificationManager.get_default()` 单例，但首次 `_channels=[]`（get_default
    懒构造不读 .env）→ 告警走"无通道"软降级（仅 debug 日志，钉钉收不到，夜跑告警
    静默丢失）。cmd_daemon 作为生产入口必须显式装通道（读 .env 的
    DINGTALK_WEBHOOK/SECRET 等），这是 T3 reviewer Cannot-verify 标记、brief 之外
    补加的关键一步。
  - **early_exited 短路**：run_daemon 返回 dict 在跨夜已收敛时
    `early_exited=True, summary=None, run_id=None`——cmd_daemon 按
    `out["early_exited"]` 短路 return，不访问 `out["summary"]`（否则
    None.attribute 炸）。brief Step 5 verbatim 代码已正确处理。
- `main()` 在 `ap_rp`（report）之后注册 `daemon` 子命令：`--budget`(默认 4h) /
  `--embargo`(5) / `--n-proc` / `--lake-start` / `--tpe-trials`(默认 10，夜跑默认
  与 run 的 0 不同) / `--rho-threshold`(0.8) / `--k-rounds`(3，跨夜收敛 K 判据①)。

#### 4. `discovery/__init__.py`（改动：导出 run_daemon）
追加 `from discovery.daemon import run_daemon, run_daemon_cycle` + `__all__`
追加两符号。Plan 4 L4 生产入口对外可见。

#### 5. 顺手清 T3 Minor M1（`discovery/daemon.py`）
`_notify_champion` 的死形参 `snapshot_hash=""`（T3 遗留，函数体用 `summary.snapshot_hash`
不用形参，brief 之外的关键事实里 reviewer 标记"可选顺手清"）。已删——cmd_daemon
不直传 snapshot_hash 给 _notify_champion（run_daemon 内部调），无调用方影响
（已 grep 确认 tests 无直接调 `_notify_champion`，都是 mock notify_fn）。

### 测试（`tests/discovery/test_schtasks.py` 新，2 测试）
- `test_build_register_commands_shape`：纯函数验证——`len(cmds)==1` /
  `c["task"]==DAEMON_TASK_NAME` / `c["time"]=="02:00"` /
  `c["bat"].endswith("discovery\\run_daemon.bat")`（包内 bat，不指向 scripts）。
- `test_register_calls_schtasks_delete_then_create`：`monkeypatch.setattr(sch,
  "_schtasks", _fake)` mock subprocess（不污染真实 Windows 任务计划程序），调
  `register()` 后断言 `calls` 里至少一次 `/Delete` + 一次 `/Create`（幂等序）。

## TDD 证据
1. **RED**：先写 test_schtasks.py → 跑 `pytest tests/discovery/test_schtasks.py -v`
   → 2 FAILED（`ModuleNotFoundError: No module named 'discovery.schtasks'`）。
2. **GREEN**：实现 schtasks.py + run_daemon.bat + cmd_daemon + __init__ 导出 →
   跑同一组 → 2 PASSED。
3. **回归**：
   - `pytest tests/discovery/test_cli_run.py tests/discovery/test_cli_plan3.py
     tests/discovery/test_daemon.py` → 全 exit 0（cli 既有不回归 + T2/T3 daemon 不回归）。
   - `pytest tests/discovery/ -q -m "not slow"` → **96 passed, 8 deselected**
     （T3 的 94 + T4 的 2 = 96，零回归）。
4. **import 自检**：`from discovery import run_daemon, run_daemon_cycle` +
   `from discovery.schtasks import DAEMON_TASK_NAME, build_register_commands,
   register, main` + `from discovery.cli import cmd_daemon` → OK（防运行时
   ImportError）。
5. **CLI 入口自检**：`python -m discovery.schtasks --help` 与
   `python -m discovery daemon --help` → 正常输出（三互斥选项 / 七参数全注册）。
6. **CRLF 校验**：run_daemon.bat 8 CRLF / 0 lone LF（Windows bat 硬要求）。

## 测试输出
```
tests/discovery/test_schtasks.py::test_build_register_commands_shape PASSED [ 50%]
tests/discovery/test_schtasks.py::test_register_calls_schtasks_delete_then_create PASSED [100%]
============================== 2 passed in 0.35s ==============================

====================== 96 passed, 8 deselected in 5.59s =======================
```

## commits
- `feat(discovery): T4 cli daemon 子命令 + 包自包含 schtasks 注册（Plan 4 L4）`

## concerns / 遗留
- **cmd_daemon 未做 slow 端到端集成测试**（真跑会触达 data_lake + 真钉钉 + schtasks）：
  留 T7 slow 端到端集成（task #16）统一覆盖跨夜状态累积 + outer 去偏 + 告警送达。
  本任务的 96 non-slow 已覆盖所有纯函数与 mock 路径。
- **schtasks register 真实执行未测**：Windows 任务计划程序注册是副作用操作，CI/开发机
  反复跑会污染真实调度——按 brief 纪律只用 mock 验幂等序（先 /Delete /F 再 /Create），
  真实注册需人手 `python -m discovery.schtasks --register` 一次（一次性配置，不进自动化）。
- **build_default_manager 是隐式契约**：cmd_daemon 开头必须调它是"约定胜于配置"——
  未来若有其他入口调 run_daemon（如手动夜跑脚本），也需同样先 build_default_manager，
  否则告警静默丢失。T7 slow 集成会真验钉钉通道是否装上（Cannot-verify 收口）。

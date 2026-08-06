# 2026-08-04 网关/真相源治理运维 SOP

> **背景**：08-04 事故真根因是 `python -m trading` 嵌套子进程（系统 Python 310 +
> venv310 父子 37168→35736 并存）抢 QMT session，致 `gw.connect()` 返 -1 全天锁死
> （memory [[qmt-connect-1-rootcause]]）。代码侧已加 **W1.4 启动探测告警**
> （`trading/__main__.py::_assert_single_instance`），但探测拦不住嵌套父子（共享端口
> 归属父），**真根治靠本 SOP 运维清理 + 启动告警双管齐下**。
>
> 本 SOP 是 Phase 0 收尾的运维侧落地，配合代码侧 5 task（W1.1~W1.4 + W2）形成
> 「探测 → 告警 → 运维清理 → 配置回正」的完整闭环。

---

## 适用范围

- 事故机恢复（QMT connect -1 / 双引擎进程并存）
- 重启 engine 前的标准前置检查
- .env 配置漂移排查
- CSV 测试脏行清理（W3 前置）

---

> **2026-08-06 起（B5）**：引擎启动/重启唯一入口 =
> `python ops/restart_trading.py restart [--yes]`；状态查看 = `python ops/restart_trading.py status`。
> 不再手动 `schtasks /Run` / `python -m trading`（A5 已加生产 fail-closed：`start_server.bat`
> 置 `QUANTER_REQUIRE_LIVE=1`，非 live 实例拒绝启动）。

## 1. 多余引擎进程清理（W1.4 · 真根治）

### 1.1 现象识别

- 启动日志见 `端口 8000 已被既有引擎实例占用` CRITICAL → 本实例已 sys.exit(1)
- `gw.connect()` 返 -1（session 占用）
- `tasklist` 见多个 `python.exe` 且命令行含 `trading`

> B5：先跑 `python ops/restart_trading.py status` 看三合一拓扑（端口/pid 文件/session 锁）；
> 确认为旧链/多余链后再 `python ops/restart_trading.py restart --yes`（默认 dry-run，
> 三合一不一致会拒绝重启，绝不自动 taskkill 未知链）。

### 1.2 清理步骤

```cmd
REM 1. 列出所有 python.exe 进程（含 PID + 内存占用）
tasklist /FI "IMAGENAME eq python.exe"

REM 2. 查 schtasks QuanterServer 拉起的合法链 PID（必须保留，误杀会断生产启动链）
schtasks /Query /TN QuanterServer /V /FO LIST | findstr /I "PID"

REM 3. 查端口 8000 持有者（确认哪个 PID 真在 bind）
netstat -ano | findstr :8000

REM 4. 杀多余进程（保留 schtasks 链 PID，其余 taskkill）
REM    ⚠️ 嵌套父子（父 PID → 子 PID）必须一并杀，否则子进程继承 fd 继续抢 session
taskkill /F /PID <多余子进程 PID>
```

### 1.3 验证清理彻底

```cmd
REM netstat 只剩一个 PID 持有 8000（即 schtasks 链的合法实例）
netstat -ano | findstr :8000
REM 期望输出：单行 TCP 0.0.0.0:8000 ... LISTENING <一个 PID>
```

### 1.4 重新启动

```cmd
schtasks /Run /TN QuanterServer
```

### ⚠️ 为什么不靠代码自动 taskkill

W1.4 探测命中只 `sys.exit(1)` + CRITICAL 告警，**绝不自动 taskkill**：
- 误杀 schtasks QuanterServer 拉起的合法链会断生产启动链；
- 嵌套父子的 PID 归属判定不可靠（Windows 拿不到跨进程 PID，见
  `_port_holder_alive` docstring）；
- 真根治是「预防嵌套」（统一 venv 入口 + schtasks 单一拉起方），非「事后清理」。

### B2-4（G4）：miniQMT 客户端「自动登录」配置 SOP

1. 打开 miniQMT 客户端，进入「系统设置 / 交易设置」。
2. 勾选「记住账号/自动登录」（人工一次性配置；guard 不负责输密码）。
3. 验证：重启 XtMiniQmt.exe 后无需输入密码即进入已登录态。
4. guard 职责边界：进程不在 → 拉起 `XtMiniQmt.exe`；登录态/会话文件陈旧 → 钉钉 WARN；
   **不代为输入密码、不误杀进程**。

---

## 2. .env 配置回正（spec #7）

### 2.1 必查项

| 变量 | 红线值 | 事故机值 | 说明 |
|------|--------|----------|------|
| `TRADE_SHADOW_MIN_DAYS` | `5` | `1`（事故机） | 影子期硬闸下限，低于 5 天拒切 LIVE（`check_shadow_gate`） |
| `AUTO_TRADE_MODE` | `dry_run`/`live` | — | 确认是否真要全自动 live（W2 已修人审 veto 双写 DB） |
| `AUTO_CONFIRM_PLAN` | `true`/`false` | — | 与 `AUTO_TRADE_MODE` 同口径，全自动 live 需两者一致 |
| `QMT_SESSION_ID` | 与 miniQMT 客户端一致 | 事故机 session 漂移（123456 vs 123458） | 见 `log_startup_banner` 启动时固化进日志 |

### 2.2 回正步骤

```cmd
REM 1. 编辑 .env（F:\quanter\.env）
notepad F:\quanter\.env

REM 2. 校正红线值
REM    TRADE_SHADOW_MIN_DAYS=5
REM    AUTO_TRADE_MODE=live（仅当人审 veto 已就位 + 影子期 ≥5 天）
REM    AUTO_CONFIRM_PLAN=true（与 AUTO_TRADE_MODE 同步）

REM 3. 重启 engine 让 load_dotenv(override=True) 生效
schtasks /Run /TN QuanterServer

REM 4. 核对启动 banner（session/account/userdata/mode/confirm）与 .env 一致
REM    日志关键字：「=== 启动 banner === session=... account=... userdata=... mode=... confirm=...」
```

### ⚠️ AUTO_TRADE_MODE=live 前置确认

切 LIVE 前**必须**确认以下刹车就位（任一缺失禁止切 live）：
- W2 veto/confirm DB 双写已生效（`veto_plan.veto` 写 DB，`_pre_open_impl` 查 VETOED 跳过）；
- `check_shadow_gate` 返 True（所有 ACTIVE 实验影子期 ≥ `TRADE_SHADOW_MIN_DAYS`）；
- 对账连续无 drift（W3.4 post_close broker 权威对账，本 SOP 不覆盖，见 W3 follow-up）。

---

## 3. CSV 测试脏行清理（W3.5 前置）

### 3.1 背景

`logs/live_trades.csv` 历史混入 `scripts/migrate_live_trades_csv.py` 的
`TEST_FILL_SYMBOLS = {"300001.SZ", "300002.SZ", "600000.SH"}` 测试成交回报行
（`成交回报@` 前缀），W3 成交回报 CSV 幂等消费前必须清理，否则会污染对账。

### 3.2 ⚠️ 人工核对警告（清理前必做）

**清理 `600000.SH/300001.SZ/300002.SZ` 测试行前，必须人工核对 QMT 客户端真实持仓**：

- memory [[qmt-live-smoke-findings]] 记录：茅台 smoke 测试真成交买入**不扣 cash**
  （模拟盘行为），账户侧可能真有 `600000.SH` 持仓；
- 不能盲删 CSV 行——若客户端真有持仓，CSV 记录可能不是测试脏行而是真实成交；
- 核对方法：miniQMT 客户端 → 持仓查询 → 比对 `600000.SH/300001.SZ/300002.SZ`
  是否真实在场。

### 3.3 清理步骤（核对后确认是测试脏行才执行）

```cmd
REM 1. 备份（必做，清理动作不可逆）
copy F:\quanter\logs\live_trades.csv F:\quanter\logs\live_trades.csv.bak.20260804

REM 2. 跑迁移脚本（识别 TEST_FILL_SYMBOLS + 成交回报@ 前缀的测试行）
cd /d F:\quanter
.venv310\Scripts\python.exe scripts\migrate_live_trades_csv.py

REM 3. 人工复核 diff（备份 vs 清理后）
fc F:\quanter\logs\live_trades.csv.bak.20260804 F:\quanter\logs\live_trades.csv

REM 4. 确认清理行均为测试脏行（symbol ∈ TEST_FILL_SYMBOLS 且 rationale 以「成交回报@」开头）
```

### 3.4 验证

清理后 `logs/live_trades.csv` 不再含 `成交回报@` + `TEST_FILL_SYMBOLS` 组合的行，
W3 成交回报消费端（state_store.fill）可干净幂等消费。

---

## 4. 75MB 残留队列清理（W1.3 配套）

### 4.1 背景

W1.3 已实现「connect 前置清理本 session_id 残留队列」，但事故机的
`userdata_mini/down_queue_win_<session_id>` 目录可能已堆积 75MB+ 历史队列
（07-29 全天锁死期间累积）。引擎启动会自动清（W1.3），但可手动预热加速首连。

### 4.2 清理步骤

```cmd
REM 1. 定位 userdata 目录（.env QMT_USERDATA_PATH 或 miniQMT 安装目录下）
REM    典型路径：F:\quanter\userdata_mini\down_queue_win_<session_id>
dir F:\quanter\userdata_mini\down_queue_win_* /S

REM 2. 删除残留队列目录（W1.3 引擎启动会重建空队列）
rmdir /S /Q F:\quanter\userdata_mini\down_queue_win_123459
REM    ⚠️ session_id 必须与 .env QMT_SESSION_ID 一致；删错 session 会丢其他账号队列

REM 3. 验证目录已清（或由 W1.3 在引擎启动时自动重建）
```

### 4.3 验证

引擎启动后 `_health_guard` 日志 1 分钟内应见「网关已连接」或「客户端未就绪
（诊断文案，W1.1/W1.2）」WARNING，不再卡 connect -1。

---

## 5. 启动顺序（重启 engine 前的标准前置）

> **铁律**：以下 4 步必须按序完成，跳步会导致双进程抢 session 复发。

### Step 1: 清多余进程（§1）

完成 §1.2~§1.3，确认 `netstat :8000` 单 PID 持有。

### Step 2: 确认 miniQMT 客户端已启动 + 登录

- miniQMT 客户端窗口可见，登录状态为「已登录」（非「未连接」/「密码错误」）；
- 客户端进程 `XtMiniQmt.exe` 在 `tasklist` 中存在。

### Step 3: 启动 schtasks QuanterServer

```cmd
schtasks /Run /TN QuanterServer
```

- 该任务拉起 `start_server.bat` → `python -m trading` → uvicorn server（engine 由
  lifespan 装）；
- **禁止**手动 `python -m trading` 另起进程（会与 schtasks 链抢 session，W1.4 探测
  会拦并 sys.exit(1)）。

### Step 4: 观察 `_health_guard` 日志

- 1 分钟内应见以下之一：
  - ✅ `网关已连接`（connect 返 0，W1.1 is_client_ready 权威判定）
  - ⚠️ `客户端未就绪：<诊断文案>`（connect 返非 0，W1.2 WARNING + 限流钉钉）
- 超过 1 分钟无上述日志 → 回 §1 检查是否仍有残留进程，或查 miniQMT 客户端登录状态。

---

## 故障速查表

| 现象 | 可能根因 | 处置 |
|------|----------|------|
| `端口 8000 已被既有引擎实例占用` CRITICAL | 双引擎并存 | §1 清理 |
| `gw.connect()` 返 -1 | session 被占（嵌套子进程） | §1 清理 + §5 启动顺序 |
| `拒切 LIVE：experiment 状态查询失败` | resolve_active 抛异常 | 查 experiment DB 连通性 |
| `拒切 LIVE：N 实验影子期不足` | TRADE_SHADOW_MIN_DAYS < 5 或实验未满期 | §2 回正 + 等影子期 |
| 启动 banner session 与 .env 不一致 | session 漂移 | §2 回正 + 重启 |
| CSV 对账 drift | 测试脏行未清 | §3 清理（先人工核对持仓） |

---

## 关联文档

- 代码：`trading/__main__.py::_assert_single_instance` / `_port_holder_alive`
- 设计：`docs/superpowers/specs/2026-08-04-gateway-ssot-hardening-design.md`
- 计划：`docs/superpowers/plans/2026-08-04-gateway-ssot-hardening.md`
- memory：`[[qmt-connect-1-rootcause]]` / `[[c9-data-observation-remediation-status]]`

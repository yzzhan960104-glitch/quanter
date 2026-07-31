# C-5 进程模型统一 + 网关健康度前置 gate 设计

- **日期**：2026-07-31
- **分支**：master（spec 阶段）
- **状态**：待审（spec review gate）
- **关联**：
  - `2026-07-30-scheduling-orchestration-design.md`（C-2——engine 收编进 uvicorn lifespan，单进程基础）
  - `2026-07-31-error-grading-and-scheduler-hardening-design.md`（C-4——`_critical_guard` + `_health_guard` 不升 L1 决议）
  - memory [[qmt-connect-1-rootcause]]（session 互踢故障根因——07-29 全天锁死）
- **范围**：**(C) 进程模型统一到 server**——`__main__` 改 uvicorn 薄壳 + 端口天然单例，消除双进程抢 session；**(B) 触发点前置 gate 统一**——抽 `_gw_health_gate` 共享方法，`_stoploss`/`_post_close` 入口调，锁态 skip+CRITICAL。

---

## 1. 背景与现状

### 1.1 痛点（用户原话）
1. engine + server 双进程共享 `QMT_SESSION_ID` → 互相踢 session
2. 网关重连耗尽（60s 退避）→ 锁态但 cron 仍触发 → 静默全失败
3. 缺「网关健康度」在调度触发点的显式前置 gate

### 1.2 现状（master HEAD `4fbbec64`，C-4 merged）

| 项 | 现状 | 证据 |
|---|---|---|
| 进程入口 | **两条并存** | 生产 `ops/start_all.py` → detach uvicorn（`presentation.server.main:app`）；开发 `python -m trading`（`trading/__main__._run_forever` 独立 engine） |
| engine 装配 | **仅 server lifespan** | `main.py:191-207` lifespan 构造 TradingEngine + bootstrap + shadow_gate + start；`__main__` 另走 `_run_forever` 独立装配 |
| 双进程防护 | **无** | 误同时起 start_all + `python -m trading` → 两进程 `gw.connect()` 抢同一 `QMT_SESSION_ID`（07-29 session=123456 vs .env=123458 故障） |
| pre_open gate | **独享前置** | `engine.py:_pre_open_gate` S3 三段（计划/网关/数据），gw 锁态 return skip+CRITICAL |
| stop_loss/post_close gate | **无前置** | `_stoploss` 靠运行时 L1 兜底（C-4 U3b 查持仓失败停，30s 才发现）；`_post_close` 无 gate 无 L1，锁态静默跑 |
| `_health_guard` | **不升 L1**（C-4 决议） | 60s 自愈，锁态持续 CRITICAL+自愈，**不阻止 cron 触发业务** |

**核心病灶**：双入口无互斥 → session 互踢；触发点 gate 不统一 → gw 锁态时 `_stoploss`/`_post_close` 静默跑业务（L1 后置反应或无兜底）。

### 1.3 为什么「统一到 server」治本（vs 文件锁补丁）
- C-2 已把 engine 收编进 uvicorn lifespan（生产单进程）。残留的双进程风险来自 `__main__` 开发入口仍独立装配 engine。
- 与其在 engine 层加文件锁防双进程（防御性补丁），不如**消除双入口**——让 `__main__` 也走 uvicorn，engine 只由 lifespan 装配。
- uvicorn bind 端口 8000 天然提供单例防护（第二实例 bind 失败 exit），**无需文件锁**。

---

## 2. 目标与非目标

### 目标
1. **(C) 统一到 server**：`__main__` 改 uvicorn 薄壳，消除双入口；端口 8000 天然单例（替代文件锁）。
2. **(B) 共享前置 gate**：抽 `_gw_health_gate`，`_stoploss`/`_post_close` 入口调，锁态 skip+CRITICAL 不跑业务。
3. lifespan 加 session 漂移 banner（生产链可见）。

### 非目标（显式 out of scope）
- **不改 C-4 的 `_health_guard` 不升 L1 决议**（自愈取向；B gate skip 与之一致）。
- **不抽 gate 装饰器**（极简显式，三入口显式调共享方法，不引入新抽象层）。
- **不做 C-6 时间上下文统一**（C-6 单独 brainstorm；B gate 入口预留 trading_day 注入位但本期不实现，YAGNI）。
- **不改 schtasks 注册命令**（`python -m trading` 字面不变，内部变起 uvicorn）。
- **不加文件锁/PID 锁**（端口单例已够；C 选定后 A 锁方案废弃）。
- **不删 `ops/start_all.py`/`scripts/start_all.bat`/`ops/manage_ops_schtasks.py`**（生产全栈编排：起 uvicorn :8000 + connect 5 钉钉机器人 + discovery schtasks；C5 只统一开发入口 `python -m trading`，未收编 connect/discovery 进 uvicorn lifespan，删除需另立项收编后简化）。

---

## 3. C 架构统一到 server

### 3.1 `__main__.py` 改造为 uvicorn 薄壳
- **废弃** `_run_forever`（独立起 engine）。
- **`if __name__ == "__main__"`** 改：
  ```python
  import uvicorn
  uvicorn.run(
      "presentation.server.main:app",
      host=os.getenv("SERVER_HOST", "0.0.0.0"),
      port=int(os.getenv("SERVER_PORT", "8000")),
      reload=(os.getenv("AUTO_TRADE_MODE", "dry_run") != "live"),  # live 不 reload（reload 起子进程抢 session，自扰）
  )
  ```
- **保留模块函数** `check_shadow_gate` / `_days_since_activation` / `log_startup_banner`（lifespan 已 `from trading.__main__ import check_shadow_gate`；banner 移 lifespan 调）。
- `__main__` 不再 `sys.exit` shadow_gate（lifespan 内 `check_shadow_gate()` 决定是否 `eng.start()`，main.py:196 已如此）。

### 3.2 lifespan 加 `log_startup_banner`
- `main.py` lifespan engine bootstrap 前（line 191 try 块内，`TradingEngine()` 前）调 `log_startup_banner()`——从 `__main__` 搬。
- 生产链（start_all→uvicorn→lifespan）也输出 session/account/mode/口径 banner，session 漂移一眼可见（[[qmt-connect-1-rootcause]] 教训）。

### 3.3 端口 8000 天然单例
- uvicorn 默认 bind 8000，**不开 `SO_REUSEPORT`**（uvicorn 默认 False + 本期显式不传 `reuse_port`）。
- 第二实例 bind → `WSAEADDRINUSE`（Windows）/ `EADDRINUSE`（Linux）→ uvicorn exit → 不到 lifespan 装 engine → **天然不双进程**。
- **无需文件锁/PID 锁**（C 选定，A 方案废弃）。

### 3.4 schtasks 兼容
- `python -m trading` 命令字面不变（schtasks/PM2/`scripts/start_all.bat` 注册的命令保持）。
- 内部从「独立 engine」变「起 uvicorn」——schtasks 触发后起 server（engine 由 lifespan 装）。
- 回归验证：`scripts/start_all.bat` + `ops/manage_ops_schtasks.py` 注册的命令仍正常起。

---

## 4. B 共享前置 gate

### 4.1 抽 `_gw_health_gate`
```python
# engine.py TradingEngine method
def _gw_health_gate(self, gw) -> tuple[bool, str]:
    """网关健康前置 gate（从 _pre_open_gate ② 段抽，共享给 _stoploss/_post_close）。

    物理意图（C-5 B）：触发点业务前显式探测网关健康，锁态时 skip+CRITICAL
    不跑业务（防静默全失败），与 _pre_open_gate + _health_guard 自愈取向一致。
    """
    if gw is None or not getattr(gw, "_connected", False):
        return False, "网关未连接"
    if not gw.is_client_ready():
        return False, "miniQMT 客户端未就绪"
    return True, ""
```

### 4.2 `_pre_open_gate` ② 段改调它（DRY）
- `_pre_open_gate` 的 ② 网关健康段（engine.py:1907-1911）改调 `self._gw_health_gate(gw)`，行为不变。

### 4.3 `_stoploss` / `_post_close` 入口调 gate
- `_stoploss` / `_post_close` 在 `@_critical_guard` 后、业务逻辑前：
  ```python
  @_critical_guard
  async def _stoploss(self) -> None:
      gw = get_gateway()
      ok, reason = self._gw_health_gate(gw)
      if not ok:
          _alert_critical(f"stop_loss 跳过：{reason}（gw 锁态，等 _health_guard 自愈）")
          return  # skip 不跑业务，不停调度
      # ... 原业务（交易日判定 + stop_loss_monitor）
  ```
- 锁态 → skip+CRITICAL（不调 stop_loss_monitor，不写 DB/网关）；与 `_pre_open_gate` 一致；与 `_health_guard` 不升 L1 一致（自愈取向）。

---

## 5. 风险

| # | 风险 | 缓解 |
|---|---|---|
| R1 | uvicorn 单点依赖（起不来 = engine 不起） | lifespan 软降级保手动 API；uvicorn 稳定；运维监控 uvicorn 进程存活 |
| R2 | 端口 8000 被非 uvicorn 占用 → 误判单例 | banner + 日志明确「uvicorn bind 8000 失败」；`ops/clean_ports.py` 已有清 8000 残留 |
| R3 | 开发体验：起 server 比 pure engine 重 | 接受（统一代价）；开发用 `uvicorn --reload` |
| R4 | schtasks 改造后 `python -m trading` 行为变（起 server） | 回归 start_all.bat + manage_ops_schtasks；__main__ docstring 更新 |
| R5 | `_stoploss` gate skip 在 gw 锁态时持续 skip（每 30s CRITICAL） | 接受（持续告警 + `_health_guard` 自愈）；与 `_pre_open_gate` 一致；`% N` 节流防风暴留 plan 定 |
| R6 | uvicorn `SO_REUSEPORT` 误开 → 双实例 bind 同端口 | plan 显式断言不传 `reuse_port`；uvicorn 默认 False |

---

## 6. 测试策略

- **C**：
  - `test_main_runs_uvicorn`：mock `uvicorn.run`，`python -m trading` 调它（断言 host/port/reload 逻辑：live 不 reload）。
  - `test_lifespan_logs_banner`：lifespan 装配 engine 前调 `log_startup_banner`（caplog 断言 session/account/mode/口径）。
  - 端口单例 smoke：第二实例 bind 8000 失败 exit（集成/手动）。
  - schtasks 回归：`scripts/start_all.bat` + `manage_ops_schtasks` 命令 smoke。
- **B**：
  - `test_gw_health_gate`：单元（gw None / `_connected=False` / `is_client_ready()=False` 各返 `(False, reason)`；全绿返 `(True, "")`）。
  - `test_stoploss_gw_locked_skips`：gw 锁态 → `_stoploss` skip+CRITICAL，不调 `stop_loss_monitor`。
  - `test_post_close_gw_locked_skips`：同。
  - `test_pre_open_gate_uses_shared_gate`：`_pre_open_gate` ② 段走 `_gw_health_gate`（DRY 后行为不变，回归）。

---

## 7. 验收标准

1. `__main__.py` `if __name__` 起 uvicorn（不再独立 engine）；`_run_forever` 废弃。
2. lifespan 装配 engine 前调 `log_startup_banner`（生产链可见 session 漂移）。
3. 端口 8000 单例（第二实例 exit；plan 显式断言不开 `SO_REUSEPORT`）。
4. schtasks 命令 `python -m trading` 行为变起 server，`start_all.bat` + `manage_ops_schtasks` 回归通过。
5. `_gw_health_gate` 抽出；`_pre_open_gate` ② 段 DRY 改调它（行为不变）。
6. `_stoploss`/`_post_close` 入口调 `_gw_health_gate`，锁态 skip+CRITICAL 不跑业务（不停调度）。
7. 全量回归零退化（C-4 后 1146 基线）。

---

## 8. 实现步骤（高层 · 详细 diff 见 plan）

| 阶段 | 内容 | gate |
|---|---|---|
| **V1 `__main__` 改造** | uvicorn 薄壳 + 废弃 `_run_forever` + docstring 更新 | `__main__` 单测 |
| **V2 lifespan banner** | lifespan 加 `log_startup_banner` | lifespan 测试 |
| **V3 抽 `_gw_health_gate`** | 抽方法 + `_pre_open_gate` ② DRY | gate 单测 + pre_open 回归 |
| **V4 `_stoploss`/`_post_close` gate** | 两入口调 gate，锁态 skip+CRITICAL | 两 job gate 测试 |
| **V5 schtasks 回归 + 全量** | start_all.bat + schtasks smoke + 全量回归 | smoke + 1146/0 |

---

## 9. spec review 要点

1. **C 统一 server**（替代 A 文件锁）：`__main__` uvicorn 薄壳 + 端口 8000 天然单例——接受？
2. **B gate skip 不停调度**（与 `_pre_open_gate` + `_health_guard` 自愈一致）——接受？
3. **不改 schtasks 命令字面**（`python -m trading` 起 server）——接受？

spec 通过后落 plan（`docs/superpowers/plans/2026-07-31-c5-process-model-and-gate.md`）。

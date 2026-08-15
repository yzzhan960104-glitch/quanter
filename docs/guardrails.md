# 全栈验证护栏 SOP

把「后端独木桥」升级为「端口 + 契约 + 后端单测 + 前端类型 + 前端组件/单测 + E2E」多层护栏，防止前后端漂移、
类型回归、鉴权缺口这类**后端测试抓不到**的问题溜进主干。

> **时效标注（CR-11 · 2026-08-15 刷新）**：本文所有会漂的数字（测试数/检查项数/耗时）均为当日实测，
> 以 `pytest --collect-only` / `python ops/run_checks.py` 当日输出为准——数字过期不构成护栏失效。

---

## 一、护栏总览

| 层 | 工具 | 速度 | 何时跑 | 抓什么 |
|---|---|---|---|---|
| 端口一致性 | `ops/check_ports.py` | 秒级 | 每次 `npm run dev`（predev 自动）+ CI | vite proxy 与后端 API_PORT 漂移（ECONNREFUSED） |
| 前后端契约 | `ops/check_contracts.py` | 秒级 | CI | `api/*.ts` 调用了后端 openapi 不存在的端点（404/契约漂移） |
| 后端单测 | `pytest tests` | ~2min | CI | 1892 项后端逻辑/路由/状态机/鉴权回归（截至 2026-08-15 collect 实测，随测试增删漂移） |
| 前端类型 | `npm run typecheck`（vue-tsc） | ~30s | CI | TS 类型回归 |
| 前端组件/单测 | `npm run test`（vitest + @vue/test-utils） | ~5s | CI | facade 契约姿势（URL/method/timeout）+ 组件渲染/emit 回归 |
| E2E（L3） | `tests/e2e_long_cycle`（pytest 长周期闭环） | ~4.9min | 本地，波次合并前必跑（不入 CI） | plan→pre_open→fill→review 完整闭环行为等价 |

---

## 二、一键 fast gate（CI 与本地同源）

```bash
python ops/run_checks.py
```

串跑端口/契约/后端单测/前端类型/前端组件单测 5 项（① check_ports → ② check_contracts → ③ pytest →
④ vue-tsc → ⑤ vitest），逐项中文报告，任一失败 exit 1。**这是 push 前的标准动作**——
CI 跑的就是这一条，本地过了 CI 必过。

---

## 三、单项跑（定位用）

```bash
python ops/check_ports.py            # 端口一致性（前端 predev 也自动跑这个）
python ops/check_contracts.py        # 前后端契约（exit 0=一致，1=漂移，2=解析失败）
python -m pytest tests -q                # 后端全量单测
python -m pytest tests/test_check_contracts.py -v   # 单个测试文件
npm --prefix presentation/web run typecheck  # 前端类型检查（vue-tsc --noEmit）
npm --prefix presentation/web run test      # 前端组件/单测（vitest：trading/discovery facade 契约 + cockpit 卡片/DatasetTable/视图/router 组件测）
```

> **路径红线（2026-07-25 教训）**：前端目录是 `presentation/web/`（旧 `web/` 已迁走），
> 任何文档/脚本沿用旧前缀都会静默断链——CI 已加「路径未漂移」自检 step 兜底（见第五节）。

契约护栏的纯函数也有单测：`tests/test_check_contracts.py`（17 项）+
`tests/test_check_ports.py`（9 项），两文件合计 26 项（截至 2026-08-15 collect 实测）。

---

## 四、E2E（端到端）

**历史沿革（CR-11 · 2026-08-15 订正）**：原 `tests/e2e/`（Playwright + `with_server.py` 起真实
前后端编排，断言 caisen token 注入链路）**已随 caisen 前端退役整体删除**——其断言的
`/caisen/*` 路由与 `caisen.spec.ts` 已不存在，旧 E2E 段落描述的链路全部失效。

E2E 现由两层承担：

1. **`tests/e2e_long_cycle/test_e2e_long_cycle.py`（pytest，L3 端到端）**——长周期
   plan→pre_open→fill→review 真实闭环行为等价，~4.9min，**每波次合并前必跑**
   （定位与分层策略见 `docs/architecture/09-t7-validation-strategy.md`）。跑法：
   ```bash
   # 务必用 .venv310 绝对 python——曾踩 subprocess 绕过 venv 激活落到系统解释器的坑
   E:/quanter/.venv310/Scripts/python.exe -m pytest tests/e2e_long_cycle -q
   ```
2. **vitest 组件测（gate⑤，入 CI）**——原「浏览器实测」中可组件化的部分（渲染、emit、
   facade 契约姿势）已沉淀为 `presentation/web/src/**/*.spec.ts`（cockpit 卡片、视图、
   router、date 工具、trading/discovery facade）。

> **Windows 残留端口坑（仍适用）**：本地手动起 uvicorn/vite 编排后若残留进程占
> 5173-5177/8000，下次绑定报 `[WinError 10013]`——根因常是 winnat（Hyper-V/WSL）动态保留，
> `netstat` 滞后于实际保留状态，socket bind 实测才准。一键兜底：`python ops/clean_ports.py`
> （清残留 + socket bind 实测端口可绑）。

---

## 五、CI（GitHub Actions）

`.github/workflows/ci.yml`：`push`/`pull_request` 到 master/main + 网页 `workflow_dispatch` 手动触发
+ **每周一 UTC 01:17 `schedule` 自身心跳（CR-10 · 2026-08-15 起）**。
ubuntu runner 装 Python 3.10 + Node 20 → `pip install -r requirements.txt` →
`npm ci --prefix presentation/web` → `python ops/run_checks.py`（跑满 ①-⑤ 5 项 gate，含 gate⑤ vitest）。

- CI 内含「**自检·run_checks 路径未漂移**」前置 step（G1）：`test ! -f scripts/run_checks.py`
  + `test -f ops/run_checks.py` 两条 POSIX 原语断言——scripts/→ops/ 迁移后 CI 曾长期指向已删
  脚本静默断链 19 天，此 step 把「路径漂移」从静默失败升级为显式中文报错；
- 同 ref 重复 push 自动取消旧 run（省额度）；
- fast gate 全过才放行合并；
- E2E（L3，~4.9min）不入 CI，波次合并前本地必跑。

### CI 心跳元守卫（CR-10 · 2026-08-15 新增）

`.github/workflows/ci-heartbeat.yml`：每日 UTC 02:23 独立调度（concurrency 组 `ci-heartbeat`，
不被 push run 取消；`permissions: actions: read` 最小权限），断言 **ci.yml 最近成功 run 距今 ≤ 7 天**，
超龄或查无成功 run 即 workflow 红——把「保护链已死亡 N 天」从无人知晓变成显式失败 + 邮件。
> 边界：GitHub 对 60 天无 commit 活动的 repo 自动停 schedule——长期休眠时本守卫与 ci.yml
> 心跳会一起静默，属平台边界。

---

## 六、熔断/鉴权 fail-closed 语义（CR-11 · 2026-08-15 新增）

风控类缺信息时**一律收紧而非放开**，这是贯穿引擎的三条已裁决红线（评审追溯号）：

- **DG-G2（鉴权 fail-closed）**：live 模式未配 `QUANTER_API_TOKEN` 直接**拒绝启动**（拒所有受保护
  请求的引擎没有资格上线）；同时默认 host 锁 `127.0.0.1` 仅本机回环，防默认监听 `0.0.0.0`
  把下单/熔断端点暴露到局域网。
- **DG-G3（熔断基线缺失 fail-closed）**：熔断基线（`account_daily.start_total_asset`）整链缺失时，
  **live 停调度**（raise `_CriticalHalt`）/**dry 停手**（返 True 跳过开仓）——不选「仅告警不动作」。
- **CR-4（curr_equity 缺失同口径，2026-08-15）**：盘中当前权益（`query_asset` 返空/异常，恰是断线
  场景）缺失与缺基线**同口径 fail-closed**——live 推 CRITICAL + 停调度；dry 保留
  `breaker_skipped` 标记不抛 halt（无真实资金敞口）。

### 风险取向显式声明

**本系统在超卖与漏挂之间系统性选择防超卖（stop_loss.py `_tp_ok=True` / order_state.py
`_tp_already=True` / audit 旧版单向扫描），漏挂方向靠 CR-5 反向扫描+人工补挂兜底——
这是有意的取向，不是遗漏。**

展开（防误读）：DB 查询失败时止损/止盈「已挂判定」保守视为已挂（宁可漏挂人工补，不重复挂超卖）；
代价是漏挂方向曾长期无观测——CR-5（2026-08-15）已补 `audit_ssot.check_fill_position` 反向扫描
（fill 净额≠0 而 position 缺行/为 0 即告警），漏挂从「静默」变「有声 + 人工补挂兜底」。

### emergency_halt 后的实际状态与人工解锁 SOP（CR-3 语义边界 · 2026-08-15 终审对齐）

`emergency_halt()`（gateway_service）触发后的**真实系统态**（不是直觉上的「只拒新单」）：

1. **发单全拒**：`set_risk_halt(True)` 置 `_risk_halted=True + _lock_down=True`，
   `submit_order` 见 `is_blocked`（risk_halted ∨ lock_down）即拒——pre_open 补挂、
   止损/止盈单全部进不来，幂等（重复调用不再重复处理）。
2. **止损监控被健康闸跳过（残余持仓无止损覆盖）**：emergency_halt 同时置
   `_connected=False`，engine `_gw_health_gate` 每轮对 `_stoploss` 返
   「网关未连接」skip。CR-3 注释里的「保监控存活」实为：**APScheduler 调度器存活 +
   `_health_guard` 在岗可人工解锁**；监控体（巡检/止损/撤单）本身在 lock_down 期间
   **不跑**——残余持仓的止损保护中断，直到人工解锁。
3. **自愈被粘滞锁阻断（有意设计）**：`_health_guard` 见 `_risk_halted=True` 只告警
   不重连（风控熔断不得自动解除，防「熔断→自愈→再熔断」循环放血）；网络断线
   （`_risk_halted=False`）才走 60s 自愈重连。

**人工解锁路径**（唯一解锁口，无 API 端点——`POST /api/v1/trading/emergency_halt`
只置锁不解锁）：

```
# 引擎进程内（如 REPL/运维脚本持 gw 引用）：
gw = get_gateway()
gw.clear_risk_halt()      # 仅清 _risk_halted；_lock_down/_connected 交给重连恢复
# 之后 _health_guard 下轮（60s 内）自动重连 → connect 成功清 _lock_down、置
# _connected=True → 健康闸过 → _stoploss 监控恢复巡检。
```

**解锁前操作员须知**：
- 先查熔断原因再解锁——`logs/alerts.log`（CR-7 本地通道必有痕）与 `trading.engine`
  logger 的 CRITICAL（CR-3 触发/基线缺失/评估失明三分支文案各异），勿盲解；
- 核对柜台真实持仓与未终态单（CR-3 触发分支已尽力撤单，但 `unconfirmed>0` 时敞口
  可能残留），评估「监控被跳过期间」的无止损覆盖敞口是否须先人工平仓；
- 解锁即恢复自动发单权限——确认熔断根因（如基线缺失已补采、行情已恢复）已消除，
  否则下一轮巡检可能再次触发（5min 节流评估）。

---

## 七、开发流程建议

1. **改代码后**：`python ops/run_checks.py`（~2min，5 项门禁）。
2. **碰前端 UI/交互/鉴权/契约后**：跑 gate⑤ vitest（`npm --prefix presentation/web run test`）
   补/跑组件测；组件测覆盖不了的跨层闭环，L3 `tests/e2e_long_cycle` 兜底。
3. **push/PR**：CI 自动复跑同一套（外加路径自检 + 每周一心跳 schedule），本地过则 CI 必过。
4. **加新端点/改 facade**：契约护栏会在 CI 暴露漂移；**加新 UI 交互则在
   `presentation/web/src/**` 补 `.spec.ts` 并跑 gate⑤**。

---

## 八、已知 follow-up（非本次范围）

- 契约护栏目前只比「端点路径 + 方法」，不比响应字段（如 `CandidatePlan` 字段级）——
  字段级契约可走 openapi→TS codegen（更重，下一层）。
- 存量 warning（`pct_change` FutureWarning 等）非阻断，建议择机清理；
  `emt_gateway.py` coroutine never awaited 是真实潜在 bug，优先处理。
- 本 SOP 的会漂数字（测试数/gate 耗时）随波次演进——改测试体量时顺手更新本文
  「截至日期」标注，防再次过期（CR-11 的教训正是 118 行文档挂了 3 周没人发现）。

# 全库评审清偿与优化改造方案（0828 评审 → 8 波次）

> 日期：2026-08-28 ｜ 状态：**W0-W8 全部执行完毕**（九提交 63e52caf→cb8ad1a4，分支 `fix/full-repo-review-0828`）
> 回归：后端 2286 / emquant 782 / 前端 32 全绿；三步清查净；schtasks 四任务已重注册（bat 输出重定向实弹验证 13:39 拍 exit 0）
> 遗留两件：①W1 产物（stamp aa101850）待用户手动部署至 d9324346 策略目录；②B3 复验待 R10 并行跑完再launch（`python diag/r6_10_b3_loop.py --hours 0`，R10 正占 CPU/trials 库）
> 依据：2026-08-28 全库深度评审（6 代理分域 + pilot_body 资金链人工全文双读 + 全部 P0/P1 逐条亲验，结论见会话报告与记忆 `full-repo-review-0828`）
> 基线：master `3dd87d21`，emquant 763 / 后端 1839 / 前端 32 全绿
> 范围：4 P0 + 12 P1 + P2/P3 家族，全部为**代码/配置修复，零参数变更**（params 指纹 7fe3d5b3 全程不动）

---

## 一、目标终态

```
实盘面（掘金唯一平台）                     研究面                          本地腿
├ 卖出守卫 purpose 全覆盖（反转语义）      ├ 搜索热路径真用扩展切分          ├（裁决A）完成退役：
├ 买向 purpose 单源 {OPEN,CHASE}          ├ engine_hash 进 trial 去重键     │  删净 engine live 面死引用
├ place→save 崩溃窗闭合（逐单落盘）        ├ TPE 收敛模式可真跑              │  保留 pipeline/state_store/
├ 成交逐笔 FILL 审计                      ├ D2 等不可部署维度废弃标注        │  job_ledger（ops_sched 依赖）
├ 钉钉监控复活（通道装配+同步发送）        └ outer-as-selection 入 ADR       └（裁决B）修三处复活必修项
└ 数据湖：adj 校验/当日重拉/内容闸
```

**执行原则**（本仓既定纪律的沿用）：
1. 实盘面先行：监控复活 → 卖出守卫 → 数据完整性，研究面与卫生项排后；
2. 每波独立分支、独立可验证、独立可回滚（`debt/` 或 `fix/` 前缀，worktree 并行，开工先 `git status`）；
3. emquant 代码修复走产物链纪律：body 修改 → `build_pilot` 重组装逐字节校验 → 双 venv 实跑 → **用户手动部署**（粘贴至 d9324346 策略目录并重启策略进程；旧腿先停防双策略同账户）；
4. 删除波收尾必跑三步清查（活代码 grep 被删符号 / 配置面失读者键 / 无测试工具目录）；
5. 每波后对应测试面全绿重推；改 `trading/orchestrate/pipeline` 的测试须 patch `_spawn_all_datasets_sync`。

---

## 二、波次总览

| 波次 | 主题 | 清偿对象 | 工作量 | 部署依赖 |
|---|---|---|---|---|
| W0 | 监控复活 | P0-2（ops 钉钉零装配） | 0.5d | schtasks 重注册 |
| W1 | 实盘守卫闭环 | P0-1 + P0-4 + emquant P1×4 | 1d | 产物重建+用户手动部署 |
| W2 | 数据湖完整性 | P0-3 + data P1 部分 | 1.5d | 湖重算（一次） |
| W3 | 运维误报清偿 | audit_ssot/晨检/dev.py/死注册 | 0.5d | schtasks 重注册 |
| W4 | 前端 cockpit 处置 | 前端 P1×2 | 0.5-1d | **裁决点①** |
| W5 | 研究面三小修+方法论 | research P1×4 + ADR | 1d | **裁决点③④** |
| W6 | trading 腿处置 | trading P1×3（dormant） | 1d | **裁决点②** |
| W7/W8 | QMT 残留+卫生 | P3 家族 | 各 0.5d | 可并入 W6-A |

---

## 三、逐波详情

### W0 监控复活（P0-2 —— 当前最高优先级）

**问题回顾**：`ops/gm_ops_common.py:69-79` `notify()` 用裸 `get_default()`（零通道、零本地留痕）→ 晨检/EOD 播报/看护/数据熔断告警自 08-27 起全部静默；`fire_and_forget` daemon 线程在短命进程退出时被掐断构成第二层。

**修法**：
1. `gm_ops_common.notify` 改**同步发送**（短命批处理不搞后台线程）：
   ```python
   def notify(level: str, msg: str) -> None:
       try:
           from infra.notifier import build_default_manager
           import asyncio
           asyncio.run(build_default_manager().notify_risk_event(msg, level))
       except Exception as e:
           print(f"[notify 降级 print] {level} {msg}（通道失败：{e!r}）")   # 最后一层兜底进 schtasks 日志
   ```
   Why 同步而非 fire_and_forget+宽限：ops 脚本无后续逻辑，3-10s 阻塞零代价，消灭整类退出竞态；`build_default_manager` 幂等守卫已有；`infra.notifier` 的引擎侧用法不动。
2. 三件套 + `run_data_check._alert` 同步切换到该实现（`run_data_check` 自有 `_alert` 也补装配）。
3. schtasks `/TR` 补输出重定向：`"...python.exe" "...script.py" >> logs\ops_schtask.log 2>&1`（对照 `ops/run_audit.bat` 范式），三件套注册函数各改一行。

**测试**：新增 `tests/ops/test_notify_channel_assembly.py`——monkeypatch `DingTalkChannel.send` 断言 `notify()` 后通道被装配且被调用（钉死"零通道静默"回归）；现有 29 绿重推。

**验收**：手动 `python ops/emquant_morning_check.py` → 钉钉群实收全绿播报；`logs/alerts.log` 有行。

**回滚**：单文件改动，revert 即回。

---

### W1 实盘卖出守卫 + emquant 崩溃窗口（P0-1 / P0-4 / emquant P1）

**W1-1 `_has_open_sell` 反转语义（P0-1 核心）**
- 现状：白名单 `("EXPIRE","STOP_LOSS","TP1","TP2")`（`pilot_body.py:1360`）漏 `TP2_SHARE`/`TP2_EOD_SWEEP`/`TIME_STOP`——当前反转 regime 下 tp2→tp1 连续冲高可双倍卖出。
- 修法（反转式，根治"新增枚举忘扩白名单"模式）：
  ```python
  _BUY_SIDE_PURPOSES = frozenset({"OPEN", "CHASE"})
  def _has_open_sell(self, sym) -> bool:
      for o in self.state["orders"].values():
          if (o.get("symbol") == sym
                  and o.get("purpose") not in _BUY_SIDE_PURPOSES   # 卖向=非买向（新 reason 自动覆盖）
                  and o.get("status") not in _TERMINAL_ORDER_STATES):
              return True
      return False
  ```
  UNKNOWN（柜台吸收单）随之纳入卖向守卫：保守方向（在途 UNKNOWN 挡一轮挂卖，方向安全）。
- 测试：参数化用例——7 个 `reason.upper()` 目的逐一落单后断言 `_has_open_sell(sym) is True`；OPEN/CHASE 落单后 `is False`。

**W1-2 买向 purpose 单源 {OPEN, CHASE}（P0-4）**
- 三处替换：`_open_buy_amount`（:1268 `("OPEN","UNKNOWN")` → `not in _BUY_SIDE_PURPOSES` 的补集语义：UNKNOWN 保守保留计入）、⑤'' 回补 `live_open`（:1951）、③ 信号防重 `open_buy`（:1828）。
- chase 落单（on_tick :2169）补一行 `st.setdefault("placed",{}).setdefault(today,[]).append(cid)`——单日闸"在场效果"语义对 chase 生效。
- 测试：chase 在途时 ③ 新信号被 SIGNAL_SKIP_HELD 挡；⑤'' 不回补；CAP 额度把 chase 计入。

**W1-3 崩溃窗口闭合（emquant P1-1/P1-2）**
- ⑤/⑤''/on_tick 卖单三处：`place → state 写入 → save_state()` 三步压到循环内逐单完成（`pilot_body.py:1890-1919/:1983-2012/:2206-2238` 各加一行落盘）。tick 热路径维持"有动作才落盘"，只是把落盘点从函数尾提前到每单成交后。
- 重启后守卫看不到柜台在途单的根因（state 口径）就此闭合；`_repair_pass` 启动回补不再能双挂。

**W1-4 成交逐笔 FILL 审计（emquant P2-2 升格顺带）**
- `absorb_reality` 加可选参 `fill_sink: callable|None`：买/卖向 fill 增量发生时回调 `(symbol, side, delta, price, cl_ord_id)`；`reconcile` 传入 `lambda **kw: self._audit("FILL", **kw)`。纯函数契约不变（默认 None 零影响），现有测试零改动。

**W1-5 export_snapshot 锚更新（emquant P2-1）**
- `:253` `assert trade_cfg["pos_cap"] == 0.05` → `== 0.075`，注释钉死"硬闸 4 并×7.5% 联动（c243da10 用户裁决）"；顺带核对 `PILOT_MAX_POSITION_PCT` 与快照同值断言可加一行。

**W1-6 注释漂移批量修正（P3 顺带，防晨检误导）**
- `pilot_body.py`：:527-529"写死 2/5%"→4/7.5%、:611-613 trailing 三件=0/0/0 → 10/0.05/0.5（删"退化固定止损"表述）、:1856/:2767 注释 pos_cap=0.05→0.075、:195 window=80→60；`tools/gm_data_pull.py:189` 同步；快照 notes "ACTIVE 实验=8"→champion cooldown=0。

**部署 runbook（产物链）**：
1. body 修改后 `python emquant/build_pilot.py` → 重组装逐字节 diff 校验 + 指纹复算（应仍= `7fe3d5b3f4a04786`，参数未动）；
2. 双 venv 实跑：`.venv310` 识别内核等价测试 + `.venv_emquant` 离线 FakeGm 全事件链；
3. emquant 测试面全绿（763+新增）；
4. **用户手动**：停旧腿策略进程 → 粘贴产物至 `C:\Users\yzzhan\.emgm3\projects\d9324346-...\` → 重启 → 核对 INIT 行 `build_stamp` 为新值。
- 回滚：产物单文件，回滚=贴回旧版（git 内两版皆可取）。

---

### W2 数据湖完整性（P0-3 + data P1）

**W2-1 adj 完整性校验（主路径对齐 `_recompute_symbol` 守卫）**
- `sync_daily_incremental.py:196-212` merge 后：`nan_adj = merged["adj_factor"].isna()` 任一存在 → 该交易日**整日拒落盘** + CRITICAL 告警（走 W0 修好的 notifier）+ 非零退出；绝不写出 NaN 价格行。
**W2-2 当日重拉与时间闸**
- `:165-168` `d0 >= today` 短路改为：`d0 == today` 时若 `--refetch-today` 显式传参则重取当日（dedup keep="last" 已有，天然覆盖）；默认行为不变。
- 入口加**交易时段硬闸**：本地时钟 ∈ [09:15, 15:05) 且非 `--allow-intraday` → 拒跑（防盘中手动同步写半截 bar；17:30 pipeline 与跨 18:00 补跑不受影响）。
**W2-3 freshness 内容闸（日级行数环比）**
- `data/freshness.py`：PASS 判据在 max-date 之外加"当日行数 ≥ 昨日行数×0.9"（日级部分写入/半截数据 FAIL）；复用骤降检测的口径，日粒度。
**W2-4 原子写收口**
- `data/tools/sync_incremental.py:277/298/303` 三处直写 → `safe_overwrite`（同仓主湖标准）。
**W2-5 除权重算失败留痕重试**
- `:239-255` 失败标的写 `data_lake/.syncing/pending_recompute.json` sidecar（原子写）+ 次轮同步优先重试；连续失败 CRITICAL。
**W2-6 宏观月频发布滞后（研究效度纠偏）**
- `sync_macro_credit.py align_to_daily`：月频锚点 +15 自然日（对齐 FRED 月频标准 `fetcher.py:568-573`，社融/M2 央行次月 10-15 日公布）。**注意**：macro 湖历史值整体后移，service/research 消费读数将下移——这是纠偏不是回归；重导 macro 湖一次。

**测试**：单测注入 adj 空响应/部分行断言拒落盘；时间闸边界（09:14/09:15/15:04/15:05）钉值；freshness 部分行 FAIL 用例；pipeline 相关测试 patch `_spawn_all_datasets_sync`。
**验收**：连续两日 18:00 pipeline 全绿 + `run_data_check` t1/t2 PASS + 人工注入故障演练一次。

---

### W3 运维误报清偿

| 项 | 修法 |
|---|---|
| audit_ssot miniQMT 恒 FAIL | `check_client_process` 改查掘金链进程（emgm3/gmterm-serv），名目改"掘金终端进程"；探测脚本复用 `gm_terminal_guard` 的探测段 |
| 晨检周末误报 | `emquant_morning_check.main` 加交易日闸：非交易日直接 INFO 播报"非交易日跳过"退出 0（闸复用 `gm_terminal_guard._in_market_window` 的周末判定 + gm 日历可选） |
| dev.py 死路径 | `ops/dev.py:61` `scripts/clean_ports.py` → `ops/clean_ports.py` |
| 死 schtasks 注册 | `manage_ops_schtasks.register_guard` 删除 + `RETIRED_TASKS` 追加 `QuanterMiniQmtGuard`（本机若仍挂着即清退）；`register` CLI 子命令同步收口 |
| EOD 非交易日全零播报（P3 顺带） | 同晨检闸：非交易日跳过或标注"非交易日" |

**验收**：周末手动跑晨检/EOD 零告警；audit_ssot 全绿退出 0。

---

### W4 前端 cockpit 处置 —— **裁决点①**

- **方案 A（最小·推荐先行）**：cockpit 三卡（StatusCard/AssetCard/TradesTable）+ `/jobs` 视图转"掘金退役占位态"（静态说明+跳转掘金终端指引）；删 `api/trading.ts` 死 facade 与 `trading.spec.ts` 死契约测试；`client.ts` 拦截器对 404 且路由前缀已退役的场景静默（或组件级 catch）。0.5d。
- **方案 B（恢复价值·新功能）**：server 加只读代理 `/api/v1/gm/*` 转发 7002 网关（token 从策略目录 runtime.json 读或 env 注入，绝不入前端 bundle），三卡改接新源（持仓/资金/委托/成交流水 schema 已实测通）。1-1.5d，含前端契约测试重建。
- 两案共同项：`paramMeta.ts` 漏删件与其守护测试删除（`/lab` 已删）。

---

### W5 研究面修复 + 方法论 —— **裁决点③④**

**W5-1 worker 切分修复**：`worker._init_worker` 增参 `split_kind: str = "holdout"`（简单类型可 pickle），extended 时 worker 内构造 `extended_split()`；`runner.eval_batch`/`EvalPool` 透传。旧 trial 不迁移（split_tag 已区分口径），README 注记"0828 前 `holdout_2021_2025` 标签 trial 实际 inner=2025"。
**W5-2 engine_hash 入去重键**：`trial_id_of(params, snapshot_hash, seed, engine_hash="")` 追加可选参进签名哈希；写入链（`_persist_trial`）传现算 engine_hash；`read_trials_by_snapshot`/`cmd_champions` 增可选 `engine_hash` 过滤（默认当前指纹——新旧 trial 天然分流）；删 `r5_pipeline.py:277` 魔法值 `'4b54e9475dd8'`。
**W5-3 TPE 收敛模式溢出**：`_tpe_round` 的 timeout 改辅助函数：`deadline is None → 21600`（6h 硬顶）否则 `max(600, int(_remaining(deadline)) - 45*60)`；同族检查 r4/r6_8 循环。
**W5-4 D2 不可部署维度**：`r9_breadth_loop.py` D2_dow 维度入口加显式 `KNOWN_NON_DEPLOYABLE` 标注 + 预登记清单同步注明"作用于 buy_date（未来随机变量），采纳闸应恒 VETO"。
**W5-5（裁决③）outer-as-selection ADR**：立 `docs/adr/` 条目正式记录——2026 outer 自 R4 起为选择段、R8/R9 终审非处女 holdout、诚实读数=deployable 中位、未来采纳闸迁移 `portfolio_metrics_dual` 口径、fresh_window 成熟后为新窗口唯一裁决来源。
**W5-6（裁决④）B3 冠军复验**：修复后（W5-1/3 生效）跑一轮收敛模式复核 B3 链——预期结论不变（TPE 臂本就未参与历史选择），但闭合"三臂宣称与事实不符"的账。

---

### W6 trading dormant 腿处置 —— **裁决点②**

- **方案 A（完成退役·推荐）**：按 0827 退役方案把"退役对象"剩余部分删净——`engine.py` live 面、`phases/stop_loss.py`、`phases/pre_open.py`/`exit.py`/`post_close.py` 中依赖 gw 的段、`io/quotes.py`/`io/orders.py`/`io/positions.py` 死引用、`gateway_service.get_gateway`（含 `main.py` lifespan 调用点）。**保留**：`orchestrate/pipeline.py`、`state_store.py`、`job_ledger.py`、`compute/*` 纯判定、`calendar/clock`（ops_sched 研究面四 cron 与 pilot 对拍参照的依赖）。收尾跑三步清查。P1-13/14/15（撤当日单/chase 死链/止损限价）随删除自然消账。
- **方案 B（保复活）**：修三处——`io/breaker._cancel_via_broker_query` 加"当日单跳过"守卫（对齐 pilot I-2）、`io/quotes.py:40` 死 import 改 gm 通道或显式桩+退役 docstring、`stop_loss.py:444` 止损改 `price=None` 市价（或挂跌停价对齐 `close_expired_positions:884` 范式）。头部加"复活前须全面复核"横幅注释。
- 无论 A/B：`trading/__init__.py:45` 懒加载即炸的 `QmtExecutionGateway` 导出、`gateway_service` 失实日志文案一并处理（并入 W7）。

---

### W7 QMT 残留清偿（若 W6 选 A 则大半并入）

`scripts/qmt_clear_session_lock.py` 删；`broker/base.py` 文档改写（宣称现役的段落）；`tests/e2e_long_cycle/conftest.py:72` QMT_ACCOUNT_ID 注入删；`scripts/run_trading_engine.bat` 等死 bat 归档删除；`.env.example` 清 QMT/EMT 死键；`.env` 本机清 9 个 EMT_* 键（含带实值的 QUOTE_PASSWORD/USER）与无读者 QMT_* 键（本机运维动作，不入 git）；`trading/__init__` 懒加载与 `__all__`、`get_qmt_gateway` 别名、`broker_ports.py` 契约注释。

### W8 P3 卫生（可选/顺手波）

`pipeline.py:228` 死条件 `melted`；`job_ledger.finish_run` 时间源统一 clock；audit CSV 公式注入转义（detail 列首字符 `=`/`+`/`-`/`@` 前缀 `'`）；`notifier` 异常日志 URL 脱敏（token/secret 掩码）；state.pkl 跨日清理（after_close 归档昨日 `placed` 键与终态 orders，工具化 `archive_terminal_state` 逻辑收编）；tests 供养死路径瘦身（P2-3，可缓）。

---

## 四、用户裁决点汇总

| # | 裁决 | 选项 | 推荐 |
|---|---|---|---|
| ① | W4 前端 | A 占位退役 / B 改接掘金 7002 代理 | 先 A 后 B（B 可立项为新需求） |
| ② | W6 trading 腿 | A 完成退役 / B 修三处保复活 | A（掘金唯一平台已裁决，死代码供养是负资产；git 历史可复活） |
| ③ | W5-5 | 立 outer-as-selection ADR | 立（研究认识论正式化，衔接 fresh_window 计划） |
| ④ | W5-6 | B3 冠军修复后复验 | 复验一轮（预期结论不变，闭账） |
| ⑤ | 节奏 | W0-W3 先行实盘面 / 全量 8 波排期 | W0-W3 先行；W1 部署窗口择下一交易日盘前 |

---

## 五、验收与回归矩阵

| 波 | 新增测试 | 既有面 | 部署动作 |
|---|---|---|---|
| W0 | notify 装配钉值 | ops 29 | 三件套 schtasks 重注册（带日志重定向） |
| W1 | purpose 守卫参数化×9 / 逐单落盘 / FILL sink | emquant 763+ | 产物重建+指纹复算+用户手动部署 |
| W2 | adj 拒写/时间闸/内容闸 | data+pipeline 全量 | macro 湖重导一次 |
| W3 | 掘金进程检查/交易日闸 | ops+scripts | audit schtasks 重注册 |
| W4 | —（删死契约） | 前端 32 | dist 重建 |
| W5 | worker 切分/engine_hash/timeout | discovery+diag 抽样 | — |
| W6/W7 | 三步清查脚本 | 后端 1839 | — |

## 六、风险与回滚总述

- 全部波次零参数变更，`params_snapshot` 指纹 `7fe3d5b3f4a04786` 不动（W1-5 仅改断言锚到已裁决值）；
- W2-6 宏观滞后会使 macro 湖历史值变化——研究重测须遵守"同湖重跑原脚本"纪律；
- 每波单分支单 PR 粒度，revert 即回；W1 产物部署回滚=贴回旧产物；
- 最大不确定性：W1-1 修复后 `_has_open_sell` 对 UNKNOWN 的保守纳入可能在极端场景多挡一轮挂卖（方向安全，audit 可见）——接受。

---

## 七、双轴 code review 收口留档（2026-08-28 下午）

评审发现与处置（全部亲验后清偿）：
- **W2-1 告警面闭合**：sync_daily_incremental 失败路径补 best-effort CRITICAL notify
  （原方案明文要求、首跑缺失）；W2-5 连续失败升 critical。
- **W5-2 补完**：cmd_champions 加 engine_hash 过滤（--all-engines 排障口）。
- **Standards**：r6_8 补 `_tpe_timeout` 同构副本（含事故注记）；`_capsafe` 死函数删；
  order_state 死 import 删；pilot_body pos_cap 缺省对齐 0.075（两处）；runner split
  嗅探改精确映射+未知形态 WARNING 回落；gm.py 抽 `_cfg_token`；trades_export 抽
  `_fmt_ts` + direction 双口径注记（upper/lower 是两个消费面契约，非漂移）。

**对方案明文的三处显式偏离**（评审裁定可接受，留档）：
1. `/jobs` 视图采用【删除】而非 §W4-A 字面"占位态"——B 方案接管三卡数据源后占位无
   观测价值，路由级删除更彻底（前端死契约测试同步清除）。
2. W8 公式注入【不改】——复核 detail 列恒为 json.dumps(dict) 以 `{` 开头，结构性
   不可能以 =/+/-/@ 起始，Excel 公式注入面不存在。
3. W6 保留面超方案清单 7 模块（data_ctx/trading_plan/types/critical/single_instance/
   risk_ctrl/order_state 裁剪版）——逐模块有活消费者（broadcast/process_topology/
   backtest.mock_broker/state_store），trading/__init__ 已逐模块列明。

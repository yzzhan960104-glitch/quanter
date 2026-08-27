# 深度代码审查报告 — master 全库（2026-08-04）

> 审查方法:9 个并行 sub-agent 按模块深度审查,对照仓库 `CLAUDE.md` 标准(全中文/极简/Grill 拷问/Definition of Done)+ Fowler smell baseline。
> 覆盖:trading(engine/state/compute/io/tools)、discovery、data+backtest、broker+strategies+research+experiment、前端 presentation、infra/config/ops/broadcast/compute_unit。
> 跳过:`xtquant/`(20957 行 vendored QMT SDK,第三方)、`tests/`(33678 行测试代码)。
> 生产代码规模:~37k 行 Python + ~2600 行 Vue/TS。

---

## 1. 执行摘要

**整体评价**:仓库工程质量**显著高于一般量化工程**——CLAUDE.md 全中文协议像素级贯彻,C-1~C-8 系列重构(错误分级/进程模型/时间统一/启动补跑)把实盘钱风险当一等人对待,颈线法 3 缺口已实质修复,注释普遍深挖 Why 而非 What。Karpathy 极简纪律在 compute/* 纯函数层贯彻得最彻底(零 pandas/numpy 黑盒,dataclass + 显式数学)。

**但存在 4 个系统性根因**(多模块独立指向同一问题,优先级最高):

1. **撤单 CANCELLED 状态撒谎**——3 个模块(state_store / io.breaker / broker.qmt)各自在「撤单指令发出但柜台未确认」时就标 CANCELLED 终态,模拟盘 1-2s 主推延迟窗口内状态机撒谎 → 重复废单 / 敞口误释放。
2. **auto_publish 护栏仍 fail-open**——commit 0459639f 只堵了已知 TypeError,但 `_active_outer_ann` 仍存在 `db_path` 不透传 + `except Exception: return None` 双层缺陷,护栏可被任何下游异常静默绕过 → 0% 垃圾冠军照样 publish 成 DRAFT。
3. **部分成交精度未量化**——`position_book.apply_fill` 浮点裸累加无小数位/NaN/零价防护,且 broker 层不返回 `traded_volume/avg_price`,实盘部分成交后账本与 broker 持仓口径分歧 → reconcile 误报 drift。
4. **engine.py 3223 行巨石**——五条生命周期 + 状态机 + 网关 health + 成交回报 handler 全揉一文件,任一改动都是 Shotgun Surgery 温床,`__init__.py` 已声明拆分蓝图但未落地。

---

## 2. 横切发现(系统性根因 — 多模块联动)

### 🔴 XC-1 撤单 CANCELLED 状态撒谎(live P0,横切 3 模块)

| 模块 | 位置 | 问题 |
|------|------|------|
| trading/state | `state_store.py:413` `cancel_order_by_broker_oid_db` | 无条件 `SET state='CANCELLED'`,不校验当前态,FILLED 单会被撤单主推覆盖 |
| trading/io | `io/breaker.py:124-142` | `cancel_by_oid_fn` 返回非 FAILED 后立即回写 CANCELLED,然后才 `await confirm_fn`,未确认只 `n_unconfirmed+=1` |
| broker | `broker/qmt.py:990` `cancel_order` | 注释自承「rc==0 仅表指令已发出,非终态」却返 `OrderState.CANCELLED` 终态 |

**根因**:QMT 模拟盘实测 CANCELLED 主推延迟 1-2s(`qmt-live-smoke-findings`),三处实现都「先标终态后确认」,顺序反了。engine 拿到 CANCELLED 就释放敞口 slot / 推进状态机,而柜台真实状态仍 PENDING → 重复废单风险(`trading-gap4` 列为 live P0)。

**统一修改方案**:
- 引入 `PENDING_CANCEL` / `CANCEL_REQUESTED` 中间态,DB 回写**移到 `confirmed=True` 分支后**;
- `cancel_order_by_broker_oid_db` 加 WHERE 守卫:`WHERE state IN ('PENDING','SUBMITTED','PARTIAL')`,rowcount=0 时 CRITICAL 警告「终态单被撤单主推覆盖,人工复核」;
- OrderStateMachine 拒绝据未确认 CANCELLED 释放敞口 slot。

### 🔴 XC-2 auto_publish 护栏 fail-open(2 层缺陷叠加)

`research/discovery_bridge.py` 的护栏物理意图是「失败必须 fail-closed,不建 DRAFT」。commit 0459639f 修复了 `list_versions` 缺参 TypeError,但:

- **层 1**(discovery agent 发现):`bridge.py:137` `active_ann = _active_outer_ann()` 仍不透传 `db_path`,非默认库路径下 ACTIVE 比对读错库;
- **层 2**(broker agent 发现):`bridge.py:120-121` `except Exception: return None` 把任何下游 store/SQLite 异常降级为 None,消费方 `if active_ann is not None and outer <= active_ann` 把 None 解释成「ACTIVE 不存在=无门槛」→ fail-open。

**修改方案**:
- `_active_outer_ann(db_path)` 显式透传;
- `except Exception: return None` 改为 raise(让 `auto_publish_champion` 外层 try 兜底跳过),或返哨兵 `float('-inf')` 让「不优于 ACTIVE」恒成立;
- 加回归测试:`_active_outer_ann` 抛异常时 `auto_publish_champion` 必须返 None(不建 DRAFT)。

### 🔴 XC-3 部分成交精度未量化(账本漂移)

- `position_book.py:166-206` / `state_store.py:567-592`:`delta=float(qty)`、`new_avg=(old_qty*old_avg+qty*price)/new_qty` 浮点裸运算,无 Decimal/无精度截断/无 NaN 校验。`price=0`(误推/停牌)会让 avg 归零 → 浮盈虚增 → 风控误判;`new_qty` 归零走 DELETE 但浮点可能算出 1e-16 残留 → 对账幽灵行。
- `broker/qmt.py` `OrderResult.filled_qty/avg_price` 字段恒空,不聚合 `traded_volume` 增量。

**修改方案**:
- `if not (qty>0 and price>0): raise` 显式拒零/负/NaN;
- `new_qty` 归零判定改 `abs(new_qty)<1e-6`;avg 计算 `round(...,6)`;
- QMT `submit_order` 从 `on_stock_order` 累计 traded_volume/avg_price 填 OrderResult。

### 🟠 XC-4 engine.py 3223 行巨石(Shotgun Surgery 温床)

一个文件同时承载:① 模块级业务函数 `eod_plan/pre_open/stop_loss_monitor/post_close/place_take_profit`(661-1968)② `TradingEngine` 类——调度 + `_health_guard`/`_halt`/`_gw_health_gate`/`_pre_open_gate`/`bootstrap`(1968-3222)③ 成交回报状态机 `_handle_order_update`/`_advance_order_state_from_status`(2920-3167)。`__init__.py` 已声明 types/compute/state/io/orchestrate 五层蓝图但**实际未拆**。

**拆分蓝图**(strangler 渐进,行为零变更):
1. `orchestrate/stop_loss.py` ← `stop_loss_monitor`(最高风险、最长方法,~370 行独立闭环);
2. `orchestrate/pre_open.py` ← `pre_open` + `_pre_open_impl`(~270 行),顺带把 `_ACTIVE_ENGINE` 全局反查(H2)收编为实例方法;
3. `state/order_update_handler.py` ← `_handle_order_update` + `_advance_order_state_from_status`;
4. `engine.py` 收敛到 `TradingEngine` + 调度装配,**目标 < 800 行**;
5. 顶部留 re-export 保 `monkeypatch engine.xxx` 测试兼容。

---

## 3. 🔴 CRITICAL 修改清单

> 除 XC-1/2/3 外的单点 CRITICAL。

### C-1 止损盲单:`should_trigger_stop` 不防 NaN
- 📍 `trading/compute/stop.py:121-146`
- ❓ docstring 把「price 为 None/NaN 跳过」责任甩给调用方(编排层),但 `io/quotes.py:31` 的 `last_price` 可能为 None/NaN,`NaN <= 任何数` IEEE 754 下恒 False → 静默漏判止损。
- ✏️ 函数内显式 `if price is None or price != price: return False` + logger.warning,把「无价跳过」从约定升级为契约。

### C-2 trailing 首轮哨兵:`update_trailing_stop(prev_stop=0.0)`
- 📍 `trading/compute/stop.py:167-187`
- ❓ docstring 写「首轮可传 0.0」,`return max(new_stop, prev_stop)`。若调用方误传 0.0 且 atr/k 极小 → `new_stop=high-0.001` 远高于真实前低,止损线虚高 → 后续回撤立刻触发卖飞。
- ✏️ 改签 `prev_stop: float | None`,None 时直接 `return new_stop`;或要求首轮传 `compute_stop_price(...)` 锚定值,docstring 钉死首轮契约。

### C-3 双 apply_fill 路径漂移
- 📍 `position_book.py:137` apply_fill vs `state_store.py:550` apply_fill_to_position
- ❓ 两条独立写同一张 position 表,加权 avg 公式相同但调用方不同;`state_store.apply_fill_to_position` 当前无调用方(死代码)。任一方公式漂移(如送股/转股除权时 avg 应调整)→ 账本与 broker drift。
- ✏️ 删除 `apply_fill_to_position` 死代码(YAGNI),或注释显式标注「未启用·保留扩展点·生产路径只走 position_book」。

### C-4 硬编码涨停价买单(known issue 复发)
- 📍 `trading/tools/qmt_smoke.py:96` + `qmt_live_smoke.py:206` `price=5.0`
- ❓ 固定 5.0 元:对 ETF(1.x)是涨停上方废价触发 REJECTED,对茅台(1685)是跌停下方远离盘口永远不成交但占额度。`qmt-live-smoke-findings` 已明确「模拟盘拒涨停价买单(改卖一价)」,正确实现 `min(last*0.95, last-0.3)` 就在同目录 `realorder.py` 却未回填。
- ✏️ 回填 realorder 安全挂单价算法,或退役两脚本由 `qmt_live_smoke_realorder.py` 取代。

### C-5 headless 真单脚本无 AUTO 护栏
- 📍 `trading/tools/qmt_live_smoke_realorder.py:80-117`
- ❓ 文件名宣称 headless 自动,docstring 仅写「模拟仓无顾忌」,代码层对 `AUTO_TRADE_MODE=live` / .env 漂移零防御(对比 moutai.py 有 `QMT_SMOKE_AUTO` + `_gate()` 双层把守)。`.env` 全自动 live 是用户预期配置,一旦生效此脚本静默真发单。
- ✏️ 文件首行 `assert os.getenv("AUTO_TRADE_MODE") != "live"`;默认 `DRY_RUN_GATE=1` 环境变量。

### C-6 除权标的历史 qfq 不重算
- 📍 `data/tools/sync_daily_incremental.py:145-157`
- ❓ 增量同步只 append 新日期,除权标的(adj 在窗口变化)整段历史 `price_qfq = raw × adj / latest_adj` 基准都应重算却未做。除权后该标的历史 qfq 全部错位一个除权断崖,颈线法会把除权缺口误判为突破/止损。分红季(5-7 月)批量爆发。脚本已 warning 但 follow-up 未做。
- ✏️ 除权标的整段重算(从 lake 起点到 today),或 `scan_integrity` 加规则 6「除权断崖检测」。

### C-7 钉钉 unified-app-id 硬编码进 git
- 📍 `infra/tools/dingtalk_review_bridge.py:16` + `.superpowers/sdd/task-5-broadcast-report.md:69,77,87`
- ❓ 真实 `REVIEW_BOT_UNIFIED_APP_ID=e2695383-...` 硬编码在注释与已入库的 `.superpowers/` doc 里。虽非私钥,但泄漏后可冒充 review 桥接端点。
- ✏️ 注释改 `<REVIEW_BOT_UNIFIED_APP_ID>` 占位;`.env.example` 留空为范式;核对其余 docs。

### C-8 AKShare 死代码残留(退役决议未贯彻)
- 📍 `data/clients/akshare_client.py:62-330`
- ❓ memory `akshare-jqdata-retired` 明确 AKShare 已退役 7 项、Tushare 唯一源,但 `fetch_margin_detail`/`fetch_sector_fund_flow`/`fetch_individual_fund_flow` 三方法仍在(非 macro_credit 路径)。误导后续维护者以为可调。
- ✏️ 逐方法 grep 调用方,无 caller 整删,收敛「唯一源 Tushare」决议。

---

## 4. 🟠 HIGH 修改清单(精选)

### H-1 `cancel_order_by_broker_oid_db` 撤单主推覆盖终态
(并入 XC-1,见上)

### H-2 `add_order_qty` 非原子(止盈差额补挂竞态)
- 📍 `state_store.py:428-460`
- ❓ SELECT→UPDATE→INSERT 三步同连接但无行锁,跨进程(dry_run test + live engine 共库)无防护。止盈差额补挂是「超挂黑洞」防御,竞态致 qty 算错 → 超挂或漏挂。
- ✏️ 单条 UPSERT:`INSERT ... ON CONFLICT(account_id,trade_date,symbol,purpose) DO UPDATE SET qty=qty+excluded.qty`(SQLite 原子)。

### H-3 `trading_plan` JSON 整文件覆盖写
- 📍 `trading_plan.py:57` save_plan / `:91` confirm_plan
- ❓ `p.write_text(...)` 非原子(写一半崩溃=文件截断损坏),整文件覆盖无 audit trail。崩溃在 write 中段 → 次日全天不挂单。memory `plan-sqlite-deferred` 排在 live P0 四项后,但当前仍 JSON,代码注释未显式标注风险。
- ✏️ 写临时文件 + `os.replace` 原子替换(2 行代码,零成本);顶部加注释标注已知缺陷。

### H-4 `direction is None` 时 `_handle_order_update` 不挂止盈 → 敞口裸奔
- 📍 `engine.py:2991-2996`
- ❓ BUY 回报方向未知 → 永不挂止盈 → 持仓无保护直到下次巡检兜底市价平。违反「敞口/逼空」拷问。
- ✏️ direction None 时反查 DB `order.side`,仍失败则视为 BUY(保守挂止盈,宁可多挂一张限价单被拒,不留敞口)+ CRITICAL。

### H-5 熔断冷启动失效(`start_equity<=0 → return False`)
- 📍 `trading/compute/breaker.py:50-53`
- ❓ 官方理由「冷启动无基线不应贸然熔断,由其他维度兜底」——但实际无任何兜底(risk.py 关挡板不查总权益)。冷启动日开盘亏 10% 不会熔断。风险方向选错。
- ✏️ `start_equity<=0` 时读最近一期 `position_book.cash_end` 兜底,仍无则**触发熔断并 CRITICAL**(保守停手)。

### H-6 涨跌停封板误判(`last >= high`)
- 📍 `trading/compute/risk.py:131-136`
- ❓ `high` 是当日涨停价,`last==high` 不等价于「封死涨停」——早盘触及后打开的票 last 一度等于 high 但盘口仍有卖单。过早拒单错过颈线法买点。
- ✏️ BUY 判封板改用「连续 N 笔成交价==涨停价」或 `ask1_price is None or ask1_price >= high`(无卖单才真封死)。

### H-7 discovery daemon 无并发保护
- 📍 `discovery/daemon.py:41-157`
- ❓ `run_daemon_cycle` 纯函数,无文件锁/单例守卫。schtasks 02:00 + 人误触 + 上夜未跑完次夜又起 → 两 daemon 并发:同 snapshot 同 seed → 同 trial_id,`read_latest_search_run` 读中间态,k_rounds_no_expansion 计数错乱(伪收敛或永不收敛)。`store.py` 的 `threading.Lock` 仅进程内。
- ✏️ 入口加 pid 文件 + `psutil.pid_exists` 检测,或 SQLite `BEGIN EXCLUSIVE` 跨进程门。

### H-8 `calmar=+Inf` 进 discovery 排序
- 📍 `discovery/objective.py:51-64`
- ❓ `metrics_of` 显式允许 `calmar=float("inf")`,TRE/Pareto 直接拿 calmar 排序 → +Inf 垄断 top,一笔交易 max_dd≈0 的退化解被当冠军。极端行情正是拷问①要求防御的边界;+Inf 冠军是伪收敛元凶。
- ✏️ `metrics_of` 对 calmar 加 `np.isfinite` clamp(超 CALMAR_CEIL 如 50 截断),或 `feasibility_gate` 加 `calmar < CALMAR_CEIL`。

### H-9 `n_total==0` 静默丢 trial 污染 ρ 与 DSR
- 📍 `discovery/worker.py:60` + `runner.py:144`
- ❓ `_eval_worker` 对 n_total==0 返 None,runner 计 failed,但 ρ 用 `all_evaluated`(只含成功组)→ ρ 虚高 → coverage_gate 提前放行收敛自停;DSR `n_trials` 不含被丢组 → 多重比较修正偏小 → DSR 虚高。
- ✏️ 采样组合数(含退化)应作为 ρ 分母,或 coverage_gate 显式要求「评估成功数/采样数 ≥ ratio」。

### H-10 回测撮合两套不一致
- 📍 `backtest/mock_broker.py`(330 行)vs trading QMT broker
- ❓ Mock 的 partial_fill_prob 均匀随机,与真实盘口语义不符;滑点用 `shares/avg_volume` 线性,无盘口深度。回测好的策略上实盘可能因撮合不一致翻车。
- ✏️ 抽 `FillPolicy` 接口(实盘/回测各一 impl)共享账户模型;回测加「撮合一致性 fuzz」(同信号流过 Mock 与真 QMT 模拟盘比对偏差)。

### H-11 前端死视图仍为首页
- 📍 `presentation/web/src/router/index.ts:38-40` + `CaisenScreenView.vue` / `ParamLabView.vue`
- ❓ caisen 形态 2026-07-23 退役,后端 `/caisen/*` 全删,GET 必 404;但 `/` → `/caisen` 仍是首页默认落地。用户访问直接看到空表/404,无退役视觉提示。
- ✏️ 删除两死视图 + 对应测试 + `api/caisen.ts`,路由重定向 `/` → `/live` 或 `/jobs`;或加退役 `<el-alert>` 横幅。

### H-12 前端 UTC 业务日期时区 bug
- 📍 `presentation/web/src/views/JobCockpitView.vue:34` + `TradesTable.vue:63`
- ❓ `new Date().toISOString().slice(0,10)` 返回 UTC 日期,北京时间 00:00–07:59 期间 UTC 已跨日 → 查到「明天」的空台账。
- ✏️ 改 `new Date().toLocaleDateString('sv-SE')`(本地时区 YYYY-MM-DD),或显式 `+8h` 偏移。

### H-13 schtasks ONSTART 注册失败无告警
- 📍 `ops/manage_ops_schtasks.py:112-143` `register_server`
- ❓ 不带 `/RP` 密码时 rc≠0,函数提示「手动跑」却返 0 继续执行 → `QuanterServer` 静默未注册,下次开机不自动起 server,且无钉钉告警。
- ✏️ rc≠0 时 `return 1`;加 `schtasks /Query /TN QuanterServer` 二次确认,失败即 CRITICAL 推钉钉。

### H-14 config 包级 `load_dotenv` 副作用(测试污染面)
- 📍 `config/__init__.py:16-20`
- ❓ 包 import 即 `load_dotenv()`,conftest autouse 只隔离 `AUTO_CONFIRM_PLAN`/`AUTO_TRADE_MODE` 两个旗标,**其余几十个 env(QMT_*/TOKEN/WEBHOOK)全程可读**,未来任何测试 `os.getenv` 判分支会被生产 .env 污染。
- ✏️ 中期改 `load_env()` 显式函数,由 lifespan/CLI 入口调用,包 import 零副作用。

---

## 5. 优先级行动清单

### 🚨 live P0 阻塞项(上实盘前必修)

1. **XC-1 撤单状态撒谎**——引入 PENDING_CANCEL 中间态 + DB WHERE 守卫(`trading-gap4` 已列);
2. **C-1/C-2 止损盲单 + trailing 哨兵**——`should_trigger_stop` 内置 NaN 守卫,`update_trailing_stop` 改 `None` 哨兵;
3. **XC-3 部分成交精度**——position_book 拒零/负/NaN + 精度截断 + broker 返 traded_volume/avg_price(`trading-gap4` gap①);
4. **H-5 熔断冷启动**——`start_equity<=0` 改 fail-closed(`trading-gap4` gap②熔断);
5. **H-3 trading_plan 原子写**——`os.replace` 兜底,避免 write 中段致次日全天不挂单;
6. **XC-2 auto_publish fail-open**——`_active_outer_ann` 透传 db_path + except raise(防 0% 冠军 publish)。

### ⚡ 短期改进(1-2 周)

7. C-4/C-5 smoke 脚本硬编码价 + 真单护栏;
8. C-6 除权历史 qfq 重算 + integrity 规则 6;
9. C-7 钉钉 UUID 脱敏;C-8 AKShare 死代码收敛;
10. H-11/H-12 前端死视图删除 + UTC 时区;
11. H-7/H-8/H-9 discovery daemon 并发锁 + calmar clamp + ρ 分母修正;
12. H-13 schtasks 注册失败告警。

### 🏗️ 中长期演进

13. **XC-4 engine.py 巨石拆分**(strangler,先 stop_loss_monitor → pre_open → order_update_handler,目标 < 800 行);
14. H-10 回测/实盘撮合 FillPolicy 统一 + 撮合一致性 fuzz;
15. H-14 config 零副作用 import;
16. `compute_unit` 455MB parquet 走对象存储,git 只存代码;
17. backtest 向量化(`np.maximum.accumulate` 替手写回撤循环);
18. scripts/ 老 bat 归档或加 DEPRECATED 横幅。

---

## 6. 做得好的沉淀(肯定)

- **C-4 错误分级 + `_critical_guard`/`_halt` 双层停调度**:实盘少见的「DB 写失败升 L1 停调度」纪律;
- **C-6 单一时间源 `trading/clock.py`**:eod 用 `trading_day=next_trading_day(today)` 防 T+1 错位;
- **颈线法 3 缺口 R3 闭环**:`decide_exit` 执行单源 + `compute_stop_price` 双向 re-export + `cancel_on` 下推 `PlannedOrder`;
- **moutai smoke 的 seq↔real_oid 三向映射 + 轮询终态校验**:把「CANCELLED 主推延迟」实测教训编进代码;
- **discovery Sobol 自写(Joe & Kuo 2008 方向数)+ OOS embargo + DSR 闭式**:符合极简原则,无黑盒优化库;
- **前端只读化铁律**:grep 全网零写调用残留,api/*.ts 五 facade 仅 GET;
- **reconcile_positions 的 GO/NO-GO 风控红线**:纯只读、源 C 权威、连接失败保守 NO-GO。

---

*审查产物归档:`docs/code_review_2026-08-04.md`。各模块 sub-agent 原始 findings 含更多 MEDIUM/LOW 细节,按需可追溯。*

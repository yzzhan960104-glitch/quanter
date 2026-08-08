# T4 调研报告：miniQMT/xtquant 连接真根因与候选修复方向

> 工单：`plans/wayfinder/T4.md` ｜ 产出方式：`mattpocock-skills:research` ｜ 日期：2026-08-08
> 信源分级：**SDK 源码/官方文档（一手）> 本仓库代码/memory（实证）> 社区（参考）**。每个结论标注 [信源]，存疑处显式标「待实证」。

---

## 0. TL;DR（一句话根因 + 推荐方向）

**根因**：xtquant 交易层（`XtQuantTrader`）通过 **userdata 目录下的共享内存队列文件（`down_queue_win_{sid}` + `lock_*` + `__mutex`）** 与 miniQMT 客户端进程通信，**同一 session_id 同一时刻只能被一个 Python 进程独占**；连接是**一次性、无内置心跳、无内置重连**的（官方文档 `xttrader.md` 明示「断开后不会重连，需再次主动调用」）。本系统的全部"连接不稳定"现象，本质都是这三个物理约束被违反后的派生症状：① **进程侧**——双 `python -m trading` 抢同一 sid（C-5 前的历史入口分叉 / 系统 Python 子进程副作用）；② **资源侧**——旧 `XtQuantTrader` 实例未 `stop()` 即被替换/进程强杀，残留 `down_queue_win_{sid}` 文件致同 sid `connect` 恒返 -1；③ **逻辑侧**——`on_disconnected` 只在 socket 显式断时触发，**客户端重启、sid 失效、启动就连不上**都不触发，旧代码仅挂 `on_disconnected` 重连，故"启动失败"和"客户端重启"两类场景全天死锁。

**推荐方向**：不重构 broker 整体，做**连接韧性层加固**——(A) 启动期 connect 失败也纳入重连循环（已部分由 C-5 后的 `_reconnect` + `_try_rotate_session` 覆盖，需补"客户端不在"探测）；(B) 外置 watchdog 探活（定期 `query_account_status` 主动探针，补 `on_disconnected` 的盲区）；(C) 进程侧加 OS 级单例锁（PID 文件 + 端口探测已部分由 `__main__::_assert_single_instance` 覆盖，需补"嵌套父子"探测）。**不建议**引入第三方 QMT 框架（easytrader/qlib 不直接支持 miniQMT 交易层，反魔法原则）。

---

## 1. 现象收敛与核实

来自 memory（`qmt-live-smoke-findings` / `qmt-connect-1-rootcause` / `state-store-init-missing`）的 5 个已知现象，逐条核实并归因：

| # | 现象（memory） | 核实结论 | 归类 | 信源 |
|---|---|---|---|---|
| ① | `connect -1` 全天锁死（07-29） | **属实，多因复合**。见 §3 四层根因。 | 资源+逻辑+进程 | memory `qmt-connect-1-rootcause`；`broker/qmt.py:633-660`（-1 → 清队列重试 → L2 sid 轮换） |
| ② | 双进程抢 session（系统 Python310 + venv310 父子并存） | **现象属实，"真身"部分定位**。见 §3.5。`trading/` 代码 grep 零 `multiprocessing`/`subprocess`，双起不在业务代码显式 spawn。 | 进程 | memory `qmt-connect-1-rootcause` 07-30 复盘；`trading/__main__.py:99-192`（W1.4 单实例探测局限说明） |
| ③ | 串通挂撤成交（茅台模拟盘 07-27） | **属实，非连接问题**。是 seq↔real_oid 双 ID 对账机制：`submit_order` 返 `seq`，柜台主推用 `real_oid`，由 `on_order_stock_async_response` 回调填映射。 | 业务机制（非连接） | memory `qmt-live-smoke-findings`；`xtquant/xttrader.py:238-251`（`on_push_OrderStockAsyncResponse` + `queuing_order_errors_byseq`） |
| ④ | 模拟盘拒涨停价买单 | **属实，模拟盘特性，非 SDK bug**。现价 1288.51 挂涨停 1427.15 → REJECTED。模拟盘对远离现价限价单保守拒。实盘须重新验证。 | 柜台策略（非连接） | memory `qmt-live-smoke-findings` |
| ⑤ | 撤单 CANCELLED 主推延迟 1-2s | **属实，非 bug**。柜台主推异步，固定 `sleep 1.5s` 判定会误判"未撤"→ 重试 → `cancel_error`。须轮询 `gw._orders` 等终态。 | 业务机制（非连接） | memory `qmt-live-smoke-findings`；`xtquant/xttrader.py:256-276`（`on_push_CancelOrderStockAsyncResponse`） |

**结论**：5 个现象里，只有 ①② 是真正的"连接不稳定"问题，③④⑤ 是业务机制/柜台特性（虽曾表现为"奇怪行为"但根因不在连接层）。本报告后续聚焦 ①②。

---

## 2. xtquant SDK 连接机制（一手：源码 + 官方文档）

### 2.1 进程/线程模型

`XtQuantTrader.__init__(path, session_id)` [xttrader.py:136-171]：
- 构造即创建 `async_client = _XTQC_.XtQuantAsyncClient(path.encode('gb18030'), 'xtquant', session)`（C++ pyd 后端 `xtpythonclient.cp310-win_amd64.pyd`，**不可读源码**）。
- session_id 作为构造参数传入 C++ 后端，**绑定整个实例生命周期**。
- 创建独立 `asyncio.new_event_loop()`，主线程若是 MainThread 会保存旧 loop。

`start()` [xttrader.py:433-440]：
- 调 `async_client.init()` + `async_client.start()`（C++ 后端启动交易线程）。
- 创建 `ThreadPoolExecutor(max_workers=1)` 承载回调（**所有 on_* 回调串行执行**）。

`stop()` [xttrader.py:442-447]：
- 调 `async_client.stop()` + `loop.call_soon_threadsafe(loop.stop)` + `executor.shutdown(wait=True)`。
- **没有显式的 disconnect / session 释放 API**——释放靠 C++ 后端 `stop()` 内部清理（不可见）。

`connect()` [xttrader.py:449-452]：
```python
def connect(self):
    result = self.async_client.connect()
    self.connected = result == 0
    return result
```
返回 int：`0=成功，非 0=失败`（官方文档 [xttrader.md:894-903]）。

### 2.2 session 生命周期（关键）

**一次性连接，无内置重连**。官方文档 `xttrader.md` line 894-896 原文：

> #### 创建连接 `connect()`
> * 释义：连接 MiniQMT
> * 返回：连接结果信息，**连接成功返回 0，失败返回非 0**
> * **备注：该连接为一次性连接，断开连接后不会重连，需要再次主动调用**

这是所有重连逻辑必须用户层实现的**根本约束**。

### 2.3 心跳 / 保活

**SDK 层无显式心跳 API** [xttrader.py 全文 grep 无 heartbeat/keepalive]。
保活靠两机制：
1. C++ 后端内部 TCP keepalive（不可见，待实证是否有）；
2. 用户层的查询/订阅主推（`on_stock_order` 等推送维持 socket 活跃）。

社区资料（web_search）提及"XtQuant 通过 TCP 连接与本地 miniQMT 服务通信"，但 TCP 层心跳细节属 C++ pyd 黑盒，**待实证**。

### 2.4 断线回调 `on_disconnected` 触发条件

`xttrader.py:281-286` 注册了 `bindOnDisconnectedCallback`：
```python
def on_push_disconnected():
    if self.callback:
        self.callback.on_disconnected()
self.async_client.bindOnDisconnectedCallback(on_common_push_callback_wrapper(0, on_push_disconnected))
```

**触发条件**（综合官方文档 + 本仓库实证 memory）：
- ✅ **socket 显式断开**（网线拔、客户端 crash）—— 触发。
- ❌ **客户端重启** —— memory `qmt-connect-1-rootcause` 实证："QMT 客户端重启 ≠ engine 重连——on_disconnected 不触发（socket 未显式断），_reconnect 不跑"。
- ❌ **启动期 connect 就失败** —— 根本没连上，自然无 disconnected 回调。
- ❌ **session_id 失效/被占用** —— connect 返 -1，不触发 disconnected。

**这是旧代码"全天死锁"的逻辑根因**：重连只挂在 `on_disconnected` 上，而 07-29 事故是"启动就 connect -1"（session 残留），根本进不了 disconnected 分支 [memory `qmt-connect-1-rootcause` 根因 4]。

### 2.5 连接数上限 / session 唯一性

官方文档 `xttrader.md` line 826-828：
> - session_id - int 与 MiniQMT 通信的会话 ID，**不同的会话要保证不重**

物理实现（本仓库逆向实证，`scripts/qmt_clear_session_lock.py` + `broker/qmt.py:244-298`）：
- xtquant 通过 **userdata 目录下的共享内存文件** 与客户端通信：
  - `down_queue_win_{sid}` —— 主下行队列（按 sid 分桶）
  - `lock_down_queue_win_{sid}` —— 队列 lock 文件
  - `*_queue_win_*__mutex` —— 跨进程互斥量
- **同一 sid 同一时刻只能被一个进程独占**（`__mutex` 跨进程锁保证）。
- 进程崩溃/强杀未 `stop()` → 残留 `down_queue_win_{sid}` + lock → 下个同 sid `connect` 返 -1 [broker/qmt.py:244-277 注释]。

"连接数上限"在 SDK 层未显式文档化，但物理约束是：**每进程一个 `XtQuantTrader` 实例，每实例绑一个 sid，每 sid 独占一份队列文件**。多进程想同时连，必须用**不同 sid**。

### 2.6 数据层（xtdata）vs 交易层（xttrader）——两套连接

`xtquant/xtconn.py` 是**数据层**连接（行情）：`try_create_connection(addr)` → `cl.connect()` 返 `(ec, msg)`，与客户端通过 TCP 端口（如 58610）通信，扫描 `%USERPROFILE%/.xtquant/` 下的服务实例。

**本系统连接问题全部指交易层（xttrader）**，数据层不在本调研范围（行情源是另一条链路，见 `broker/qmt.py` 的 `xtdata` 调用）。

---

## 3. 真根因定位

### 3.1 根因 A：旧实例未 stop 残留会话文件（资源侧）

**机制**：进程未调 `XtQuantTrader.stop()` 就退出（强杀 `taskkill /F`、崩溃、`__del__` 未触发），`down_queue_win_{sid}` 会话文件残留，同 sid 后续 `connect` 恒返 -1。

**实证**：
- `broker/qmt.py:228-242` `_stop_trader_safely` 注释（2026-08-03 根治）："同 sid 的 XtQuantTrader 未 stop 即被替换/进程退出，会话会残留共享内存（down_queue_win_{sid} 等队列文件），此后任何同 sid connect 必返 -1——包括本进程重试（旧实例未停，新实例自锁）"。
- `broker/qmt.py:244-277` `_cleanup_session_files`：删指定 sid 的 `down_queue_win_{sid}` / `lock_down_queue_win_{sid}`（含 `__mutex` 后缀）即恢复。
- memory `qmt-connect-1-rootcause` 根因 2："123456 旧 session 共享队列/互斥锁残留（07-28 老 engine 09:11 断线后资源未释放，lock_down_queue_win_123456/__mutex 锁文件在）"。

**现状（已部分修复）**：
- `connect()` 前置清本 sid 残留 [qmt.py:610-615]。
- 首轮 -1 → 清队列重试第 2 次 [qmt.py:633-639]。
- 两轮仍 -1 → L2 自动轮换未占用 sid [qmt.py:644-647, 531-566]。
- 重连前 stop-before-recreate [qmt.py:594-597]。

**残余风险**：L2 轮换耗尽（前 100 个候选都被占）仍 fail-closed，但概率极低。

### 3.2 根因 B：双进程抢同一 sid（进程侧）

**机制**：两个 `python -m trading` 进程用同一 `QMT_SESSION_ID` 同时 `connect`，第二个必 -1（sid 已被第一个独占）。

**实证**：
- memory `qmt-connect-1-rootcause` 07-29："engine 09:15:16 启动时 .env 还是 123456...进程用 123456 connect 返 -1"。07-30 复盘："`python -m trading` 每次启动自带一个系统 Python310 子进程（进程树：venv 主进程 .venv310 → 子进程 C:\Users\...\Python310\python.exe -m trading，同秒启动）"。
- `trading/__main__.py:99-192` W1.4 单实例探测：端口 8000 被占即 `sys.exit(1)`。但 docstring 诚实标注局限："嵌套父子拦不住——父子进程共享端口归属父"。

**"双进程真身"评估（memory 记"真身未定位"）**：
- **业务代码零 spawn**：`trading/__main__.py` grep 无 `multiprocessing`/`subprocess`/`os.fork`（本调研已核实）。
- **疑点**：memory 指向 xtquant SDK 副作用。`xttrader.py:150-153` 在 `__init__` 里 `asyncio.new_event_loop()` + `asyncio.set_event_loop(self.loop)`——**这在 MainThread 会替换全局 event loop**，但不会 spawn 子进程。
- **更可能的真实来源（待实证）**：
  1. **Windows `python -m` 的已知行为**：`python -m package` 在某些 Windows Python 构建里会通过 launcher 间接启动（特别是 venv 与系统 Python 路径混合时），可能产生中间进程。这是 Windows + venv 的已知坑，非 xtquant 特有。
  2. **uvicorn reloader 子进程**：`__main__.run_server()` 在 dry_run 模式默认 `reload=True` [qmt.py:376-377]，reloader 会 fork 子进程 import main → lifespan → `gw.connect()` 抢同一 session。**C-5 V1 已在 live 模式显式 `reload=False`** [qmt.py:376]，但 dry_run 仍有风险（虽无真网关）。
  3. **schtasks ONSTART 链重复触发**：`start_server.bat` 若被多个 schtasks 触发（开机 + 登录各一次），会起两个 server。
- **诚实结论**：07-30 观测到的"venv 主 + 系统 Python 子"双进程，**最可能是 Windows venv launcher 机制 + `python -m` 组合的副作用**，而非 xtquant SDK 主动 spawn。**彻底定位需用 Process Monitor 抓 spawn 链**（待实证，非本调研能闭环）。

**现状（已部分修复）**：
- C-5 V1 合并 `python -m trading` 与生产链同入口（uvicorn lifespan 装 engine），消除"两条并存入口" [__main__.py:320-378]。
- W1.4 端口单例探测 [__main__.py:165-192]。
- live 模式 `reload=False` 防 reloader 子进程 [__main__.py:376]。

**残余风险**：
- 嵌套父子（fd 继承绕过端口 bind 校验）拦不住 [__main__.py:148-149 docstring]。
- 系统 Python 子进程连不上 QMT、不抢锁（memory 07-30 复盘："双起虽是常态，但 venv 主独占 session 后那个系统 python 子进程连不上 QMT、不抢锁、不构成双重下单（无害但需 P1 定位）"）——**即当前观测到的双进程实际无害**，但若是"两个 venv 主进程"就致命。

### 3.3 根因 C：重连逻辑盲区（逻辑侧）

**机制**：旧代码重连只挂 `on_disconnected`，而"启动 connect 失败"和"客户端重启"都不触发 `on_disconnected`，故全天死锁。

**实证**：
- memory `qmt-connect-1-rootcause` 根因 4："启动 connect 失败 → _lock_down=True 永久锁；_reconnect（broker/qmt.py:919）只挂 on_disconnected（已连接后断线），启动就没连上不走重连，全天死锁"。
- 07-30："QMT 客户端重启 ≠ engine 重连——on_disconnected 不触发（socket 未显式断），_reconnect 不跑，必须重启 engine 进程"。

**现状（已部分修复）**：
- `_reconnect` 指数退避 `(2,4,8,16,30)` 5 次，由 `on_disconnected` 经 `call_soon_threadsafe` 投递主线程后触发 [qmt.py:1294-1369]。
- M1 互斥锁防 on_disconnected 与 health_guard 两条重连路径并发 [qmt.py:409-413, 1332-1335]。
- **但"启动 connect 失败"仍不走 `_reconnect`**——`connect()` 失败直接 `raise ConnectionError` [qmt.py:659-662]，由上层 lifespan 决策（若 lifespan 不捕获重试，engine 不 start）。这是**当前架构选择**（fail-closed 让人工介入），非 bug，但与"全天锁死"边界模糊。

### 3.4 根因 D：客户端就绪判定难（环境侧）

**机制**：miniQMT 客户端是东财定制 GUI，进程名不匹配 mini/xt 正则，"客户端在不在"难探测。

**实证**：
- memory `qmt-connect-1-rootcause`："MiniQMT 客户端是否在跑看 userdata_mini 目录是否活跃（共享内存 shm 文件），不看进程名"。
- `broker/qmt.py:416-496` `is_client_ready` 迭代史：
  - 旧版用 `miniqmtShm*Cache*/up_queue_win_*` mtime 判活 → 08-04 事故："那是客户端启动时一次性生成的共享内存镜像，运行期间不刷新，>5min 即判死 → _health_guard 永不 connect" [qmt.py:424-428]。
  - 现版（2026-08-04 根治）：仅判 userdata 目录存在且非空 → 放行让上层 connect，由 `trader.connect()` 返回码定权威结论 [qmt.py:419-422]。

**残余风险**：客户端"登录了但 session 失效"（如柜台踢线）仍需 `on_account_status` 回调感知 [qmt.py:1393-1428]，这是账号级而非连接级故障。

### 3.5 四层根因汇总（坐实层级）

| 层 | 根因 | 触发场景 | 现状 |
|---|---|---|---|
| A 资源 | 旧实例未 stop 残留会话文件 | 强杀/崩溃后重启 | **已修**（前置清理 + -1 自愈 + L2 轮换） |
| B 进程 | 双进程抢同一 sid | 入口分叉/venv launcher/reloader | **部分修**（C-5 统一入口 + W1.4 端口探测；嵌套父子待实证） |
| C 逻辑 | 重连盲区（仅挂 on_disconnected） | 启动失败/客户端重启 | **部分修**（_reconnect + health_guard 双路径；启动失败仍 fail-closed） |
| D 环境 | 客户端就绪判定难 | shm 陈旧/进程名不匹配 | **已修**（connect 返回码唯一权威） |

---

## 4. 业界/社区稳定连接方案

### 4.1 官方文档示例（最权威参考）

`xtquant/doc/xttrader.md` 的"快速入门"示例 [line 220-296]：
- `on_disconnected` 只 `print("connection lost")`，**无重连逻辑**。
- 主流程：`start() → connect() → subscribe() → 下单 → run_forever()`（阻塞）。

社区（web_search 转述 thinktrader.net 完整实例）：官方有一个断线重连示例，但**官方明示「示例非线程安全，仅演示重连逻辑，实战需额外保护」**。

### 4.2 社区共识做法（web_search 综合）

| 做法 | 描述 | 本项目是否已用 |
|---|---|---|
| **单例守卫** | 每进程一个 `XtQuantTrader` 实例，防多实例抢 sid | ✅ `QmtExecutionGateway` 单实例（lifespan 装） |
| **进程隔离** | 每策略独立 Python 进程，故障隔离 | ✅ engine 单进程（C-5 后）；社区 miniqmt.com 知乎专栏证实 miniQMT 原生支持此模式 |
| **断线重连 + 告警** | on_disconnected → 记录断线时间/状态 → 指数退避重连 → 钉钉/邮件告警 → **断线期间暂停下单** | ✅ `_reconnect` 指数退避 + `_lock_down` 拒单 + 钉钉告警 |
| **主动探针 watchdog** | 定期 `query_account_status`/`query_stock_asset` 探活，补 on_disconnected 盲区 | ⚠️ **部分**（health_guard 存在但具体探针待核实 trading/engine.py） |
| **pause-order 期间** | 断线期间禁止下单，防废单 | ✅ `_lock_down` + `submit_order` 前置检查 [qmt.py:779] |

### 4.3 第三方 Python QMT 框架

- **easytrader**：主要支持同花顺/雪球/老虎，**不直接支持 miniQMT/xtquant 交易层**（部分 fork 有实验性支持，非主流）。
- **qlib（微软）**：研究框架，不含实盘交易适配。
- **社区封装类（腾讯云开发者文章 2659334）**：封装 xtquant 含自动重连，但域名抓不到一手代码，且本仓库已实现等价功能。

**结论**：**不建议引入第三方框架**。xtquant 本身就是官方 Python 封装，再套一层只会增加黑盒（反魔法原则）。社区共识做法本仓库已基本覆盖，缺口在"主动探针 watchdog"和"嵌套父子探测"两点。

---

## 5. 候选修复方向 + Trade-off

按投入/收益排序，供后续 broker 重构工单（T2 衍生）选用。

### 方向 A：补"客户端重启"感知（主动探针 watchdog）★★★ 推荐

**问题**：`on_disconnected` 在客户端重启时不触发（memory 07-30 实证），engine 以为连着继续发废单。

**方案**：在 health_guard 定时 job 里，除了 `is_client_ready`（文件检查），加一次**轻量主动探针**：
- 调 `query_account_status(account)`（同步，投线程池），返 `None`/`ACCOUNT_STATUS_INVALID(-1)`/超时 → 视为断线 → 触发 `_reconnect`。
- 间隔：每 30-60s 一次（撞柜台限频风险低，是查询不是下单）。

**Trade-off**：
- ✅ 补 `on_disconnected` 盲区（客户端重启、session 失效、socket 假活）。
- ✅ 复用现有 `_reconnect` 互斥入口（M1 软锁已防并发）。
- ⚠️ 多一次定时查询，理论上撞柜台限频（但 query 频率远低于 order，风险小）。
- ⚠️ 探针本身返 None 有多种含义（查询失败 vs 真断线），需区分（连续 2-3 次失败才判定）。

**待实证**：`query_account_status` 在客户端重启后的返回行为（是否阻塞、返 None、超时）——需模拟盘实测。

### 方向 B：嵌套父子进程探测强化 ★★ 可选

**问题**：W1.4 端口探测拦不住嵌套父子（fd 继承绕过 bind 校验）[__main__.py:148-149]。

**方案**：
- **B1（轻量）**：启动时 `psutil.Process(os.getpid()).parent()` 链上溯，若发现多个 `python.exe` 祖先且命令行含 `-m trading` → CRITICAL 告警（不杀，防误杀 schtasks 合法链）。
- **B2（重量）**：用 Windows Job Object 或 PID 文件 + 文件锁（`msvcrt.locking`）做 OS 级单例锁，第二实例启动即退出。

**Trade-off**：
- ✅ B1 不引入新依赖（`psutil` 已在依赖树？待核实；若无需评估）。
- ⚠️ B2 跨平台/权限坑多，Windows 文件锁语义复杂。
- ⚠️ 07-30 实证"系统 Python 子进程连不上 QMT、不抢锁、无害"——**当前双进程实际不致命**，优先级低于方向 A。

**待实证**：`psutil` 是否已在 `requirements.txt`；嵌套父子在当前 schtasks 链（`start_server.bat → python -m trading`）下是否真的发生。

### 方向 C：启动期 connect 失败也纳入重连循环 ★ 可选

**问题**：`connect()` 失败直接 `raise ConnectionError` [qmt.py:659-662]，由 lifespan 决策。若 lifespan 不重试，engine 不 start，全天不交易。

**方案**：在 `connect()` 外包一层启动重试（如最多 5 次，每次间隔 30s，区分"-1 session 残留" vs "客户端没起"——后者重试无意义）。

**Trade-off**：
- ✅ 防"客户端启动比 engine 晚"场景（schtasks 触发时序竞态）。
- ⚠️ 当前是 fail-closed 设计（启动失败让人工介入），改重试可能掩盖环境问题。
- ⚠️ 若客户端真没起，重试 5 次仍失败，行为不变。

**建议**：仅在"客户端就绪判定通过但 connect -1"时重试（`is_client_ready` 真 + connect 假 = session 残留，可自愈）；客户端没起则直接 fail-closed。

### 方向 D：Session 运行时 SSoT 强化 ★ 可选

**现状**：`_write_runtime_session` 写 `logs/engine_session.json` [qmt.py:306-324]，记录 preferred/actual sid。L2 轮换后 actual 与 preferred 分离。

**问题**：运维端点读 actual_sid 需主动查 JSON 文件，若 engine crash 后 actual 未清理，下次启动可能读到旧值。

**方案**：engine 启动时先读 `engine_session.json` 的 actual 作为首选 sid（而非 .env preferred），避免轮换后 preferred 仍指向被占 sid。

**Trade-off**：
- ✅ L2 轮换的 actual 自动延续，减少下次启动再轮换。
- ⚠️ 增加状态文件依赖，crash 恢复语义复杂。

**优先级**：低（L2 轮换已能处理，只是多一次轮换）。

### 方向 E（不建议）：重构 broker 适配层

**问题**：`broker/qmt.py` 1540 行堆补丁（工单 Context 提及）。

**为什么不建议**：
- 1540 行里**连接相关逻辑已相当扎实**（四层根因 A/B/C/D 多数已修），补丁堆叠主要在**业务层**（seq↔real 映射、回报解析、状态机、风控熔断）——这些是 miniQMT 业务复杂性的固有体现，非连接问题。
- 重构 broker 是 T2 工单的范围，**不应由连接调研驱动**。连接调研的产出应是指明"连接层缺口"（方向 A/B/C/D），重构时一并吸收。

---

## 6. 待实证清单（反臆测，明确未知）

| # | 未知 | 验证方法 | 优先级 |
|---|---|---|---|
| 1 | `query_account_status` 在客户端重启后的返回行为（None/阻塞/超时） | 模拟盘：连上后 taskkill 客户端，调 `query_account_status` 观返回 | 高（方向 A 前置） |
| 2 | "双进程"真身——Windows venv launcher 还是 xtquant 副作用 | Process Monitor 抓 `python -m trading` 的进程 spawn 链（CreateProcess 事件） | 中（方向 B 前置） |
| 3 | C++ 后端 `xtpythonclient.pyd` 是否有 TCP keepalive | 抓包（Wireshark 看 xtquant↔客户端 TCP 流是否有 Keep-Alive 包） | 中（理解保活机制） |
| 4 | `psutil` 是否已在 `requirements.txt` | `grep psutil requirements.txt` | 低（方向 B1 前置） |
| 5 | health_guard（T8）当前探针具体是什么（是否已有主动 query） | 读 `trading/engine.py` 的 `_health_guard` 实现 | 高（方向 A 是否已部分实现） |
| 6 | schtasks 链是否真的产生嵌套父子（B2 前置） | 开机后 `wmic process where "name='python.exe'" get ProcessId,ParentProcessId,CommandLine` | 中 |

---

## 7. 信源索引

### 一手（SDK 源码 + 官方文档）
- `xtquant/xttrader.py`（完整可读 Python 源码）：`XtQuantTrader` 类、`connect/start/stop/subscribe`、回调注册、`on_disconnected` wrapper
- `xtquant/xtconn.py`：数据层连接（与本调研范围区分）
- `xtquant/doc/xttrader.md`：官方 API 文档，line 894-896「一次性连接不重连」关键备注，line 826-828 session 唯一性

### 本仓库代码（实证）
- `broker/qmt.py:228-303` `_stop_trader_safely` / `_cleanup_session_files` / `_used_session_ids` / `_candidate_session_ids`
- `broker/qmt.py:327-413` `QmtExecutionGateway.__init__`（session_id/lock_down/reconnecting 状态）
- `broker/qmt.py:497-680` `_run_bootstrap` / `_try_rotate_session` / `connect`（四层自愈链）
- `broker/qmt.py:1294-1428` `_on_disconnect_fatal` / `_reconnect` / `on_disconnected` / `on_account_status`
- `trading/__main__.py:99-192, 320-378` W1.4 单实例探测 + C-5 V1 uvicorn 统一入口
- `scripts/qmt_clear_session_lock.py` 共享内存队列文件机制逆向（`down_queue_win_{sid}` / `__mutex`）
- `trading/tools/trigger_pre_open_once.py` 补挂范式（停 engine → 等 6s → connect 重试 3 次）
- `trading/tools/qmt_live_smoke_moutai.py` 模拟盘 smoke 范式

### memory（实证观测，注明时效）
- `qmt-live-smoke-findings.md`（11 天前）：seq↔real 对账、撤单延迟、模拟盘特性
- `qmt-connect-1-rootcause.md`（9 天前）：四层根因、07-29/07-30 事故复盘、双进程观测
- `state-store-init-missing.md`（9 天前）：DB 建表缺失（非连接问题，关联项）
- `syspath-calendar-shadowing.md`（11 天前）：sys.path 遮蔽（非连接问题，关联项）

### 社区（参考）
- web_search 综合结论：官方 thinktrader.net 重连示例「非线程安全」；miniQMT 原生支持进程隔离；社区封装类（腾讯云 2659334）有自动重连实现（域名抓不到一手，结论来自搜索摘要）
- 注：thinktrader.net / cloud.tencent.com 在本环境 WebFetch 被网络策略拦截，未取得一手代码；结论以本地 `xttrader.md`（同源权威）+ web_search 摘要为准

---

## 8. 毕业建议

**是否需新建 broker 重构工单？** —— **不需要为连接问题新建**。

理由：
1. 连接层四层根因（A/B/C/D）中，A（资源残留）和 D（环境判定）**已根治**；B（双进程）和 C（重连盲区）**已部分修复**，残余缺口是方向 A/B/C 三个增量改进，非整体重构。
2. `broker/qmt.py` 1540 行的"堆补丁"主要在**业务层**（seq↔real、状态机、风控），非连接层。业务层重构应由 T2（broker 适配层）工单驱动，**不应由连接调研毕业**。
3. 连接层的三个增量改进（方向 A/B/C）可作为**独立小工单**补进 wayfinder MAP，每个投入远小于 broker 重构。

**建议补进 MAP 的工单草稿**：

- **T-conn-1**（方向 A，高优先级）：health_guard 加主动探针（`query_account_status`），补 on_disconnected 客户端重启盲区。Question："`broker/qmt.py` 的 `on_disconnected` 在 miniQMT 客户端重启时不触发（memory 07-30 实证），engine 以为连着继续发废单。在 health_guard 定时 job 里加一次主动 `query_account_status` 探针，连续失败触发 `_reconnect`。需先模拟盘实测探针返回行为（待实证清单 #1）。"
- **T-conn-2**（方向 B，中优先级）：嵌套父子进程探测强化。Question："`trading/__main__::_assert_single_instance` W1.4 端口探测拦不住嵌套父子（fd 继承，docstring 已注局限）。补 `psutil` 祖先链检查或 PID 文件锁。需先实证嵌套父子在当前 schtasks 链是否真发生（待实证 #2/#6）。"
- **T-conn-3**（方向 C，低优先级）：启动期 connect 失败重试。Question："`connect()` 失败直接 raise，engine 不 start 全天死锁。在'客户端就绪判定通过但 connect -1'场景下加启动重试（区分 session 残留 vs 客户端没起）。"——此项也可作为 T-conn-1 的子任务，不单独建工单。

**broker 重构（T2 衍生）**：保持开放，但由业务复杂度驱动（seq↔real 映射、回报状态机、风控熔断的模块化），不由连接调研毕业。本调研的产出（方向 A/B/C + 待实证清单）供 T2 重构时一并吸收。

# emquant 颈线策略试点 runbook（掘金终端托管腿）

## 一、这是什么

掘金（东财 Goldminer3）终端托管的颈线策略试点单文件——与本地 tushare 腿**双轨并行**，
用同一颗识别内核回答「换执行通道会不会改变信号与成交」，为 W2 双轨对照与后续
通道决策提供掘金侧真值。

```
emquant_neckline_pilot.py（组装产物，勿手改）
  = §0 参数快照（params_snapshot/universe 导出定稿）
  + §1 识别内核逐字块（strategies/neckline/{signal,method_v0}.py 原字节）
  + §2-§7 执行编排（pilot_body.py：数据/状态/风控闸/订单/事件编排）
  ← 由 build_pilot.py 纯拼接生成（幂等；改任何源后必须重跑组装器）
```

运行期产物全部落在**策略文件所在目录**（自定位 `BASE_DIR`，零配置）：
`state/state.pkl`（策略唯一持久状态）、`state/RISK_BLOCK.flag` / `state/CAP.txt`
（人工风控双值文件）、`audit/audit_YYYYMMDD.csv`（对拍审计，晨检主入口）、
`config/runtime.json`（token / strategy_id / account_id，gitignore 不入库）。

---

## 二、终端挂载步骤（首次部署）

1. **新建策略**：掘金终端 → 量化研究 → 我的策略 → 新建 Python 策略（终端文件名
   惯例 `main.py`，内容粘贴本仓库 `emquant/emquant_neckline_pilot.py` 全文；或把
   文件放入策略目录）。记下 strategy_id（当前实跑腿真值
   `08b25d85-9cf8-11f1-a09d-7c10c93fcb7d`——08-21 首启 audit INIT 行自证；
   `7ed8526e-…` 为早期空目录已作废。策略列表 / `~/.emgm3/projects/` 目录名可见）。
2. **建策略目录旁的 config**：产物按自身所在目录定位 `config/runtime.json`
   （`BASE_DIR = Path(__file__).resolve().parent`）——**终端跑的是策略目录里这份，
   不是仓库 `emquant/config/` 那份**。在策略目录下建 `config/runtime.json`：

   ```json
   {
     "token": "<第 3 步生成的值>",
     "strategy_id": "08b25d85-9cf8-11f1-a09d-7c10c93fcb7d",
     "account_id": "e7cb55d6-04ab-4ea9-98fc-503d9f97d2a1"
   }
   ```

   （account_id 已是仿真账户真值；三键语义与仓库侧
   `emquant/config/runtime.json.example` 一致。缺文件 / 缺 token 启动即中文 raise，
   不静默空跑。）
3. **生成 token**：终端右上角「系统管理 → 密钥管理」生成并复制（绑定本机 ID；
   **生成新值即旧 token 作废**），粘进上一步 `token` 键。值只进 runtime.json
   （gitignore 已覆盖），勿复制进任何文档 / 终端输出 / commit。
4. **绑定仿真账户**：策略设置里绑定仿真账户（即 account_id 那个账户）。SDK 层
   掘金「仿真交易」与实盘同为 MODE_LIVE，唯一自动防线是账户白名单守卫
   （C1：account_id 必须等于 `PILOT_ACCOUNT_ID`，否则启动期 raise）。
5. **运行（必须走仿真通道，勿点回测）**：终端下方「交易」区先**切换到「仿真」标签并
   快捷登录仿真账户**，再到**策略列表 / 策略监控页**对该策略点「运行」。
   ⚠️ **不要用策略编辑器右上角的「运行回测」按钮**——终端会经命令行注入
   `--mode=2`（回测），**覆盖 `run(mode=MODE_LIVE)` 形参**（gm `basic.py:519`
   `mode = convert(options.mode, int, mode)`，实测 2026-08-21），随后因
   `backtest_start_time` 为空报
   `GmError 1021: 非法日期时间格式; 回测开始时间:[]不符合[yyyy-mm-dd hh:mm:ss]`
   ——本试点按仿真实盘事件模型设计（schedule 盘前五阶段/tick 巡检），不支持
   回测模式，此报错即「入口走错」的确定性信号。
   init 链（读配置 → C1 复核 → 柜台对账 → 订阅 → 注册
   09:15:00 / 15:35:00 双定时）跑通后，当日 `audit/audit_YYYYMMDD.csv` 首行落
   `INIT`。`state/`、`audit/` 目录首启自动创建。

> 仓库侧冒烟（可选，验证脚本形态入口）：
> `PYTHONUTF8=1 E:/quanter/.venv_emquant/Scripts/python.exe emquant/emquant_neckline_pilot.py`
> ——走 `run_pilot()`：读仓库侧 `emquant/config/runtime.json`，缺 token 即中文
> raise 后退出，全程不触内核演示路径。

---

## 三、晨检清单（每个交易日）

### 09:15 前

- [ ] **终端在线**：`netstat -ano | findstr :7001`（Git Bash 用 `grep 7001`）有
  LISTENING；或按进程名 `gmterm-serv.exe` 检索。**不要**用
  `tasklist | findstr /i "goldminer eastmoney"`——进程名不是这两个词，会漏报。
- [ ] **策略运行态**：终端 UI 该策略状态为运行中（INIT 行日期=今日则 init 链已过）。

### 09:15 后（09:20–09:30 窗口）

- [ ] **audit CSV 首行存在**：`<策略目录>/audit/audit_YYYYMMDD.csv` 存在且首行
  `INIT`（detail 含 account / strategy_id / subscribed 数）；09:15 定时触发后应有
  pre_open 留痕（RECONCILE / SIGNAL* / ORDER_* / EOD 前的各阶段行，或
  `calendar_missing` WARN——后者=日历取数失败，②③停判，须人工看终端日志）。
- [ ] **挂单 ≤2**：柜台挂单页未成交单 ≤2，且 audit `ORDER_PLACED` 行数 ≤2
  （试点硬闸 `PILOT_MAX_NEW_ORDERS_PER_DAY=2`；超限会是 `ORDER_BLOCKED` 行）。
- [ ] **无 fail-closed WARN**：audit 无 `get_cash_fail/empty/invalid`、
  `reconcile_orders_fail/positions_fail`、`pre_open_get_orders_fail`、
  `bootstrap_accounts_empty/account_absent` 等查询失败族（出现=权益/持仓/账户
  绑定查不到 → 当日 fail-closed 不挂单或账户绑定异变，看终端连接态与账户绑定）。
  注意 `cap_missing` 是**正常态**（CAP.txt 缺省 1.0 不限制）；`fetch_fail/fetch_empty`
  是单标的降级，成片出现才升级为通道事故；`limit_down_fallback_t1` 是跌停价
  API 通道降级（已用 T-1 收盘自算兜底），偶发可观察、成片=数据服务异常。
- [ ] **SIGNAL 行计数 >0**（终审修复新增）：当日 audit 有 `SIGNAL` 行（或
  `SIGNAL_COOLDOWN_SKIP`/`SIGNAL_SKIP_HELD` 等跳过留痕）。若 =0 且当日有行情
  （前一交易日 K 线可得），先查 `fetch_end_missing` WARN——那是 history
  `end_time` 端性异变被末根不变量拦下的痕迹（缺末根的 df 已按故障丢弃，该标的
  当日无信号），出现即对照本地腿当日信号面确认影响范围。
- [ ] **归档当日运行时**（盘后 15:35 后补跑更优——EOD 行与终态 state 已落）：

  ```
  PYTHONUTF8=1 E:/quanter/.venv310/Scripts/python.exe emquant/tools/archive_terminal_state.py
  ```

  把终端腿 state/audit 快照拷回 `emquant/archive/YYYY-MM-DD/`（gitignored，含
  manifest 溯源+sha256）——终端侧单机单副本，这是双轨证据与 W3 采集器的原料
  （详见已知限制 10）。同日重跑覆盖为更晚快照，幂等无害；多候选策略目录时按
  提示 `--src` 显式指定。

### token 失效的症状与处置

症状：探针或策略日志报
`GmError: {"status": 1000, "message": "错误或无效的token", ...}`（`set_token` 后
服务端拒绝鉴权）。处置：终端「系统管理 → 密钥管理」重新生成 → 复制新值覆盖
runtime.json 的 `token` 键 → 重启策略。旧 token 在新值生成瞬间即作废，凡「刚还能跑
突然 1000」先怀疑别处生成了新 token。

---

## 四、回退 SOP（三步，按序执行）

1. **拦增量**：`touch <策略目录>/state/RISK_BLOCK.flag`（Windows Git Bash 同名命令；
   文件存在即生效，内容不看）。只拦**新挂买单**（pre_open 阶段④跳过挂单段）——
   存量管理照常执行：撤昨日单、超期平仓、tick 巡检的止损/止盈不会被它停。
2. **撤未成交单**：终端手动交易页，逐张撤掉柜台未成交委托（策略侧 ① 也会在次日
   pre_open 自动撤昨日非终态买单，但回退场景不等次日）。
3. **清仓仿真腿**：终端手动卖出全部持仓，把仿真账户恢复空仓。

恢复试点：删除 `RISK_BLOCK.flag`（`rm` 即可），次一交易日 pre_open 起自动恢复挂单。
可选收紧项 `state/CAP.txt`：单值总仓位上限（如 `0.5`）；缺文件=1.0 不限制，值非法
/越界 [0,1] 视同 0 **全拦**（fail-closed，宁误拦不误放）。

---

## 五、数据对拍晨间补跑（W0'，Task 10 夜间降级的出口）

> 环境注记：三步命令均在 **Git Bash、仓库根目录 `E:/quanter`** 下执行——命令是
> POSIX 前缀写法（`PYTHONUTF8=1 <python> <脚本>`），PowerShell/cmd 的 env 前缀语法
> 不同会直接报错。完整版见
> `docs/research/2026-08-21-emquant-data-parity-report.md` 第三节。

第 0 步（人工，一次性）：终端「系统管理 → 密钥管理」生成 token → 填进仓库侧
`emquant/config/runtime.json`。

第 1 步：锚表（`.venv310`；lake 未更新时免跑，已有
`emquant/state/parity_anchors_20260821.json`）：

```
PYTHONUTF8=1 E:/quanter/.venv310/Scripts/python.exe emquant/tools/compare_data.py anchors
```

第 2 步：gm 腿取数（`.venv_emquant`）：

```
PYTHONUTF8=1 E:/quanter/.venv_emquant/Scripts/python.exe emquant/tools/gm_data_pull.py
```

产物：`emquant/state/parity_gm_YYYYMMDD.csv` + 失败清单
`parity_pull_fail_YYYYMMDD.json`。退出码分流：**2 = runtime.json 缺失或 token 空
（当前最可能先命中——回第 0 步）** / 3 = token 无效（status 1000，重新生成） /
4 = 服务不在线（先拉起终端），**或探针返回 0 行（服务应答但无数据——检查终端
登录态与数据服务配置后重试）** / 5 = 缺锚表（回第 1 步） / 6 = 全部标的失败（看
异常摘要与终端日志）。

第 3 步：对拍 + 报告（`.venv310`）：

```
PYTHONUTF8=1 E:/quanter/.venv310/Scripts/python.exe emquant/tools/compare_data.py compare
```

产物：排除池 `emquant/state/parity_exclude_YYYYMMDD.json`（W2 一致率分母豁免清单）
+ markdown 摘要（`--md-out` 可直接落盘成段，贴回对拍报告第四节）。判读口径：容差
`|gm−lake|/|lake| ≤ 1e-6` 逐日逐列、标的级判定；交叠日 <60 单列
`insufficient_overlap`；gm（东财源）与 tushare 前复权因子独立维护，**大面积不一致
是有效结论而非工具故障**——如实记录一致率 / 最大相对偏差 / 典型样例三数，不为
好看放宽容差。

compare 侧退出码 10–13 / 20–21（10 = data_lake 不存在；11 = lake 读回形状异变；
12 = 缺 gm 腿产物；13 = gm 腿符号口径泄漏；20 / 21 = selftest 抽样不足 / 未过）：
脚本首行会打印 `[compare_data] 终止：<中文诊断>`（`_die` 统一出口），按该行处置。

---

## 六、双轨比对方法（W2：掘金腿 vs 本地腿）

- **信号面**：掘金腿 audit 的 `SIGNAL` 行（detail 字段 symbol / neckline /
  entry_price / rr / formed_at / atr）逐字段对本地腿 trade_event 的
  `SIGNAL.meta` 同名字段。核心四字段 **symbol / neckline / entry_price / rr**
  逐一对数（浮点按显示精度或微容差人工判读），formed_at 用于对齐同日信号。
- **比对域 = 双腿 universe 交集**：本地腿 universe 与快照 universe 各自演进时不
  强求全集一致，交集外的单边信号先记录不判分歧。
- **排除池豁免**：对拍排除池（`parity_exclude_YYYYMMDD.json`）内的标的不参与
  一致率分母——数据源已判分叉的标的，信号分歧是下游结论不是新证据。
- **已知分歧行剔除**：audit `SELL` 行 detail 含 `"known_divergence":
  "tp1_dust_clears"` 的记录（tp1 不足一手时 pilot 清剩余 vs 本地两腿模型份额沉到
  tp2 腿）在双轨复盘时剔除，不计数为分歧。
- **执行面辅助**：`ORDER_PLACED`（price/qty/cancel_on）vs 本地腿订单表；
  `EXPIRE_SELL`/`SELL`（reason/qty/price）vs 本地平仓事件——口径差参照下节已知限制。

---

## 七、已知限制与口径注记（双轨复盘前必读）

1. **trailing 退化固定止损 = 本地现状**：快照 EXEC_PARAMS 的 trailing 三件
   grace 0 / step 0.0 / floor 0.0 → `compute_stop_price` 恒走 base_stop 支。这是
   ACTIVE 实验实弹值（`params_snapshot.json` notes.trailing_dual_source_note），
   两腿在退化数学下逐位一致；trailing 活跃分支仅为未来参数演进预置。
2. **单票 5% 闸不聚合同票存量 = 本地 pos_cap 同口径**：闸④只校验「本单金额 ≤
   5%×equity」，同票次日再挂的聚合敞口由闸②CAP 总额度兜底——与本地
   pre_open 单票定尺 + 总额度复核的两层结构同构，不是缺口。
3. **state.pkl 内容是 JSON、名是 pkl**：pickle 绑 Python 版本/类路径，跨终端版本
   不可移植；JSON 人工可检视、可手工修复。损坏 / version≠1 / 缺必备键 → 启动期
   raise（宁停不裸奔），不要手工「重置」它——先对柜台实况。
4. **get_orders 只返日内委托 → 「撤昨日单」近似空转**：gm 委托查询无「隔夜单」
   概念，昨日未成交单在柜台属日效单、次日自然失效（A 股现货委托当日有效，残留
   风险有限）。pre_open ①以柜台 created_at==今日判「当日单不自杀」，实际撤到的
   几乎只有当日早前的单——语义保守方向正确，晨检不必因「没撤到昨日单」报警。
5. **09:15 schedule 触发属 live 待验证项**：`schedule(pre_open_job, "1d",
   "09:15:00")` 的时刻规则在源码与官方文档均无禁令，机制上应可触发，但未入已
   验证面——live 首日重点观察 09:15 是否如约落 pre_open 痕迹；不触发则五阶段
   整体后移到首个可触发时刻并回评（Task 2 文档 §五残余不确定清单第 1 条）。
6. **同账户双网关冲突是 W3 前必答题**：本地腿（QMT）与掘金腿若绑同一仿真/实盘
   账户，两侧订单会互相进入对方对账视野（absorb_reality 会把对方挂单收编成
   UNKNOWN 单）。W3 接线前必须物理分账户或显式约定单账户单网关，未解决前掘金腿
   只用上述绑定账户跑。
7. **cooldown 语义复刻的是引擎后置去重**：scan_live 本身无 cooldown，本地是
   `_eod` 在扫描后按 formed_at 锚点 + cooldown（8 交易日）过滤同标的；pilot 以
   `state["last_signal"]` 锚点复刻同语义（依据与证据链见
   `params_snapshot.json` notes.cooldown_semantics）。
8. **history `end_time` 含端性属 live 待验证项**（终审修复注记）：取数按闭区间
   `含 end_date` 契约实现（与本地 `.loc[:date]` 同口径），已加**末根不变量**
   防御——返回缺 `end_date` 末根时按故障丢弃（`fetch_end_missing` WARN + 返
   None），绝不在偏一日的序列上算信号。首夜观察该 WARN 是否出现：出现即为
   服务端端性行为与文档口径分叉，须回评取数参数。
9. **get_history_symbol 盘前当日行可见性属 live 待验证项**（终审修复注记）：
   跌停价取值以 `get_history_symbol` 的 `lower_limit` 优先，但盘前 09:15 调用时
   【当日】证券信息行是否已生成未入已验证面。已有三级回退兜底（API 值 → 同行
   pre_close 自算 → T-1 收盘价自算），盘前缺行时走 T-1 收盘自算（20% 档，创板
   科创池内与 API 值几乎恒等，`limit_down_fallback_t1` WARN 留痕）；首夜观察
   该 WARN 频度即可判定真实可见性。
10. **终端腿运行时单机单副本，靠仓库侧归档缓解**：state/audit 只活在终端策略
    目录（`~/.emgm3/projects/<strategy_id>/`），重装终端/误删即丢——在途单与
    持仓可由柜台对账（absorb_reality）重建，但历史 orders/positions 档案、
    cooldown 锚（last_signal）与 audit 原始件不可再生。缓解：每日跑
    `emquant/tools/archive_terminal_state.py`（晨检清单末项）把快照拷回
    `emquant/archive/`。W3 若转正，演进为「audit → trading_state.db 采集器」
    （仓库侧单向消费，掘金腿保持哑终端；本地库全表已预留 account_id 维度，
    掘金账户天然隔离）。

---

## 八、开发注记（改代码前必读）

1. **改 `pilot_body.py` / 识别内核 / 快照后必须重跑组装器**：
   `PYTHONUTF8=1 E:/quanter/.venv310/Scripts/python.exe emquant/build_pilot.py`。
   `emquant_neckline_pilot.py` 是**产物不是源**——手改会被「重跑 build == 已提交
   产物」幂等断言（test_build_idempotent）当场拦下。
2. **无 gm import 验证（C4 红线：顶层禁 `import gm`）——用修正版命令**。plan 原版
   `spec_from_file_location` 不注册 `sys.modules` 直接 exec 会踩 dataclasses 注解
   解析崩溃（Task 4 实测）。修正版（import_module 式，毒丸让任何 `import gm` 必炸）：

   ```
   PYTHONUTF8=1 E:/quanter/.venv310/Scripts/python.exe -c "import sys; sys.path.insert(0, 'emquant'); sys.modules['gm']=None; import importlib; m=importlib.import_module('emquant_neckline_pilot'); print('no-gm import ok', m.PARAMS_FINGERPRINT, len(m.UNIVERSE))"
   ```

   预期输出指纹 `01903dec5029da9d`（R6-8 冠军快照，2026-08-26 换代；上一代
   `3466a8fca0b554cb`）、universe 300 只。
3. **`.venv_emquant` 禁止 pip install 新包**：gm 装在专用 venv，pandas/numpy 已与
   `.venv310` 钉齐版本；对拍中间产物用 csv 正是为双端零依赖等价（parquet 需
   pyarrow，禁装）。任何「顺手装个包」都可能破坏双环境对拍口径。
4. **双环境全量测试**（提交前必绿）：

   ```
   PYTHONUTF8=1 E:/quanter/.venv310/Scripts/python.exe -m pytest tests/emquant -q
   PYTHONUTF8=1 E:/quanter/.venv_emquant/Scripts/python.exe -m pytest tests/emquant -q
   ```

5. **快照重导出**：本地实验参数 / universe 演进后跑
   `emquant/export_snapshot.py` 重导出（fingerprint 随之变更），再重跑组装器——
   指纹不一致的两腿没有对拍资格。

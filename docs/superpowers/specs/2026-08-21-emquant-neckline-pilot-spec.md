# 东财掘金颈线单文件试点 · 规格（Spec）2026-08-21

> 上游：[设计 v1](../superpowers/plans/2026-08-21-emquant-neckline-pilot-design.md) · [东财掘金接入调研](../../research/2026-08-21-emquant-integration-feasibility.md)。本 spec 是今夜自主开发的唯一需求真相源；计划见 [plan](../superpowers/plans/2026-08-21-emquant-neckline-pilot-plan.md)。

## 1. 目标（一句话）

把颈线策略（本地引擎当前生效参数集）的「识别 + 挂单 + 巡检出场」闭环装进**一个 gm SDK 单文件**（`emquant/emquant_neckline_pilot.py`），以东财掘金**仿真模式**运行，与本地 QMT 引擎构成双轨对照。

## 2. 硬约束（Global Constraints，违反即事故）

| # | 约束 | 依据 |
|---|---|---|
| C1 | **仿真 only**：run 模式硬编码 MODE_SIMULATION；account 固定仿真账户 `e7cb55d6-04ab-4ea9-98fc-503d9f97d2a1`；除非 env `PILOT_ALLOW_LIVE=I_KNOW_REAL_MONEY` 否则拒绝实盘模式 | D2 + 资金安全 |
| C2 | **识别内核逐字**：`method_v0.py` + `signal.py` 进单文件仅允许两个变换——①删 `from .signal import Signal` 一行；②`from __future__ import annotations` 提升到组装文件顶部。其余逐字节一致，由等价性测试钉死 | 设计 §2 等价证据 |
| C3 | **零引擎改动**：不修改仓库任何既有文件（除 `.gitignore` 追加与 docs）；生产引擎/策略/测试全部不动 | 试点隔离 |
| C4 | **gm 懒加载**：单文件在无 gm 环境下可 import（§0-§4/§7 纯 stdlib+pandas；gm 调用经 `_api()` 惰性取，测试经模块级 seam 注入 FakeGm） | 本地 pytest 同一基线 |
| C5 | **数值等价环境**：专用 venv `.venv_emquant`（系统 Python 3.10.11），pandas/numpy 钉 `requirements.txt` 同款下限（pandas≥2.0.0 / numpy≥1.24.0），绝不污染 `.venv310` | 浮点逐位等价 |
| C6 | **分支纪律**：全程 `feature/emquant-pilot-0821` 分支，逐任务提交，**不 push、不碰 master** | 可回滚 |
| C7 | **全中文像素级注释**（What+Why），风格对齐仓库既有注释密度 | CLAUDE.md |
| C8 | **token/secret 不入库**：token 只存 `emquant/config/runtime.json`（gitignored）；任何文档/日志/commit 不得出现 token 值 | 安全 |
| C9 | 生命周期口径以本地实码为准：max_wait 以 **formed_at 起算严格大于**（`pre_open.py:574-575`）；超期 holding 基准日 **T-1（pretrade_date）**（`pre_open.py:436-439`）；挂单价 **entry = neckline + buy_limit_atr_mult×ATR**（`method_v0.py:525`）；五价位公式与 `price_levels.py` 单源一致 | 08-20 复审实证 |

## 3. 交付物（文件树）

```
emquant/
  build_pilot.py            # 组装器：signal.py+method_v0.py(变换C2)+pilot_body.py → 单文件
  pilot_body.py             # §0/§2-§7 源（组装进单文件；不直接被测试 import）
  export_snapshot.py        # 在 .venv310 下跑：导出参数快照 + universe + 指纹
  emquant_neckline_pilot.py # 【最终单文件】组装产物（提交，可拷入掘金终端）
  README.md                 # 运维 runbook（挂载/晨检/回退 SOP）
  config/
    params_snapshot.json    # 生成物（提交）
    universe.json           # 生成物（提交）
    runtime.json.example    # 模板（提交）
    runtime.json            # token/strategy_id 真值（gitignored）
  tools/
    gm_api_probe.py         # SDK 签名探针（安装后可跑）
    gm_data_pull.py         # 对拍取数（终端/token 在线时跑）
    compare_data.py         # gm vs data_lake 对拍（1e-6，产排除池）
  state/ audit/             # 运行时目录（gitignored）
docs/research/2026-08-21-gm-sdk-api-verified.md   # SDK 源码级 API 核对
tests/emquant/              # fake_gm.py + 5 个测试文件（见 plan 各任务）
```

## 4. 功能需求

- **FR1 参数快照**：`export_snapshot.py` 在 .venv310 下实例化 `NecklineMethodStrategy()`（无 override = 当前生产缺省口径）+ `trading.critical._trade_cfg()`（.env 实弹值），导出 `ID_PARAMS`/`EXEC_PARAMS` 全键（**必须含 `buy_limit_atr_mult` 与 `cooldown`**）+ `PARAMS_FINGERPRINT`（sha256(sorted kv)）。同时导出 UNIVERSE ≤300：来源=本地引擎实盘 `_load_universe` 同口径（**含创板科创过滤** `_filter_chuangke_kechuang`：剔 300/301/688/689 前缀），不足 300 用 data_lake 流动性补足；并核实 `_eod` 是否施加 cooldown 去重（若有则 pilot 同步复刻）。
- **FR2 状态层**：`state.pkl`（scan_done/placed/orders/positions 四表，schema 同设计 §3）tmp+rename 原子写；`RISK_BLOCK.flag`（存在=拦增量，存量管理照常）/ `CAP.txt`（缺省 1.0）。
- **FR3 风控闸**：CAP 逐单扣减 `总权益×CAP−持仓市值−已挂买单金额`，查询失败 **fail-closed 当日不挂**；试点硬闸：单日新挂 ≤2、单票市值 ≤5%（写死 §0）。
- **FR4 数据层**：gm `history` 定点前复权（ADJUST_PREV + adjust_end_time=T-1，end_time=T-1）→ df_upto（OHLCV 列名对齐 + DatetimeIndex）；符号映射 `600000.SH↔SHSE.600000`；交易日历从指数日线序列推导（不依赖额外 API）。
- **FR5 订单生命周期**（§4 全表，C9 口径）：盘前限价买 entry=颈线+bl×ATR、每日撤昨日重挂、cancel_on 触价撤、max_wait formed_at 严格大于过期、止损/tp1（portion 一次）/tp2 触价卖、超期 T-1 基准日跌停价清仓、`compute_stop_price`（海龟 trailing）从 `strategies/neckline/execution.py` **逐句移植**（golden 对拍仓库真身；快照默认 grace=0/step=0.0 → 退化为固定止损=本地现状）。部分成交：剩余腿继续等、撤单后已成交部分转持仓。
- **FR6 事件编排**：`init`（读配置+订阅巡检标的）→ `schedule 09:15` 盘前五阶段（①撤昨日买单 ②超期平仓 ③扫描识别 ④风控闸 ⑤逐单挂限价买，幂等三查以柜台实况修 state）→ `on_tick` 巡检（pending: cancel_on；positions: stop/tp1/tp2/超期）→ `schedule 15:35` 盘后落 audit + 清动态订阅。
- **FR7 对拍审计**：`audit_YYYYMMDD.csv` 逐行（ts,event,symbol,cl_ord_id,price,qty,reason,detail）；每日扫描信号集合全量落 audit（symbol/neckline/entry_price/rr）供双轨比对。
- **FR8 组装与等价**：`build_pilot.py` 生成单文件；测试钉死（a）内核逐字节等价（b）pilot_body 逐字包含（c）行为等价——同一合成数据上 `pilot.detect_signal == repo.detect_signal`（symbol/neckline/entry_price/atr/rr 逐字段）。
- **FR9 W0' 夜间实测（尽力）**：token 发现（掘金终端用户数据目录）→ 数据对拍脚本跑通并出报告；token 不可得则降级为 runbook 晨间步骤。

## 5. 验收标准（AC）

1. `.venv310 -m pytest tests/emquant -q` 全绿；`.venv_emquant` 下同套测试亦绿（gm 共存证明）。
2. 单文件在无 gm 的解释器下 `import emquant_neckline_pilot` 成功（C4）。
3. 内核等价性测试逐字节通过（C2）。
4. `export_snapshot.py` 产物键集完整（含 buy_limit_atr_mult/cooldown/trailing 三件）且 fingerprint 稳定。
5. 仿真模式守卫测试：mode 非 simulation 且无 PILOT_ALLOW_LIVE → 拒绝启动。
6. 所有提交在 feature 分支；`git status` 无生产文件改动（C3）。
7. README runbook 完整（终端挂载步骤/晨检清单/回退三步 SOP/token 填写位）。

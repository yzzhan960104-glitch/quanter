# 东财掘金颈线单文件试点 · 实施计划（2026-08-21 夜班）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把颈线策略完整闭环装进一个 gm SDK 单文件（东财掘金仿真模式），与本地 QMT 引擎构成双轨对照腿。

**Architecture:** 单文件 = `signal.py` + `method_v0.py`（逐字内核，两个白名单变换）+ `pilot_body.py`（§0 参数快照由 build 注入 / §2-§7 编排）经 `build_pilot.py` 组装；测试直接 import 组装产物，gm 经模块级 seam 懒加载，FakeGm 注入做全生命周期 TDD。

**Tech Stack:** Python 3.10（`.venv_emquant` 专用环境 + 仓库 `.venv310` 双跑）、gm SDK（`pip install gm -i https://mirrors.aliyun.com/pypi/simple/ -U`）、pandas≥2.0.0 / numpy≥1.24.0、pytest。

## Global Constraints（摘自 [spec](../specs/2026-08-21-emquant-neckline-pilot-spec.md)，每个任务隐含遵守）

- **C1 仿真 only**：`MODE_SIMULATION` 硬编码；account=`e7cb55d6-04ab-4ea9-98fc-503d9f97d2a1`；`PILOT_ALLOW_LIVE` 未显式设 `I_KNOW_REAL_MONEY` 时拒绝非仿真模式。
- **C2 内核逐字**：仅允许 ①删 `from .signal import Signal` 行 ②`from __future__ import annotations` 提升；等价性测试钉死。
- **C3 零引擎改动**：不碰仓库既有文件（`.gitignore` 追加与 docs 除外）。
- **C4 gm 懒加载**：无 gm 环境可 import 单文件；`_api()` seam + 模块级 `_GM`，测试 monkeypatch。
- **C5 环境隔离**：`.venv_emquant` 用系统 `C:\Users\yzzhan\AppData\Local\Programs\Python\Python310\python.exe`（3.10.11）创建；pandas/numpy 装与 `.venv310` 相同版本（先 `pip freeze` 比对再装）；绝不 `pip install` 进 `.venv310`。
- **C6 分支纪律**：`feature/emquant-pilot-0821`，逐任务提交，不 push 不碰 master。
- **C7 全中文像素级注释**（What+Why，对齐仓库密度）。
- **C8 token 不入库**：只写 `emquant/config/runtime.json`（gitignored）；文档/日志/commit 禁止出现 token 值。
- **C9 生命周期口径**：max_wait 以 formed_at 起算**严格大于**；超期 holding 基准日 **T-1**；entry=neckline+buy_limit_atr_mult×ATR；五价位与 `price_levels.py` 单源同式。
- **API 名权威规则**：Task 2 产出的 `docs/research/2026-08-21-gm-sdk-api-verified.md` 是 gm API 名/签名的**唯一权威**；本计划代码片段中的 API 名为通识写法，若与 Task 2 核对结果冲突，以 Task 2 为准并在实现处注明核对来源行号。
- **测试命令**（两环境都须绿）：`.venv310/Scripts/python.exe -m pytest tests/emquant -q` 与 `.venv_emquant/Scripts/python.exe -m pytest tests/emquant -q`。

---

### Task 1: 分支与专用环境（.venv_emquant + gm 安装 + 探针）

**Files:**
- Modify: `.gitignore`（追加 emquant 运行时条目）
- Create: `emquant/tools/gm_api_probe.py`

**Interfaces:**
- Produces: `.venv_emquant`（后续任务的 gm 环境）；`gm_api_probe.py` 打印关键 API 签名（Task 2 的取材对象）。

- [ ] **Step 1: 建分支**

```bash
cd E:/quanter && git checkout -b feature/emquant-pilot-0821
```

- [ ] **Step 2: 创建专用 venv 并安装（绝不装进 .venv310）**

```bash
"C:\Users\yzzhan\AppData\Local\Programs\Python\Python310\python.exe" -m venv E:/quanter/.venv_emquant
E:/quanter/.venv_emquant/Scripts/python.exe -m pip install -i https://mirrors.aliyun.com/pypi/simple/ -U pip
E:/quanter/.venv_emquant/Scripts/python.exe -m pip install -i https://mirrors.aliyun.com/pypi/simple/ -U gm pandas numpy pytest
# 版本对齐核对（与 .venv310 的 pandas/numpy 主版本一致即可，不一致则 pip install == 同版本）
E:/quanter/.venv_emquant/Scripts/python.exe -c "import gm,pandas,numpy;print(gm.__version__ if hasattr(gm,'__version__') else 'gm-ok',pandas.__version__,numpy.__version__)"
E:/quanter/.venv310/Scripts/python.exe -c "import pandas,numpy;print(pandas.__version__,numpy.__version__)"
```

- [ ] **Step 3: 写探针脚本** `emquant/tools/gm_api_probe.py`：import gm.api，`inspect.signature` 打印 `run/schedule/order_volume/order_cancel/get_orders/history/subscribe/current` 及常量 `MODE_LIVE/MODE_SIMULATION/MODE_BACKTEST/ADJUST_PREV/ADJUST_NONE`、枚举 `OrderSide/OrderType/PositionEffect`（存在才打）；末尾打印 `gm.api.__file__`（site-packages 路径，Task 2 用）。全中文注释。

- [ ] **Step 4: 跑探针验证** `E:/quanter/.venv_emquant/Scripts/python.exe emquant/tools/gm_api_probe.py` → 输出签名清单。

- [ ] **Step 5: .gitignore 追加**

```
# 东财掘金试点运行时（state/audit/token 真值，不入库）
.venv_emquant/
emquant/state/
emquant/audit/
emquant/config/runtime.json
```

- [ ] **Step 6: Commit**

```bash
git add .gitignore emquant/tools/gm_api_probe.py
git commit -m "feat(emquant): 试点分支环境——专用 venv+gm 探针+运行时 gitignore"
```

---

### Task 2: gm SDK 源码级 API 核对文档（权威名映射）

**Files:**
- Create: `docs/research/2026-08-21-gm-sdk-api-verified.md`

**Interfaces:**
- Consumes: `.venv_emquant` 内 gm 包源码（`E:/quanter/.venv_emquant/Lib/site-packages/gm/`）。
- Produces: **权威 API 名映射表**（后续所有任务的 gm 调用以此为准）：run/schedule/订阅/下单族/撤单/查委托/查成交/资金/持仓/跌停价（证券基本信息接口，找 `get_history_symbol`/`current` 类字段）/history 及其 DataFrame 列名与 adjust 参数/交易日历函数/仿真常量。每条注明源码 file:line 与签名原文。

- [ ] **Step 1: 通读 gm 包**：重点 `gm/api.py`（或 `gm/__init__.py` 暴露面）、`gm/constant.py`、CSdk 封装层；确认：①仿真模式常量真名与 run 参数；②`schedule` 签名（date_rule/time_rule 格式）；③下单函数限价买/卖的最小参数集（symbol/volume/side/order_type/position_effect/price/account）；④`get_orders`/成交回报/持仓与资金的**真函数名与返回字段**（掘金4 可能是 `get_account_positions(account_id)` 类命名）；⑤`history` 的 fields/adjust/adjust_end_time/df 参数与返回列名；⑥tick 数据结构字段（last_price/high/low?）；⑦策略目录约定（终端如何定位 main.py）。
- [ ] **Step 2: 写核对文档**：逐条「用途 → 权威签名原文 → 源码位置 → 与设计/计划通识名的差异」。差异表单独一节（例：`cash()` 实为 `get_account_cash(account_id)`）。
- [ ] **Step 3: Commit**

```bash
git add docs/research/2026-08-21-gm-sdk-api-verified.md
git commit -m "docs(emquant): gm SDK 源码级 API 核对——签名/常量/列名权威映射"
```

---

### Task 3: 参数与 universe 快照导出器（.venv310 侧）

**Files:**
- Create: `emquant/export_snapshot.py`
- Create(生成): `emquant/config/params_snapshot.json`、`emquant/config/universe.json`

**Interfaces:**
- Consumes: `strategies.neckline.method_v0.DEFAULTS`、`strategies/neckline/backtest.py EXEC_DEFAULTS`、`trading.critical._trade_cfg()`（读 .env 实弹值）、data_lake。
- Produces: `params_snapshot.json = {"id_params": {...11键}, "exec_params": {...12+键含 buy_limit_atr_mult/cooldown/trailing 三件}, "trade_cfg": {pos_cap,...}, "fingerprint": sha256, "exported_at", "sources": {...每键来源 file:line}}`；`universe.json = {"symbols": ["600000.SH", ...≤300], "source": "...", "exported_at"}`。

- [ ] **Step 1: 写导出器**（在 .venv310 下运行，sys.path 注入项目根，范式抄 `trading/tools/veto_plan.py:30-33`）：
  - id_params = `dict(DEFAULTS)`；
  - exec_params = `dict(EXEC_DEFAULTS)` 逐键；**必须断言** `buy_limit_atr_mult`、`cooldown`、`trailing_grace/step/floor` 在键中（防 signal.py:117 型漏键——C9）；
  - trade_cfg = `_trade_cfg()`（pos_cap/max_wait/max_holding/trailing env 实弹值）；
  - **口径对齐说明写入 json**：本地引擎 exec 实弹 = EXEC_DEFAULTS（无实验 override 时）；若发现 `_eod` 有实验 override 通道且当前激活（查 `research/proposals.py` 最新 ACCEPTED + engine 读取点），把激活实验参数合并进 exec_params 并记 sources；
  - **cooldown 语义核实**：读 `trading/engine.py` `_eod`，确认 scan 后是否有 cooldown 去重（strategy.scan_at 有、scan_live 无——引擎层是否补）；结论与依据写进 json 的 `notes`；
  - universe：定位引擎实盘 `_load_universe`（grep `trading/` 找定义），**复刻其口径**（含 `_filter_chuangke_kechuang` 剔 300/301/688/689），从 data_lake 取该池子；若池子 >300，按近 60 日成交额降序取前 300；若 <300 全取。列表用 ts 格式（`600000.SH`）。
  - fingerprint = `hashlib.sha256(json.dumps({id,exec,trade,universe_symbols}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]`。
- [ ] **Step 2: 运行** `.venv310/Scripts/python.exe emquant/export_snapshot.py` → 生成两个 json；打印 fingerprint 与键数、universe 数。
- [ ] **Step 3: 验证产物**：`.venv310/Scripts/python.exe -c "import json;d=json.load(open('emquant/config/params_snapshot.json',encoding='utf-8'));assert {'buy_limit_atr_mult','cooldown','trailing_grace','trailing_step','trailing_floor'}<=set(d['exec_params']);u=json.load(open('emquant/config/universe.json',encoding='utf-8'));assert len(u['symbols'])<=300;assert not any(s[:3] in ('300','301','688','689') for s in u['symbols'])"`
- [ ] **Step 4: Commit**

```bash
git add emquant/export_snapshot.py emquant/config/params_snapshot.json emquant/config/universe.json
git commit -m "feat(emquant): 参数/universe 定稿快照导出器——EXEC 全键含 buy_limit_atr_mult+创板科创过滤"
```

---

### Task 4: 组装器 + 内核等价性测试（C2 钉死）

**Files:**
- Create: `emquant/build_pilot.py`、`emquant/pilot_body.py`（本任务先放最小骨架）、`emquant/emquant_neckline_pilot.py`（生成物）
- Test: `tests/emquant/test_kernel_equivalence.py`

**Interfaces:**
- Produces: `build_pilot.build(output_path=None) -> Path`（读 signal/method_v0/params/universe/pilot_body → 写单文件）；单文件内核段可 `import`。
- 后续任务往 `pilot_body.py` 增补 §2-§7 后重跑 build。

- [ ] **Step 1: 写失败测试** `tests/emquant/test_kernel_equivalence.py`：

```python
# -*- coding: utf-8 -*-
"""内核逐字等价（C2 红线）：组装文件中的 signal/method_v0 与仓库真身逐字节一致
（仅允许删 `from .signal import Signal` 行与提升 __future__ 两变换）。"""
import importlib.util, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ARTIFACT = ROOT / "emquant" / "emquant_neckline_pilot.py"

def _import_artifact():
    spec = importlib.util.spec_from_file_location("emquant_neckline_pilot", ARTIFACT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m

def _normalize(src: str) -> str:
    """白名单变换的逆：两侧各删 __future__ 行与 Signal 相对 import 行后必须逐行相等。"""
    drop = ("from __future__ import annotations", "from .signal import Signal")
    return "\n".join(l for l in src.splitlines() if l.strip() not in drop)

def test_kernel_byte_equivalent():
    art = ARTIFACT.read_text(encoding="utf-8")
    sig = (ROOT / "strategies/neckline/signal.py").read_text(encoding="utf-8")
    mv0 = (ROOT / "strategies/neckline/method_v0.py").read_text(encoding="utf-8")
    assert _normalize(sig) in _normalize(art)          # signal 段逐字在
    assert _normalize(mv0) in _normalize(art)          # method_v0 段逐字在

def test_artifact_importable_without_gm():
    sys.modules.pop("gm", None); sys.modules.pop("gm.api", None)
    m = _import_artifact()                              # C4：无 gm 也能 import
    assert hasattr(m, "detect_signal") and hasattr(m, "Signal")

def test_detect_signal_behavior_equal():                # 行为等价：合成数据双跑逐字段比对
    import pandas as pd, numpy as np
    from strategies.neckline.method_v0 import detect_signal as repo_det
    m = _import_artifact()
    rng = np.random.default_rng(42)                     # 合成上行突破形态
    n = 120; base = 10.0
    close = base + np.cumsum(rng.normal(0.002, 0.02, n))
    high = close * (1 + np.abs(rng.normal(0, 0.008, n))); low = close * (1 - np.abs(rng.normal(0, 0.008, n)))
    vol = np.full(n, 1e6) * (1 + np.abs(rng.normal(0, 0.3, n)))
    idx = pd.date_range("2026-05-01", periods=n, freq="B")
    df = pd.DataFrame({"open": close * 0.999, "high": high, "low": low, "close": close, "volume": vol}, index=idx)
    a = repo_det("600000.SH", df, m.ID_PARAMS, m.EXEC_PARAMS, idx[-1])
    b = m.detect_signal("600000.SH", df, m.ID_PARAMS, m.EXEC_PARAMS, idx[-1])
    assert (a is None) == (b is None)
    if a is not None:
        for k in ("symbol", "neckline", "bottom", "entry_price", "atr", "rr"):
            assert getattr(a, k) == getattr(b, k)
```

- [ ] **Step 2: 跑测试确认失败** `.venv310/Scripts/python.exe -m pytest tests/emquant/test_kernel_equivalence.py -q` → FAIL（文件不存在）。
- [ ] **Step 3: 实现 `build_pilot.py`**：

```python
# -*- coding: utf-8 -*-
"""组装器：内核逐字块 + §0 快照 + pilot_body → 单文件（C2 白名单变换-only）。"""
# 变换纪律（C2）：①删 `from .signal import Signal`（Signal 已内嵌于前段）
#               ②`from __future__ import annotations` 提升至组装文件顶部（语法要求）。
# 其余任何字节差异都会被 test_kernel_byte_equivalent 拦下。
from __future__ import annotations
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FUTURE = "from __future__ import annotations"
SIGNAL_IMPORT = "from .signal import Signal"

def _strip(src: str, *drops: str) -> str:
    return "\n".join(l for l in src.splitlines() if l.strip() not in drops).strip("\n")

def build(output_path: Path | None = None) -> Path:
    snap = json.loads((ROOT / "emquant/config/params_snapshot.json").read_text(encoding="utf-8"))
    uni = json.loads((ROOT / "emquant/config/universe.json").read_text(encoding="utf-8"))
    sig = _strip((ROOT / "strategies/neckline/signal.py").read_text(encoding="utf-8"), FUTURE)
    mv0 = _strip((ROOT / "strategies/neckline/method_v0.py").read_text(encoding="utf-8"), FUTURE, SIGNAL_IMPORT)
    body = (ROOT / "emquant/pilot_body.py").read_text(encoding="utf-8")
    head = (
        "# -*- coding: utf-8 -*-\n"
        '"""东财掘金·颈线策略单文件试点（组装产物，勿手改——改 pilot_body.py 后重跑 build_pilot.py）。\n'
        f"PARAMS_FINGERPRINT={snap['fingerprint']}  生成物见 emquant/config/。\n"
        '"""\n' + FUTURE + "\n\n"
    )
    sec0 = (
        "# ============================ §0 参数区（export_snapshot 导出的定稿快照）============================\n"
        f"ID_PARAMS = {snap['id_params']!r}\n"
        f"EXEC_PARAMS = {snap['exec_params']!r}\n"
        f"TRADE_CFG = {snap['trade_cfg']!r}\n"
        f"UNIVERSE = {uni['symbols']!r}\n"
        f"PARAMS_FINGERPRINT = {snap['fingerprint']!r}\n"
        "# 试点硬闸（spec FR3）：单日新挂 ≤2；单票市值 ≤5%；仿真账户固定。\n"
        "PILOT_MAX_NEW_ORDERS_PER_DAY = 2\n"
        "PILOT_MAX_POSITION_PCT = 0.05\n"
        "PILOT_ACCOUNT_ID = 'e7cb55d6-04ab-4ea9-98fc-503d9f97d2a1'\n\n\n"
    )
    parts = [head, sec0,
             "# ============================ §1 识别内核（signal.py + method_v0.py 逐字块，C2）============================\n" + sig + "\n\n\n" + mv0 + "\n\n\n",
             "# ============================ §2-§7 执行编排（pilot_body.py）============================\n" + body]
    out = output_path or (ROOT / "emquant" / "emquant_neckline_pilot.py")
    out.write_text("\n".join(parts), encoding="utf-8")
    return out

if __name__ == "__main__":
    print(build())
```

  并创建最小 `pilot_body.py` 骨架（本任务只需可组装）：`# -*- coding: utf-8 -*-` + 空实现 `def run_pilot(): raise NotImplementedError` + `if __name__ == "__main__": run_pilot()`。
- [ ] **Step 4: 跑 build + 测试通过**：`E:/quanter/.venv310/Scripts/python.exe emquant/build_pilot.py` 后 pytest 全绿（行为等价用例若合成数据无信号，两侧 None==None 也算过；可再加一个用 repo 测试素材构造有信号用例：import `tests/test_detect_signal.py` 的 fixture 函数若可复用则复用）。
- [ ] **Step 5: Commit**

```bash
git add emquant/build_pilot.py emquant/pilot_body.py emquant/emquant_neckline_pilot.py tests/emquant/test_kernel_equivalence.py
git commit -m "feat(emquant): 组装器+内核逐字等价测试——C2 白名单变换钉死"
```

---

### Task 5: §3 状态层 + §4 风控闸（pilot_body 增补，TDD）

**Files:**
- Modify: `emquant/pilot_body.py`
- Test: `tests/emquant/test_state_and_gates.py`

**Interfaces:**
- Produces（单文件内的名字，后续任务消费）：
  - `BASE_DIR/STATE_DIR/AUDIT_DIR/CONFIG_DIR: Path`（基于 `__file__`）
  - `load_state() -> dict`（schema：`{"version":1,"scan_done":set→json 用 list,"placed":{date:[cl_ord_id]},"orders":{id:{...}},"positions":{symbol:{...}}}`，缺文件返初始 dict）
  - `save_state(state) -> None`（tmp+rename 原子写）
  - `is_blocked() -> bool`（RISK_BLOCK.flag 存在）
  - `read_cap() -> float`（CAP.txt，缺失/非法 → 1.0 并记 audit WARN——注意：**非法值不拦**与本地 `state_store.py:2016` 「非法视同 0 全跳」不同，试点选本地同口径：非法 → 0.0 全拦，缺文件 → 1.0；两分支都写清注释）
  - `check_caps(sod_state, equity, positions_mv, open_buy_amount, price, qty, today) -> tuple[bool, str]`（CAP 逐单 + 单日 ≤2 + 单票 ≤5% 三闸合一，False 时返回拦截原因）

- [ ] **Step 1: 写失败测试**（要点用例）：
  - `test_save_state_atomic`：monkeypatch `os.replace` 抛错 → 原 state.pkl 未被破坏（旧内容仍在），tmp 文件不残留（或残留无害）；
  - `test_load_state_missing_returns_init`；
  - `test_block_flag_blocks_via_check_gates`（block 与额度无关——block 在编排层拦「扫描后挂单前」，本测试只验证 `is_blocked` 语义）；
  - `test_cap_quota_deduction`：equity=1_000_000, CAP=0.5, 持仓 200_000, 已挂 100_000 → 新单 150_000 被拒（超 200_000 余量）、100_000 通过；
  - `test_cap_fail_closed`：equity 查询失败（传 None）→ 拒；
  - `test_daily_order_cap`：当日 placed 已 2 → 拒；
  - `test_symbol_cap`：单票市值超 5% equity → 拒。
- [ ] **Step 2: 跑测试确认失败**。
- [ ] **Step 3: pilot_body.py 实现**（stdlib only；中文像素注释；写明与 `pre_open.py:540`/`state_store.py:1470` 同式）→ 重跑 build → 测试绿。
- [ ] **Step 4: Commit** `feat(emquant): 状态层原子写+ADR-16 双值模拟+试点三硬闸`

---

### Task 6: §2 数据层 + 符号映射 + 交易日历（TDD）

**Files:**
- Modify: `emquant/pilot_body.py`
- Test: `tests/emquant/test_data_layer.py`（FakeGm 数据函数放 `tests/emquant/fake_gm.py`，本任务先建该文件的 history/current 部分）

**Interfaces:**
- Consumes: Task 2 权威 API 名（`history` 签名/列名）。
- Produces:
  - `to_gm_symbol(ts: str) -> str` / `from_gm_symbol(g: str) -> str`（600000.SH↔SHSE.600000；000001.SZ↔SZSE.000001）
  - `fetch_df_upto(api, ts_symbol: str, end_date: str) -> pd.DataFrame | None`（frequency='1d'，end_time=end_date，adjust=ADJUST_PREV，adjust_end_time=end_date，列→open/high/low/close/volume，index=DatetimeIndex(eob)，按需分段防 33000 上限；异常返 None 记 audit）
  - `build_calendar(api, end_date: str, lookback_days: int = 500) -> list[str]`（拉 SHSE.000300 日线 eob 序列 → 升序 YYYY-MM-DD 列表）
  - `trading_days_between(cal: list[str], start: str, end: str) -> int`（**(start, end] 口径**——与 `compute/stop.py:22` 逐字同语义：不含 start 含 end；start 缺失/不可解析 → 0）

- [ ] **Step 1: 写失败测试**：FakeGm.history 返回合成 df（列名按 Task 2 权威）→ `fetch_df_upto` 形状/列/index/截断正确；符号映射往返；`trading_days_between` 用固定日历验证 (start,end] 语义与缺失容错（对齐 `stop.py` 边界：ed<=sd→0、解析失败→0）。
- [ ] **Step 2: 失败确认 → Step 3: 实现（gm 调用经 `_api()`，本任务同时建立 `_GM=None; def _api(): ...` seam，C4）→ build → 绿**。
- [ ] **Step 4: Commit** `feat(emquant): 数据层——定点前复权取数+符号映射+(start,end] 交易日历`

---

### Task 7: §5 订单工具 + compute_stop_price 逐句移植 + 生命周期判定（TDD，FakeGm 完全体）

**Files:**
- Modify: `emquant/pilot_body.py`
- Test: `tests/emquant/test_order_lifecycle.py`；`tests/emquant/fake_gm.py` 扩全（order_volume/order_cancel/get_orders/持仓/资金/证券信息）

**Interfaces:**
- Consumes: Task 5 状态/闸、Task 6 日历、Task 2 权威订单/持仓 API 名。
- Produces:
  - `compute_stop_price(...)`：**从 `strategies/neckline/execution.py` 逐句移植**（先读真身，签名/分支/边界逐字对齐；注释标明源 file:line）
  - `place_limit_buy(api, ts, price, qty, account) -> cl_ord_id|None`；`cancel(api, cl_ord_id)`；`sell_limit(api, ts, price, qty, account)`
  - `limit_down_price(prev_close, symbol) -> float`（档位：主板 ±10%、创板科创 ±20%（池子已滤但保留口径）、ST 5%——快照池已剔创板；按 `round(prev_close×(1-限), 2)` 二位取整）
  - `decide_pending(tick_price, order, today, cal) -> str|None`：`cancel_on`（tick≥order["cancel_on"]）或 `max_wait`（trading_days_between(formed_at,today) > max_wait）→ 撤单原因；否则 None
  - `decide_position(tick_price, pos, today, cal) -> tuple[str,int,str]|None`：返回 `("sell", qty, reason)`，reason ∈ {stop_loss, tp1, tp2}；tp1 只触发一次（pos["tp1_done"]）；qty=tp1 档 `floor(remaining×tp1_portion/100)×100`（不足 100 股则本档卖全部剩余的逻辑与注释对齐本地）、stop/tp2 卖 remaining 全量
  - `absorb_reality(state, api_orders, api_positions)`：幂等三查的「以柜台实况修 state」纯逻辑（柜台有 state 无 → 吸收；state 有柜台无且非终态 → 视为已撤/未挂）

- [ ] **Step 1: 写失败测试**（含 golden）：
  - `test_compute_stop_price_golden`：网格用例（holding_days×grace/step/floor 组合）与 `from strategies.neckline.execution import compute_stop_price as repo_csp` 输出逐位相等（tests 在 .venv310 有仓库依赖）；
  - `test_decide_pending_cancel_on` / `test_decide_pending_max_wait_strict_gt`（formed_at 起、恰好 ==max_wait 不撤、>才撤）；
  - `test_decide_position_stop_tp1_tp2`（触价、portion 一次、剩余全量、tp1 后 stop 用同 base）；
  - `test_partial_fill_to_position`（撤单后已成交部分转 positions）；
  - `test_limit_down_price_rounding`。
- [ ] **Step 2: 失败确认 → Step 3: 实现（订单族经 `_api()`；FakeGm 提供 cl_ord_id 自增、订单簿、成交回调模拟）→ build → 绿**。
- [ ] **Step 4: Commit** `feat(emquant): 订单工具+trailing 逐句移植+生命周期判定（golden 对拍真身）`

---

### Task 8: §6 事件编排（盘前五阶段/巡检/盘后，TDD）

**Files:**
- Modify: `emquant/pilot_body.py`
- Test: `tests/emquant/test_events_orchestration.py`

**Interfaces:**
- Consumes: 全部前序。
- Produces（gm 事件入口 + 编排核心，**单文件顶层**）：
  - `class PilotRuntime`：`__init__(api, workdir=None)`；方法 `pre_open(context)`（五阶段，见下）、`on_tick(context, tick)`、`after_close(context)`、`reconcile(context)`（幂等三查入口）
  - 模块级 `init(context)` / `pre_open_job(context)` / `after_close_job(context)` / `on_tick(context, tick)`（gm 回调名以 Task 2 为准）；`RT: PilotRuntime|None`
  - `run_pilot()`：C1 守卫（mode 强制仿真 + `PILOT_ALLOW_LIVE` 检查）→ `run(...)`（参数以 Task 2 为准）
- **pre_open 五阶段（顺序红线）**：①`get_orders` 撤全部非终态买（audit 逐单）②超期平仓：positions 按 `trading_days_between(entry_date, T-1) > max_holding`（**T-1 基准日**，C9；T-1=cal 中今日前一根）挂 `limit_down_price` 卖 ③扫描：UNIVERSE 逐 `fetch_df_upto(end=T-1)` → `detect_signal`（当日信号落 audit；扫描完成置 scan_done）④闸序：`is_blocked()` → 跳过挂单段；否则逐单 `check_caps` ⑤挂限价买 `entry=neckline+buy_limit_atr_mult×ATR`、`qty=⌊equity×pos_cap/entry/100⌋×100`（equity/cash 以 Task 2 权威函数查，查询异常 fail-closed 当日不挂）；全部动作写 state+audit。
- **on_tick**：pending → `decide_pending` → 撤；positions → `decide_position` → 卖；每笔 audit。tick 字段名以 Task 2 为准。
- **after_close**：audit 收尾行 + `clear 动态订阅` + state 落盘。
- **init**：读 runtime.json（token/strategy_id/account——缺 token 时直接 raise 提示按 README 填写）、`reconcile`（幂等三查）、订阅当日巡检标的（持仓∪挂单）。

- [ ] **Step 1: 写失败测试**（FakeGm + 临时 workdir + monkeypatch `pilot._GM`）：
  - `test_pre_open_cancel_then_place_order`（昨日买单被撤、新信号按 entry 公式挂出、≤2/日闸生效）；
  - `test_pre_open_blocked_flag_skips_new_keeps_mgmt`（block 时撤单/超期照跑、不挂新）；
  - `test_expired_close_uses_t1_basis`（entry 距 T-1 恰 == max_holding 不平、> 才平，跌停价单）；
  - `test_scan_writes_audit_and_state`（信号行字段 symbol/neckline/entry_price/rr）；
  - `test_on_tick_stop_sells_all`、`test_reconcile_absorbs_counter_orders`；
  - `test_run_pilot_refuses_non_simulation`（C1：无 PILOT_ALLOW_LIVE → raise）。
- [ ] **Step 2: 失败确认 → Step 3: 实现 → build → 绿（含 Task 4-7 全部测试回归）**。
- [ ] **Step 4: Commit** `feat(emquant): 事件编排——盘前五阶段红线序+tick 巡检+盘后审计`

---

### Task 9: 最终组装 + 双环境全量验证

**Files:**
- Modify: `emquant/pilot_body.py`（收尾：`if __name__ == "__main__": run_pilot()`）
- Create: `emquant/config/runtime.json.example`

**Interfaces:**
- Produces: 可交付单文件（AC1-5 全验）。

- [ ] **Step 1: build 最终产物**；`runtime.json.example`：

```json
{"token": "<终端右上角-系统管理-token>", "strategy_id": "<终端-我的策略-新建后填策略ID>", "account_id": "e7cb55d6-04ab-4ea9-98fc-503d9f97d2a1"}
```

- [ ] **Step 2: 双环境全量**：`.venv310` 与 `.venv_emquant` 各跑 `pytest tests/emquant -q` 全绿；`.venv310 -c "import emquant.emquant_neckline_pilot"`（或 spec_from_file）无 gm import 成功；`python -m py_compile emquant/emquant_neckline_pilot.py`。
- [ ] **Step 3: 引擎零改动核验**：`git diff --stat master...HEAD -- strategies/ trading/ broker/ tests/`（除 tests/emquant 新增外无改动）；`git status` 无生产文件脏。
- [ ] **Step 4: Commit** `feat(emquant): 最终单文件组装+双环境验证通过`

---

### Task 10: W0' 夜间实测——token 发现 + 数据对拍（尽力，失败降级 runbook）

**Files:**
- Create: `emquant/tools/gm_data_pull.py`、`emquant/tools/compare_data.py`
- Create(若跑通): `docs/research/2026-08-21-emquant-data-parity-report.md`

**Interfaces:**
- Consumes: Task 2 权威 API、Task 6 数据层、`runtime.json`（token）。
- Produces: 对拍报告（|Δclose| 相对容差 1e-6，不一致标的 → 排除池列表 + 原因猜测）。

- [ ] **Step 1: token 发现**：搜 `%APPDATA%\Eastmoney*`、`%APPDATA%\gm*`、`F:\dcjj\` 下 json/ini 含 `"token"` 的配置（只读取值写入 runtime.json，**不打印**，C8）。找到 → 写 `emquant/config/runtime.json`；找不到 → 跳到 Step 4 降级。
- [ ] **Step 2: 取数脚本** `gm_data_pull.py`：token 设置（Task 2 权威 set_token 类调用）→ UNIVERSE × 近 `2×window+20` 日 qfq 日线（`adjust_end_time=T-1` 定点）→ 存 `emquant/state/parity_gm_YYYYMMDD.parquet`。夜间数据服务可用性未知——异常即降级。
- [ ] **Step 3: 对拍** `compare_data.py`：data_lake `a_shares_daily.parquet` 同窗同标的 qfq → 逐列比对 close/high/low（1e-6）→ 报告 + 排除池写 `emquant/state/parity_exclude_YYYYMMDD.json`（排除池语义：W2 一致率分母豁免，设计 §7）。
- [ ] **Step 4: 降级路径（token 不可得或夜间服务不可用）**：把 Step 2/3 的执行步骤写进 README 晨检清单（Task 11），并在此任务提交信息注明「夜间实测未完成的原因」。
- [ ] **Step 5: Commit** `feat(emquant): 数据对拍工具链（+夜间实测报告或降级记录）`

---

### Task 11: README runbook + 收尾验证（verification-before-completion）

**Files:**
- Create: `emquant/README.md`

- [ ] **Step 1: 写 runbook**（全中文、无占位）：①终端挂载步骤（我的策略→新建 Python 策略→用 `emquant_neckline_pilot.py` 内容/导入→策略设置绑定仿真账户 `e7cb55d6-...`→填 runtime.json token/strategy_id→运行）；②晨检清单（09:15 前看终端策略运行态、audit CSV 首行、09:15 后核对挂单 ≤2；token 失效症状与更新法）；③回退 SOP 三步（touch RISK_BLOCK.flag → 终端手动撤单 → 清仓仿真腿）；④数据对拍晨间补跑命令（Task 10 降级时）；⑤与本地腿双轨比对方法（audit 信号行 vs 本地 SIGNAL.meta）；⑥已知限制（trailing 退化为固定止损=本地现状；cooldown 结论引用 params_snapshot notes）。
- [ ] **Step 2: 收尾验证（全部要有输出证据）**：双环境 pytest 全绿输出；`py_compile`；分支提交历史 `git log --oneline master..HEAD`；`git status` 干净（除用户会话前已存在的无关改动）；确认未 push。
- [ ] **Step 3: Commit** `docs(emquant): 试点 runbook——挂载/晨检/回退/对拍/双轨比对`

---

## Self-Review 记录

- **Spec 覆盖**：FR1→Task3；FR2/FR3→Task5；FR4→Task6；FR5→Task7；FR6→Task8；FR7→Task5/8（audit 写入点）；FR8→Task4/9；FR9→Task10；AC1-7→Task9 Step2/3 + Task11 Step2；C1→Task8 run_pilot 守卫+测试；C2→Task4；C5→Task1；C6→各任务 commit；C8→Task10 Step1。
- **占位符扫描**：无 TBD/「适当处理」；gm API 名的不确定性由「Task 2 权威规则」显式承接（这是事实依赖，非占位）。
- **类型一致性**：`check_caps`/`fetch_df_upto`/`trading_days_between(cal,...)`/`decide_pending`/`decide_position`/`absorb_reality`/`PilotRuntime.pre_open·on_tick·after_close` 在 Task5-8 间签名一致；`cl_ord_id` 为订单主键贯穿。

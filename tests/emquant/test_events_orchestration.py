# -*- coding: utf-8 -*-
"""§6 事件编排（组装产物口径）：盘前五阶段红线序 / tick 巡检 / 盘后收尾 / C1 守卫 / 入口抑制。

物理定位：
    被测对象是【组装产物】emquant/emquant_neckline_pilot.py 的 §6 段——改源
    pilot_body.py 后必须先重跑 emquant/build_pilot.py 再跑本文件（与 test_state_
    and_gates / test_order_lifecycle 同范式）。import 范式同款：先注册 sys.modules
    再 exec_module——§1 内核 Signal 是 dataclass + PEP563 字符串注解，不注册则
    exec 当场 AttributeError(NoneType)。

隔离纪律：
    PilotRuntime(workdir=tmp_path) 把 state/audit/config 三目录整体重定向到 tmp
    （§3/§7 的显式 path 参数 + §6 的 workdir 根规则）；日期/日历/识别内核/universe
    经 monkeypatch 模块属性（_today_str/build_calendar/detect_signal/UNIVERSE）钉死
    到固定夹具——编排测试考【时序与降级】，识别数值等价是 Task 4 的职责（职责分离，
    同 FakeGm 头注纪律）。识别内核一律 stub 成确定性 Signal（neckline=10/bottom=8/
    atr=0.8 → entry=10.4、cancel_on=14.0 可手算）。

口径锚点（C9 红线——断言期望值全部由此推出，非拍脑袋）：
    - 五阶段顺序（brief 红线）：①撤非终态买（get_orders 柜台实况）→②超期平仓
      （entry 距 T-1 > max_holding 严格大于才平；T-1=cal 中今日前一根）→③扫描
      （fetch_df_upto(end=T-1) → detect_signal → cooldown 去重 → SIGNAL 落 audit）→
      ④is_blocked 跳挂单段（存量管理①②照跑）→⑤挂限价买（entry=Signal.entry_price
      =颈线+0.5×ATR；qty=⌊equity×pos_cap/entry/100⌋×100，pos_cap 取 TRADE_CFG 0.05）；
    - cooldown 去重（engine.py:1036-1050 的 pilot 复刻）：state.last_signal 锚点
      map，trading_days_between(cal, last, today) < cooldown(=8) 丢弃；
    - tp1 已知分歧（Task 7 评审指令）：reason=tp1 且 qty==remaining（不足一手清
      剩余）→ audit detail 加 known_divergence=tp1_dust_clears（双轨复盘剔除用）；
    - 空历降级（Task 6→8 必记）：build_calendar→[] → audit calendar_missing，
      ①照跑、②③停判（挂单依赖扫描产物自然为零）、不炸不静默；
    - audit 写失败（Task 5 minor ③ 对账）：观测通道损失不拦交易（print 降级）；
    - C1（08-21 修订版）：run_pilot 固定 MODE_LIVE + 账户白名单 PILOT_ACCOUNT_ID，
      env PILOT_ALLOW_LIVE=I_KNOW_REAL_MONEY 是唯一逃生门；
    - 订阅三时点（Task 8 评审 R1 C-1）：bootstrap 崩溃重启 / pre_open ⓪后跨日重建
      （after_close 清空后常驻进程 day 2+ 不断供）/ ⑤后同日增补（当日新挂即入巡检
      面）——增量差集下发，gm subscribe 幂等性不赌 SDK、去重后调用；
    - ① 昨日判据（R1 I-2）：柜台单 created_at 日期==today 跳过（state 滞后时柜台
      是唯一真值源；缺失退 state 侧 date）——同日重跑不自杀当日进场（scan_done
      防重扫=撤了不补，进场静默丢失）；
    - equity fail-closed（R1 M-3 编排接线）：get_cash 抛错/空表 → equity=None →
      有信号 0 挂单 + ORDER_BLOCKED 留 fail-closed 原因；
    - 对账失败退避（R1 M-4）：reconcile 查询失败后 60s 窗内 on_tick 不重试；
    - ② 后防御性落盘（R1 M-5）：超期卖上柜台后 state 先落——③④⑤ 中途异常不吃
      「柜台有单 state 无单」的双卖窗口；
    - 入口抑制（Task 4 遗留）：§1 内核逐字块尾部 `if __name__ == "__main__": main()`
      在产物第 ~804 行、先于 §2-§7 拼接位执行——抑制块必须被组装器剪出到 head 区
      （§0 之前）且全产物唯一一份（复制会让第二次 _IS_MAIN 赋值把入口哑火）。
"""
from __future__ import annotations

import csv
import importlib.util
import json
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

from tests.emquant.fake_gm import ORDER_STATUS, FakeGm

ROOT = Path(__file__).resolve().parents[2]
ARTIFACT = ROOT / "emquant" / "emquant_neckline_pilot.py"

# 固定交易日夹具（bdate_range 确定性：周末剔除；2026-06-01..2026-08-21）——超期 T-1
# 基准、cooldown 日数差、on_tick 的 max_wait 全部从这份历手算推出。
CAL = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-06-01", "2026-08-21")]
TODAY = "2026-08-21"
T_MINUS_1 = "2026-08-20"

# Signal.exec_params 定终身快照（= §0 快照 ID/EXEC 的实弹口径抄录：window=80 实验
# 25c602 的 13 键全集里编排消费的子集——stop/tp 乘数来自 ID_PARAMS，其余来自 EXEC_PARAMS）
EXEC_SNAP = {"stop_atr_mult": 1.0, "tp_h_mult": 2.5, "tp1_h_mult": 1.0,
             "tp1_portion": 0.3, "max_wait": 8, "cancel_thresh_mult": 2.0,
             "max_holding": 20, "trailing_grace": 0, "trailing_step": 0.0,
             "trailing_floor": 0.0}


def _import_artifact():
    """按文件位置 exec 组装产物为全新模块（范式与理由见模块 docstring）。"""
    spec = importlib.util.spec_from_file_location("emquant_pilot_events", ARTIFACT)
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m          # 先注册再 exec：dataclass 注解回查依赖
    spec.loader.exec_module(m)
    return m


@pytest.fixture()
def pilot():
    """每用例全新 exec 一次产物——上一用例对模块常量的 monkeypatch 零残留。"""
    return _import_artifact()


class _Ctx:
    """gm context 占位（编排层不读其字段——日期口径走 _today_str seam，见 §6 头注）。"""


# ============================================================================
# 夹具：确定性 Signal stub / 日期日历钉死 / runtime 构造
# ============================================================================
def _signal(pilot, symbol="300750.SZ", formed=T_MINUS_1, neckline=10.0, bottom=8.0, atr=0.8):
    """确定性 Signal（neckline 10 / bottom 8 / atr 0.8 → entry=10+0.5×0.8=10.4）。

    entry_price 手算锚：detect_signal 装配式 neckline+buy_limit_atr_mult×atr（§1
    _post_detect 同式）；rr=3.0 为注入常量（识别数值等价归 Task 4，此处只需非 None）。
    """
    return pilot.Signal(symbol=symbol, formed_at=pd.Timestamp(formed),
                        breakout_date=pd.Timestamp(formed), neckline=neckline,
                        bottom=bottom, atr=atr,
                        entry_price=neckline + 0.5 * atr, rr=3.0,
                        exec_params=dict(EXEC_SNAP))


def _detect_map(mapping):
    """detect_signal stub 工厂：{symbol: Signal}——编排测试不考识别、只考时序。"""
    def _detect(symbol, df_upto, id_cfg, exec_cfg, date, atr_full=None):
        return mapping.get(symbol)
    return _detect


def _pin(pilot, monkeypatch, tmp_path, universe=(), detect=None, cal=None):
    """编排夹具钉死：today/日历/UNIVERSE/识别内核 + 数据层 WARN 的 audit 落 tmp。

    build_calendar stub 的存在同时保住了「日历唯一来源是 build_calendar」的 seam——
    空历降级用例传 cal=[] 即触发（真实路径是 gm 拉指数失败返 []，Task 6 已测）。
    """
    monkeypatch.setattr(pilot, "_today_str", lambda: TODAY)
    monkeypatch.setattr(pilot, "build_calendar",
                        lambda api, end_date, lookback_days=500: (CAL if cal is None else cal))
    monkeypatch.setattr(pilot, "UNIVERSE", list(universe))
    if detect is not None:
        monkeypatch.setattr(pilot, "detect_signal", detect)
    # 数据层 _audit_warn（fetch/日历/跌停 WARN）走模块缺省 AUDIT_DIR——指向 tmp 防仓库污染
    monkeypatch.setattr(pilot, "AUDIT_DIR", tmp_path / "dl_audit")


def _rt(pilot, fake, tmp_path, state=None):
    """构造被测运行时（api 直注 FakeGm 离线路径；state 显式替换=构造指定盘初态）。"""
    rt = pilot.PilotRuntime(fake, workdir=tmp_path)
    if state is not None:
        rt.state = state
    return rt


def _audit_rows(tmp_path):
    """读 workdir 侧当日 audit CSV（§6 全部行经 _audit → audit_dir=workdir/audit）。"""
    f = tmp_path / "audit" / f"audit_{date.today():%Y%m%d}.csv"
    if not f.exists():
        return []
    return list(csv.reader(f.read_text(encoding="utf-8").splitlines()))


def _events(tmp_path):
    return [r[1] for r in _audit_rows(tmp_path)]


def _details(tmp_path, event):
    return [json.loads(r[2]) for r in _audit_rows(tmp_path) if r[1] == event]


def _pos_state(pilot, entry_date, remaining=150):
    """schema v1 持仓记录（exec_params 空 → 超期判走 EXEC_PARAMS 缺省 20）。"""
    return {"entry_date": entry_date, "entry_price": 10.0, "qty": remaining,
            "remaining_qty": remaining, "stop": None, "tp1_price": None,
            "tp1_done": False, "tp2_price": None, "trailing": {}, "exec_params": {}}


def _back_counter_position(fake, gm_sym, volume, vwap):
    """柜台持仓注入（on_tick 前置对账会把 state 无柜台背书的持仓归零——absorb ③ 语义，
    tick 用例必须给 state 持仓配柜台真值）。"""
    fake.positions[gm_sym] = {"account_id": "fake_account", "symbol": gm_sym, "side": 1,
                              "volume": volume, "volume_today": volume, "vwap": vwap,
                              "amount": volume * vwap, "available": volume,
                              "available_now": volume, "market_value": volume * vwap}


def _backdate_order(fake, cid, day):
    """柜台单 created_at 显式定值（I-2 守卫以柜台日判「昨日单」——替身落单用真实
    时钟，显式定值后用例期望不随真实运行日期漂移：真实日期恰与夹具 TODAY 同日时，
    未定值的「昨日单」会被误判成当日单）。"""
    fake.orders[cid]["created_at"] = datetime.fromisoformat(f"{day}T09:31:00")


# ============================================================================
# pre_open 五阶段（红线序）
# ============================================================================
def test_pre_open_cancel_then_place_order(pilot, tmp_path, monkeypatch):
    """①昨日买单被撤 + ⑤新信号按 entry 公式挂出 + ③单日 ≤2 闸（三阶段一链钉死）。

    手算锚：equity=FakeGm nav 1,000,000；entry=10.4 → qty=⌊1M×0.05/10.4/100⌋×100=4800
    （金额 49,920 ≤ 单票 5%×1M=50,000 恰好放行）；cancel_on=颈线+2.0×H=10+2×2=14.0。
    """
    fake = FakeGm()
    yid = pilot.place_limit_buy(fake, "300750.SZ", 10.0, 100, "acc-y")   # 昨日挂单（在场柜台）
    _backdate_order(fake, yid, T_MINUS_1)                   # created_at 回拨昨日（I-2 判据）
    sigs = {s: _signal(pilot, s) for s in ("300750.SZ", "688981.SH", "300059.SZ")}
    _pin(pilot, monkeypatch, tmp_path, universe=tuple(sigs), detect=_detect_map(sigs))
    rt = _rt(pilot, fake, tmp_path)
    rt.pre_open(_Ctx())

    # ① 昨日非终态买单被撤（audit 逐单留痕）
    assert fake.orders[yid]["status"] == ORDER_STATUS["Canceled"]
    assert "CANCEL" in _events(tmp_path)
    # ⑤ 三个信号只挂两单（试点硬闸 FR3：单日新挂 ≤2）
    buys = [c for c in fake.calls if c.get("api") == "order_volume"
            and c["side"] == 1 and c["price"] == pytest.approx(10.4)]
    assert len(buys) == 2 and all(c["volume"] == 4800 for c in buys)
    blocked = _details(tmp_path, "ORDER_BLOCKED")
    assert len(blocked) == 1 and "单日" in blocked[0]["reason"]
    assert blocked[0]["symbol"] == "300059.SZ"               # 第三单按扫描序被拦
    # SIGNAL 行字段（brief：symbol/neckline/entry_price/rr）
    sig_rows = _details(tmp_path, "SIGNAL")
    assert len(sig_rows) == 3
    assert sig_rows[0]["neckline"] == pytest.approx(10.0)
    assert sig_rows[0]["entry_price"] == pytest.approx(10.4)
    assert sig_rows[0]["rr"] == pytest.approx(3.0)
    # state 落盘：scan_done/placed/orders（cancel_on/formed_at/exec_params 定终身）
    st = pilot.load_state(path=tmp_path / "state" / "state.pkl")
    assert st["scan_done"] == {TODAY}
    assert len(st["placed"][TODAY]) == 2
    for cid in st["placed"][TODAY]:
        o = st["orders"][cid]
        assert o["purpose"] == "OPEN" and o["qty"] == 4800
        assert o["price"] == pytest.approx(10.4)
        assert o["cancel_on"] == pytest.approx(14.0)          # 颈线+cancel_thresh_mult×H
        assert o["formed_at"] == T_MINUS_1
        assert o["exec_params"]["max_wait"] == 8
    assert st["last_signal"]["300750.SZ"] == T_MINUS_1        # cooldown 锚点更新


def test_pre_open_blocked_flag_skips_new_keeps_mgmt(pilot, tmp_path, monkeypatch):
    """RISK_BLOCK.flag：①撤单/②超期平仓照跑、⑤不挂新（block 只拦增量，ADR-16）。"""
    fake = FakeGm(symbol_info={"SHSE.688981": {"pre_close": 10.0, "lower_limit": 7.77,
                                               "upper_limit": 12.0}})
    yid = pilot.place_limit_buy(fake, "300750.SZ", 10.0, 100, "acc-y")
    _backdate_order(fake, yid, T_MINUS_1)                   # created_at 回拨昨日（I-2 判据）
    _back_counter_position(fake, "SHSE.688981", 150, 10.0)    # 持仓配柜台背书（absorb ③ 语义）
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state" / "RISK_BLOCK.flag").write_text("", encoding="utf-8")   # 人工 touch
    i_t1 = CAL.index(T_MINUS_1)
    st = pilot._initial_state()
    st["positions"]["688981.SH"] = _pos_state(pilot, CAL[i_t1 - 21])            # 21 > 20 超期
    sigs = {"300750.SZ": _signal(pilot)}
    _pin(pilot, monkeypatch, tmp_path, universe=("300750.SZ",), detect=_detect_map(sigs))
    rt = _rt(pilot, fake, tmp_path, state=st)
    rt.pre_open(_Ctx())

    assert fake.orders[yid]["status"] == ORDER_STATUS["Canceled"]     # ① 照跑
    sells = [c for c in fake.calls if c.get("api") == "order_volume" and c["side"] == 2]
    assert len(sells) == 1 and sells[0]["symbol"] == "SHSE.688981"    # ② 照跑（gm 符号换装）
    assert sells[0]["price"] == pytest.approx(7.77)                   # API 跌停值优先
    new_buys = [c for c in fake.calls if c.get("api") == "order_volume"
                and c["side"] == 1 and c["price"] == pytest.approx(10.4)]
    assert new_buys == []                                             # ⑤ 不挂新
    assert "BLOCK_SKIP" in _events(tmp_path)
    assert len(_details(tmp_path, "SIGNAL")) == 1                     # ③ 扫描照常留痕


def test_expired_close_uses_t1_basis(pilot, tmp_path, monkeypatch):
    """超期平仓 T-1 基准（C9）：entry 距 T-1 恰 == max_holding(20) 不平、> 才平；跌停价单。"""
    fake = FakeGm(symbol_info={"SHSE.688981": {"pre_close": 10.0, "lower_limit": 7.77,
                                               "upper_limit": 12.0}})
    _back_counter_position(fake, "SZSE.300750", 300, 10.0)   # 持仓配柜台背书（absorb ③ 语义）
    _back_counter_position(fake, "SHSE.688981", 150, 10.0)
    i_t1 = CAL.index(T_MINUS_1)
    st = pilot._initial_state()
    st["positions"]["300750.SZ"] = _pos_state(pilot, CAL[i_t1 - 20], remaining=300)  # 恰等 20：不平
    st["positions"]["688981.SH"] = _pos_state(pilot, CAL[i_t1 - 21], remaining=150)  # 21>20：平
    _pin(pilot, monkeypatch, tmp_path, universe=())                     # 无信号干扰
    rt = _rt(pilot, fake, tmp_path, state=st)
    rt.pre_open(_Ctx())

    sells = [c for c in fake.calls if c.get("api") == "order_volume" and c["side"] == 2]
    assert len(sells) == 1
    assert sells[0]["symbol"] == "SHSE.688981" and sells[0]["volume"] == 150
    assert sells[0]["price"] == pytest.approx(7.77)
    exp = _details(tmp_path, "EXPIRE_SELL")[0]
    assert exp["holding_days"] == 21 and exp["max_holding"] == 20       # 审计留判定依据


def test_scan_writes_audit_and_state(pilot, tmp_path, monkeypatch):
    """扫描幂等：SIGNAL 行字段齐 + scan_done/last_signal 落盘 + 同日重跑不重扫。"""
    fake = FakeGm()
    sigs = {"300750.SZ": _signal(pilot)}
    _pin(pilot, monkeypatch, tmp_path, universe=("300750.SZ",), detect=_detect_map(sigs))
    rt = _rt(pilot, fake, tmp_path)
    rt.pre_open(_Ctx())
    rt.pre_open(_Ctx())                                                # 同日重触发（schedule 重复/人工补跑）

    assert len(_details(tmp_path, "SIGNAL")) == 1                      # scan_done 防重扫
    st = pilot.load_state(path=tmp_path / "state" / "state.pkl")
    assert st["last_signal"] == {"300750.SZ": T_MINUS_1}
    assert st["scan_done"] == {TODAY}


def test_scan_cooldown_dedup(pilot, tmp_path, monkeypatch):
    """cooldown 跨日去重（engine.py:1036-1050 复刻）：(last, today] < 8 丢、== 8 放。"""
    i_t = CAL.index(TODAY)
    last_recent, last_edge = CAL[i_t - 7], CAL[i_t - 8]   # 7 < 8 丢 / 8 == 8 放
    sigs = {"300750.SZ": _signal(pilot)}

    # 近窗：去重丢弃 + 锚点不更新
    fake_a = FakeGm()
    _pin(pilot, monkeypatch, tmp_path / "a", universe=("300750.SZ",), detect=_detect_map(sigs))
    st = pilot._initial_state()
    st["last_signal"] = {"300750.SZ": last_recent}
    rt_a = _rt(pilot, fake_a, tmp_path / "a", state=st)
    rt_a.pre_open(_Ctx())
    assert [c for c in fake_a.calls if c.get("api") == "order_volume"] == []
    skip = _details(tmp_path / "a", "SIGNAL_COOLDOWN_SKIP")
    assert len(skip) == 1 and skip[0]["last_signal"] == last_recent
    assert rt_a.state["last_signal"]["300750.SZ"] == last_recent       # 锚点不被丢弃信号刷新

    # 恰满 cooldown 窗：放行挂单
    fake_b = FakeGm()
    _pin(pilot, monkeypatch, tmp_path / "b", universe=("300750.SZ",), detect=_detect_map(sigs))
    st_b = pilot._initial_state()
    st_b["last_signal"] = {"300750.SZ": last_edge}
    rt_b = _rt(pilot, fake_b, tmp_path / "b", state=st_b)
    rt_b.pre_open(_Ctx())
    buys = [c for c in fake_b.calls if c.get("api") == "order_volume" and c["side"] == 1]
    assert len(buys) == 1


def test_scan_symbol_exception_contained(pilot, tmp_path, monkeypatch):
    """单标的挡板（engine.py:1034 同款）：识别抛错只损失该标的，其余信号照挂、不炸环。"""
    fake = FakeGm()
    good = _signal(pilot, "688981.SH")

    def _detect(symbol, df_upto, id_cfg, exec_cfg, date, atr_full=None):
        if symbol == "300750.SZ":
            raise RuntimeError("模拟识别内核对脏数据炸裂")
        return good if symbol == "688981.SH" else None

    _pin(pilot, monkeypatch, tmp_path, universe=("300750.SZ", "688981.SH"), detect=_detect)
    rt = _rt(pilot, fake, tmp_path)
    rt.pre_open(_Ctx())

    buys = [c for c in fake.calls if c.get("api") == "order_volume" and c["side"] == 1]
    assert len(buys) == 1 and buys[0]["symbol"] == "SHSE.688981"    # 好标的照挂
    warns = _details(tmp_path, "WARN")
    assert any(w.get("type") == "scan_fail" and w.get("symbol") == "300750.SZ" for w in warns)
    st = pilot.load_state(path=tmp_path / "state" / "state.pkl")
    assert st["scan_done"] == {TODAY} and st["last_signal"] == {"688981.SH": T_MINUS_1}


def test_calendar_missing_degrades(pilot, tmp_path, monkeypatch):
    """空历显式降级：①照跑、②超期停判、③扫描停（audit calendar_missing），不炸不静默。"""
    fake = FakeGm()
    yid = pilot.place_limit_buy(fake, "300750.SZ", 10.0, 100, "acc-y")
    _backdate_order(fake, yid, T_MINUS_1)                   # created_at 回拨昨日（I-2 判据）
    i_t1 = CAL.index(T_MINUS_1)
    st = pilot._initial_state()
    st["positions"]["688981.SH"] = _pos_state(pilot, CAL[i_t1 - 30])   # 真超期，但无历不判
    sigs = {"300750.SZ": _signal(pilot)}
    _pin(pilot, monkeypatch, tmp_path, universe=("300750.SZ",),
         detect=_detect_map(sigs), cal=[])                             # build_calendar → []
    rt = _rt(pilot, fake, tmp_path, state=st)
    rt.pre_open(_Ctx())                                                # 不抛即过

    assert fake.orders[yid]["status"] == ORDER_STATUS["Canceled"]      # ① 无历依赖照跑
    assert [c for c in fake.calls if c.get("api") == "order_volume" and c["side"] == 2] == []  # ② 停
    assert not any(c.get("symbol") for c in fake.calls if "start_time" in c)  # ③ 无行情拉取（扫描停）
    warns = _details(tmp_path, "WARN")
    assert any(w.get("type") == "calendar_missing" for w in warns)


def test_audit_write_failure_does_not_block_trading(pilot, tmp_path, monkeypatch):
    """audit_log 写失败：print 降级 + 交易照常（观测通道损失不拦交易——Task 5 minor ③ 对账）。"""
    fake = FakeGm()
    sigs = {"300750.SZ": _signal(pilot)}

    def _boom(*args, **kwargs):
        raise RuntimeError("模拟磁盘满：audit 通道不可写")

    monkeypatch.setattr(pilot, "audit_log", _boom)
    _pin(pilot, monkeypatch, tmp_path, universe=("300750.SZ",), detect=_detect_map(sigs))
    rt = _rt(pilot, fake, tmp_path)
    rt.pre_open(_Ctx())                                                # 不炸（_audit 接住降级 print）
    buys = [c for c in fake.calls if c.get("api") == "order_volume"
            and c["side"] == 1 and c["price"] == pytest.approx(10.4)]
    assert len(buys) == 1
    st = pilot.load_state(path=tmp_path / "state" / "state.pkl")       # state 通道独立于 audit
    assert len(st["placed"][TODAY]) == 1


# ============================================================================
# Task 8 评审 R1：订阅三时点（C-1）/ 撤单昨日判据（I-2）/ equity 接线（M-3）
# / ②后防御落盘（M-5）——同波落在 pre_open 一段的四个修复面
# ============================================================================
def test_pre_open_same_day_new_orders_join_subscription(pilot, tmp_path, monkeypatch):
    """C-1 时点③：⑤ 挂出的当日新买单即入订阅面——cancel_on/成交后止损的 tick 巡检
    当日成立，不等次日 pre_open（否则当日新进场全无风控链供价）。"""
    fake = FakeGm()
    sigs = {"300750.SZ": _signal(pilot)}
    _pin(pilot, monkeypatch, tmp_path, universe=("300750.SZ",), detect=_detect_map(sigs))
    rt = _rt(pilot, fake, tmp_path)
    rt.pre_open(_Ctx())                                       # 盘初空仓：⓪' 重建为 no-op

    assert fake.subscriptions == {"SZSE.300750": "tick"}      # gm 侧订阅面（gm 符号）
    assert rt._subscribed == ["300750.SZ"]                    # 账本镜像（ts 口径）
    subs = [c for c in fake.calls if c.get("api") == "subscribe"]
    assert len(subs) == 1 and subs[0]["symbols"] == ["SZSE.300750"]   # 增量：单次下发


def test_pre_open_cross_day_rebuilds_subscription(pilot, tmp_path, monkeypatch):
    """C-1 时点②：after_close 清空订阅后，day-2 pre_open 按 ⓪ 对账后的最新 state 重建
    （= 新 positions ∪ 未终态 orders）——常驻进程 day 2+ 巡检不断供。"""
    fake = FakeGm()
    holder = {"today": TODAY}                                 # 可中途推进的日期锚
    sigs = {"300750.SZ": _signal(pilot)}
    _pin(pilot, monkeypatch, tmp_path, universe=("300750.SZ",), detect=_detect_map(sigs))
    monkeypatch.setattr(pilot, "_today_str", lambda: holder["today"])
    rt = _rt(pilot, fake, tmp_path)
    rt.pre_open(_Ctx())                                       # day1：⑤ 挂单 + ⑤' 增补订阅
    cid = rt.state["placed"][TODAY][0]
    _backdate_order(fake, cid, TODAY)                         # 钉 created_at（不随真实钟漂）
    rt.after_close(_Ctx())                                    # day1 盘后：订阅清空（生命周期终点）
    assert fake.subscriptions == {} and rt._subscribed == []

    holder["today"] = "2026-08-24"                            # day2（CAL 外的自然下一交易日）
    _back_counter_position(fake, "SZSE.300059", 150, 10.0)    # day2 新持仓（柜台背书）
    rt.state["positions"]["300059.SZ"] = _pos_state(pilot, T_MINUS_1)
    rt.pre_open(_Ctx())                                       # day2：⓪' 跨日重建

    # 订阅集 = 新 positions ∪ 未终态 orders（重建时点在 ⓪ 后 ① 前——昨日单此际在场）
    assert fake.subscriptions == {"SZSE.300750": "tick", "SZSE.300059": "tick"}
    assert rt._subscribed == ["300059.SZ", "300750.SZ"]
    assert fake.orders[cid]["status"] == ORDER_STATUS["Canceled"]   # ① 随后撤昨日单（I-2 侧证）


def test_pre_open_cancel_keeps_today_orders_only(pilot, tmp_path, monkeypatch):
    """I-2 昨日判据：①只撤昨日单——当日单不被自杀（scan_done 防重扫=撤了不补，当日
    进场静默丢失）；柜台 created_at 缺失时退回 state 侧 date 同守卫。"""
    fake = FakeGm()
    _pin(pilot, monkeypatch, tmp_path, universe=())           # 无信号：只考 ① 的守卫
    rt = _rt(pilot, fake, tmp_path)
    old_id = pilot.place_limit_buy(fake, "300750.SZ", 10.0, 100, "acc-y")
    _backdate_order(fake, old_id, T_MINUS_1)                  # 昨日单 → 该撤
    new_id = pilot.place_limit_buy(fake, "688981.SH", 10.0, 100, "acc-y")
    _backdate_order(fake, new_id, TODAY)                      # 当日单 → 不撤
    fb_id = pilot.place_limit_buy(fake, "300059.SZ", 10.0, 100, "acc-y")
    fake.orders[fb_id]["created_at"] = None                   # 柜台字段缺失 → 退 state 判据
    rt.state["orders"][fb_id] = {"symbol": "300059.SZ", "date": TODAY, "price": 10.0,
                                 "qty": 100, "purpose": "OPEN", "cancel_on": None,
                                 "formed_at": None, "exec_params": {}, "status": "SUBMITTED",
                                 "filled": 0, "account": "acc-y"}

    rt.pre_open(_Ctx())
    assert fake.orders[old_id]["status"] == ORDER_STATUS["Canceled"]   # 昨日单撤（红线①）
    assert fake.orders[new_id]["status"] == ORDER_STATUS["New"]        # 当日单留（柜台判据）
    assert fake.orders[fb_id]["status"] == ORDER_STATUS["New"]         # 当日单留（state 退回判据）
    cancels = _details(tmp_path, "CANCEL")
    assert len(cancels) == 1 and cancels[0]["cl_ord_id"] == old_id     # 全场只撤昨日单


def test_pre_open_equity_failure_fail_closed_no_orders(pilot, tmp_path, monkeypatch):
    """M-3 编排接线：get_cash 空表/抛错 → equity=None → 有信号但 0 挂单，
    ORDER_BLOCKED 逐单留 fail-closed 原因（Q1：空=故障，绝不拿 0 冒充真值）。"""
    sigs = {"300750.SZ": _signal(pilot), "688981.SH": _signal(pilot, "688981.SH")}

    fake_a = FakeGm()                                         # 场景一：空表（返 {}）
    _pin(pilot, monkeypatch, tmp_path / "a", universe=tuple(sigs), detect=_detect_map(sigs))
    rt_a = _rt(pilot, fake_a, tmp_path / "a")
    fake_a.cash = {}
    rt_a.pre_open(_Ctx())
    assert [c for c in fake_a.calls if c.get("api") == "order_volume"] == []
    blocked = _details(tmp_path / "a", "ORDER_BLOCKED")
    assert len(blocked) == 2 and all("fail-closed" in b["reason"] for b in blocked)
    assert any(w.get("type") == "get_cash_empty" for w in _details(tmp_path / "a", "WARN"))

    fake_b = FakeGm()                                         # 场景二：异常上抛
    _pin(pilot, monkeypatch, tmp_path / "b", universe=tuple(sigs), detect=_detect_map(sigs))
    rt_b = _rt(pilot, fake_b, tmp_path / "b")

    def _boom(account_id=None):
        raise RuntimeError('{"status": 1100, "message": "模拟资金查询失败（终端未连接）"}')

    fake_b.get_cash = _boom
    rt_b.pre_open(_Ctx())
    assert [c for c in fake_b.calls if c.get("api") == "order_volume"] == []
    assert all("fail-closed" in b["reason"] for b in _details(tmp_path / "b", "ORDER_BLOCKED"))
    assert any(w.get("type") == "get_cash_fail" for w in _details(tmp_path / "b", "WARN"))


def test_pre_open_state_saved_after_expire_before_later_failure(pilot, tmp_path, monkeypatch):
    """M-5：② 超期卖上柜台后、③④⑤ 中途异常炸出时 state 已先落盘——重启自愈不吃
    「柜台有单 state 无单」的双卖窗口（_has_open_sell 失守=重复挂卖，致命方向）。"""
    fake = FakeGm(symbol_info={"SHSE.688981": {"pre_close": 10.0, "lower_limit": 7.77,
                                               "upper_limit": 12.0}})
    _back_counter_position(fake, "SHSE.688981", 150, 10.0)
    i_t1 = CAL.index(T_MINUS_1)
    st = pilot._initial_state()
    st["positions"]["688981.SH"] = _pos_state(pilot, CAL[i_t1 - 21])   # 21 > 20 超期
    _pin(pilot, monkeypatch, tmp_path, universe=())

    def _boom(path=None):
        raise RuntimeError("模拟 ④ 段异常（③④⑤ 中途炸出的形态代表）")

    monkeypatch.setattr(pilot, "is_blocked", _boom)
    rt = _rt(pilot, fake, tmp_path, state=st)
    with pytest.raises(RuntimeError):
        rt.pre_open(_Ctx())                                   # 函数尾统一落盘未达

    disk = pilot.load_state(path=tmp_path / "state" / "state.pkl")
    sells = [o for o in disk["orders"].values() if o.get("purpose") == "EXPIRE"]
    assert len(sells) == 1 and sells[0]["symbol"] == "688981.SH"       # ② 的单已先落


# ============================================================================
# on_tick 巡检（pending 撤单 / 持仓离场 / tp1 已知分歧 / 在途卖单防重）
# ============================================================================
def _managed_position(pilot, remaining=400):
    """已被富化的持仓（trailing 六件套齐 → decide_position 活口径）。"""
    return {"entry_date": "2026-08-14", "entry_price": 10.4, "qty": 400,
            "remaining_qty": remaining, "stop": 9.5, "tp1_price": 11.0,
            "tp1_done": False, "tp2_price": 15.0,
            "trailing": {"neckline": 10.0, "atr": 0.5, "stop_atr_mult": 1.0,
                         "grace": 0, "step": 0.0, "floor": 0.0},
            "exec_params": {"tp1_portion": 0.3}}


def test_on_tick_stop_sells_all(pilot, tmp_path, monkeypatch):
    """止损触价（tick ≤ stop=颈线−1×ATR=9.5）→ 卖剩余全量，限价跟现价（跳空也能成交）。"""
    fake = FakeGm()
    _back_counter_position(fake, "SZSE.300750", 400, 10.4)
    st = pilot._initial_state()
    st["positions"]["300750.SZ"] = _managed_position(pilot, remaining=400)
    _pin(pilot, monkeypatch, tmp_path)
    rt = _rt(pilot, fake, tmp_path, state=st)
    rt.on_tick(_Ctx(), {"symbol": "SZSE.300750", "price": 9.4})        # tick.price（Q3/D5）

    sells = [c for c in fake.calls if c.get("api") == "order_volume"]
    assert len(sells) == 1 and sells[0]["side"] == 2 and sells[0]["volume"] == 400
    assert sells[0]["price"] == pytest.approx(9.4)                     # 止损价=现价（非 stop 价）
    row = _details(tmp_path, "SELL")[0]
    assert row["reason"] == "stop_loss" and row["qty"] == 400
    assert "known_divergence" not in row                               # 非 tp1 分歧路径


def test_on_tick_tp1_dust_clears_marks_known_divergence(pilot, tmp_path, monkeypatch):
    """tp1 不足一手清剩余（qty==remaining）→ audit 打 known_divergence=tp1_dust_clears。"""
    fake = FakeGm()
    _back_counter_position(fake, "SZSE.300750", 200, 10.4)
    st = pilot._initial_state()
    st["positions"]["300750.SZ"] = _managed_position(pilot, remaining=200)  # 200×0.3 不足一手
    _pin(pilot, monkeypatch, tmp_path)
    rt = _rt(pilot, fake, tmp_path, state=st)
    rt.on_tick(_Ctx(), {"symbol": "SZSE.300750", "price": 11.0})       # 触 tp1

    sells = [c for c in fake.calls if c.get("api") == "order_volume"]
    assert len(sells) == 1 and sells[0]["volume"] == 200               # 清全部剩余
    assert sells[0]["price"] == pytest.approx(11.0)                    # tp 档挂触发价
    row = _details(tmp_path, "SELL")[0]
    assert row["reason"] == "tp1" and row["known_divergence"] == "tp1_dust_clears"
    assert rt.state["positions"]["300750.SZ"]["tp1_done"] is True      # 一档一次（落单即置位）


def test_on_tick_no_duplicate_sell_while_pending(pilot, tmp_path, monkeypatch):
    """在途卖单防重：首跳已挂卖出、次跳同价不再重复挂（state 未吸收成交前的窗口）。"""
    fake = FakeGm()
    _back_counter_position(fake, "SZSE.300750", 400, 10.4)
    st = pilot._initial_state()
    st["positions"]["300750.SZ"] = _managed_position(pilot, remaining=400)
    _pin(pilot, monkeypatch, tmp_path)
    rt = _rt(pilot, fake, tmp_path, state=st)
    rt.on_tick(_Ctx(), {"symbol": "SZSE.300750", "price": 9.4})
    rt.on_tick(_Ctx(), {"symbol": "SZSE.300750", "price": 9.3})        # 同 symbol 连续 tick

    sells = [c for c in fake.calls if c.get("api") == "order_volume"]
    assert len(sells) == 1


def test_on_tick_pending_cancel_on_touch(pilot, tmp_path, monkeypatch):
    """挂单等待期：tick ≥ cancel_on → 撤单（decide_pending 判定 + audit 留 reason）。"""
    fake = FakeGm()
    cid = pilot.place_limit_buy(fake, "300750.SZ", 10.4, 4800, "acc-1")
    st = pilot._initial_state()
    st["orders"][cid] = {"symbol": "300750.SZ", "date": TODAY, "price": 10.4, "qty": 4800,
                         "purpose": "OPEN", "cancel_on": 11.0, "formed_at": T_MINUS_1,
                         "exec_params": {"max_wait": 8}, "status": "SUBMITTED", "filled": 0}
    _pin(pilot, monkeypatch, tmp_path)
    rt = _rt(pilot, fake, tmp_path, state=st)
    rt.on_tick(_Ctx(), {"symbol": "SZSE.300750", "price": 11.2})

    assert fake.orders[cid]["status"] == ORDER_STATUS["Canceled"]
    row = _details(tmp_path, "CANCEL")[0]
    assert row["reason"] == "cancel_on" and row["stage"] == "on_tick"
    assert st["orders"][cid]["status"] == "CANCELLED"                  # state 同步落终态


def test_on_tick_reconcile_failure_backoff(pilot, tmp_path, monkeypatch):
    """M-4：柜台查询失败后 60s 退避——故障期不每 tick 重打两次注定失败的查询
    （WARN 计数钉死=1）；退避窗过期后重试成功即复位锚（不残留死退避）。"""
    fake = FakeGm()

    def _boom():
        raise RuntimeError('{"status": 1100, "message": "模拟委托查询失败（终端未连接）"}')

    fake.get_orders = _boom                                   # 实例级注入（只影响本替身）
    _pin(pilot, monkeypatch, tmp_path)
    rt = _rt(pilot, fake, tmp_path)
    for _ in range(3):
        rt.on_tick(_Ctx(), {"symbol": "SZSE.300750", "price": 10.0})
    fails = [w for w in _details(tmp_path, "WARN")
             if w.get("type") == "reconcile_orders_fail"]
    assert len(fails) == 1                   # 首 tick 失败一次；后两 tick 被退避闸拦下

    del fake.get_orders                                       # 撤注入：柜台恢复可用
    rt._absorb_retry_after = 0.0                              # 白盒推进：模拟退避窗已过（不真等 60s）
    rt.on_tick(_Ctx(), {"symbol": "SZSE.300750", "price": 10.0})
    assert rt._absorb_retry_after is None and rt._last_absorb is not None   # 成功复位双锚
    assert len([w for w in _details(tmp_path, "WARN")
                if w.get("type") == "reconcile_orders_fail"]) == 1          # 无新增失败


# ============================================================================
# reconcile：幂等三查入口 + 持仓富化（信号几何 → 止损/止盈价）
# ============================================================================
def test_reconcile_absorbs_counter_orders(pilot, tmp_path, monkeypatch):
    """柜台有 state 无 → 吸收（订单+持仓）；audit RECONCILE 留痕；state 落盘。"""
    fake = FakeGm()
    fake.orders["cx1"] = {"cl_ord_id": "cx1", "symbol": "SZSE.300750", "side": 1,
                          "status": ORDER_STATUS["New"], "volume": 200, "price": 10.0,
                          "filled_volume": 0, "filled_vwap": 0.0, "account_id": "acc-1",
                          "created_at": datetime(2026, 8, 21, 9, 31, 0)}
    fake.positions["SHSE.688981"] = {"symbol": "SHSE.688981", "side": 1, "volume": 150,
                                     "vwap": 55.0}
    _pin(pilot, monkeypatch, tmp_path)
    rt = _rt(pilot, fake, tmp_path)
    rt.reconcile(_Ctx())

    o = rt.state["orders"]["cx1"]                                      # 订单正向吸收
    assert o["symbol"] == "300750.SZ" and o["status"] == "SUBMITTED"
    p = rt.state["positions"]["688981.SH"]                             # 持仓吸收（entry 用柜台 vwap）
    assert p["remaining_qty"] == 150 and p["entry_price"] == 55.0
    assert p["exec_params"] == {}                                      # 柜台无信号信息，不伪造
    assert "RECONCILE" in _events(tmp_path)
    st = pilot.load_state(path=tmp_path / "state" / "state.pkl")       # 幂等三查后落盘
    assert "cx1" in st["orders"]


def test_reconcile_enriches_position_from_order_geometry(pilot, tmp_path, monkeypatch):
    """买单成交 → 持仓按订单信号几何富化：stop=颈线−1×ATR、tp1=颈线+1×H、tp2=颈线+2.5×H。"""
    fake = FakeGm()
    sigs = {"300750.SZ": _signal(pilot)}                               # 10 / 8 / 0.8 → H=2
    _pin(pilot, monkeypatch, tmp_path, universe=("300750.SZ",), detect=_detect_map(sigs))
    rt = _rt(pilot, fake, tmp_path)
    rt.pre_open(_Ctx())
    cid = rt.state["placed"][TODAY][0]
    fake.fill_order(cid, price=10.4)                                   # 全部成交（柜台转持仓）

    rt.reconcile(_Ctx())
    pos = rt.state["positions"]["300750.SZ"]
    assert pos["remaining_qty"] == 4800 and pos["entry_date"] == TODAY
    tr = pos["trailing"]                                               # trailing 六件套（活口径原料）
    assert tr["neckline"] == pytest.approx(10.0) and tr["atr"] == pytest.approx(0.8)
    assert tr["stop_atr_mult"] == 1.0 and tr["grace"] == 0
    assert pos["stop"] == pytest.approx(10.0 - 1.0 * 0.8)              # 9.2（盘后预算兜底价）
    assert pos["tp1_price"] == pytest.approx(12.0)                     # 10 + 1.0×H
    assert pos["tp2_price"] == pytest.approx(15.0)                     # 10 + 2.5×H
    assert "POS_ENRICHED" in _events(tmp_path)


# ============================================================================
# init/bootstrap：runtime.json + 定时注册 + 巡检订阅
# ============================================================================
def test_bootstrap_registers_schedules_and_subscribes(pilot, tmp_path, monkeypatch):
    """init 链：读 runtime.json（白名单账户）→ 对账 → 订阅持仓∪挂单 tick → 注册双定时。"""
    fake = FakeGm()
    fake.orders["cx1"] = {"cl_ord_id": "cx1", "symbol": "SZSE.300750", "side": 1,
                          "status": ORDER_STATUS["New"], "volume": 200, "price": 10.0,
                          "filled_volume": 0, "filled_vwap": 0.0, "account_id": "acc-1",
                          "created_at": datetime(2026, 8, 21, 9, 31, 0)}
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "runtime.json").write_text(
        json.dumps({"token": "test-token", "strategy_id": "st-1",
                    "account_id": pilot.PILOT_ACCOUNT_ID}), encoding="utf-8")
    _pin(pilot, monkeypatch, tmp_path)
    rt = pilot.PilotRuntime(fake, workdir=tmp_path)
    rt.bootstrap(_Ctx())

    scheds = {(c["func"], c["date_rule"], c["time_rule"])
              for c in fake.calls if c.get("api") == "schedule"}
    assert scheds == {("pre_open_job", "1d", "09:15:00"),
                      ("after_close_job", "1d", "15:35:00")}
    subs = [c for c in fake.calls if c.get("api") == "subscribe"]
    assert len(subs) == 1 and subs[0]["symbols"] == ["SZSE.300750"]    # 持仓∪挂单（gm 符号）
    assert subs[0]["frequency"] == "tick"
    assert "INIT" in _events(tmp_path)


def test_bootstrap_missing_token_raises(pilot, tmp_path, monkeypatch):
    """runtime.json 缺 token → 直接 raise（中文诊断指向 README，绝不带空 token 进 run）。"""
    fake = FakeGm()
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "runtime.json").write_text(
        json.dumps({"strategy_id": "st-1", "account_id": pilot.PILOT_ACCOUNT_ID}),
        encoding="utf-8")
    _pin(pilot, monkeypatch, tmp_path)
    rt = pilot.PilotRuntime(fake, workdir=tmp_path)
    with pytest.raises(RuntimeError, match="token"):
        rt.bootstrap(_Ctx())


# ============================================================================
# after_close：审计收尾 + 清订阅 + state 落盘
# ============================================================================
def test_after_close_closes_the_day(pilot, tmp_path, monkeypatch):
    """盘后：EOD 收尾行 + 动态订阅清空 + state 落盘三件套。"""
    fake = FakeGm()
    _pin(pilot, monkeypatch, tmp_path)
    rt = _rt(pilot, fake, tmp_path)
    rt._subscribed = ["300750.SZ"]                                    # 模拟 init 订阅在场
    rt.after_close(_Ctx())

    assert "EOD" in _events(tmp_path)
    unsubs = [c for c in fake.calls if c.get("api") == "unsubscribe"]
    assert len(unsubs) == 1 and unsubs[0]["symbols"] == ["SZSE.300750"]
    assert unsubs[0]["frequency"] == "tick"
    assert rt._subscribed == []
    assert (tmp_path / "state" / "state.pkl").exists()


# ============================================================================
# run_pilot：C1 仿真守卫（08-21 修订版——账户白名单制）
# ============================================================================
def test_run_pilot_refuses_non_sim_account(pilot, tmp_path, monkeypatch):
    """account 非白名单且无 PILOT_ALLOW_LIVE → RuntimeError；env 逃生门放行到 run(MODE_LIVE)。"""
    fake = FakeGm()
    monkeypatch.setattr(pilot, "CONFIG_DIR", tmp_path)                 # runtime.json 查 tmp（C8 隔离）
    (tmp_path / "runtime.json").write_text(
        json.dumps({"token": "test-token", "strategy_id": "st-1",
                    "account_id": "real-money-account"}), encoding="utf-8")
    monkeypatch.delenv("PILOT_ALLOW_LIVE", raising=False)
    with pytest.raises(RuntimeError, match="白名单"):
        pilot.run_pilot()

    monkeypatch.setenv("PILOT_ALLOW_LIVE", "I_KNOW_REAL_MONEY")        # 唯一逃生门（知情实盘）
    monkeypatch.setattr(pilot, "_GM", fake)                            # _api() seam 离线
    pilot.run_pilot()
    r = next(c for c in fake.calls if c.get("api") == "run")
    assert r["mode"] == fake.MODE_LIVE == 1                            # 无仿真常量，MODE_LIVE 绑仿真账户
    assert r["token"] == "test-token"


def test_run_pilot_whitelist_account_launches(pilot, tmp_path, monkeypatch):
    """白名单账户直接放行：run(strategy_id/filename=__file__/MODE_LIVE/token) 全参可审计。"""
    fake = FakeGm()
    monkeypatch.setattr(pilot, "CONFIG_DIR", tmp_path)
    (tmp_path / "runtime.json").write_text(
        json.dumps({"token": "test-token", "strategy_id": "st-pilot",
                    "account_id": pilot.PILOT_ACCOUNT_ID}), encoding="utf-8")
    monkeypatch.delenv("PILOT_ALLOW_LIVE", raising=False)
    monkeypatch.setattr(pilot, "_GM", fake)
    pilot.run_pilot()
    r = next(c for c in fake.calls if c.get("api") == "run")
    assert r["strategy_id"] == "st-pilot" and r["token"] == "test-token"
    assert r["mode"] == 1
    assert str(r["filename"]).replace("\\", "/").endswith("emquant/emquant_neckline_pilot.py")


# ============================================================================
# schema v1 兼容扩展 + 入口抑制结构（hoist 唯一性/位次）
# ============================================================================
def test_load_state_backfills_last_signal(pilot, tmp_path):
    """旧 v1 文件缺 last_signal 键 → load 填默认 {}（Task 8 追加键的兼容扩展，不升版）。"""
    p = tmp_path / "state.pkl"
    p.write_text(json.dumps({"version": 1, "scan_done": [], "placed": {},
                             "orders": {}, "positions": {}}), encoding="utf-8")
    st = pilot.load_state(path=p)
    assert st["last_signal"] == {}
    st["last_signal"]["300750.SZ"] = T_MINUS_1
    pilot.save_state(st, path=p)
    assert pilot.load_state(path=p)["last_signal"] == {"300750.SZ": T_MINUS_1}


def test_entrance_suppression_hoisted_before_kernel_guard():
    """入口抑制块：全产物唯一一份，且位于 §1 内核演示守卫之前（复制/位次错都会哑火）。

    Why 结构级断言而非只跑 subprocess：subprocess 冒烟（runbook 项）依赖仓库
    config/runtime.json 缺失这一前提；本断言在任何环境下钉死「抑制先于内核守卫执行」
    的位次不变量。
    """
    art = ARTIFACT.read_text(encoding="utf-8")
    assert art.count('__name__ = "pilot_kernel_suppressed"') == 1      # 唯一一份（复制即哑火）
    supp = art.index('__name__ = "pilot_kernel_suppressed"')
    kernel_guard = art.index('if __name__ == "__main__":\n    main()')  # §1 演示守卫（method_v0 尾）
    assert supp < kernel_guard
    # 尾入口用捕获值（允许行内注释）：末两行有效代码必须是 if _IS_MAIN: / run_pilot()
    tail = [l for l in art.rstrip().splitlines() if l.strip()][-2:]
    assert tail[0].split("#")[0].strip() == "if _IS_MAIN:"
    assert tail[1].strip() == "run_pilot()"
    # 源侧标记块仍在 pilot_body 顶部（组装器剪切源——源与产物各持一份，不违反唯一性：
    # 产物里只出现 hoist 后的那一份）
    body = (ROOT / "emquant" / "pilot_body.py").read_text(encoding="utf-8")
    assert body.count("__name__ = \"pilot_kernel_suppressed\"") == 1

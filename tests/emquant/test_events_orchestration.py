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
    - 订阅容错（Task 8 评审 Minor，Task 9 收口）：subscribe 抛错 → WARN 留痕降级
      继续（不中止 pre_open——行情断≠交易断），账本不动、恢复后重试全差集；
    - ① 昨日判据（R1 I-2）：柜台单 created_at 日期==today 跳过（state 滞后时柜台
      是唯一真值源；缺失退 state 侧 date）——同日重跑不自杀当日进场（scan_done
      防重扫=撤了不补，进场静默丢失）；
    - equity fail-closed（R1 M-3 编排接线）：get_cash 抛错/空表 → equity=None →
      有信号 0 挂单 + ORDER_BLOCKED 留 fail-closed 原因；
    - 对账失败退避（R1 M-4）：reconcile 查询失败后 60s 窗内 on_tick 不重试；
    - ② 后防御性落盘（R1 M-5）：超期卖上柜台后 state 先落——③④⑤ 中途异常不吃
      「柜台有单 state 无单」的双卖窗口；
    - 终审 R2 修复面：跌停价三级回退（I-3——get_history_symbol 整体失败 → T-1
      收盘自算 round(×0.80,2)）；定尺不足一手独立文案（M-5）；context.accounts
      核验（M-1——空表/白名单不在场=故障 WARN）；unsubscribe 容错且账本恒清（M-4）；
      tick 符号折算 WARN-skip 单事件降级（M-6）。fetch 末根不变量（I-2）与日历
      「今日不在历」补计（I-1）在 test_data_layer / test_order_lifecycle 侧。
    - 入口抑制（Task 4 遗留）：§1 内核逐字块尾部 `if __name__ == "__main__": main()`
      在产物第 ~804 行、先于 §2-§7 拼接位执行——抑制块必须被组装器剪出到 head 区
      （§0 之前）且全产物唯一一份（复制会让第二次 _IS_MAIN 赋值把入口哑火）。
"""
from __future__ import annotations

import csv
import importlib.util
import json
import sys
from datetime import datetime
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


class _SdkTick:
    """gm 3.8.19 TickLikeDict2 实测形态替身（2026-08-26 首日实弹教训）。

    C 扩展真身并非 dict 子类（c_sdk.pyi 的 dict 声明失实）：实例 `.get` 解析为
    None（按 dict 习惯调 tick.get(...) 即 TypeError——当日首只脏 tick 的价格守卫
    正是踩在报错路径的 .get 上把策略打死）、`__getitem__` 可用且缺键返 None、
    对象恒真值。测试若用裸 dict 当 tick，一切 dict 习惯写法全数漏网（745 绿照样
    实弹崩）；on_tick 系测试一律用本替身钉死真实形态。
    """

    get = None   # 实测：按 dict 习惯调 tick.get(...) 即 TypeError

    def __init__(self, **fields):
        self._fields = fields

    def __getitem__(self, key):
        return self._fields.get(key)   # 实测：缺键返 None（不抛 KeyError）


class _CtxAccts:
    """带 accounts 的 context 占位（终审 M-1：bootstrap 核验 context.accounts 用）。

    形态对齐 Task 2 文档 Q1：context.accounts 是 Dict[account_id, Account]——测试只
    需键集（值为任意），核验逻辑做成员测不触值。
    """

    def __init__(self, accounts):
        self.accounts = accounts


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
    """读 workdir 侧当日 audit CSV（§6 全部行经 _audit → audit_dir=workdir/audit）。

    文件名取夹具 TODAY（终审 M-3 后 audit_log 的按日分文件走 _today_str() seam，
    _pin 已把它钉到 TODAY——读真实 date.today() 会在非 2026-08-21 的运行日错位）。
    """
    f = tmp_path / "audit" / f"audit_{TODAY.replace('-', '')}.csv"
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
    _mh = int(pilot.EXEC_PARAMS["max_holding"])          # §0 换代健壮：超期龄期动态取
    st = pilot._initial_state()
    st["positions"]["688981.SH"] = _pos_state(pilot, CAL[i_t1 - _mh - 1])        # 恰超 max_holding
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
    _mh = int(pilot.EXEC_PARAMS["max_holding"])          # §0 换代健壮：恰等/超期动态构造
    st = pilot._initial_state()
    st["positions"]["300750.SZ"] = _pos_state(pilot, CAL[i_t1 - _mh], remaining=300)   # 恰等：不平
    st["positions"]["688981.SH"] = _pos_state(pilot, CAL[i_t1 - _mh - 1], remaining=150)  # 恰超：平
    _pin(pilot, monkeypatch, tmp_path, universe=())                     # 无信号干扰
    rt = _rt(pilot, fake, tmp_path, state=st)
    rt.pre_open(_Ctx())

    sells = [c for c in fake.calls if c.get("api") == "order_volume" and c["side"] == 2]
    assert len(sells) == 1
    assert sells[0]["symbol"] == "SHSE.688981" and sells[0]["volume"] == 150
    assert sells[0]["price"] == pytest.approx(7.77)
    exp = _details(tmp_path, "EXPIRE_SELL")[0]
    assert exp["holding_days"] == _mh + 1 and exp["max_holding"] == _mh  # 审计留判定依据


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
    """cooldown 跨日去重（engine.py:1036-1050 复刻）：(last, today] < cd 丢、== cd 放。

    cooldown 显式注入 5（隔离 §0 值——R6-8 冠军 cooldown=0「不去重」本身是配置
    语义，机制测试不随 §0 换代失效）。"""
    monkeypatch.setitem(pilot.EXEC_PARAMS, "cooldown", 5)
    i_t = CAL.index(TODAY)
    last_recent, last_edge = CAL[i_t - 4], CAL[i_t - 5]   # 4 < 5 丢 / 5 == 5 放
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


def test_pre_open_subscribe_failure_degrades_not_aborts(pilot, tmp_path, monkeypatch):
    """订阅容错（Task 8 评审 Minor，Task 9 收口）：行情通道瞬时异常（subscribe 抛错）
    不中止 pre_open——⓪' 重建失败后 ① 撤昨日单 / ⑤ 挂新单等交易通道动作照跑；订阅
    账本不动，恢复后一次重试补齐全差集（行情断≠交易断的双通道现实组合）。"""
    fake = FakeGm()
    yid = pilot.place_limit_buy(fake, "688981.SH", 10.0, 100, "acc-y")
    _backdate_order(fake, yid, T_MINUS_1)                   # 昨日单 → ① 的撤单对象
    _back_counter_position(fake, "SZSE.300750", 150, 10.0)    # 持仓配柜台背书（absorb ③ 语义）
    st = pilot._initial_state()
    st["positions"]["300750.SZ"] = _pos_state(pilot, T_MINUS_1)   # 昨进仓不超期 → ⓪' 订阅面非空
    sigs = {"300059.SZ": _signal(pilot, "300059.SZ")}
    _pin(pilot, monkeypatch, tmp_path, universe=("300059.SZ",), detect=_detect_map(sigs))
    rt = _rt(pilot, fake, tmp_path, state=st)

    def _boom(symbols, **kw):
        raise RuntimeError('{"status": 1100, "message": "模拟行情订阅失败（终端未连接/GmError 形态）"}')

    orig_subscribe = fake.subscribe                          # 先存 bound method（恢复用）
    fake.subscribe = _boom
    rt.pre_open(_Ctx())                                      # 不炸：五阶段完整走完
    # 交易通道动作全在（若 ⓪' 订阅异常上抛，①②⑤ 全部陪跳——本用例的红线）
    assert fake.orders[yid]["status"] == ORDER_STATUS["Canceled"]   # ① 撤昨日单在
    buys = [c for c in fake.calls if c.get("api") == "order_volume"
            and c["side"] == 1 and c["price"] == pytest.approx(10.4)]
    assert len(buys) == 1                                           # ⑤ 新挂单在
    warns = [w for w in _details(tmp_path, "WARN") if w.get("type") == "subscribe_fail"]
    # ⓪' 首发（⓪ 对账已吸收柜台昨日单 → 订阅面=持仓∪未终态单）+ ⑤' 再发（账本仍空 → 全差集）
    assert len(warns) == 2
    assert set(warns[0]["symbols"]) == {"300750.SZ", "688981.SH"}
    assert set(warns[1]["symbols"]) == {"300059.SZ", "300750.SZ"}
    assert rt._subscribed == []                                     # 账本不动（失败不吞成功）

    fake.subscribe = orig_subscribe                                # 行情通道恢复
    rt._subscribe_watchlist()                                      # 下次重试：全差集一次补齐
    assert fake.subscriptions == {"SZSE.300059": "tick", "SZSE.300750": "tick"}
    assert rt._subscribed == ["300059.SZ", "300750.SZ"]


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
    _mh = int(pilot.EXEC_PARAMS["max_holding"])          # §0 换代健壮：超期龄期动态取
    st = pilot._initial_state()
    st["positions"]["688981.SH"] = _pos_state(pilot, CAL[i_t1 - _mh - 1])   # 恰超 max_holding
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


def test_expired_close_t1_close_fallback_price(pilot, tmp_path, monkeypatch):
    """跌停价 T-1 收盘回退（终审 I-3 编排面）：get_history_symbol 整体失败 + T-1 取数
    成功 → 超期卖单价格 = round(T-1 收盘×0.80, 2)——「缺一行证券信息=整日放弃超期
    平仓」的洞补上（API 通道故障不再吃掉存量退出，20% 自算在创板科创池内几乎恒等）。"""
    fake = FakeGm(raise_on_symbol_info=True)            # 证券信息通道整体失败（盘前当日行未生成的形态）
    _back_counter_position(fake, "SHSE.688981", 150, 10.0)   # 持仓配柜台背书（absorb ③ 语义）
    i_t1 = CAL.index(T_MINUS_1)
    _mh = int(pilot.EXEC_PARAMS["max_holding"])          # §0 换代健壮：超期龄期动态取
    st = pilot._initial_state()
    st["positions"]["688981.SH"] = _pos_state(pilot, CAL[i_t1 - _mh - 1])   # 恰超 max_holding
    _pin(pilot, monkeypatch, tmp_path, universe=())                     # 无信号干扰
    rt = _rt(pilot, fake, tmp_path, state=st)
    # 期望值：同一行情口径下 T-1 末根 close 的 20% 自算（替身确定性合成，无手算魔法数）
    prev_close = float(pilot.fetch_df_upto(FakeGm(), "688981.SH", T_MINUS_1)["close"].iloc[-1])
    expected = pilot.limit_down_price(prev_close, "688981.SH")

    rt.pre_open(_Ctx())

    sells = [c for c in fake.calls if c.get("api") == "order_volume" and c["side"] == 2]
    assert len(sells) == 1 and sells[0]["symbol"] == "SHSE.688981"
    assert sells[0]["price"] == pytest.approx(expected)                 # = round(T-1收×0.80, 2)
    dl_rows = (tmp_path / "dl_audit").glob("audit_*.csv")
    assert any("limit_down_fallback_t1" in l
               for f in dl_rows for l in f.read_text(encoding="utf-8").splitlines())  # 回退进晨检面


def test_pre_open_sizing_below_one_lot_blocked_with_clear_reason(pilot, tmp_path, monkeypatch):
    """定尺不足一手（终审 M-5）：equity×pos_cap 按当前 entry 凑不出 100 股 →
    ORDER_BLOCKED 文案明示「定尺不足一手」——不再落进 check_caps ① 的「委托参数
    残缺」（那是查询通道故障语义，会误导晨检排障方向）。"""
    fake = FakeGm()
    fake.cash["nav"] = 10_000.0                          # 10,000×5%=500 < 100×10.4=1040
    sigs = {"300750.SZ": _signal(pilot)}
    _pin(pilot, monkeypatch, tmp_path, universe=("300750.SZ",), detect=_detect_map(sigs))
    rt = _rt(pilot, fake, tmp_path)
    rt.pre_open(_Ctx())

    assert [c for c in fake.calls if c.get("api") == "order_volume"] == []   # 不发废单
    blocked = _details(tmp_path, "ORDER_BLOCKED")
    assert len(blocked) == 1 and "定尺不足一手" in blocked[0]["reason"]
    assert "100 股" in blocked[0]["reason"]
    assert len(_details(tmp_path, "SIGNAL")) == 1                       # ③ 扫描照常留痕


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


def _dead_open_order(cid, symbol, status="REJECTED", **over):
    """当日死单档案（⑤'' 死单回补的输入）：几何参数齐备（neckline=10/bottom=8/
    ctm=2.0 → 回补 cancel_on=10+2×2=14.0 可手算），status 默认 REJECTED=柜台拒。"""
    o = {"symbol": symbol, "date": TODAY, "price": 10.4, "qty": 4800,
         "purpose": "OPEN", "cancel_on": 14.0, "formed_at": T_MINUS_1,
         "exec_params": {"cancel_thresh_mult": 2.0}, "neckline": 10.0,
         "atr": 0.8, "bottom": 8.0, "status": status, "filled": 0,
         "account": "fake"}
    o.update(over)
    return cid, o


# ============================================================================
# ⑤'' 死单回补 + tick 自愈 repair（2026-08-27「没挂成功的单子随时重启随时挂」）
# ============================================================================
def test_pre_open_repairs_dead_orders(pilot, tmp_path, monkeypatch):
    """⑤''：当日 OPEN 死单（REJECTED/零成交）在 pre_open 重跑时按当前权益重新定尺
    重挂——几何参数从死单档案继承（cancel_on=14.0 手算锚）、repair_of 留溯源；
    同标第二张死单被在途守卫拦下（回补挂活即在场，防双挂）。"""
    fake = FakeGm()
    st = pilot._initial_state()
    st["scan_done"].add(TODAY)                    # 当日扫描已完成：③ 不产新信号，回补是唯一挂单源
    st["orders"]["dead1"] = dict(_dead_open_order("dead1", "600000.SH")[1])
    st["orders"]["dead2"] = dict(_dead_open_order("dead2", "600000.SH")[1])
    st["placed"][TODAY] = ["dead1", "dead2"]      # 两死单：有效 0/2，额度全部可用
    _pin(pilot, monkeypatch, tmp_path)
    rt = _rt(pilot, fake, tmp_path, state=st)
    rt.pre_open(_Ctx())

    placed = [c for c in fake.calls if c.get("api") == "order_volume"]
    assert len(placed) == 1 and placed[0]["symbol"] == "SHSE.600000"
    assert placed[0]["volume"] == 4800            # ⌊1M×0.05/10.4/100⌋×100（FakeGm nav 1M）
    row = _details(tmp_path, "ORDER_PLACED")[0]
    assert row["repair_of"] == "dead1" and row["cancel_on"] == 14.0
    skips = _details(tmp_path, "REPAIR_SKIP")
    assert len(skips) == 1 and skips[0]["cl_ord_id"] == "dead2"   # 同标第二死单：在途守卫拦
    new_cid = next(c for c, o in st["orders"].items() if o.get("repair_of") is None
                   and o["status"] == "SUBMITTED")
    assert st["placed"][TODAY] == ["dead1", "dead2", new_cid]     # placed 追加（额度计数消费）


def test_pre_open_repair_skips_live_sibling_and_held(pilot, tmp_path, monkeypatch):
    """⑤'' 守卫：同标已有在途 OPEN 单（如首挂存活）或已有剩余持仓 → 死单不回补
    （防对已成功标的重复进场），REPAIR_SKIP 留痕、零下单。

    live 兄弟单须在柜台落真值（⓪ 对账以柜台实况修 state：state-only 的非终态单
    会被 absorb ② 收敛 CANCELLED，守卫前提就被对账拆了）+ created_at 回填夹具日
    （I-2：真实时钟日期 ≠ TODAY 会被 ① 误判昨日单撤掉）。
    """
    fake = FakeGm()
    live_res = fake.order_volume("SHSE.600000", 4800, 1, 1, 1, price=10.4)
    live_cid = live_res[0]["cl_ord_id"]
    _backdate_order(fake, live_cid, TODAY)
    st = pilot._initial_state()
    st["scan_done"].add(TODAY)
    st["orders"]["dead1"] = dict(_dead_open_order("dead1", "600000.SH")[1])
    st["orders"][live_cid] = dict(_dead_open_order(live_cid, "600000.SH",
                                                   status="SUBMITTED")[1])
    st["orders"]["dead3"] = dict(_dead_open_order("dead3", "300750.SZ")[1])
    st["positions"]["300750.SZ"] = _managed_position(pilot, remaining=400)
    st["placed"][TODAY] = ["dead1", live_cid, "dead3"]
    _pin(pilot, monkeypatch, tmp_path)
    rt = _rt(pilot, fake, tmp_path, state=st)
    # 300750 持仓需柜台背书（⓪ 对账 absorb ③ 语义：无背书持仓归零）
    _back_counter_position(fake, "SZSE.300750", 400, 10.4)
    rt.pre_open(_Ctx())

    assert [c for c in fake.calls if c.get("api") == "order_volume"
            and c["symbol"] != "SHSE.600000"] == []      # 除预置 live 单外零下单
    skips = {s["cl_ord_id"] for s in _details(tmp_path, "REPAIR_SKIP")}
    assert skips == {"dead1", "dead3"}            # 在途兄弟单/持仓两守卫各拦一张


def test_pre_open_repair_lot_too_small_blocked(pilot, tmp_path, monkeypatch):
    """⑤'' 定尺：equity×pos_cap 不足一手（新账户 10 万×5%=5000 < 100×60）→
    ORDER_BLOCKED 独立文案（回补定尺不足一手），不炸不挂——选项 A 口径下的
    高价股预期行为（301018@122 实弹同型）。"""
    fake = FakeGm()
    fake.cash["nav"] = 100_000.0                  # 新账户 10 万
    st = pilot._initial_state()
    st["scan_done"].add(TODAY)
    st["orders"]["dead1"] = dict(_dead_open_order("dead1", "301018.SZ", price=60.0)[1])
    st["placed"][TODAY] = ["dead1"]
    _pin(pilot, monkeypatch, tmp_path)
    rt = _rt(pilot, fake, tmp_path, state=st)
    rt.pre_open(_Ctx())

    assert [c for c in fake.calls if c.get("api") == "order_volume"] == []
    blocked = _details(tmp_path, "ORDER_BLOCKED")
    assert len(blocked) == 1 and "回补定尺不足一手" in blocked[0]["reason"]


def test_on_tick_self_heal_repair_fires_once_per_process(pilot, tmp_path, monkeypatch):
    """tick 自愈 repair：当日 pre_open 已跑 + 存在死单 + 窗口内（09:32~15:00）→ 首个
    tick 补跑 pre_open 走回补段；同进程第二 tick 被闩拦（每进程每日一次——「随时
    重启随时挂」= 重启=新进程=新一次机会，不做盘中无限重试）。"""
    fake = FakeGm()
    monkeypatch.setattr(pilot, "_today_clock", lambda: "10:00:00")
    st = pilot._initial_state()
    st["scan_done"].add(TODAY)
    st["last_pre_open_date"] = TODAY              # 当日 pre_open 已跑：常规自愈不触发
    st["orders"]["dead1"] = dict(_dead_open_order("dead1", "600000.SH")[1])
    st["placed"][TODAY] = ["dead1"]
    _pin(pilot, monkeypatch, tmp_path)
    rt = _rt(pilot, fake, tmp_path, state=st)
    rt.on_tick(_Ctx(), _SdkTick(symbol="SHSE.600000", price=10.4))

    heals = [w for w in _details(tmp_path, "WARN") if w.get("type") == "tick_self_heal_repair"]
    assert len(heals) == 1
    placed = [c for c in fake.calls if c.get("api") == "order_volume"]
    assert len(placed) == 1 and placed[0]["symbol"] == "SHSE.600000"   # 回补已挂出

    rt.on_tick(_Ctx(), _SdkTick(symbol="SHSE.600000", price=10.4))     # 第二 tick：闩生效
    assert len([c for c in fake.calls if c.get("api") == "order_volume"]) == 1
    assert len([w for w in _details(tmp_path, "WARN")
                if w.get("type") == "tick_self_heal_repair"]) == 1


def test_probe_self_heal_repair_fires_without_ticks(pilot, tmp_path, monkeypatch):
    """R6-13 探针兼任回补通道：空仓+全死单 → 订阅面空 → on_tick 无事件（账户切换
    当日实锤盲区）——13:31 探针直评触发回补挂单（无闩，schedule 小时级限频）；
    15:31 拍在窗外只留 SCHEDULE_TICK 不回补。"""
    fake = FakeGm()
    _pin(pilot, monkeypatch, tmp_path)
    st = pilot._initial_state()
    st["scan_done"].add(TODAY)
    st["last_pre_open_date"] = TODAY
    st["orders"]["dead1"] = dict(_dead_open_order("dead1", "600000.SH")[1])
    st["placed"][TODAY] = ["dead1"]
    rt = _rt(pilot, fake, tmp_path, state=st)
    monkeypatch.setattr(pilot, "RT", rt)                 # 模块级 job 入口读全局 RT

    monkeypatch.setattr(pilot, "_today_clock", lambda: "13:31:00")
    pilot.schedule_probe_job(_Ctx())
    heals = [w for w in _details(tmp_path, "WARN")
             if w.get("type") == "probe_self_heal_repair"]
    assert len(heals) == 1                               # 探针通道 fire（无 tick 参与）
    placed = [c for c in fake.calls if c.get("api") == "order_volume"]
    assert len(placed) == 1 and placed[0]["symbol"] == "SHSE.600000"
    row = _details(tmp_path, "ORDER_PLACED")[0]
    assert row["repair_of"] == "dead1"

    monkeypatch.setattr(pilot, "_today_clock", lambda: "15:31:00")
    pilot.schedule_probe_job(_Ctx())                     # 窗外拍：窗口闸自拒
    assert len([c for c in fake.calls if c.get("api") == "order_volume"]) == 1
    assert len([w for w in _details(tmp_path, "WARN")
                if w.get("type") == "probe_self_heal_repair"]) == 1


def test_pre_open_dedup_cancels_older_duplicate(pilot, tmp_path, monkeypatch):
    """①' 同标去重（R6-13b）：两张同标在途 OPEN 单 → 撤旧留新（DEDUP CANCEL 留痕 +
    柜台撤单），新单存活——13:31 实弹竞态双挂的结果侧兜底锚。"""
    fake = FakeGm()
    r1 = fake.order_volume("SHSE.600000", 100, 1, 1, 1, price=47.43)
    r2 = fake.order_volume("SHSE.600000", 100, 1, 1, 1, price=47.43)
    c1, c2 = r1[0]["cl_ord_id"], r2[0]["cl_ord_id"]
    _backdate_order(fake, c1, TODAY)                     # 当日单：① 不撤（I-2 同日守卫）
    _backdate_order(fake, c2, TODAY)
    st = pilot._initial_state()
    st["scan_done"].add(TODAY)
    st["orders"][c1] = dict(_dead_open_order(c1, "600000.SH", status="SUBMITTED")[1],
                            placed_at=100.0)             # 旧（竞态前产物）
    st["orders"][c2] = dict(_dead_open_order(c2, "600000.SH", status="SUBMITTED")[1],
                            placed_at=200.0)             # 新
    st["placed"][TODAY] = [c1, c2]
    _pin(pilot, monkeypatch, tmp_path)
    rt = _rt(pilot, fake, tmp_path, state=st)
    rt.pre_open(_Ctx())

    cancels = [d for d in _details(tmp_path, "CANCEL") if d.get("stage") == "dedup"]
    assert len(cancels) == 1 and cancels[0]["cl_ord_id"] == c1   # 只撤旧的
    assert st["orders"][c1]["status"] == "CANCELLED"
    assert st["orders"][c2]["status"] == "SUBMITTED"             # 新单存活
    assert fake.orders[c1]["status"] == ORDER_STATUS["Canceled"] # 柜台真撤
    assert fake.orders[c2]["status"] == ORDER_STATUS["New"]
    assert [c for c in fake.calls if c.get("api") == "order_volume"
            and c.get("symbol") != "SHSE.600000"] == []          # 无新挂单（去重非补挂）


def test_on_tick_self_heal_repair_window_guard(pilot, tmp_path, monkeypatch):
    """窗口守卫：15:00 后（收盘）不再触发回补——当日残务归 15:36 after_close 与
    次日 pre_open（挂单窗口外重挂无成交语义）。"""
    fake = FakeGm()
    monkeypatch.setattr(pilot, "_today_clock", lambda: "15:05:00")
    st = pilot._initial_state()
    st["scan_done"].add(TODAY)
    st["last_pre_open_date"] = TODAY
    st["orders"]["dead1"] = dict(_dead_open_order("dead1", "600000.SH")[1])
    st["placed"][TODAY] = ["dead1"]
    _pin(pilot, monkeypatch, tmp_path)
    rt = _rt(pilot, fake, tmp_path, state=st)
    rt.on_tick(_Ctx(), _SdkTick(symbol="SHSE.600000", price=10.4))

    assert [w for w in _details(tmp_path, "WARN")
            if w.get("type") == "tick_self_heal_repair"] == []
    assert [c for c in fake.calls if c.get("api") == "order_volume"] == []


def test_on_tick_stop_sells_all(pilot, tmp_path, monkeypatch):
    """止损触价（tick ≤ stop=颈线−1×ATR=9.5）→ 卖剩余全量，限价跟现价（跳空也能成交）。"""
    fake = FakeGm()
    _back_counter_position(fake, "SZSE.300750", 400, 10.4)
    st = pilot._initial_state()
    st["positions"]["300750.SZ"] = _managed_position(pilot, remaining=400)
    _pin(pilot, monkeypatch, tmp_path)
    rt = _rt(pilot, fake, tmp_path, state=st)
    rt.on_tick(_Ctx(), _SdkTick(symbol="SZSE.300750", price=9.4))        # tick.price（Q3/D5）

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
    rt.on_tick(_Ctx(), _SdkTick(symbol="SZSE.300750", price=11.0))       # 触 tp1

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
    rt.on_tick(_Ctx(), _SdkTick(symbol="SZSE.300750", price=9.4))
    rt.on_tick(_Ctx(), _SdkTick(symbol="SZSE.300750", price=9.3))        # 同 symbol 连续 tick

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
    rt.on_tick(_Ctx(), _SdkTick(symbol="SZSE.300750", price=11.2))

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
        rt.on_tick(_Ctx(), _SdkTick(symbol="SZSE.300750", price=10.0))
    fails = [w for w in _details(tmp_path, "WARN")
             if w.get("type") == "reconcile_orders_fail"]
    assert len(fails) == 1                   # 首 tick 失败一次；后两 tick 被退避闸拦下

    del fake.get_orders                                       # 撤注入：柜台恢复可用
    rt._absorb_retry_after = 0.0                              # 白盒推进：模拟退避窗已过（不真等 60s）
    rt.on_tick(_Ctx(), _SdkTick(symbol="SZSE.300750", price=10.0))
    assert rt._absorb_retry_after is None and rt._last_absorb is not None   # 成功复位双锚
    assert len([w for w in _details(tmp_path, "WARN")
                if w.get("type") == "reconcile_orders_fail"]) == 1          # 无新增失败


def test_on_tick_unmappable_symbol_warns_and_skips(pilot, tmp_path, monkeypatch):
    """符号折算 WARN-skip（终审 M-6）：tick 带试点外符号（期货所形态）→ 不炸巡检环，
    WARN 留痕跳过本事件；后续正常标的的 tick 照常处理（止损链不被单支异变劫持）。"""
    fake = FakeGm()
    _back_counter_position(fake, "SZSE.300750", 400, 10.4)
    st = pilot._initial_state()
    st["positions"]["300750.SZ"] = _managed_position(pilot, remaining=400)
    _pin(pilot, monkeypatch, tmp_path)
    rt = _rt(pilot, fake, tmp_path, state=st)

    rt.on_tick(_Ctx(), _SdkTick(symbol="CFFEX.IF2409", price=3300.0))      # 异变 tick：不抛即过
    warns = [w for w in _details(tmp_path, "WARN")
             if w.get("type") == "tick_symbol_unmappable"]
    assert len(warns) == 1 and warns[0]["symbol"] == "CFFEX.IF2409"
    assert [c for c in fake.calls if c.get("api") == "order_volume"] == []

    rt.on_tick(_Ctx(), _SdkTick(symbol="SZSE.300750", price=9.4))          # 正常 tick 照常止损
    sells = [c for c in fake.calls if c.get("api") == "order_volume"]
    assert len(sells) == 1 and sells[0]["side"] == 2                     # 巡检环未被劫持


def test_on_tick_invalid_price_warns_and_skips(pilot, tmp_path, monkeypatch):
    """tick 价防御（2026-08-26 首日实弹崩点回归钉）：异变价（0.0/None）→ WARN 留痕
    跳过本事件、绝不炸巡检环；后续正常 tick 照常止损。

    当日 09:33 实弹：首只脏 tick 触发本守卫后，报错路径的 tick.get(...) 在
    TickLikeDict2 上解析为 None（TypeError）——守卫自杀把策略整死。本测试用
    _SdkTick 真身形态（get=None），修复前必红；两分支各验一腿：0.0 走
    ValueError 腿、None 走 float() TypeError 腿，err 里须带原始异变值。
    """
    fake = FakeGm()
    _back_counter_position(fake, "SZSE.300750", 400, 10.4)
    st = pilot._initial_state()
    st["positions"]["300750.SZ"] = _managed_position(pilot, remaining=400)
    _pin(pilot, monkeypatch, tmp_path)
    rt = _rt(pilot, fake, tmp_path, state=st)

    rt.on_tick(_Ctx(), _SdkTick(symbol="SZSE.300750", price=0.0))       # 非正价：不抛即过
    rt.on_tick(_Ctx(), _SdkTick(symbol="SZSE.300750", price=None))      # None 价：float() 抛→同走 WARN
    warns = [w for w in _details(tmp_path, "WARN")
             if w.get("type") == "tick_price_invalid"]
    assert len(warns) == 2 and warns[0]["symbol"] == "SZSE.300750"
    assert "0.0" in warns[0]["err"] and "NoneType" in warns[1]["err"]
    assert [c for c in fake.calls if c.get("api") == "order_volume"] == []

    rt.on_tick(_Ctx(), _SdkTick(symbol="SZSE.300750", price=9.4))       # 正常 tick 照常止损
    sells = [c for c in fake.calls if c.get("api") == "order_volume"]
    assert len(sells) == 1 and sells[0]["side"] == 2                     # 巡检环未被劫持


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
    # R6-12：盘前/收盘移到行情确认期（09:31/15:36）+ 半点探针序列（schedule 可用性观测）
    assert scheds == {("pre_open_job", "1d", "09:31:00"),
                      ("after_close_job", "1d", "15:36:00"),
                      ("schedule_probe_job", "1d", "10:31:00"),
                      ("schedule_probe_job", "1d", "11:31:00"),
                      ("schedule_probe_job", "1d", "13:31:00"),
                      ("schedule_probe_job", "1d", "14:31:00"),
                      ("schedule_probe_job", "1d", "15:31:00")}
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


def test_bootstrap_verifies_context_accounts(pilot, tmp_path, monkeypatch):
    """context.accounts 核验（终审 M-1，Task 2 文档 Q1）：空表=故障 WARN（_set_accounts
    静默 return 空表——不是正常态）；白名单账户不在场=终端绑定异变 WARN；在场=零此类
    WARN。三形态一次钉死（WARN 不 raise：init 炸掉连 INIT 行都留不下，观测面更差）。"""
    def _boot(accounts_ctx, sub):
        fake = FakeGm()
        work = tmp_path / sub                                 # 三形态各自独立 workdir（audit 不串读）
        cfg_dir = work / "config"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "runtime.json").write_text(
            json.dumps({"token": "test-token", "strategy_id": "st-1",
                        "account_id": pilot.PILOT_ACCOUNT_ID}), encoding="utf-8")
        _pin(pilot, monkeypatch, work)
        pilot.PilotRuntime(fake, workdir=work).bootstrap(accounts_ctx)
        return [w for w in _details(work, "WARN")
                if w.get("type") in ("bootstrap_accounts_empty", "bootstrap_account_absent")]

    warns_empty = _boot(_CtxAccts({}), "empty")               # 空表：账户拉取失败形态
    assert len(warns_empty) == 1 and warns_empty[0]["type"] == "bootstrap_accounts_empty"
    warns_absent = _boot(_CtxAccts({"other-account": object()}), "absent")   # 白名单不在场
    assert len(warns_absent) == 1 and warns_absent[0]["type"] == "bootstrap_account_absent"
    warns_ok = _boot(_CtxAccts({pilot.PILOT_ACCOUNT_ID: object()}), "ok")    # 在场：零此类 WARN
    assert warns_ok == []


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


def test_after_close_unsubscribe_failure_still_clears_ledger(pilot, tmp_path, monkeypatch):
    """清订阅容错（终审 M-4）：unsubscribe 抛错（断连/已收市）→ WARN 留痕不炸盘后
    收尾，且账本【无论成败都清】——失败不清的后果是次日差集恒空（gm 侧订阅可能已
    死而账本记在场）= 巡检静默断供；清账本后次日 pre_open ⓪' 全量重订自洽恢复。"""
    fake = FakeGm()

    def _boom(symbols, frequency="1d"):
        raise RuntimeError('{"status": 1100, "message": "模拟退订失败（终端已断连）"}')

    fake.unsubscribe = _boom                                         # 实例级注入（只影响本替身）
    _pin(pilot, monkeypatch, tmp_path)
    rt = _rt(pilot, fake, tmp_path)
    rt._subscribed = ["300750.SZ"]
    rt.after_close(_Ctx())                                           # 不抛即过（EOD/state 不陪跳）

    warns = [w for w in _details(tmp_path, "WARN") if w.get("type") == "unsubscribe_fail"]
    assert len(warns) == 1 and warns[0]["symbols"] == ["300750.SZ"]
    assert rt._subscribed == []                                      # 失败也清账本（红线）
    assert "EOD" in _events(tmp_path)                                # 收尾行已落
    assert (tmp_path / "state" / "state.pkl").exists()               # state 落盘不跳


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
    """白名单账户直接放行：run(strategy_id/裸 filename/MODE_LIVE/token) 全参可审计。"""
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
    # filename 必须是裸文件名（2026-08-21 终端首启实测修正，见下一个回归用例的 Why）
    assert r["filename"] == "emquant_neckline_pilot.py"


def test_run_pilot_filename_is_bare_name_not_absolute(pilot, tmp_path, monkeypatch):
    """2026-08-21 终端首启实测回归：filename 传绝对路径在 Windows 盘符下必炸。

    gm run()（basic.py:574-587）先剥「与 sys.path 的公共前缀」再转模块名
    import_module——Windows 绝对路径的公共前缀常只剩盘符根，剥完剩
    `\\Users\\...` → 转点成前导点开头的名字 → import_module 误判**相对导入**
    抛 TypeError（终端实测 traceback 即此）。裸文件名走「脚本目录天然在
    sys.path[0]」的原生解析；import 出的副本 __name__ != '__main__'，
    顶部入口抑制守卫恰好令其不重入 run_pilot（无递归）。
    """
    fake = FakeGm()
    monkeypatch.setattr(pilot, "CONFIG_DIR", tmp_path)
    (tmp_path / "runtime.json").write_text(
        json.dumps({"token": "test-token", "strategy_id": "st-pilot",
                    "account_id": pilot.PILOT_ACCOUNT_ID}), encoding="utf-8")
    monkeypatch.setenv("PILOT_ALLOW_LIVE", "I_KNOW_REAL_MONEY")
    monkeypatch.setattr(pilot, "_GM", fake)
    pilot.run_pilot()
    r = next(c for c in fake.calls if c.get("api") == "run")
    fn = str(r["filename"])
    assert "/" not in fn and "\\" not in fn and not fn.startswith(".") and fn.endswith(".py")


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

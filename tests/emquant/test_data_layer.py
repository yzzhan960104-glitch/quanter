# -*- coding: utf-8 -*-
"""§2 数据层（组装产物口径）：符号映射 / 定点前复权取数 / (start,end] 交易日历。

物理定位：
    被测对象是【组装产物】emquant/emquant_neckline_pilot.py（改源 pilot_body.py 后
    必须先重跑 build_pilot.py 再跑本文件，与 test_state_and_gates 同范式）。import
    范式照抄：先注册 sys.modules 再 exec_module——§1 内核 Signal 是 dataclass +
    PEP563 字符串注解，dataclasses 解析注解要回查 sys.modules[cls.__module__]。

    gm 隔离（C4 红线）：不 import 真 gm——产物模块级 `_GM` monkeypatch 注入
    tests/emquant/fake_gm.py 的 FakeGm（签名/列名/常量按 Task 2 权威文档钉死，
    替身口径与真 SDK 的一致性本身就是对拍价值的一部分）。

口径锚点（断言期望值全部由此推出）：
    - history 契约（Task 2 文档 Q6/D8）：frequency='1d'、skip_suspended=True、
      df=True、fields 过滤、adjust=ADJUST_PREV(=1) 且 adjust_end_time=end_date
      （定点前复权【两端】都钉 end_date——adjust_end_time 缺省''会漂到"最新"基准，
      跨日重取同一 end_date 的数值不再确定）；
    - 取数根数 = 2×ID_PARAMS['window']+40（§0 快照 window=80 → 200 根：识别窗 +
      ATR/极值掩码预热 + 停牌冗余）；
    - eob→零点 DatetimeIndex：真 SDK 日线 eob 的时分秒口径未钉死（替身刻意带
      15:00:00 施压折算路径），本地腿 tushare trade_date 是零点——normalize 折算后
      formed_at/index 才逐字段可比（data_ctx.py:59 load_df_upto 的 .loc[:date]
      闭区间同口径含 end_date）；
    - (start,end] 语义逐字对齐 trading/compute/stop.py:22-70：不含 start 含 end、
      start/end 缺失或解析失败→0、ed<=sd→0；差异点：stop.py 历不可用退化自然日
      （保守多判超期），本函数空历返 0（历由调用方保证非空——见被测 docstring）。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pandas as pd
import pytest

# 替身导入走包路径（tests/conftest.py 已把项目根注入 sys.path；tests/emquant 有
# __init__.py，模块以包成员身份加载，裸 import fake_gm 在该布局下不可达）
from tests.emquant.fake_gm import ADJUST_PREV, FakeGm

ROOT = Path(__file__).resolve().parents[2]
ARTIFACT = ROOT / "emquant" / "emquant_neckline_pilot.py"
END = "2026-08-21"                       # 周五（交易日）——截断日锚点


def _import_artifact():
    """按文件位置 exec 组装产物为全新模块（范式与理由见模块 docstring）。"""
    spec = importlib.util.spec_from_file_location("emquant_pilot_data_layer", ARTIFACT)
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m          # 先注册再 exec：dataclass 注解回查依赖
    spec.loader.exec_module(m)
    return m


@pytest.fixture()
def pilot():
    """每用例全新 exec 一次产物——上一用例对 _GM/AUDIT_DIR 的 monkeypatch 零残留。"""
    return _import_artifact()


# ============================================================================
# 符号映射：ts 格式 ↔ gm 格式（未知市场 fail-loud）
# ============================================================================
def test_symbol_mapping_pairs(pilot):
    """brief 权威对 + universe 实际构成（创板科创 300/301→SZSE、688→SHSE）。"""
    assert pilot.to_gm_symbol("600000.SH") == "SHSE.600000"
    assert pilot.to_gm_symbol("000001.SZ") == "SZSE.000001"
    assert pilot.to_gm_symbol("300750.SZ") == "SZSE.300750"
    assert pilot.to_gm_symbol("688111.SH") == "SHSE.688111"
    assert pilot.from_gm_symbol("SHSE.600000") == "600000.SH"
    assert pilot.from_gm_symbol("SZSE.000001") == "000001.SZ"
    # 往返恒等（映射必须无信息损失——对拍两侧 symbol 可互推）
    for ts in ("600000.SH", "000001.SZ", "300308.SZ", "688981.SH"):
        assert pilot.from_gm_symbol(pilot.to_gm_symbol(ts)) == ts


def test_symbol_mapping_covers_universe(pilot):
    """§0 定稿 universe 全量可映射且往返恒等——260 只里任何一只是映射盲区即当场炸。"""
    assert len(pilot.UNIVERSE) >= 200                                   # 快照规模哨兵（快照换代先知）
    for ts in pilot.UNIVERSE:
        g = pilot.to_gm_symbol(ts)
        assert g.split(".")[0] in ("SHSE", "SZSE"), f"{ts} 映射出非沪深交易所：{g}"
        assert pilot.from_gm_symbol(g) == ts


def test_symbol_mapping_fail_loud(pilot):
    """未知市场/残缺格式 fail-loud：北交所/期货所/无后缀串绝不静默透传。

    Why fail-loud 而非返 None：映射错一只 = 下单/取数打到错误市场（真实资金面向
    错误标的），宁可在启动期炸给人工看；universe 导出侧已滤北交所，这里见到 .BJ
    说明上游过滤被绕过——本身就是须人工介入的异变。
    """
    for bad in ("830799.BJ", "430047.BJ", "600000", "", "600000.SH.XD", "600000.HK"):
        with pytest.raises(ValueError):
            pilot.to_gm_symbol(bad)
    for bad_gm in ("CFFEX.IF2409", "BJSE.830799", "SHFE.rb2410", "600000.SH", "", ".SHSE"):
        with pytest.raises(ValueError):
            pilot.from_gm_symbol(bad_gm)


# ============================================================================
# fetch_df_upto：形状/列/index/截断契约（api 显式传入路径）
# ============================================================================
def test_fetch_df_upto_contract(pilot):
    """形状/列/零点 index/尾部 200 根截断——数据层与识别内核的接口契约一次钉死。"""
    fake = FakeGm()
    df = pilot.fetch_df_upto(fake, "600000.SH", END)
    assert df is not None
    # 列恰五列且有序（识别内核按名取列；多余列 = 传输冗余，缺失列 = 内核 KeyError）
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    # 根数 = 2×window+40（§0 换代健壮：动态取 window）：识别窗 + ATR/极值预热 + 停牌跳空冗余
    assert len(df) == 2 * pilot.ID_PARAMS["window"] + 40
    # index：零点 DatetimeIndex 升序，末根 == 截断日（.loc[:date] 闭区间同口径）
    assert isinstance(df.index, pd.DatetimeIndex)
    assert df.index.is_monotonic_increasing
    assert all(t == t.normalize() for t in df.index)          # 15:00 eob 已折零点
    assert df.index[-1] == pd.Timestamp(END)
    # 数值面：OHLCV 全为正的有限数（合成价 sanity——NaN/inf 渗透会让 ATR 守卫静默拒）
    assert df.notna().all().all() and (df > 0).all().all()
    # 尾部截断（非头部）：500 自然日回看窗（_FETCH_LOOKBACK_DAYS 契约）内共 359 个
    # 营业日，取最后 200 根 → 首根 == 全序列倒数第 200 个营业日
    lookback_start = (pd.Timestamp(END) - pd.Timedelta(days=500)).strftime("%Y-%m-%d")
    all_bdays = pd.bdate_range(lookback_start, END)
    assert len(all_bdays) > 200                                       # 截断前提：可得根数富余
    _n = 2 * pilot.ID_PARAMS["window"] + 40
    assert df.index[0] == all_bdays[-_n]


def test_fetch_df_upto_call_params(pilot):
    """取数契约逐参钉死（Q6 签名）：'1d'/skip_suspended/df=True/fields/定点复权两端。

    adjust=ADJUST_PREV(1) 且 adjust_end_time=end_date【两端同钉】是本函数的对拍
    命门：adjust_end_time 缺省 '' 时前复权基准漂到「最新」，跨日重取同日数据会
    得到不同价格——对拍腿的数值可重复性就此瓦解。
    """
    fake = FakeGm()
    pilot.fetch_df_upto(fake, "688111.SH", END)
    assert len(fake.calls) == 1                                     # 单次调用（500 自然日日线 << 33000 服务端上限，无需分页）
    kw = fake.calls[0]
    assert kw["symbol"] == "SHSE.688111"                           # ts→gm 映射已就位
    assert kw["frequency"] == "1d"
    assert kw["end_time"] == END and kw["start_time"] < kw["end_time"]
    assert kw["adjust"] == ADJUST_PREV == 1                        # 定点前复权（FakeGm 常量与文档值互证）
    assert kw["adjust_end_time"] == END                            # 复权基准 == 截断日（两端同钉）
    assert kw["skip_suspended"] is True                            # 停牌日剔除（本地腿 tushare 同无停牌行）
    assert kw["df"] is True                                        # DataFrame 形态（List[Dict] 分支不进）
    assert set(str(kw["fields"]).split(",")) == {"eob", "open", "high", "low", "close", "volume"}


def test_fetch_df_upto_via_gm_seam(pilot, monkeypatch):
    """api=None → _api() seam 路径：monkeypatch 产物模块级 _GM 即注入替身（C4 红线范式）。"""
    fake = FakeGm()
    monkeypatch.setattr(pilot, "_GM", fake)
    df = pilot.fetch_df_upto(None, "300750.SZ", END)
    assert df is not None and len(fake.calls) == 1
    assert fake.calls[0]["symbol"] == "SZSE.300750"
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]


def test_fetch_df_upto_short_history_truncates_to_available(pilot):
    """标的可用根数 < 200（新上市/长停牌）：返回全部可得根数，不造数不报错。

    识别内核自带 len<window 守卫（method_v0 detect_signal 首守卫），根数不足由
    内核返 None——数据层职责是「如实传输」，不替内核做截断判断。替身用「上市日
    截短 start_time」模拟短史（不改 FakeGm 通用面）。
    """

    class _YoungStock(FakeGm):
        LISTED = "2026-06-01"                                       # 上市日晚于回看窗起点

        def history(self, symbol, frequency, start_time, end_time, **kw):
            return super().history(symbol, frequency, max(str(start_time), self.LISTED),
                                   end_time, **kw)

    df = pilot.fetch_df_upto(_YoungStock(), "301717.SZ", END)
    expected = len(pd.bdate_range("2026-06-01", END))               # 上市后全部营业日（<200）
    assert 0 < expected < 2 * pilot.ID_PARAMS["window"] + 40
    assert df is not None and len(df) == expected
    assert df.index[-1] == pd.Timestamp(END)                        # 短史末根仍是截断日


def test_fetch_df_upto_failure_returns_none_with_audit(pilot, monkeypatch, tmp_path):
    """gm 抛错（GmError/断连）→ None + audit WARN 留痕——编排层 None-check 跳过该标的。"""
    monkeypatch.setattr(pilot, "AUDIT_DIR", tmp_path / "audit")
    fake = FakeGm(raise_on_history=True)
    assert pilot.fetch_df_upto(fake, "600000.SH", END) is None
    csv = next(iter(tmp_path.glob("audit/audit_*.csv")))
    rows = csv.read_text(encoding="utf-8").splitlines()
    assert any("WARN" in r and "fetch_fail" in r and "600000.SH" in r for r in rows)


def test_fetch_df_upto_audit_failure_does_not_blow(pilot, monkeypatch):
    """审计写失败不得炸数据层：audit_log 异常被接住降级 print，取数失败仍返 None。

    Why：取数失败路径里再抛审计异常，会把「返 None 降级跳过」恶化成「编排崩溃」
    ——磁盘满/目录被锁时策略整轮停摆，而契约只是失败可观测（WARN 或 print）。
    """
    def _boom(event, audit_dir=None, **fields):
        raise OSError("模拟磁盘满：audit 写失败")
    monkeypatch.setattr(pilot, "audit_log", _boom)
    fake = FakeGm(raise_on_history=True)
    assert pilot.fetch_df_upto(fake, "600000.SH", END) is None      # 不抛 = 契约成立


def test_fetch_df_upto_empty_returns_none(pilot, monkeypatch, tmp_path):
    """空结果（查无/退市/新上市未满一日）→ None + WARN——空 df 不是可识别输入。"""
    monkeypatch.setattr(pilot, "AUDIT_DIR", tmp_path / "audit")
    fake = FakeGm(empty_symbols={"SHSE.999999"})
    assert pilot.fetch_df_upto(fake, "999999.SH", END) is None
    rows = next(iter(tmp_path.glob("audit/audit_*.csv"))).read_text(encoding="utf-8").splitlines()
    assert any("fetch_empty" in r for r in rows)


def test_fetch_df_upto_missing_last_bar_returns_none_with_audit(pilot, monkeypatch, tmp_path):
    """末根不变量（终审 I-2）：history 返回缺 end_date 末根的 df → None + WARN。

    Why 拦：end_time 端性若被服务端当开区间/时刻边界处理，少了最新一根的序列仍能
    过识别内核（在偏一日的 formed_at 上算信号或静默零信号）——条件性静默比取数
    失败更难察觉，三行防御让故障进 audit 晨检面（fetch_end_missing）。
    """

    class _MissingTail(FakeGm):
        """history 剥掉末行——模拟 end_time 端性异变（返回序列恰缺 end_date 当日 bar）。"""

        def history(self, symbol, frequency, start_time, end_time, **kw):
            frame = super().history(symbol, frequency, start_time, end_time, **kw)
            return frame.iloc[:-1] if len(frame) else frame

    monkeypatch.setattr(pilot, "AUDIT_DIR", tmp_path / "audit")
    df = pilot.fetch_df_upto(_MissingTail(), "300750.SZ", END)
    assert df is None
    rows = next(iter(tmp_path.glob("audit/audit_*.csv"))).read_text(encoding="utf-8").splitlines()
    hit = [r for r in rows if "fetch_end_missing" in r]
    assert len(hit) == 1 and "300750.SZ" in hit[0] and END in hit[0]


# ============================================================================
# build_calendar：指数日线推导交易日历
# ============================================================================
def test_build_calendar_from_index_daily(pilot, monkeypatch):
    """SHSE.000300 日线 eob → 升序去重 YYYY-MM-DD 列表（少一层日历 API 口径转换）。"""
    monkeypatch.setattr(pilot, "_GM", FakeGm())
    cal = pilot.build_calendar(None, END, lookback_days=60)
    assert cal and cal == sorted(set(cal))                          # 升序 + 无重复
    assert all(len(d) == 10 and d[4] == "-" and d[7] == "-" for d in cal)   # YYYY-MM-DD 定长
    assert cal[-1] == END                                           # 含截断日（.loc[:date] 同口径）
    # 自然日窗口 [END-60, END] 内营业日全集（替身 bdate 口径的确定性期望）
    start = (pd.Timestamp(END) - pd.Timedelta(days=60)).strftime("%Y-%m-%d")
    assert cal == [t.strftime("%Y-%m-%d") for t in pd.bdate_range(start, END)]
    # 取数走指数符号 + eob 单列（fields 契约）
    kw = pilot._GM.calls[0]
    assert kw["symbol"] == "SHSE.000300" and kw["frequency"] == "1d"
    assert str(kw["fields"]) == "eob" and kw["df"] is True


def test_build_calendar_failure_returns_empty_with_audit(pilot, monkeypatch, tmp_path):
    """指数取数失败 → [] + WARN：空历由编排层（Task 8）显式降级，不在本层兜底造历。"""
    monkeypatch.setattr(pilot, "AUDIT_DIR", tmp_path / "audit")
    monkeypatch.setattr(pilot, "_GM", FakeGm(raise_on_history=True))
    assert pilot.build_calendar(None, END) == []
    rows = next(iter(tmp_path.glob("audit/audit_*.csv"))).read_text(encoding="utf-8").splitlines()
    assert any("cal_fetch_fail" in r for r in rows)


# ============================================================================
# trading_days_between：(start, end] 口径（逐字对齐 stop.py:22-70 的可移植重述）
# ============================================================================
# 固定日历：2026-08-03(周一) ~ 2026-08-21(周五)，剔除周末 08-08/09、08-15/16
CAL = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07",
       "2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14",
       "2026-08-17", "2026-08-18", "2026-08-19", "2026-08-20", "2026-08-21"]


def test_trading_days_between_half_open(pilot):
    """(start, end]：不含 start 含 end——「start 当日形成，end 今日是否已到」的持有计数。"""
    assert pilot.trading_days_between(CAL, "2026-08-11", "2026-08-18") == 5   # 12,13,14,17,18
    assert pilot.trading_days_between(CAL, "2026-08-03", "2026-08-10") == 5   # 04,05,06,07,10
    assert pilot.trading_days_between(CAL, CAL[0], CAL[-1]) == len(CAL) - 1   # 首尾全窗（首日不计）
    assert pilot.trading_days_between(CAL, "2026-08-14", "2026-08-17") == 1   # 跨周末只数 17（16,15 非历日）
    # start 非历日（周六）：区间左开语义下 (15, 18] = 17,18——start 不须在历上
    assert pilot.trading_days_between(CAL, "2026-08-15", "2026-08-18") == 2
    # end 非历日（周六）：右闭落在非历日 = 区间内历日全计（17 > 16 不入区间）
    assert pilot.trading_days_between(CAL, "2026-08-13", "2026-08-16") == 1   # 仅 14


def test_trading_days_between_tolerances(pilot):
    """边界容错逐字对齐 stop.py：ed<=sd→0；start/end 缺失·解析失败→0；空历→0。"""
    assert pilot.trading_days_between(CAL, "2026-08-18", "2026-08-18") == 0   # 同日（ed==sd）
    assert pilot.trading_days_between(CAL, "2026-08-18", "2026-08-11") == 0   # 倒序（ed<sd）
    for bad in (None, "", "not-a-date", "2026/08/11", 20260811, "..."):
        assert pilot.trading_days_between(CAL, bad, "2026-08-18") == 0        # start 坏 → 0
        assert pilot.trading_days_between(CAL, "2026-08-11", bad) == 0        # end 坏 → 0
    # 空历 → 0：与 stop.py 的自然日兜底【刻意不同】——单文件无仓库 calendar 模块
    # （C3），且「不知道就当未超期」的 fail-open 后果由调用方（Task 8 对空历显式
    # 降级）承担，本函数保持纯函数可测不做隐性兜底。
    assert pilot.trading_days_between([], "2026-08-11", "2026-08-18") == 0
    assert pilot.trading_days_between(None, "2026-08-11", "2026-08-18") == 0


def test_trading_days_between_today_not_in_calendar_counts(pilot):
    """「今日不在历」补计 +1（终审 I-1）：盘中恒态——指数 bar 只到最近已收盘日，
    end=今日 ∉ cal；不补计则 max_wait/cooldown/holding_days 比本地（全量 trade_cal
    当日计入，pre_open.py:574 / engine.py:1315）系统性少一日。

    断言形态：缺今日的历与含今日的历【同值】——补计后两份历在同样的 (start, end]
    查询下不可区分（对齐本地「end 计入」口径的直接表达）。
    """
    CAL_NO_TODAY = CAL[:-1]                      # 模拟盘中 build_calendar：末根=昨日 T-1
    assert CAL[-1] not in CAL_NO_TODAY           # 前提：今日确不在缺尾历上
    for start in ("2026-08-11", "2026-08-14", "2026-08-20"):
        assert (pilot.trading_days_between(CAL_NO_TODAY, start, CAL[-1])
                == pilot.trading_days_between(CAL, start, CAL[-1]))
    # 手算锚：含今日历上 (08-11, 08-21] = 12,13,14,17,18,19,20,21 共 8 日——缺尾历
    # 计 7 + 补计 1 = 8
    assert pilot.trading_days_between(CAL_NO_TODAY, "2026-08-11", "2026-08-21") == 8
    # end 在历内（盘后/回放）零影响：end ≤ cal[-1] 不触发补计，既有口径不漂移
    assert pilot.trading_days_between(CAL_NO_TODAY, "2026-08-11", "2026-08-20") \
        == pilot.trading_days_between(CAL, "2026-08-11", "2026-08-20") == 7


def test_prev_trading_day_regression_today_not_in_calendar(pilot):
    """_prev_trading_day 回归（终审 I-1 伴随）：今日不在历（盘中恒态）→ prev =
    cal[-1] = 真实 T-1——补计逻辑不得外溢到本函数（它是纯 bisect_left 取「< today
    的最大者」，pre_open ②③ 的 T-1 基准日依赖此语义）。"""
    CAL_NO_TODAY = CAL[:-1]                      # 末根 2026-08-20 = 盘中场景的真实 T-1
    assert pilot._prev_trading_day(CAL_NO_TODAY, "2026-08-21") == "2026-08-20"
    # 今日在历（盘后场景）：前一根仍是同一 T-1——两场景 T-1 同值（口径稳定）
    assert pilot._prev_trading_day(CAL, "2026-08-21") == "2026-08-20"
    # 周末补跑：周六/周日问 T-1 → 同取周五 08-21
    assert pilot._prev_trading_day(CAL, "2026-08-22") == "2026-08-21"
    assert pilot._prev_trading_day(CAL, "2026-08-23") == "2026-08-21"
    # 边界：历空 / 今日 ≤ 历首 → None（调用方显式降级）
    assert pilot._prev_trading_day([], "2026-08-21") is None
    assert pilot._prev_trading_day(CAL, CAL[0]) is None

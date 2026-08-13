# -*- coding: utf-8 -*-
"""外部 SDK 超时注入单测（Task G4 · 韧性链复活）。

物理意图（Why 此文件存在）：
  tushare pro_api / fredapi / xtdata 底层走 requests / C++ 同步调用，【默认无 timeout】。
  当对端 TCP 挂起（半开连接 / 对端 hold 住不回 FIN / GFW 注入 RST-阻断）时：
    - requests.read() 阻塞在 socket.recv，【不抛异常】——上层 CircuitBreaker / 退避重试
      永远等不到异常触发 → 韧性链被旁路（熔断不跳闸、退避不重试、record_failure 不计）。
    - xtdata C++ 调用在 run_in_executor 里挂起 → 主事件循环虽然不阻塞，但 await 永远不
      返回 → stop_loss_monitor / risk_shield 看不到行情，行情缺失等同风控失能。

  本测试钉死 4 个注入点的行为契约：
    ① data._tushare_compat._call_with_timeout：挂起 fn → TimeoutError；正常 fn → 原值透传；
       异常 fn → 异常透传（非 TimeoutError 的异常不得被吞）。
    ② broker.qmt_quote.get_quotes：xtdata 挂起 → asyncio.wait_for 超时 → 全 None 降级
       （与"行情缺失跳过"语义一致）。
    ③ data.tushare_sync._fetch_with_guard：pro 调用挂起 → TimeoutError → _classify_exc 归
       transient（"超时" 关键词命中）→ 走退避重试链（最终 record_failure 触发熔断）。
    ④ data.calendar.fetch_trade_cal：trade_cal 挂起 → TimeoutError 被 broad except 捕获 →
       weekday 兜底（与"无 token / 网络失败"语义同口径）。
    ⑤ data.fetcher.FredDataFetcher._fetch_series_from_api：get_series 挂起 → TimeoutError
       → fetch_macro except 捕获 → record_failure + 空 DF（基础设施异常口径）。

  测试加速：所有用例 monkeypatch 把 30s/5s 默认超时降到 0.3~0.5s（不真等），并 mock
  time.sleep 为 no-op（退避重试路径不睡）。
"""
import asyncio
import threading
import time
import types

import pandas as pd
import pytest


def _make_hangfn(delay: float = 30.0):
    """构造一个【不受 time.sleep mock 影响】的挂起 fn（模拟 TCP 挂起）。

    Why 用 threading.Event().wait 而非 time.sleep：tushare_sync 退避重试路径里 mock 了
    `tushare_sync.time.sleep`——而 `tushare_sync.time` IS 全局 time 模块，patch 它会同时
    让本测试 _hang 的 time.sleep 也变 no-op，导致 _hang 立即返 None 走"空数据路径"
    （而非挂起→TimeoutError）。Event.wait 是不同原语，不受 time.sleep mock 影响。
    """
    def _hang(*args, **kwargs):
        threading.Event().wait(delay)
    return _hang


# ============================================================================
# ① _call_with_timeout 单元契约（data._tushare_compat）
# ============================================================================


def test_call_with_timeout_raises_on_hang(monkeypatch):
    """挂起 fn → 抛 TimeoutError（不永远等）。

    场景：fn 内部阻塞 60s（模拟 TCP 挂起——socket.recv 阻塞等不到数据）。
    期望：_call_with_timeout 在 monkeypatch 的小 timeout（0.3s）后抛 TimeoutError，
    而非等到 60s 真睡完。这是"挂起可观测化"的核心——让上层 except 能捕获。
    """
    import data._tushare_compat as tc
    monkeypatch.setattr(tc, "_CALL_TIMEOUT", 0.3)

    with pytest.raises(TimeoutError):
        tc._call_with_timeout(_make_hangfn(60))


def test_call_with_timeout_normal_returns_value(monkeypatch):
    """正常 fn → 原值透传（超时包裹不得污染正常路径）。

    场景：fn 立即返值。期望 _call_with_timeout 透传返回值，不抛、不改。
    钉死契约：30s 兜底仅对挂起生效，正常调用零开销（线程池 submit+result 极快）。
    """
    import data._tushare_compat as tc

    def _ok(x, *, y):
        return x + y

    assert tc._call_with_timeout(_ok, 1, y=2) == 3


def test_call_with_timeout_propagates_exception(monkeypatch):
    """fn 抛非超时异常 → 原异常透传（不得吞成 TimeoutError 或被掩盖）。

    场景：fn 抛 ValueError（模拟 SDK 业务异常，如积分不足/解析错）。期望 ValueError
    原样向上抛——上层 _classify_exc / FRED except 依赖异常类型与消息做分类决策，
    若被 _call_with_timeout 包成 TimeoutError 会误归 transient 触发无意义退避。
    """
    import data._tushare_compat as tc

    def _boom():
        raise ValueError("积分不足 permission denied")

    with pytest.raises(ValueError, match="积分不足"):
        tc._call_with_timeout(_boom)


def test_call_with_timeout_explicit_timeout_override(monkeypatch):
    """显式 timeout 参数覆盖默认 _CALL_TIMEOUT（per-call 调用方自定义阈值）。"""
    import data._tushare_compat as tc
    # 模块默认保持大值（30s），显式传小 timeout 应优先于默认
    monkeypatch.setattr(tc, "_CALL_TIMEOUT", 30.0)

    with pytest.raises(TimeoutError):
        tc._call_with_timeout(_make_hangfn(5), timeout=0.3)


# ============================================================================
# ② get_quotes 超时降级（broker.qmt_quote）
# ============================================================================


def test_get_quotes_timeout_returns_all_none(monkeypatch):
    """xtdata.get_full_tick 挂起 → asyncio.wait_for 超时 → 全 None 降级（不抛、不等）。

    场景：xtdata.get_full_tick 内部 sleep 5s（模拟 C++ 同步调用挂起——QMT 进程卡死/
    柜台网络断开但 TCP 未断）。期望 get_quotes 在 _QMT_CALL_TIMEOUT（测试降到 0.5s）内
    返 {symbol: None}，而非 await 5s 阻塞 stop_loss_monitor 巡查循环。

    Why 全 None 而非抛：与既有"行情缺失跳过"语义同口径——risk_shield 第9关见 None 即
    跳过涨跌停校验，stop_loss_monitor 见 None 即跳过现价检查，主路径不阻断。
    """
    from broker import qmt_quote as md
    # 测试加速：5s 默认降到 0.5s（不真等 5s）
    monkeypatch.setattr(md, "_QMT_CALL_TIMEOUT", 0.5)
    md._LIMIT_PRICE_CACHE.clear()

    def _hang_tick(codes):
        # threading.Event 不受 time.sleep mock 影响（qmt_quote 测试未 mock time.sleep，
        # 但保持与其他 hang 测试一致的隔离原语，防未来 mock 漂移）
        threading.Event().wait(5)

    fake = types.SimpleNamespace(get_full_tick=_hang_tick, get_instrument_detail=lambda c: {})
    monkeypatch.setattr(md, "xtdata", fake)
    monkeypatch.setattr(md, "_XTDATA_AVAILABLE", True)

    async def run():
        return await md.get_quotes(["600000.SH", "000001.SZ"])

    t0 = time.monotonic()
    r = asyncio.run(run())
    elapsed = time.monotonic() - t0
    # 全 None 降级（每标的显式 None，不漏键防下游 KeyError）
    assert r == {"600000.SH": None, "000001.SZ": None}
    # 实际超时应在 ~0.5s 返回，远小于挂起的 5s（留 2s 上限防 CI 慢机器 flaky）
    assert elapsed < 2.0, f"应在 ~0.5s 超时降级，实际 {elapsed:.2f}s"


# ============================================================================
# ③ _fetch_with_guard 超时归类 transient（data.tushare_sync）
# ============================================================================


def test_fetch_with_guard_timeout_classified_transient(monkeypatch):
    """pro 调用挂起 → TimeoutError → _classify_exc 归 transient → 退避重试链触发。

    场景：pro.api_name 内部 sleep 挂起（模拟 tushare TCP 半开）。期望：
      ① _call_with_timeout 抛 TimeoutError；
      ② _classify_exc 按 "超时" 关键词归 transient（不是 unknown/persistent）；
      ③ 走退避重试链（mock time.sleep no-op 不真睡）；
      ④ 退避耗尽后 record_failure 一次 + 返空 DF（韧性链复活——熔断最终能跳闸）。

    Why 关键：原 bug 是挂起永远不返 → record_failure 永远不被调 → 熔断永远不 OPEN
    → 同一接口挂起一直拖累同步主循环。本测试钉死"挂起最终能触发 record_failure"。
    """
    import data._tushare_compat as tc
    from data import tushare_sync

    # 测试加速：把默认 30s 超时降到 0.3s，退避 time.sleep mock 成 no-op
    monkeypatch.setattr(tc, "_CALL_TIMEOUT", 0.3)
    monkeypatch.setattr(tushare_sync.time, "sleep", lambda s: None)

    # 真实 breaker 计数（验证 record_failure 被调）
    fail_count = {"n": 0}
    monkeypatch.setattr(tushare_sync.tushare_breaker, "allow_request", lambda: True)
    monkeypatch.setattr(tushare_sync.tushare_breaker, "record_success", lambda: None)
    monkeypatch.setattr(tushare_sync.tushare_breaker, "record_failure",
                        lambda: fail_count.__setitem__("n", fail_count["n"] + 1))
    monkeypatch.setattr(tushare_sync.tushare_rate_limiter, "acquire",
                        lambda n=1.0, timeout=None: None)

    # pro 替身：永远挂起（模拟 TCP 半开）——用 Event.wait 防 time.sleep mock 误伤
    _hang = _make_hangfn(30)
    class _HangPro:
        def __getattr__(self, name):
            return _hang
    monkeypatch.setattr(tushare_sync, "get_pro", lambda: _HangPro())

    df = tushare_sync._fetch_with_guard("moneyflow", trade_date="20240105")
    # 退避耗尽后返空
    assert df.empty
    # 关键断言：record_failure 被调（韧性链复活——原 bug 是永远不调）
    assert fail_count["n"] >= 1, "挂起应最终触发 record_failure（熔断链复活）"


# ============================================================================
# ④ fetch_trade_cal 超时降级 weekday 兜底（data.calendar）
# ============================================================================


def test_calendar_trade_cal_timeout_weekday_fallback(monkeypatch, tmp_path):
    """trade_cal 挂起 → TimeoutError 被 broad except 捕获 → weekday 兜底返非空 list。

    场景：pro.trade_cal 内部 sleep 挂起。期望：
      ① _call_with_timeout 抛 TimeoutError；
      ② calendar 的 broad except Exception 捕获（与"无 token / 网络失败"同口径）；
      ③ 落到 _weekday_fallback 返全年非周末 list（仅识周末，不识节假日——降级语义）。

    Why 此测试：calendar 在启动期被调用（盘前判断交易日），挂起会卡死启动流程。
    本测试钉死"挂起不再卡死，落到 weekday 兜底让启动继续"。
    """
    import data._tushare_compat as tc
    from data import calendar as cal

    monkeypatch.setattr(tc, "_CALL_TIMEOUT", 0.3)
    # 缓存目录隔离到 tmp_path，避免读到真实 logs/trade_cal_*.json
    monkeypatch.setattr(cal, "_CACHE_DIR", tmp_path)

    class _HangPro:
        trade_cal = _make_hangfn(30)
    # calendar.py 在函数内 lazy import get_pro（`from data._tushare_compat import get_pro`），
    # 故 patch 须指源模块（每次函数调用都会重新 import 拿到 patch 后的版本）。
    monkeypatch.setattr("data._tushare_compat.get_pro", lambda: _HangPro())

    days = cal.fetch_trade_cal(2024)
    # weekday 兜底返非空 list（2024 全年非周末 ~261 天）
    assert len(days) > 0
    # 兜底只识周末不识节假日，故包含 2024-01-02（非周末，实际是元旦翌日但 weekday 不识）
    assert "2024-01-02" in days


# ============================================================================
# ⑤ FRED _fetch_series_from_api 超时 → fetch_macro record_failure（data.fetcher）
# ============================================================================


def test_fred_fetch_series_timeout_records_failure(monkeypatch):
    """FRED get_series 挂起 → TimeoutError → fetch_macro record_failure + 返空 DF。

    场景：self._fred.get_series 内部 sleep 挂起（模拟 FRED API TCP 挂起）。
    期望：
      ① _call_with_timeout 抛 TimeoutError（消息含 "timeout" 英文关键词）；
      ② fetch_macro except 捕获，error_msg 匹配 "timeout" → 归基础设施异常 → record_failure；
      ③ 返回空 DF（保留既有"绝不抛"契约）。

    Why 关键：FRED 原无 timeout 兜底，挂起会卡死宏观数据拉取循环。本测试钉死
    "挂起能被 timeout 打断 + 熔断计数 + 空降级"三段式韧性链。
    """
    import data._tushare_compat as tc
    from data import fetcher
    from data.resilience import fred_breaker

    monkeypatch.setattr(tc, "_CALL_TIMEOUT", 0.3)
    # 隔离 fred_breaker 计数（conftest 已 reset，此处 spy 计数）
    fail_count = {"n": 0}
    monkeypatch.setattr(fred_breaker, "allow_request", lambda: True)
    monkeypatch.setattr(fred_breaker, "record_success", lambda: None)
    monkeypatch.setattr(fred_breaker, "record_failure",
                        lambda: fail_count.__setitem__("n", fail_count["n"] + 1))
    # 缓存必须 miss（否则走缓存不触达超时路径）
    monkeypatch.setattr(fetcher, "_read_parquet_cache", lambda *a, **k: None)

    # 构造 FredDataFetcher 实例：绕过 __init__ 的 fredapi 真实 import，手动注入挂起 _fred
    inst = object.__new__(fetcher.FredDataFetcher)
    class _HangFred:
        get_series = _make_hangfn(30)
    inst._fred = _HangFred()
    inst._cache = {}

    from datetime import datetime
    df = inst.fetch_macro("DGS10", datetime(2024, 1, 1), datetime(2024, 1, 31))
    # 返空 DF（绝不抛）
    assert df.empty
    # 关键断言：record_failure 被调（超时归类为基础设施异常）
    assert fail_count["n"] == 1, "FRED get_series 挂起应触发 record_failure"

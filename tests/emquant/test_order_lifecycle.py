# -*- coding: utf-8 -*-
"""§5 订单工具 + trailing 逐句移植 + 生命周期判定（组装产物口径）。

物理定位：
    被测对象是【组装产物】emquant/emquant_neckline_pilot.py 的 §5 段——改源
    pilot_body.py 后必须先重跑 emquant/build_pilot.py 再跑本文件（与 test_state_
    and_gates / test_data_layer 同范式）。import 范式照抄：先注册 sys.modules 再
    exec_module（§1 内核 Signal 是 dataclass + PEP563 字符串注解，不注册则 exec
    当场 AttributeError(NoneType)）。

    gm 隔离（C4 红线）：不 import 真 gm——订单族调用走 FakeGm（Task 7 扩全：签名/
    返回形态/状态词汇按 Task 2 权威文档 Q4/Q5/Q7 钉死），直接以实参注入 §5 的
    api 形参（§5 函数自带 `api=None → _api()` 惰性 seam，显式传参即离线）。

口径锚点（C9 红线——断言期望值全部由此推出，非拍脑袋）：
    - compute_stop_price 逐句移植真身：strategies/neckline/execution.py:47-72
      （golden 对拍直接 import 仓库真身逐位相等）；
    - decide_pending：cancel_on 触价（tick≥order["cancel_on"]，decide_exit pending
      分支 simulate_exit:130 同式含等）；max_wait 严格大于（(formed_at,today] 交易日
      数 > max_wait 才撤，== 不撤——backtest MAX_WAIT 窗口边界 range(signal_idx+1,
      min(signal_idx+max_wait,...)+1) 的「窗口内含第 max_wait 日」语义；锚点是真身
      的 signal_idx（backtest.py:177-179，信号日起算），buy_idx 是窗内成交日、
      窗口边界不以成交日起算）；
    - decide_position 优先序 stop→tp2→tp1：strategies/neckline/execution.py:249-294
      （priority 1 止损硬风控先于止盈；priority 2 tp2 全平；priority 3 tp1 一档一次）；
    - tp1 档量取整：trading/phases/exit.py:190 tp1_target=int(total×portion/100)*100
      向下整手；不足一手（floor=0）本档卖全部剩余（brief 钉死——单仓一次性模型防
      零股残留，两腿模型对照见 exit.py:190-193 的「份额沉到另一档」语义）；
    - absorb_reality：柜台有 state 无→吸收；state 有柜台无且非终态→CANCELLED
      （PENDING/SUBMITTED/PARTIAL_FILLED 非终态、FILLED/CANCELLED/PARTIAL_CANCELLED/
      REJECTED/FAILED 终态——trading/types/order_state.py 状态词汇 + state_store.py:69
      死态集）；持仓 qty 以柜台为准、entry/exec_params 以 state 保留（信号定终身）。
"""
from __future__ import annotations

import copy
import importlib.util
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

# 替身导入走包路径（tests/conftest.py 已把项目根注入 sys.path；tests/emquant 有
# __init__.py，模块以包成员身份加载，裸 import fake_gm 在该布局下不可达）
from tests.emquant.fake_gm import ORDER_STATUS, FakeGm

ROOT = Path(__file__).resolve().parents[2]
ARTIFACT = ROOT / "emquant" / "emquant_neckline_pilot.py"

# 固定交易日历（周一~周五两组，跳过 08-15/16 周末）：decide_pending/decide_position
# 的 holding_days/max_wait 断言全靠它推出——跨周末日数差是「自然日 vs 交易日」口径
# 分叉的天然试金石（trading_days_between 是 (start,end] 半开区间，见 §2 头注）。
CAL = ["2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14",
       "2026-08-17", "2026-08-18", "2026-08-19", "2026-08-20", "2026-08-21"]


def _import_artifact():
    """按文件位置 exec 组装产物为全新模块（范式与理由见模块 docstring）。"""
    spec = importlib.util.spec_from_file_location("emquant_pilot_order_lifecycle", ARTIFACT)
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m          # 先注册再 exec：dataclass 注解回查依赖
    spec.loader.exec_module(m)
    return m


@pytest.fixture()
def pilot():
    """每用例全新 exec 一次产物——上一用例对模块常量的 monkeypatch 零残留。"""
    return _import_artifact()


# ============================================================================
# golden 对拍：compute_stop_price 逐句移植真身（strategies/neckline/execution.py:47-72）
# ============================================================================
# 仓库真身引用（只活在测试文件——产物自身严禁 import 仓库模块，C3 单文件纪律）。
# 缓存壳（成功缓存真身 / 失败缓存 None 哨兵，单次定性全会话一致）：
# Why 连失败也要缓存——strategies/neckline/__init__.py 在 line 43（schema→pydantic）
# 炸之前已把 execution 子模块完整导入并留在 sys.modules（部分导入泄漏），首次失败后
# 的重试会「意外成功」拿到真身——546 格参数化会呈现 1 skip + 545 pass 的无规律分裂
# （.venv_emquant 实测复现）。单次定性后统一全跳/全跑，行为可预测、报告可读。
_REPO_CSP_CACHE: list = []

_GOLDEN_SKIP_REASON = (
    ".venv_emquant 侧无仓库依赖（strategies 包链拉不动：pydantic 等缺位），golden 对拍"
    "仅 .venv310 有效——属预期跳过；退化态数值覆盖见 test_compute_stop_price_degenerate")


def _repo_csp_or_skip():
    """取仓库真身 compute_stop_price；.venv_emquant 侧无仓库依赖则跳过（预期行为）。

    Why importorskip：该 golden 用例的价值是「与真身逐位相等」，真身不可导入的环境
    （.venv_emquant 只有 gm+pandas，无仓库依赖链）对拍无从谈起——跳过并注明原因，
    绝不在缺真身的环境里拿手算值冒充对拍（那测的是测试自己）。
    注：tests/emquant/ 全目录套跑时，先行用例（test_kernel_equivalence 的真身导入
    尝试）会把 execution 叶模块泄入 sys.modules——届时本组用例会拿【真身】实跑对拍
    （比较是真实的，非降级）；单文件/-k 过滤跑则按上述预期全跳。两种形态都正确。
    """
    if _REPO_CSP_CACHE:
        got = _REPO_CSP_CACHE[0]
        if got is None:
            pytest.skip(_GOLDEN_SKIP_REASON)     # 首试已定性失败：全会话统一跳（不再重试）
        return got
    try:
        execution = pytest.importorskip(
            "strategies.neckline.execution", reason=_GOLDEN_SKIP_REASON)
        _REPO_CSP_CACHE.append(execution.compute_stop_price)
    except BaseException:                        # importorskip 的 Skipped 走此（不上抛吞错，仅记哨兵）
        _REPO_CSP_CACHE.append(None)
        raise                                    # 首次失败仍原样上抛（保留完整诊断面）
    return _REPO_CSP_CACHE[0]


# 网格三轴（覆盖：全退化 grace=0 / 实弹快照形态 / 活跃 trailing 收紧 / 无 floor /
# floor 卡底 / 大 grace 跨界），holding_days 0..25 全扫——grace 边界（恰好 == grace
# 走 base_stop、> grace 起收紧）与 step 累积越界（eff_mult 负值穿颈线上方）都在网内。
_GRID_NAMAT = [(100.0, 2.5, 1.0), (13.37, 0.7, 1.5), (50.0, 4.0, 0.5)]
_TRAILING_COMBOS = [
    (0, 0.0, None),    # 全退化：grace=0 → 恒 base_stop（固定止损）
    (0, 0.0, 0.0),     # 实弹快照形态（trailing_grace=0/step=0.0/floor=0.0）→ 仍退化
    (5, 0.1, 0.5),     # 活跃 trailing（trade_cfg 历史形态）
    (3, 0.25, None),   # 无 floor：eff_mult 可收紧到负值（止损穿颈线上方）
    (5, 0.1, None),    # 无 floor 长 grace
    (2, 0.5, 0.75),    # floor 卡底（eff_mult 触 0.75 下限）
    (10, 0.05, 0.3),   # 大 grace：0..10 全 base_stop，之后缓收紧
]


@pytest.mark.parametrize("holding_days", range(26))
@pytest.mark.parametrize("grace,step,floor", _TRAILING_COMBOS)
@pytest.mark.parametrize("neckline,atr,mult", _GRID_NAMAT)
def test_compute_stop_price_golden(neckline, atr, mult, grace, step, floor,
                                   holding_days, pilot):
    """与仓库真身逐位相等——逐句移植的数学等价铁证（表达式序一致 ⇒ 浮点位位一致）。"""
    repo_csp = _repo_csp_or_skip()
    got = pilot.compute_stop_price(neckline=neckline, atr=atr, holding_days=holding_days,
                                   stop_atr_mult=mult, grace=grace, step=step, floor=floor)
    want = repo_csp(neckline=neckline, atr=atr, holding_days=holding_days,
                    stop_atr_mult=mult, grace=grace, step=step, floor=floor)
    assert got == want, (
        f"trailing 移植漂移：neckline={neckline} atr={atr} mult={mult} "
        f"grace={grace} step={step} floor={floor} days={holding_days} "
        f"got={got!r} want={want!r}")


@pytest.mark.parametrize("holding_days", range(26))
@pytest.mark.parametrize("grace,step,floor", [(0, 0.0, None), (0, 0.0, 0.0), (5, 0.0, 0.5)])
def test_compute_stop_price_degenerate(holding_days, grace, step, floor, pilot):
    """退化态=固定止损（grace=0 或 step=0 → 恒 base_stop=颈线−mult×ATR）。

    双环境可跑（不依赖仓库真身）：实弹快照 trailing 0/0.0/0.0 正是此形态——双轨
    一致性的要求就是「实弹参数下 trailing 退化为本地现状的固定止损」。
    """
    got = pilot.compute_stop_price(neckline=100.0, atr=2.5, holding_days=holding_days,
                                   stop_atr_mult=1.0, grace=grace, step=step, floor=floor)
    assert got == 100.0 - 1.0 * 2.5     # base_stop 逐位（同表达式序手算）


@pytest.mark.parametrize(
    "grace,step,floor,days,want",
    [
        (3, 0.25, None, 3, 98.0),    # 恰好 == grace：仍 base_stop（100−1×2）
        (3, 0.25, None, 4, 98.5),    # 超 1 日：eff=1−0.25 → 100−0.75×2
        (3, 0.25, None, 7, 100.0),   # eff=0：止损=颈线
        (3, 0.25, None, 8, 100.5),   # eff=−0.25：无 floor 时止损穿颈线上方（允许）
        (3, 0.25, 0.5, 8, 99.0),     # floor 卡底：eff=max(−0.25,0.5)=0.5
    ])
def test_compute_stop_price_active_trailing_hand_computed(grace, step, floor, days, want, pilot):
    """活跃 trailing 手算档（收紧/穿颈线/floor 卡底）——golden 之外的独立数值锚。"""
    assert pilot.compute_stop_price(neckline=100.0, atr=2.0, holding_days=days,
                                    stop_atr_mult=1.0, grace=grace, step=step,
                                    floor=floor) == want


# ============================================================================
# 订单工具：place_limit_buy / sell_limit / cancel（FakeGm 契约对拍）
# ============================================================================
def test_place_limit_buy_contract(pilot):
    """限价买：Q4 最小参数集逐项钉死（side/order_type/position_effect/price/account）。

    返回 List[Dict] 取 [0]["cl_ord_id"]（D6 通识错名警示）；cl_ord_id 自增唯一；
    落簿即 OrderStatus_New(1)=已报未成交（「限价买默认 PENDING」的 gm 侧状态）。
    """
    fake = FakeGm()
    cid = pilot.place_limit_buy(fake, "300750.SZ", 10.55, 300, "acc-1")
    assert cid and cid in fake.orders
    kw = next(c for c in fake.calls if c.get("api") == "order_volume")
    assert kw["symbol"] == "SZSE.300750"          # ts→gm 换装（§2 to_gm_symbol）
    assert kw["volume"] == 300 and kw["side"] == 1 and kw["order_type"] == 1
    assert kw["position_effect"] == 1 and kw["price"] == 10.55 and kw["account"] == "acc-1"
    assert fake.orders[cid]["status"] == ORDER_STATUS["New"]
    cid2 = pilot.place_limit_buy(fake, "688981.SH", 55.0, 100, "acc-1")
    assert cid2 != cid                              # cl_ord_id 自增（重挂不撞键）


def test_sell_limit_contract(pilot):
    """限价卖：side=Sell(2)+position_effect=Close(2)（Q4 官方示例开平仓口径）。"""
    fake = FakeGm()
    cid = pilot.sell_limit(fake, "300750.SZ", 12.0, 100, "acc-1")
    assert cid
    kw = next(c for c in fake.calls if c.get("api") == "order_volume")
    assert kw["side"] == 2 and kw["position_effect"] == 2
    assert kw["symbol"] == "SZSE.300750" and kw["price"] == 12.0


def test_cancel_contract(pilot):
    """撤单：order_cancel 收 {cl_ord_id, account_id} dict（D7——不是裸字符串）。

    撤后订单状态 Canceled(5)；FakeGm 契约自检：空列表抛「撤单信息不能为空」
    （Q4 GmError(-1) 形态，产品侧永传单 dict 不触此分支，替身必须同形）。
    """
    fake = FakeGm()
    cid = pilot.place_limit_buy(fake, "300750.SZ", 10.0, 100, "acc-1")
    pilot.cancel(fake, cid, "acc-1")
    kw = next(c for c in fake.calls if c.get("api") == "order_cancel")
    assert kw["count"] == 1                         # 单 dict → 1 笔撤单
    assert fake.orders[cid]["status"] == ORDER_STATUS["Canceled"]
    with pytest.raises(RuntimeError, match="撤单信息不能为空"):
        fake.order_cancel([])


# ============================================================================
# 跌停价：limit_down_price 自算档位 + fetch_limit_down API 值优先
# ============================================================================
def test_limit_down_price_rounding(pilot):
    """档位与二位取整：创板科创（300/301/688/689）20%、主板 10%，round(×,2)。

    universe 是创板科创 only → 实弹路径恒 round(prev_close×0.80, 2)；主板档保留
    口径（brief：池子已滤但口径留全）。ST 5% 档无法从代码判别（名不在码里），
    由 fetch_limit_down 的 API 值优先补此缺口。
    """
    assert pilot.limit_down_price(10.0, "300750.SZ") == 8.0      # 300 创业板 20%
    assert pilot.limit_down_price(9.99, "688981.SH") == 7.99     # 688 科创板（7.992 取二位）
    assert pilot.limit_down_price(5.55, "301308.SZ") == 4.44     # 301 创业板
    assert pilot.limit_down_price(689.0, "689009.SH") == round(689.0 * 0.80, 2)  # 689 科创板
    assert pilot.limit_down_price(10.0, "600000.SH") == 9.0      # 主板 10%
    assert pilot.limit_down_price(3.33, "000001.SZ") == 3.0      # 主板（2.997 取二位进位）


def test_fetch_limit_down_api_value_wins(pilot):
    """API 值优先：get_history_symbol 的 lower_limit 在场即用柜台/数据服务真值。"""
    fake = FakeGm(symbol_info={"SZSE.300750": {"pre_close": 10.0, "lower_limit": 7.77,
                                               "upper_limit": 12.34}})
    got = pilot.fetch_limit_down(fake, "300750.SZ", end_date="2026-08-21")
    assert got == 7.77                              # ≠自算 8.0 → 证明走的是 API 值


def test_fetch_limit_down_fallback_self_computed(pilot):
    """缺字段回退自算：lower_limit 缺（None）→ 用同行 pre_close 按 20% 档自算。"""
    fake = FakeGm(symbol_info={"SZSE.300750": {"pre_close": 10.0, "lower_limit": None}})
    assert pilot.fetch_limit_down(fake, "300750.SZ", end_date="2026-08-21") == 8.0


def test_fetch_limit_down_total_failure_returns_none_with_audit(pilot, monkeypatch, tmp_path):
    """查询彻底失败（异常/空行）→ None + audit WARN（调用方显式降级，不造错价）。"""
    monkeypatch.setattr(pilot, "AUDIT_DIR", tmp_path)
    fake = FakeGm(raise_on_symbol_info=True)
    assert pilot.fetch_limit_down(fake, "300750.SZ", end_date="2026-08-21") is None
    # 文件名与实现侧 audit_log 同源按运行日拼 f"audit_{date.today():%Y%m%d}.csv"
    # （范式照抄 test_state_and_gates:195/205）——硬编码日期是次日起必炸的时间炸弹。
    rows = (tmp_path / f"audit_{date.today():%Y%m%d}.csv").read_text(encoding="utf-8")
    assert "limit_down_fetch_fail" in rows and "300750.SZ" in rows


def test_fetch_limit_down_t1_close_fallback(pilot, monkeypatch, tmp_path):
    """三级回退链末级（终审 I-3）：get_history_symbol 整体失败 + prev_date 在场 →
    T-1 日线末根 close 自算跌停价（创板 20%）+ WARN 留痕（limit_down_fallback_t1）。

    期望值从同一替身的 fetch_df_upto 末根 close 推出（合成序列确定性，无手算魔法数）
    ——断言的正是「卖单价 = round(T-1收×0.80, 2)」这条链本身。
    """
    monkeypatch.setattr(pilot, "AUDIT_DIR", tmp_path)
    probe = FakeGm()                                     # 行情通道正常：推 T-1 末根 close
    prev_close = float(pilot.fetch_df_upto(probe, "300750.SZ", "2026-08-20")["close"].iloc[-1])
    expected = pilot.limit_down_price(prev_close, "300750.SZ")
    assert expected == round(prev_close * 0.80, 2)       # 20% 档自算（300 创业板）

    fake = FakeGm(raise_on_symbol_info=True)            # 证券信息通道整体失败
    got = pilot.fetch_limit_down(fake, "300750.SZ", end_date="2026-08-21",
                                 prev_date="2026-08-20")
    assert got == pytest.approx(expected)
    rows = (tmp_path / f"audit_{date.today():%Y%m%d}.csv").read_text(encoding="utf-8")
    assert "limit_down_fetch_fail" in rows               # 首级失败留痕（回退不吞证据）
    assert "limit_down_fallback_t1" in rows              # 回退触发进晨检面


def test_fetch_limit_down_t1_fallback_also_fails_gives_up(pilot, monkeypatch, tmp_path):
    """回退链全败（I-3 边界）：prev_date 未传（编排层无历可喂）或 T-1 取数也失败 →
    None 放弃——绝不造错价顶上（调用方 expire_skip_no_limit_down 语义的前提）。"""
    monkeypatch.setattr(pilot, "AUDIT_DIR", tmp_path)
    fake = FakeGm(raise_on_symbol_info=True, raise_on_history=True)   # 双通道全断
    assert pilot.fetch_limit_down(fake, "300750.SZ", end_date="2026-08-21",
                                  prev_date="2026-08-20") is None
    assert pilot.fetch_limit_down(fake, "300750.SZ", end_date="2026-08-21") is None  # 未传 prev_date


# ============================================================================
# decide_pending：cancel_on 触价 + max_wait 严格大于（C9 口径红线）
# ============================================================================
def _pending_order(cancel_on=11.0, formed_at="2026-08-14", max_wait=8):
    """构造 state.orders 的一条挂单记录（§3 schema：exec_params 是信号定终身快照）。"""
    return {"symbol": "300750.SZ", "date": "2026-08-17", "price": 10.5, "qty": 300,
            "purpose": "OPEN", "cancel_on": cancel_on, "formed_at": formed_at,
            "exec_params": {"max_wait": max_wait}, "status": "SUBMITTED", "filled": 0}


def test_decide_pending_cancel_on(pilot):
    """触价撤单：tick ≥ cancel_on（含等——decide_exit pending 分支 simulate_exit:130 同式）。"""
    o = _pending_order(cancel_on=11.0, formed_at="2026-08-14", max_wait=8)
    assert pilot.decide_pending(11.0, o, "2026-08-21", CAL) == "cancel_on"   # 恰触即撤
    assert pilot.decide_pending(11.01, o, "2026-08-21", CAL) == "cancel_on"
    assert pilot.decide_pending(10.99, o, "2026-08-21", CAL) is None         # 未触继续等


def test_decide_pending_cancel_on_none_free_fly(pilot):
    """cancel_on=None（不配撤单阈值）→ 放飞：任意高价都不撤（对齐 price_levels 语义）。"""
    o = _pending_order(cancel_on=None)
    assert pilot.decide_pending(99.0, o, "2026-08-21", CAL) is None


def test_decide_pending_max_wait_strict_gt(pilot):
    """max_wait 严格大于：恰好 == max_wait 不撤、> 才撤（formed_at 起算 (formed_at,today]）。

    08-14（周五）→ 08-21（周五）跨一周末 = 5 个交易日：max_wait=5 → 5>5 假 → 不撤；
    max_wait=4 → 5>4 真 → 撤。日数必须按交易日（跳过 08-15/16 周末）而非自然日。
    """
    o = _pending_order(cancel_on=None, formed_at="2026-08-14", max_wait=5)
    assert pilot.decide_pending(10.0, o, "2026-08-21", CAL) is None          # == 不撤
    o4 = _pending_order(cancel_on=None, formed_at="2026-08-14", max_wait=4)
    assert pilot.decide_pending(10.0, o4, "2026-08-21", CAL) == "max_wait"   # > 撤
    # 第二组边界：4 个交易日
    o44 = _pending_order(cancel_on=None, formed_at="2026-08-14", max_wait=4)
    assert pilot.decide_pending(10.0, o44, "2026-08-20", CAL) is None        # 4==4 不撤
    o43 = _pending_order(cancel_on=None, formed_at="2026-08-14", max_wait=3)
    assert pilot.decide_pending(10.0, o43, "2026-08-20", CAL) == "max_wait"  # 4>3 撤
    # 同日（formed_at 当日）零持有 → 恒不撤
    assert pilot.decide_pending(10.0, _pending_order(cancel_on=None, max_wait=1),
                                "2026-08-14", CAL) is None


def test_decide_pending_cancel_on_priority_over_max_wait(pilot):
    """两因并发归因 cancel_on：价格事件盘中即时，max_wait 是窗口边界（对齐 decide_exit
    pending 分支——窗口内逐根先判 cancel_on，窗口边界只是循环外限）。"""
    o = _pending_order(cancel_on=11.0, formed_at="2026-08-14", max_wait=1)
    assert pilot.decide_pending(11.5, o, "2026-08-21", CAL) == "cancel_on"


# ============================================================================
# decide_position：stop→tp2→tp1 优先序 + tp1 一档一次 + 份额取整（C9 口径红线）
# ============================================================================
def _pos(remaining=400, tp1_done=False, trailing=None, entry_date="2026-08-14",
         tp1=11.0, tp2=12.0, portion=0.3):
    """构造 state.positions 的一条持仓记录（§3 schema v1）。

    trailing 六件套 = 信号定终身的 trailing 参数快照（neckline/atr 来自信号日，
    grace/step/floor 来自 EXEC_PARAMS，stop_atr_mult 来自 ID_PARAMS）。
    """
    return {"entry_date": entry_date, "entry_price": 10.4, "qty": 400,
            "remaining_qty": remaining, "stop": 9.5,
            "tp1_price": tp1, "tp1_done": tp1_done, "tp2_price": tp2,
            "trailing": ({"neckline": 10.0, "atr": 0.5, "stop_atr_mult": 1.0,
                          "grace": 0, "step": 0.0, "floor": 0.0}
                         if trailing is None else trailing),
            "exec_params": {"tp1_portion": portion}}


def test_decide_position_stop_touch_sells_all_remaining(pilot):
    """stop 触价（tick ≤ stop，含等）→ 卖剩余全量；stop=颈线−stop_atr_mult×ATR=9.5。"""
    assert pilot.decide_position(9.5, _pos(remaining=400), "2026-08-21", CAL) == ("sell", 400, "stop_loss")
    assert pilot.decide_position(9.4, _pos(remaining=300), "2026-08-21", CAL) == ("sell", 300, "stop_loss")


def test_decide_position_stop_priority_over_tp(pilot):
    """优先序钉死（execution.py:249-259 priority 1）：tick 同时满足 ≤stop 与 ≥tp1 → stop 胜。

    构造 stop 9.5 高于 tp1 9.0（大 ATR 形态下数学上成立：颈线 10、ATR 0.5、mult 1 →
    stop 9.5；tp1 配 9.0）——同根价格双条件并发时硬风控先于止盈，防「先止盈后穿底」。
    """
    pos = _pos(remaining=400, tp1=9.0)
    assert pilot.decide_position(9.5, pos, "2026-08-21", CAL) == ("sell", 400, "stop_loss")


def test_decide_position_tp2_clears_all(pilot):
    """tp2 触价（含等）→ 清仓全量（execution.py:269-276 priority 2：tp2 全平）。"""
    assert pilot.decide_position(12.0, _pos(remaining=400), "2026-08-21", CAL) == ("sell", 400, "tp2")
    assert pilot.decide_position(12.5, _pos(remaining=200), "2026-08-21", CAL) == ("sell", 200, "tp2")


# ============================================================================
# R6-6 反转 regime（tp1_price > tp2_price，2026-08-26）：tp2_share/tp2_dust/
# lot1 全卖/force_exit——新语义守护（对齐 R6-4 幽灵修复后的回测口径）
# ============================================================================
def test_decide_position_inverted_tp2_sells_share_not_all(pilot):
    """反转形态 tp2 触价只卖 lot2 份额（(1−portion) 向下整手），不清仓。

    R6-8 冠军形态（tp1=2H > tp2=1.5H, portion=0.9）：1000 股 @ tp2 触价 →
    lot2=floor(1000×0.1/100)×100=100 股；lot1 900 股由 tp1 承接。
    """
    pos = _pos(remaining=1000, tp1=13.0, tp2=11.5, portion=0.9)
    assert pilot.decide_position(11.5, pos, "2026-08-21", CAL) == ("sell", 100, "tp2_share")
    assert pilot.decide_position(11.6, _pos(remaining=1000, tp1=13.0, tp2=11.5,
                                            portion=0.9), "2026-08-21", CAL) == ("sell", 100, "tp2_share")


def test_decide_position_inverted_tp2_done_then_tp1_all(pilot):
    """tp2_done 已置（lot2 已出）：tp2 价区不再触发；触 tp1 → lot1 全卖（=剩余）。"""
    pos = _pos(remaining=900, tp1=13.0, tp2=11.5, portion=0.9)
    pos["tp2_done"] = True
    # 11.5~13.0 区间：持有（lot1 等强势日）
    assert pilot.decide_position(11.6, pos, "2026-08-21", CAL) is None
    assert pilot.decide_position(12.9, dict(pos), "2026-08-21", CAL) is None
    # 触 tp1（含等）→ 全卖剩余
    assert pilot.decide_position(13.0, dict(pos), "2026-08-21", CAL) == ("sell", 900, "tp1")
    assert pilot.decide_position(13.5, dict(pos), "2026-08-21", CAL) == ("sell", 900, "tp1")


def test_decide_position_inverted_gap_tick_tp2_share_first(pilot):
    """跳空 tick 直接 ≥ tp1：tp2 分支先判（priority 2 在前）→ 先卖 lot2 份额。

    同日冲高到 tp1 的承接由下一根 tick 的 tp1 分支完成（tick 序自然复刻回测
    「首触 tp2 当日 lot1 若摸 tp1 按 tp1 成交」；卖价用限价 tp2/tp1，跳空下
    limit-or-better 不吃亏）。
    """
    pos = _pos(remaining=1000, tp1=13.0, tp2=11.5, portion=0.9)
    assert pilot.decide_position(13.5, pos, "2026-08-21", CAL) == ("sell", 100, "tp2_share")


def test_decide_position_inverted_tp2_dust_sinks_to_lot1(pilot):
    """lot2 份额不足一手：("sell", 0, "tp2_dust")——置位不落单，份额沉 lot1。"""
    pos = _pos(remaining=400, tp1=13.0, tp2=11.5, portion=0.9)
    assert pilot.decide_position(11.6, pos, "2026-08-21", CAL) == ("sell", 0, "tp2_dust")


def test_decide_position_inverted_stop_priority_unchanged(pilot):
    """反转形态下 stop 仍最优先（priority 1 硬风控先于一切止盈）。"""
    pos = _pos(remaining=1000, tp1=13.0, tp2=11.5, portion=0.9)
    assert pilot.decide_position(9.5, pos, "2026-08-21", CAL) == ("sell", 1000, "stop_loss")


def test_decide_position_force_exit_sells_all_at_tick(pilot):
    """盘后 sweep 标记（tp2 已触 lot1 未出）：任意 tick 全量跟价出（tp2_eod_sweep）。"""
    pos = _pos(remaining=900, tp1=13.0, tp2=11.5, portion=0.9)
    pos["tp2_done"] = True
    pos["force_exit"] = True
    assert pilot.decide_position(11.8, pos, "2026-08-21", CAL) == ("sell", 900, "tp2_eod_sweep")
    assert pilot.decide_position(9.8, dict(pos), "2026-08-21", CAL) == ("sell", 900, "tp2_eod_sweep")


def test_decide_position_normal_regime_unchanged_by_inversion_code(pilot):
    """正常 regime（tp1≤tp2）零行为变化：tp2 仍全平、tp1 仍 portion 档。"""
    assert pilot.decide_position(12.0, _pos(remaining=400), "2026-08-21", CAL) == ("sell", 400, "tp2")
    assert pilot.decide_position(11.0, _pos(remaining=400), "2026-08-21", CAL) == ("sell", 100, "tp1")


def test_decide_position_tp1_once_and_lot_rounding(pilot):
    """tp1 一档一次：份额=floor(remaining×portion/100)×100 向下整手（exit.py:190 同式）。"""
    # 400×0.3/100=1.2 → floor 1 手 = 100 股
    assert pilot.decide_position(11.0, _pos(remaining=400), "2026-08-21", CAL) == ("sell", 100, "tp1")
    # 1000×0.3/100=3.0 → 300 股
    assert pilot.decide_position(11.2, _pos(remaining=1000), "2026-08-21", CAL) == ("sell", 300, "tp1")


def test_decide_position_tp1_under_one_lot_sells_all(pilot):
    """不足一手（floor=0）→ 本档卖全部剩余（brief 钉死：单仓一次性模型防零股残留/
    防 tp1_done 空转——两腿模型对照 exit.py:190-193 份额沉到 tp2 腿的语义）。"""
    assert pilot.decide_position(11.0, _pos(remaining=200), "2026-08-21", CAL) == ("sell", 200, "tp1")
    assert pilot.decide_position(11.0, _pos(remaining=100), "2026-08-21", CAL) == ("sell", 100, "tp1")


def test_decide_position_tp1_done_not_refired(pilot):
    """tp1_done=True → 同价位不再触发（本地 lot1_open=False 对齐 simulate_exit:191）。"""
    assert pilot.decide_position(11.5, _pos(remaining=300, tp1_done=True),
                                 "2026-08-21", CAL) is None


def test_decide_position_stop_after_tp1_same_base(pilot):
    """tp1 减仓后 stop 用同 base：trailing 只依赖 holding_days/neckline/atr（与 tp1_done
    无关）——剩余 300 在同止损价触发，卖 300（不是 400）。"""
    pos = _pos(remaining=300, tp1_done=True)
    assert pilot.decide_position(9.5, pos, "2026-08-21", CAL) == ("sell", 300, "stop_loss")


def test_decide_position_active_trailing_uses_holding_days(pilot):
    """活跃 trailing：holding_days=(entry_date,today] 交易日数喂 compute_stop_price——
    08-14→08-21 = 5 日，grace 3/step 0.2 → eff=1−(5−3)×0.2=0.6 → stop=10−0.6×0.5=9.7。"""
    tr = {"neckline": 10.0, "atr": 0.5, "stop_atr_mult": 1.0, "grace": 3, "step": 0.2, "floor": 0.0}
    assert pilot.decide_position(9.7, _pos(trailing=tr), "2026-08-21", CAL) == ("sell", 400, "stop_loss")
    assert pilot.decide_position(9.71, _pos(trailing=tr), "2026-08-21", CAL) is None


def test_decide_position_trailing_missing_falls_back_to_fixed_stop(pilot):
    """trailing 的 neckline/atr 缺 → 回退 pos["stop"]（盘后预算的当日固定价，离散化
    口径兜底）；余参缺省不触发回退——活口径门槛只看 neckline/atr 在场（与实现一致）。"""
    pos = _pos()
    pos["trailing"] = {}
    pos["stop"] = 9.8
    assert pilot.decide_position(9.8, pos, "2026-08-21", CAL) == ("sell", 400, "stop_loss")
    assert pilot.decide_position(9.81, pos, "2026-08-21", CAL) is None


def test_decide_position_zero_remaining_and_hold(pilot):
    """防御档：剩余非正 → None（无仓可卖）；未触任何价 → None（持有）。"""
    assert pilot.decide_position(9.0, _pos(remaining=0), "2026-08-21", CAL) is None
    assert pilot.decide_position(10.5, _pos(remaining=400), "2026-08-21", CAL) is None


# ============================================================================
# absorb_reality：柜台↔state 幂等三查（以柜台实况修 state）
# ============================================================================
def test_partial_fill_to_position(pilot):
    """撤单后已成交部分转 positions（部成→撤→吸收→幂等再吸收不双计）。

    全链走 FakeGm：下单 300 股 → fill_order 注入 100 股部成（status 2、柜台持仓
    100）→ absorb（state 单 SUBMITTED → PARTIAL_FILLED/filled=100、持仓 100、
    exec_params 保留）→ cancel → 再 absorb（CANCELLED、持仓不丢）→ 三吸幂等。
    """
    fake = FakeGm()
    cid = pilot.place_limit_buy(fake, "300750.SZ", 10.5, 300, "acc-1")
    fake.fill_order(cid, volume=100, price=10.5)                # 部成 100/300

    state = pilot._initial_state()
    state["orders"][cid] = {"symbol": "300750.SZ", "date": "2026-08-20", "price": 10.5,
                            "qty": 300, "purpose": "OPEN", "cancel_on": 12.0,
                            "formed_at": "2026-08-20",
                            "exec_params": {"max_wait": 8, "tp1_portion": 0.3},
                            "status": "SUBMITTED", "filled": 0}
    pilot.absorb_reality(state, fake.get_orders(), fake.get_position())

    o = state["orders"][cid]
    assert o["status"] == "PARTIAL_FILLED" and o["filled"] == 100
    pos = state["positions"]["300750.SZ"]
    assert pos["remaining_qty"] == 100 and pos["qty"] == 100
    assert pos["entry_price"] == 10.5                            # filled_vwap（柜台为准）
    assert pos["exec_params"] == {"max_wait": 8, "tp1_portion": 0.3}   # 信号定终身保留

    pilot.cancel(fake, cid, "acc-1")                             # 撤剩余未成交 200
    pilot.absorb_reality(state, fake.get_orders(), fake.get_position())
    assert state["orders"][cid]["status"] == "CANCELLED"         # 撤单落终态
    assert state["positions"]["300750.SZ"]["remaining_qty"] == 100   # 已成交部分不被撤单吞

    snap = copy.deepcopy(state)                                  # 第三吸：完全幂等
    pilot.absorb_reality(state, fake.get_orders(), fake.get_position())
    assert state == snap


def test_absorb_orders_bidirectional(pilot):
    """订单三查：柜台有 state 无→吸收；state 有柜台无且非终态→CANCELLED；终态不动。"""
    state = pilot._initial_state()
    state["orders"]["o_gone"] = {"symbol": "600000.SH", "status": "SUBMITTED", "filled": 0}
    state["orders"]["o_dead"] = {"symbol": "000001.SZ", "status": "FILLED", "filled": 100}
    api_orders = [{"cl_ord_id": "o_new", "symbol": "SZSE.300750", "side": 1,
                   "status": ORDER_STATUS["New"], "volume": 200, "price": 10.0,
                   "filled_volume": 0, "filled_vwap": 0.0, "account_id": "acc-1",
                   "created_at": datetime(2026, 8, 21, 9, 31, 0)}]
    pilot.absorb_reality(state, api_orders, [])

    assert state["orders"]["o_gone"]["status"] == "CANCELLED"    # 非终态+柜台无 → 已撤/未挂
    assert state["orders"]["o_dead"]["status"] == "FILLED"       # 终态：next 日柜台不再返，不动
    o = state["orders"]["o_new"]                                 # 柜台有 state 无 → 吸收
    assert o["symbol"] == "300750.SZ" and o["status"] == "SUBMITTED"
    assert o["qty"] == 200 and o["filled"] == 0
    assert "300750.SZ" not in state["positions"]                 # 未成交 → 无持仓转换


def test_absorb_positions_bidirectional(pilot):
    """持仓三查：qty 以柜台为准修 state；entry/exec_params 以 state 保留；双向吸收/归零。"""
    state = pilot._initial_state()
    state["positions"]["300750.SZ"] = {
        "entry_date": "2026-08-14", "entry_price": 10.4, "qty": 400, "remaining_qty": 400,
        "stop": 9.5, "tp1_price": 11.0, "tp1_done": False, "tp2_price": 12.0,
        "trailing": {"neckline": 10.0, "atr": 0.5, "stop_atr_mult": 1.0,
                     "grace": 0, "step": 0.0, "floor": 0.0},
        "exec_params": {"tp1_portion": 0.3}}
    state["positions"]["600000.SH"] = {                          # state 有柜台无 → 剩余归零
        "entry_date": "2026-08-10", "entry_price": 8.0, "qty": 100, "remaining_qty": 100,
        "stop": 7.5, "tp1_price": None, "tp1_done": False, "tp2_price": 9.0,
        "trailing": {}, "exec_params": {"tp1_portion": 0.3}}
    api_positions = [
        {"symbol": "SZSE.300750", "side": 1, "volume": 300, "vwap": 10.4},
        {"symbol": "SHSE.688981", "side": 1, "volume": 150, "vwap": 55.0},   # 柜台有 state 无
    ]
    pilot.absorb_reality(state, [], api_positions)

    p = state["positions"]["300750.SZ"]
    assert p["remaining_qty"] == 300 and p["qty"] == 400         # qty 柜台真值 / 建仓量保留
    assert p["entry_price"] == 10.4 and p["exec_params"] == {"tp1_portion": 0.3}  # state 保留
    ab = state["positions"]["688981.SH"]                          # 吸收：柜台建仓（人工/丢档）
    assert ab["remaining_qty"] == 150 and ab["entry_price"] == 55.0
    assert ab["exec_params"] == {}                                # 柜台无信号信息，不伪造快照
    assert state["positions"]["600000.SH"]["remaining_qty"] == 0  # 柜台已无此仓
    assert state["positions"]["600000.SH"]["exec_params"] == {"tp1_portion": 0.3}  # 档案保留


def test_absorb_reality_sell_fill_reduces_position(pilot):
    """卖向成交减持仓：sell_limit 部成后 absorb → remaining 扣减（tp1 减仓的柜台视角）。"""
    fake = FakeGm()
    bcid = pilot.place_limit_buy(fake, "300750.SZ", 10.5, 400, "acc-1")
    fake.fill_order(bcid, volume=400, price=10.5)                # 全成建仓 400
    scid = pilot.sell_limit(fake, "300750.SZ", 11.0, 100, "acc-1")
    fake.fill_order(scid, volume=100, price=11.0)                # 卖 100 部成

    state = pilot._initial_state()
    state["orders"][bcid] = {"symbol": "300750.SZ", "date": "2026-08-20", "price": 10.5,
                             "qty": 400, "purpose": "OPEN", "cancel_on": None,
                             "formed_at": "2026-08-20", "exec_params": {"max_wait": 8},
                             "status": "FILLED", "filled": 400}
    state["orders"][scid] = {"symbol": "300750.SZ", "date": "2026-08-21", "price": 11.0,
                             "qty": 100, "purpose": "TP1", "cancel_on": None,
                             "formed_at": "2026-08-21", "exec_params": {},
                             "status": "SUBMITTED", "filled": 0}
    # state 持仓在场（买单已在上轮吸收转仓）：qty 400 / remaining 400
    state["positions"]["300750.SZ"] = {
        "entry_date": "2026-08-20", "entry_price": 10.5, "qty": 400, "remaining_qty": 400,
        "stop": 9.5, "tp1_price": 11.0, "tp1_done": False, "tp2_price": 12.0,
        "trailing": {"neckline": 10.0, "atr": 0.5, "stop_atr_mult": 1.0,
                     "grace": 0, "step": 0.0, "floor": 0.0},
        "exec_params": {"tp1_portion": 0.3}}
    pilot.absorb_reality(state, fake.get_orders(), fake.get_position())
    assert state["orders"][scid]["filled"] == 100
    assert state["positions"]["300750.SZ"]["remaining_qty"] == 300   # 400−100
    assert state["positions"]["300750.SZ"]["qty"] == 400             # 建仓量不动

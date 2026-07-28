# -*- coding: utf-8 -*-
"""颈线法策略适配器（NecklineMethodStrategy · 阶段B）。

经 Strategy 接口接入解耦后的回测引擎。颈线法的进场/出场是完整状态机（simulate_exit：
挂单回踩 + max_wait + cancel_on 撤单 + 分级止盈 tp1/tp2 + 超时），scan_at 一站式产出
trade dict（出场逻辑归策略侧，引擎零感知）。

算法本体收口于 strategies/neckline/ 子包（method_v0 识别层 + backtest 执行层），
本适配器从 .neckline 子包 import 算法原语（Layer2 Task 1.5 收口）。

与 caisen 形态的语义差异（已在 scan_at 处理）：
    - 信号去重：颈线法用 cooldown 交易日窗（caisen 用 neckline+bottom 价对签名）
    - 进场：挂单回踩 max_wait 天（caisen T+1 回踩成交）
    - 出场：分级止盈 tp1_portion 加权（caisen 单笔全平 check_exit）
    - rr：颈线法 avg_pnl_pct/risk_pct（caisen (exit-entry)/(entry-stop)）——同口径"风险倍数"
"""
from __future__ import annotations

import logging

import pandas as pd

# 颈线法算法原挂 scripts/neckline_method_v0 + scripts/neckline_backtest（靠 sys.path hack
# 挂载），Layer2 Task 1.5 收口进 strategies/neckline/ 子包后改本包相对 import——
# sys.path hack 已彻底删除。决策逻辑零改动。
from .neckline.method_v0 import DEFAULTS, compute_atr, detect_signal
from .neckline.backtest import simulate_exit, EXEC_DEFAULTS

from .neckline.schema import NecklineConfig
from .registry import register_strategy
# Layer2 阶段1：scan_at / scan_live 统一返 list[Signal]（frozen dataclass），
# 替代散落多处的 dict 字符串键访问。决策逻辑零改动，只改返回封装。
from .neckline.signal import Signal

# 识别层 / 执行层 键集（cfg_override 拆分用）
_NECKLINE_ID_KEYS = (
    "window", "min_touches", "min_suppression", "local_extrema_window", "min_bottoms",
    "breakout_vol_mult", "min_rr", "max_h_atr", "stop_atr_mult", "tp_h_mult", "decay_tau",
)
_NECKLINE_EXEC_KEYS = (
    "max_holding", "max_wait", "cooldown", "buy_limit_atr_mult",
    "tp1_h_mult", "tp1_portion", "cancel_thresh_mult",
)

logger = logging.getLogger(__name__)

# 完整性 gate（规则4）模块级 cache：首次 scan_live 懒加载，跨 symbol 复用。
# 物理意图：gate 每次 scan_live 都要查窗口连续性，suspend 区间 + trade_days 是
# 全市场共享的静态基准，per-symbol 重加载是 IO 浪费；模块级缓存一次加载全复用。
_suspend_intervals_cache: dict | None = None
_trade_days_cache: set | None = None


def _ensure_integrity_cache():
    """懒加载停牌区间 + 近 2 年 trade_days（首次 scan_live 触发）。

    返 (suspend_intervals, trade_days_set)。读 data_lake/suspend_d.parquet +
    近 2 年 trade_cal（颈线法窗口 60 日 ≈ 3 月，2 年余量充足）。
    fail-open：加载失败（无文件/无 token/网络异常）→ 返 ({}, set()) 让 gate 放行，
    不阻断识别（gate 是新增防护，失效时退回原行为，与 reader 离线降级同口径）。
    """
    global _suspend_intervals_cache, _trade_days_cache
    if _suspend_intervals_cache is not None:
        return _suspend_intervals_cache, _trade_days_cache
    try:
        from datetime import datetime, timedelta
        from pathlib import Path
        from data.integrity import load_suspend_intervals, fetch_trade_days
        today = datetime.today().strftime("%Y-%m-%d")
        start = (datetime.today() - timedelta(days=730)).strftime("%Y-%m-%d")
        _trade_days_cache = fetch_trade_days(start, today)
        susp_path = Path("data_lake/suspend_d.parquet")
        if susp_path.exists():
            susp_df = pd.read_parquet(susp_path)
            _suspend_intervals_cache = load_suspend_intervals(susp_df, _trade_days_cache)
        else:
            logger.warning("suspend_d.parquet 缺失，完整性 gate 无停牌 ground-truth（全判漏采）")
            _suspend_intervals_cache = {}
    except Exception as e:
        logger.warning("完整性 gate cache 加载失败（fail-open 放行）：%s", e)
        _suspend_intervals_cache = {}
        _trade_days_cache = set()
    return _suspend_intervals_cache, _trade_days_cache


@register_strategy("neckline")
class NecklineMethodStrategy:
    """颈线法策略（挂单回踩 + 分级止盈 + 撤单）。

    构造：
        cfg_override: 18 维参数覆盖 dict（键在 NecklineConfig.model_fields 内）。
    """

    def __init__(self, cfg_override: dict | None = None, **kwargs):
        ov = cfg_override or {}
        self.id_cfg = {**DEFAULTS, **{k: ov[k] for k in _NECKLINE_ID_KEYS if k in ov}}
        self.exec_cfg = {**EXEC_DEFAULTS, **{k: ov[k] for k in _NECKLINE_EXEC_KEYS if k in ov}}
        self._last_signal_pos: dict = {}   # per-symbol cooldown 锚点（跨 T）

    @property
    def config_schema(self) -> type:
        return NecklineConfig

    def precompute(self, symbol: str, full_df) -> dict:
        """预算全序列 ATR（窗口对齐 id_cfg["window"]），scan_at 复用截断。"""
        atr_full = compute_atr(
            full_df["high"], full_df["low"], full_df["close"], window=self.id_cfg["window"]
        )
        return {"atr_full": atr_full, "full_df": full_df}

    def scan_at(self, symbol: str, df_T, T, strategy_state: dict) -> list:
        """对单 symbol 在 T 日：detect_signal（识别单源）→ simulate_exit → Signal 列表。

        Layer2 阶段1：返回 ``list[Signal]``（frozen dataclass）替代 trade dict。
        _compute_stats 改读 Signal 属性；旧字段集（TRADE_REQUIRED_KEYS）逐位映射到
        Signal 同名字段，决策逻辑零改动。

        识别单源（U2 · 2026-07-29 Task 3）：识别改调 ``detect_signal``
        （method_v0.py:302，与 scan_live / scan_symbol 同源），detect_signal 内部含
        ATR 预算 + detect_neckline_method + cancel_on close 守卫 + 当日突破过滤 +
        Signal 装配完整闭包。scan_at 拿到 Signal 后取 neckline/bottom/atr 喂 simulate_exit。

        严格无前视：detect_signal 用 df_T（=df.loc[:T]），atr 在 df_T 上算（rolling 到 T，
        与原 atr_full.iloc[:T_pos+1] 截断同根——都是 window 对齐 ATR）。
        cooldown 去重：相邻信号（T_pos 差 < cooldown）只处理首次。
        """
        full_df = strategy_state["full_df"]
        atr_full = strategy_state["atr_full"]
        sym_index = full_df.index
        T_pos = sym_index.get_loc(T)

        # cooldown 去重：距上次信号不足 cooldown 交易日 → 跳过
        last = self._last_signal_pos.get(symbol)
        if last is not None and T_pos - last < self.exec_cfg["cooldown"]:
            return []

        # 识别统一（U2 · 2026-07-29 Task 3）：改调单一识别源 ``detect_signal``
        # （strategies/neckline/method_v0.py:302，Task 2 已测）。
        #
        # 识别口径变化（brief 澄清 2，spec D9 设计预期）：原 scan_at 只调
        # detect_neckline_method（无 cancel_on / 无当日突破过滤），改调 detect_signal 后
        # 识别层多了【cancel_on close 守卫】+【当日突破过滤】——回测识别口径向实盘 scan_live
        # 对齐（识别单源 D9）。这【可能让回测某些冲天突破信号被识别期 cancel_on 挡掉】，
        # 影响回测 golden，但 golden gate 在 Task 5（simulate_exit 改 decide_exit），
        # 本 task 不跑 golden，只跑 A2（scan_live 路径，零变化）+ 识别/引擎单测。
        #
        # ATR 等价性（brief 澄清 3）：原 scan_at 用 precompute 的 atr_full.iloc[:T_pos+1]
        # （预算截断），detect_signal 在 df_upto(=df_T) 上重算 ATR 全序列。两者都是 ATR
        # rolling 到 T，与 scan_live 同口径（scan_live 也是 df_upto 重算），应等价。
        sig = detect_signal(symbol, df_T, self.id_cfg, self.exec_cfg, T)
        if sig is None:
            return []

        # 出场：simulate_exit 从 T_pos 推进 max_holding 根，需 full_df（推进用未来 K 线，属回测允许）。
        # 衔接（brief 澄清 4）：detect_signal 返的 Signal 含 neckline/bottom/atr 字段，从此处
        # 平滑取出喂 simulate_exit（原 res dict 同名字段 → Signal 同名字段，零语义漂移）。
        # atr 优先用 sig.atr（detect_signal 已按 scan_live 口径算 ATR 末值并落地 Signal.atr），
        # 与原 atr_full.iloc[T_pos] 同根（都是 window 对齐 ATR rolling 到 T）。
        sim = simulate_exit(
            full_df, T_pos, sig.neckline, sig.bottom, sig.atr,
            exec=self.exec_cfg, id_cfg=self.id_cfg,
        )
        # 消费信号（无论成交与否，cooldown 锚点更新，防同形态连续 T 重复计）
        self._last_signal_pos[symbol] = T_pos

        # 未成交 / 撤单 → 不计入 hits（exit_reason 标识）
        if sim is None or sim["exit_reason"] in ("skip_no_pullback", "skip_target_met"):
            return []

        # rr 口径对齐 caisen：颈线法 avg_pnl_pct(%) / risk_pct(%) = 风险倍数。
        # 边界：entry≤stop（跳空低开过止损，risk_pct≤0）→ 用 avg_pnl 符号兜底（防 rr 符号反转）。
        risk_pct = sim.get("risk_pct")
        if risk_pct and risk_pct > 0:
            rr = sim["avg_pnl_pct"] / risk_pct
        else:
            rr = sim["avg_pnl_pct"] / 100.0

        return [Signal(
            symbol=symbol,
            signal_type="neckline",
            formed_at=T,
            entry_date=sim.get("buy_date", T),
            entry_price=sim["entry"],
            exit_date=sim.get("exit_date"),
            exit_price=sim.get("exit_price"),
            exit_reason=sim["exit_reason"],
            rr=rr,
            holding_bars=sim.get("holding_bars", 0),
            # 颈线法附加字段（详情展示用，统计层不依赖）
            neckline=sim.get("neckline"),
            avg_pnl_pct=sim.get("avg_pnl_pct"),
        )]

    def scan_live(self, symbol: str, df_upto, date) -> list:
        """实盘纯识别：调 detect_signal（df_upto 截至 date），**不调 simulate_exit**。

        Layer2 阶段1：返回 ``list[Signal]``（frozen dataclass）替代 signal dict。
        实验归因字段（experiment_id/experiment_weight）由 _eod 用 ``dataclasses.replace``
        注入到返回的 Signal 上——scan_live 本身不填（保持满仓默认）。

        与 scan_at 的物理差异（Why 拆两入口）：
            - scan_at 是【回测一站式】：detect + simulate_exit 推进未来 K 线模拟出场
              （simulate_exit 从 T_pos 向前吃 max_holding 根，回测允许读未来）。
            - scan_live 是【实盘纯识别】：只识别形态，不模拟出场。实盘 T-1 晚 _eod 调用时
              根本没有"未来 K 线"可用（未来还没发生），出场由二期引擎 pre_open / stop_loss_monitor
              在交易时段实时做，不需要回测模拟。

        无前视契约：
            df_upto 由 Task7b 的 _eod 从 data_lake 加载该 symbol 截至 date 的前复权日线
            （截断于 date，不含 date 之后），atr 也在 df_upto 上算——严格因果。

        参数：
            symbol: 标的代码（归因用）
            df_upto: 该 symbol 截至 date 的前复权日线 DataFrame（OHLCV，index 为 DatetimeIndex）
            date: 当前识别日（_eod 传入 T-1 收盘日）

        返回：
            Signal 列表（仅当日突破的），识别字段供 signal_runner 消费：
                symbol / formed_at / breakout_date / neckline / bottom / entry_price / atr
            突破日非当日（res["formed_at"] != date）→ 返 []（只挂当日新信号，防历史重吐）。
        """
        # 完整性 gate（规则4）：窗口含未解释漏采 → 跳过，不产误信号（300214.SZ 教训）。
        # Why：lake 缺停牌复牌段时残缺数据误判颈线；gate 用 trade_cal + suspend_d 区分
        # 合法跳空（停牌）vs 漏采，漏采则 return []（不阻断 universe 扫描，warning 留痕）。
        # fail-open：gate 自身异常（cache 加载失败等）不阻断识别，退回原行为。
        try:
            _susp, _td = _ensure_integrity_cache()
            if _td:  # cache 非空才查（空 cache = 测试降级/首次加载失败 → 放行）
                from data.integrity import check_window_continuity
                _cont = check_window_continuity(
                    df_upto.tail(self.id_cfg["window"]), _td, _susp, symbol)
                if not _cont.ok:
                    logger.warning(
                        "颈线 scan_live %s 窗口含 %d 个未解释漏采交易日 %s，跳过（数据完整性 gate）",
                        symbol, len(_cont.unjustified), list(_cont.unjustified[:5]))
                    return []
        except Exception as _e:
            logger.warning("完整性 gate 异常（fail-open 继续）：%s", _e)

        # 识别统一（U2 · 2026-07-29 Task 3）：本入口原内联「ATR 预算 → detect_neckline_method
        # → R1 cancel_on close 守卫 → 当日突破过滤 → Signal 装配」已逐位抽取到
        # ``detect_signal``（strategies/neckline/method_v0.py:302，Task 2 已测）。
        # 此处改调单一识别源——strangler 红线：识别逻辑零改动（detect_signal 即从 scan_live
        # 抽取的等价闭包），编排逻辑（上方完整性 gate / 本入口的 list 包装语义）不动。
        #
        # 等价性依据（brief 澄清 1）：scan_live 改前已是 close 口径 cancel_on（旧 line 257），
        # detect_signal 正是从 scan_live 抽取，故改后实盘识别路径应【零行为变化】——
        # A2 gate（trigger_eod_once）改前后信号需逐位一致（标的/挂单价/止损/止盈/rr 全等）。
        sig = detect_signal(symbol, df_upto, self.id_cfg, self.exec_cfg, date)
        return [sig] if sig is not None else []

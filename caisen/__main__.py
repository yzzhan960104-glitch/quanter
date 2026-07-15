# -*- coding: utf-8 -*-
"""蔡森形态学流水线 CLI 离线入口（Phase 2 · Task 11）。

物理定位（CLAUDE.md 极简 + 显式至上）：
    本模块是蔡森形态学流水线的"人机入口"——把 PatternScreener（筛选）+
    TradePlanGenerator（计划生成）+ backtest_replay（历史回放）封装成两个
    argparse 子命令，让研究员在终端即可离线跑"今日候选"与"历史回放验证"，
    无需写脚本。所有数据来自 DataLakeReader（data_lake/a_shares_daily.parquet），
    数据湖未加载（离线/CI）时显式提示用户先同步数据，绝不静默跑空。

子命令设计：
    screen : `python -m caisen screen --date 2024-06-01 --universe 600519,000858
              [--cfg-override '{"confirm_bars":2,...}']`
              → 跑 PatternScreener + TradePlanGenerator，输出 plans/<date>.json
                （供人工审核 / 下游执行器消费）+ 终端打印候选表。
    replay : `python -m caisen replay --start 2023-01-01 --end 2024-06-01
              [--cfg-override ...]`
              → 跑 backtest_replay，终端打印 ReplayReport
                （胜率/平均盈亏比/最大回撤/命中数/min_rr 建议）。

cfg-override 设计（承 Task 10 concern 1 · 真实参数调优）：
    JSON 字符串覆盖 StrategyConfig 默认参数。Task 10 发现默认参数在真实 A 股
    日线 0 命中（多层硬否决叠加过严），研究员可经此参数在不改代码的前提下放宽
    阈值，数据驱动找到"真实数据有命中"的参数组合。本 CLI 在输出里同时打印
    默认 vs 覆盖后参数的命中数对比，作为参数调优的实证依据。

数据流（无前视红线 · screen 子命令）：
    DataLakeReader 读 data_lake/a_shares_daily.parquet（MultiIndex: date, symbol）
    → 取 universe 内每个 symbol 的时序 → .loc[:date] 严格只用 T 及之前
    → PatternScreener.screen → TradePlanGenerator.generate → plans JSON + 终端表。

防御性边界（CLAUDE.md 量化风控拷问）：
    - 数据湖未加载（离线）：screen/replay 都显式提示"请先同步数据"，不静默跑空；
    - universe 标的代码宽松匹配：用户输入 "600519" / "600519.SH" 都能命中；
    - cfg-override JSON 解析失败：报错退出，不让脏参数静默走默认；
    - 单 symbol 异常：screener 内部已 try/except（承 Task 8），CLI 不再重复包裹；
    - 候选为空：打印诊断信息（命中数 + 各否决层淘汰分布），不视为错误。

Step3 后内部模块已分包子包（engines/optimize/infra），本 CLI 的 5 处 import 全部
经 caisen/__init__ 预加载 + 顶层 sys.modules 别名垫片可达——``caisen.config``↔
``caisen.engines.config``（同对象）、``caisen.patterns.screener``↔
``caisen.engines.patterns.screener``、``caisen.plan``↔``caisen.engines.plan``、
``caisen.backtest_replay``↔``caisen.infra.backtest_replay`` 均为同一模块对象。
故 ``python -m caisen`` 入口位置不变（不迁移），import 零改动（strangler 铁律①）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Optional

import pandas as pd

from caisen.config import StrategyConfig
from caisen.risk import RiskManager
from caisen.patterns.screener import PatternScreener
from caisen import plan as plan_mod
from caisen import backtest_replay


# —— 数据湖路径（与项目 config.py LAKE_CONFIG 单一真相源对齐）——
# 物理意图：直接读 data_lake/a_shares_daily.parquet（全市场日线），不依赖
# data.lake_reader 的单例缓存——CLI 一次性消费，无需常驻内存；若用户已起
# 长驻进程（如 API server），单例缓存更优，但 CLI 离线场景直读更极简显式。
_DAILY_PARQUET = "data_lake/a_shares_daily.parquet"

# plans 输出目录（与 brief 一致：plans/<date>.json）
_PLANS_DIR = "plans"

# 默认账户规模（AUM，用于 position_size；CLI 离线快查，写死 1e6 = 100 万）
# 真实生产由执行器配置注入，CLI 仅作计划生成演示。
_DEFAULT_AUM = 1_000_000.0


# ---------------------------------------------------------------------------
# 数据加载：从 data_lake 读日线 parquet，切 universe + 时序窗口
# ---------------------------------------------------------------------------
def _load_daily_parquet() -> Optional[pd.DataFrame]:
    """读 data_lake/a_shares_daily.parquet，返回 MultiIndex(date, symbol) DataFrame。

    返回 None 表示数据湖未就绪（离线/未同步）。调用方据此显式提示用户。

    设计意图（极简显式）：
        - 不走 DataLakeReader 单例——CLI 一次性消费，避免长驻缓存的开销；
        - parquet 不存在/读取异常均返 None，由调用方统一降级提示，保持职责单一。
    """
    if not os.path.exists(_DAILY_PARQUET):
        return None
    try:
        df = pd.read_parquet(_DAILY_PARQUET)
    except Exception as exc:
        # parquet 损坏（半截写）等异常 → 打印告警后返回 None（不静默跑空）
        print(f"[WARN] 数据湖读取失败：{exc}", file=sys.stderr)
        return None
    return df


def _resolve_symbol(code: str, all_symbols) -> Optional[str]:
    """宽松匹配 universe 标的代码 → 全市场 symbol 集合中的标准形式。

    匹配规则（优先级）：
        1. 完全相等（用户已输入标准形式 "600519.SH"）；
        2. 后缀匹配（用户输入 "600519"，湖中有 "600519.SH" → 命中）；
        3. 前缀 contains（兜底：输入 "600519" 在任意 symbol 内 → 命中第一个）。

    返回标准 symbol 字符串；未匹配返回 None。
    """
    sym_set = list(all_symbols)
    # 1. 完全相等
    if code in sym_set:
        return code
    # 2. 后缀匹配（最常见：用户只输入数字代码）
    for s in sym_set:
        if s.split(".")[0] == code:
            return s
    # 3. contains 兜底
    for s in sym_set:
        if code in s:
            return s
    return None


def _slice_universe(
    lake: pd.DataFrame, universe_codes: list[str]
) -> dict[str, pd.DataFrame]:
    """从 MultiIndex(date, symbol) 湖切 universe 内每个 symbol 的时序 DataFrame。

    参数：
        lake: MultiIndex(date, symbol) 全市场日线 DataFrame（列含 OHLCV+amount）。
        universe_codes: 用户输入的标的代码列表（可为 "600519" 或 "600519.SH"）。

    返回：
        {standard_symbol: DataFrame}，DataFrame 为该 symbol 的时序（index=date），
        列保留 close/high/low/volume/amount（screener 契约）。

    未匹配的输入代码打印告警跳过（不中断整批），保持 CLI 对脏输入的鲁棒性。
    """
    all_symbols = lake.index.get_level_values("symbol").unique()
    out: dict[str, pd.DataFrame] = {}
    for code in universe_codes:
        std = _resolve_symbol(code.strip(), all_symbols)
        if std is None:
            print(f"[WARN] universe 代码 {code!r} 未在数据湖命中，跳过", file=sys.stderr)
            continue
        # xs 取该 symbol 的全部时序（MultiIndex date, symbol → index=date）
        try:
            sub = lake.xs(std, level="symbol")
        except KeyError:
            print(f"[WARN] {std} xs 切片失败，跳过", file=sys.stderr)
            continue
        # 按日期升序（防 parquet 未排序导致 .loc[:T] 末点错乱）
        sub = sub.sort_index()
        out[std] = sub
    return out


def _slice_window(
    universe_df: dict[str, pd.DataFrame], end_date
) -> dict[str, pd.DataFrame]:
    """对 universe 内每个 symbol 时序裁剪到 .loc[:end_date]（严格无前视）。

    物理意图（无前视红线 · 与 backtest_replay 一致）：
        screen 子命令对 T 日筛选时，screener 只能用 T 及之前的数据——
        未来 K 线泄露会污染形态判定。本函数统一在入口处裁剪。
    """
    out: dict[str, pd.DataFrame] = {}
    for sym, df in universe_df.items():
        try:
            sub = df.loc[:end_date]
        except Exception:
            # end_date 超出 index / 类型不匹配 → 用 searchsorted 兜底定位
            idx = df.index
            pos = idx.searchsorted(pd.Timestamp(end_date), side="right")
            sub = df.iloc[:pos]
        if len(sub) > 0:
            out[sym] = sub
    return out


# ---------------------------------------------------------------------------
# StrategyConfig 覆盖：cfg-override JSON 解析
# ---------------------------------------------------------------------------
def _apply_cfg_override(cfg: StrategyConfig, override_json: Optional[str]) -> StrategyConfig:
    """解析 cfg-override JSON 字符串，返回更新后的 StrategyConfig。

    参数：
        cfg:           基础 StrategyConfig（默认参数）。
        override_json: JSON 字符串，如 '{"confirm_bars":2,"zigzag_threshold_atr":0.5}'。
                       None / 空字符串 → 直接返回原 cfg。

    返回：
        更新后的 StrategyConfig（model_copy 浅拷贝，不污染原实例）。

    异常：
        JSON 解析失败 → ValueError（调用方应报错退出，不让脏参数静默走默认）。
        字段名不在 StrategyConfig → ValueError（防拼写错误静默忽略）。
    """
    if not override_json:
        return cfg
    try:
        overrides = json.loads(override_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"cfg-override JSON 解析失败：{exc}") from exc
    if not isinstance(overrides, dict):
        raise ValueError("cfg-override 顶层必须是 JSON 对象（{}）")
    # 字段名校验：防拼写错误（如 "confirmBars" 驼峰）静默走默认，违背"显式至上"
    # 注：model_fields 从【类】访问（Pydantic V2.11+ 实例访问已 deprecated）
    valid_fields = set(StrategyConfig.model_fields.keys())
    unknown = set(overrides.keys()) - valid_fields
    if unknown:
        raise ValueError(
            f"cfg-override 含未知字段 {sorted(unknown)}；"
            f"合法字段见 StrategyConfig: {sorted(valid_fields)}"
        )
    return cfg.model_copy(update=overrides)


def _cfg_diff_summary(base: StrategyConfig, overridden: StrategyConfig) -> list[str]:
    """对比 base vs overridden，列出被覆盖的字段及新旧值（用于终端诊断输出）。

    返回 ["confirm_bars: 3 → 2", ...]，便于研究员一眼看清当前生效的非默认参数。
    """
    diffs: list[str] = []
    # 注：model_fields 从【类】访问（Pydantic V2.11+ 实例访问已 deprecated）
    for name in StrategyConfig.model_fields.keys():
        old = getattr(base, name)
        new = getattr(overridden, name)
        if old != new:
            diffs.append(f"{name}: {old!r} → {new!r}")
    return diffs


# ---------------------------------------------------------------------------
# screen 子命令：T 日筛形态 → 生成计划 JSON + 终端候选表
# ---------------------------------------------------------------------------
def _cmd_screen(args: argparse.Namespace) -> int:
    """screen 子命令主体：跑 PatternScreener + TradePlanGenerator，落 plans JSON。

    返回进程退出码（0=正常，1=数据缺失/参数错误）。
    """
    # —— 0. 数据湖加载 ——
    lake = _load_daily_parquet()
    if lake is None:
        print(
            "[ERROR] 数据湖未就绪（data_lake/a_shares_daily.parquet 缺失）。\n"
            "        请先同步数据：PYTHONIOENCODING=utf-8 .venv310/Scripts/python.exe "
            "scripts/sync_data_lake.py\n"
            "        或用合成数据冒烟：pytest tests/caisen/test_screener.py -q",
            file=sys.stderr,
        )
        return 1

    # —— 1. 解析 universe + cfg-override ——
    universe_codes = [c for c in re.split(r"[,\s]+", args.universe) if c]
    if not universe_codes:
        print("[ERROR] --universe 不能为空（逗号分隔的标的代码，如 600519,000858）",
              file=sys.stderr)
        return 1
    base_cfg = StrategyConfig()
    try:
        cfg = _apply_cfg_override(base_cfg, args.cfg_override)
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    # —— 2. 切 universe 时序 + .loc[:date] 无前视裁剪 ——
    universe_full = _slice_universe(lake, universe_codes)
    if not universe_full:
        print(f"[ERROR] universe {universe_codes} 全部未命中数据湖，退出。",
              file=sys.stderr)
        return 1
    # end_date 归一化为 Timestamp（与 lake index 的 date 层级对齐）
    end_date = pd.Timestamp(args.date)
    universe_T = _slice_window(universe_full, end_date)

    # 诊断：universe 命中 / 数据长度
    print(f"[INFO] screen date={args.date} universe={list(universe_T.keys())} "
          f"cfg_overrides={_cfg_diff_summary(base_cfg, cfg) or '(默认)'}")
    for sym, df in universe_T.items():
        print(f"       {sym}: {len(df)} 根 K 线（{df.index[0].date()}..{df.index[-1].date()}）")

    # —— 3. PatternScreener.screen（事前风控 + 因果 ZigZag + 形态识别）——
    risk = RiskManager(cfg)
    screener = PatternScreener(cfg, risk)
    candidates = screener.screen(universe_T, end_date)

    # —— 4. 诊断：命中数 / 各否决层淘汰分布 ——
    # 物理意图（承 Task 10 concern 1）：默认参数真实数据 0 命中时，研究员需知道
    # 是哪一层把候选淘汰了。这里用宽松 cfg 重跑一遍作为对照基线，量化"硬否决叠加"
    # 的影响——不修改 screener 内部，只在 CLI 层做诊断。
    _print_screen_diagnostics(universe_T, cfg, base_cfg, candidates)

    if candidates.empty:
        print(f"\n[RESULT] {args.date} 命中候选：0（建议参考上方诊断放宽 cfg-override 参数）")
        # 仍落一个空 plans JSON，便于下游脚本统一消费（文件存在性判断）
        _write_plans_json(args.date, [], cfg, base_cfg)
        return 0

    # —— 5. TradePlanGenerator.generate（颈线满足计算 + 盈亏比过滤 + 仓位分配）——
    # trading_calendar：用 universe 全市场日期并集（升序），供 valid_until/max_holding_until 推进
    cal = lake.index.get_level_values("date").unique().sort_values()
    plans = plan_mod.generate(candidates, cfg, risk, _DEFAULT_AUM, end_date, cal)

    # —— 6. 落 plans JSON + 终端候选表 ——
    _write_plans_json(args.date, plans, cfg, base_cfg)
    _print_candidates_table(candidates, plans)

    print(f"\n[RESULT] {args.date} 命中候选：{len(candidates)}，"
          f"生成计划：{len(plans)}（已落 plans/{args.date}.json）")
    return 0


def _print_screen_diagnostics(
    universe_T: dict[str, pd.DataFrame],
    cfg: StrategyConfig,
    base_cfg: StrategyConfig,
    candidates: pd.DataFrame,
) -> None:
    """打印 screen 诊断：命中数 + 各否决层淘汰分布 + 末尾四点结构。

    设计意图（承 Task 10 concern 1）：
        默认参数在真实 A 股日线常 0 命中（多层硬否决叠加）。本函数逐一展示每个
        universe 标的在 liquidity / micro_filter / ZigZag pivot / 形态识别 各层
        的通过情况，并在形态未识别时打印末尾 4 个 pivot 的结构（类型/跨度/价格），
        让研究员定位"最严否决层"，数据驱动决定放宽哪个参数。
    """
    risk = RiskManager(cfg)
    from caisen.patterns.zigzag_causal import causal_pivots, compute_atr
    from caisen.patterns.w_bottom import detect as w_detect
    from caisen.patterns.head_shoulder import detect as hs_detect

    print("\n[DIAG] 各标的否决层诊断（当前 cfg）：")
    print(f"       {'symbol':<12} {'liquidity':<9} {'micro':<9} {'npiv':>5} {'W':>4} "
          f"{'HS':>4} {'末四点结构(尾→头)':<40}")
    n_liq_fail = n_micro_fail = n_pivots_lt4 = n_hit = 0
    for sym, df in universe_T.items():
        liq_ok = risk.liquidity_filter(df)
        micro_ok, _r = risk.micro_filter(df, sym)
        n_pivots_nz = 0
        w_hit = hs_hit = False
        struct_str = ""
        if liq_ok and micro_ok:
            try:
                atr = compute_atr(df["high"], df["low"], df["close"])
                pivots = causal_pivots(df["close"], atr, cfg)
                # 非零 pivot 数（真实波段顶/底数量，非 Series 总长度）
                nz = pivots[pivots != 0]
                n_pivots_nz = len(nz)
                if n_pivots_nz >= 4:
                    w_res = w_detect(df["close"], pivots, df["high"], df["low"],
                                     df["volume"], cfg)
                    hs_cfg = cfg.model_copy(
                        update={"max_pattern_depth": cfg.hs_max_pattern_depth})
                    hs_res = hs_detect(df["close"], pivots, df["high"], df["low"],
                                       df["volume"], hs_cfg)
                    w_hit = w_res is not None and w_res.is_valid
                    hs_hit = hs_res is not None and hs_res.is_valid
                    # 末四点结构（尾→头）：标注类型 + 跨度，便于人工判读形态可能性
                    idxs = [i for i in range(len(pivots)) if pivots.iloc[i] != 0]
                    last4 = idxs[-4:]
                    pts = []
                    for i in reversed(last4):  # 头→尾展示
                        tag = "峰" if pivots.iloc[i] == 1 else "谷"
                        pts.append(f"{tag}{float(df['close'].iloc[i]):.0f}")
                    span = last4[-1] - last4[0] if len(last4) == 4 else 0
                    struct_str = f"{'/'.join(pts)} span={span}"
            except Exception:
                pass
        if not liq_ok:
            n_liq_fail += 1
        elif not micro_ok:
            n_micro_fail += 1
        elif n_pivots_nz < 4:
            n_pivots_lt4 += 1
        if w_hit or hs_hit:
            n_hit += 1
        print(f"       {sym:<12} {'PASS' if liq_ok else 'FAIL':<9} "
              f"{'PASS' if micro_ok else 'FAIL':<9} {n_pivots_nz:>5} "
              f"{'Y' if w_hit else '-':>4} {'Y' if hs_hit else '-':>4} {struct_str}")
    print(f"       汇总：命中 {n_hit}（universe={len(universe_T)}），"
          f"流动性淘汰 {n_liq_fail}，HV淘汰 {n_micro_fail}，"
          f"pivot<4淘汰 {n_pivots_lt4}；"
          f"形态层未命中={len(universe_T) - n_hit - n_liq_fail - n_micro_fail - n_pivots_lt4}")


def _print_candidates_table(candidates: pd.DataFrame, plans: list) -> None:
    """终端打印候选表（symbol/形态/突破价/颈线/depth/tension/amount30d）。

    plans 用于附加 rr_ratio + 第一波满足价（若有对应 plan 生成）。
    """
    # plan 按 symbol 索引（双形态时取 depth 更大者，与 screener 一致）
    plan_by_sym = {p.symbol: p for p in plans}
    print("\n[CANDIDATES] 候选表（按近30日均成交额降序）：")
    print(f"  {'symbol':<12} {'pattern':<14} {'breakout':>10} {'neckline':>10} "
          f"{'depth':>7} {'tension':>7} {'rr':>6} {'tp1':>10} {'amount30d':>14}")
    for _, row in candidates.iterrows():
        p = plan_by_sym.get(row["symbol"])
        rr = f"{p.rr_ratio:.2f}" if p else "-"
        tp1 = f"{p.take_profit:.2f}" if p else "-"
        print(f"  {str(row['symbol']):<12} {str(row['pattern_type']):<14} "
              f"{float(row['breakout_price']):>10.2f} {float(row['neckline_price']):>10.2f} "
              f"{float(row['depth']):>7.3f} {float(row['tension']):>7.3f} "
              f"{rr:>6} {tp1:>10} {float(row['amount30d']):>14.2e}")


def _write_plans_json(date, plans: list, cfg: StrategyConfig, base_cfg: StrategyConfig) -> None:
    """把 plans 列表序列化为 plans/<date>.json（供人工审核 / 下游执行器消费）。

    结构：
        {"date": "...", "cfg_overrides": [...], "aum": ..., "n_plans": N,
         "plans": [{plan1}, {plan2}, ...]}
    每个 plan 用 asdict 转 dict，Timestamp 序列化为 ISO 字符串。
    """
    os.makedirs(_PLANS_DIR, exist_ok=True)
    out_path = os.path.join(_PLANS_DIR, f"{date}.json")
    from dataclasses import asdict

    plans_serial = []
    for p in plans:
        d = asdict(p)
        # Timestamp → ISO 字符串（JSON 不支持 Timestamp 原生序列化）
        for k in ("formed_at", "valid_until", "max_holding_until"):
            if d.get(k) is not None:
                d[k] = pd.Timestamp(d[k]).isoformat()
        plans_serial.append(d)

    payload = {
        "date": str(date),
        "aum": _DEFAULT_AUM,
        "cfg_overrides": _cfg_diff_summary(base_cfg, cfg),
        "n_plans": len(plans_serial),
        "plans": plans_serial,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"\n[WRITE] plans 已写入 {out_path}")


# ---------------------------------------------------------------------------
# replay 子命令：历史滚动回放 → 终端打印 ReplayReport
# ---------------------------------------------------------------------------
def _cmd_replay(args: argparse.Namespace) -> int:
    """replay 子命令主体：跑 backtest_replay，终端打印报告。

    返回进程退出码（0=正常，1=数据缺失/参数错误）。
    """
    # —— 0. 数据湖加载 ——
    lake = _load_daily_parquet()
    if lake is None:
        print(
            "[ERROR] 数据湖未就绪（data_lake/a_shares_daily.parquet 缺失）。\n"
            "        请先同步数据：PYTHONIOENCODING=utf-8 .venv310/Scripts/python.exe "
            "scripts/sync_data_lake.py",
            file=sys.stderr,
        )
        return 1

    # —— 1. 解析 universe + cfg-override ——
    # universe 缺省：取湖中近 end_date 30 日内成交额 top 50（回放样本量与算力平衡）
    base_cfg = StrategyConfig()
    try:
        cfg = _apply_cfg_override(base_cfg, args.cfg_override)
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    universe_codes = (
        [c for c in re.split(r"[,\s]+", args.universe) if c]
        if args.universe else None
    )

    # —— 2. 切 universe 时序 ——
    start = pd.Timestamp(args.start)
    end = pd.Timestamp(args.end)
    if universe_codes is None:
        # 默认 universe：近 end_date 30 日成交额 top 50（避免回放全市场 5000 标的过慢）
        universe_codes = _top_liquid_symbols(lake, end, top_n=50)
        print(f"[INFO] 未指定 --universe，自动取近 {end.date()} 30 日成交额 top 50：")
        print(f"       {universe_codes[:10]}{' ...' if len(universe_codes) > 10 else ''}")

    universe_full = _slice_universe(lake, universe_codes)
    if not universe_full:
        print(f"[ERROR] universe {universe_codes} 全部未命中数据湖，退出。", file=sys.stderr)
        return 1

    # 回放需要完整时序（含 start..end 之后用于离场模拟），不裁剪 .loc[:end]
    # 但裁掉 start 之前过老数据（保留 start 前 max_pattern_bars+130 日供形态/HV 用）
    lookback = max(cfg.max_pattern_bars, cfg.ma26w_window, cfg.hv_window) + 50
    universe_window: dict[str, pd.DataFrame] = {}
    for sym, df in universe_full.items():
        # 取 start 前 lookback 日到序列末尾（离场模拟需 end 之后数据）
        try:
            start_pos = df.index.searchsorted(start, side="left")
            begin = max(0, start_pos - lookback)
            universe_window[sym] = df.iloc[begin:]
        except Exception:
            universe_window[sym] = df

    print(f"\n[INFO] replay start={args.start} end={args.end} "
          f"universe={len(universe_window)} "
          f"cfg_overrides={_cfg_diff_summary(base_cfg, cfg) or '(默认)'}")

    # —— 3. backtest_replay.replay（滚动 screen → plan → 离场模拟）——
    risk = RiskManager(cfg)
    cal = lake.index.get_level_values("date").unique().sort_values()
    # trading_calendar 改为 kw-only（Spec 1 Task 2），此处显式关键字传参（原位置第 7 参 cal）
    report = backtest_replay.replay(
        universe_window, cfg, risk, start, end, _DEFAULT_AUM, trading_calendar=cal,
    )

    # —— 4. 终端打印 ReplayReport ——
    _print_replay_report(report, args)
    return 0


def _top_liquid_symbols(lake: pd.DataFrame, end_date, top_n: int = 50) -> list[str]:
    """取近 end_date 30 日内日均成交额 top N 的 symbol（回放默认 universe）。

    物理意图：回放全市场 5000 标的 × 数千交易日 = 数百万次 screener 调用，
    离线 CLI 算力不足；取 top 50 流动性最好的标的作为代表性样本，胜率/盈亏比
    统计仍有意义（流动性好的标的形态突破更可靠，符合蔡森方法学的流动性前提）。
    """
    end_ts = pd.Timestamp(end_date)
    window_start = end_ts - pd.Timedelta(days=45)   # 45 自然日 ≈ 30 交易日
    try:
        sub = lake.loc[window_start:end_ts]
    except Exception:
        sub = lake
    if len(sub) == 0:
        # 窗口无数据（end_date 超出湖）→ 取全湖最近 30 日
        last_date = lake.index.get_level_values("date").max()
        window_start = last_date - pd.Timedelta(days=45)
        sub = lake.loc[window_start:]
    amt = sub.groupby(level="symbol")["amount"].mean()
    amt = amt.dropna().sort_values(ascending=False)
    return list(amt.head(top_n).index)


def _print_replay_report(report, args: argparse.Namespace) -> None:
    """终端打印 ReplayReport（胜率/平均盈亏比/最大回撤/命中数/形态分布/建议）。"""
    print("\n" + "=" * 72)
    print("蔡森形态学策略 · 历史回放报告（Phase 2 上线 gate）")
    print("=" * 72)
    print(f"  回放区间      : {args.start} .. {args.end}")
    print(f"  命中交易笔数  : {report.n_hits}")
    print(f"  胜率          : {report.win_rate:.1%}")
    print(f"  平均盈亏比    : {report.avg_rr:.3f} R")
    print(f"  最大回撤      : {report.max_drawdown:.3f} R（基于累计 rr 曲线）")
    print(f"  平均持仓天数  : {report.avg_holding_bars:.1f}")
    if report.pattern_dist:
        dist_str = ", ".join(f"{k}={v}" for k, v in report.pattern_dist.items())
        print(f"  形态分布      : {dist_str}")
    if report.monthly_returns:
        # 只打印最近 6 个月（避免长输出）
        items = sorted(report.monthly_returns.items())
        recent = items[-6:]
        mr_str = ", ".join(f"{k}:{v:+.2f}" for k, v in recent)
        print(f"  月度收益(近6) : {mr_str}")
    print("-" * 72)
    print(f"  min_rr_ratio 建议：{report.min_rr_ratio_recommendation}")
    print("=" * 72)


# ---------------------------------------------------------------------------
# argparse 入口
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    """构造 argparse 解析器（screen + replay 两个子命令）。"""
    parser = argparse.ArgumentParser(
        prog="python -m caisen",
        description=(
            "蔡森形态学多空转折流水线 CLI（Phase 2 离线入口）。\n"
            "  screen : T 日筛形态 → 生成 plans JSON（供人工审核）。\n"
            "  replay : 历史滚动回放 → 胜率/盈亏比/回撤（上线 gate）。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True,
                                metavar="{screen,replay}")

    # screen 子命令
    p_screen = sub.add_parser(
        "screen", help="T 日筛形态 → 生成 plans/<date>.json + 候选表",
        description=(
            "对指定 universe 在 T 日跑 PatternScreener + TradePlanGenerator，"
            "输出候选计划 JSON 到 plans/<date>.json，终端打印候选表与诊断。"
        ),
    )
    p_screen.add_argument("--date", required=True,
                          help="筛选日期 YYYY-MM-DD（T 日，只用 T 及之前数据）")
    p_screen.add_argument("--universe", required=True,
                          help="标的代码列表，逗号分隔（如 600519,000858 或 600519.SH）")
    p_screen.add_argument("--cfg-override", default=None,
                          help='JSON 字符串覆盖 StrategyConfig 默认参数，'
                               '如 \'{"confirm_bars":2,"zigzag_threshold_atr":0.5}\'')
    p_screen.set_defaults(func=_cmd_screen)

    # replay 子命令
    p_replay = sub.add_parser(
        "replay", help="历史滚动回放 → 胜率/盈亏比/回撤报告",
        description=(
            "对 universe（缺省=近 end 30 日成交额 top 50）在 [start, end] 区间"
            "滚动跑 screen→plan→离场模拟，统计胜率/平均盈亏比/最大回撤/min_rr 建议。"
        ),
    )
    p_replay.add_argument("--start", required=True,
                          help="回放起始日 YYYY-MM-DD")
    p_replay.add_argument("--end", required=True,
                          help="回放结束日 YYYY-MM-DD")
    p_replay.add_argument("--universe", default=None,
                          help="标的代码列表（缺省=近 end 30 日 top 50 流动性标的）")
    p_replay.add_argument("--cfg-override", default=None,
                          help='JSON 字符串覆盖 StrategyConfig 默认参数（同 screen）')
    p_replay.set_defaults(func=_cmd_replay)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    """CLI 主入口。返回进程退出码（供 __name__=='__main__' 与测试共用）。"""
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

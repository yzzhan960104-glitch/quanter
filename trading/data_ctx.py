# -*- coding: utf-8 -*-
"""T1 模块化拆分 · 集群 B：lake 数据加载 helper（从 ``trading/engine.py`` 外迁）。

物理意图（spec §1.1 / deep-dive engine-current-state.md 集群 B）：
    引擎 ``_eod`` / ``_pre_open_gate`` 在数据就绪与盘后扫信号阶段需要一系列「从 lake /
    state_store / integrity 反查只读上下文」的纯 helper：标的宇宙、单标的截至日日线、
    cooldown 去重标的集、完整性 gate 上下文、识别窗口、计划数据集反查。这些 helper
    **零下游交易耦合**（不读 engine 实例状态、不下单、不写状态机），仅消费 ``lake``
    DataFrame / 模块级 ``state_store`` / ``data.integrity``——是 engine 内最独立的集群，
    故 T1 优先外迁。

外迁后去前导 ``_``（原 engine 内 private，外迁后变公开 API）；``engine.py`` re-export
新名 + 旧 ``_`` 名兼容（``engine`` 内部调用点保留旧 ``_`` 名，保既有
``patch("trading.engine._load_*")`` / ``eng._load_df_upto(...)`` 命中——行为等价红线）。

依赖边界（窄 Ports 红线）：
    - ``data.lake_reader`` 经 ``state_store`` 等模块级访问（不入 EnginePorts）；
    - 不依赖 engine 实例（``_plan_data_keys`` 原 engine 实例方法经验证不读 self 状态，
      安全改 free function；engine 类留薄 wrapper ``_plan_data_keys(self, plan)`` 调用
      ``plan_data_keys(plan)``，保 ``patch("trading.engine.TradingEngine._plan_data_keys")``
      + ``eng._plan_data_keys(plan)`` 测试命中）。

行为等价：函数体逐行原样从 engine.py 移入（lake/state_store 读写口不变、lazy import
位置不变、logger 异常文案不变；唯一差异：``logger`` 名从 ``trading.engine`` 变为
``trading.data_ctx``，仅影响日志 attribute，不影响功能）。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


# ============================================================================
# 策略数据源辅助（二期 gap② · _eod 从 data_lake 加载 universe + 单 symbol 前复权日线）
# ============================================================================
def load_universe(lake) -> list:
    """加载创板科创可交易标的池（300/301/688/689 开头）。

    物理意图：复用 data_lake/a_shares_daily.parquet（MultiIndex date,symbol，全市场
    5 年前复权日线），按 symbol 前缀过滤创板科创。

    ⚠️ 性能不变量（Task 7b fix · 性能阻断级修复）：
        本函数**绝不 read_parquet**——lake 由调用方（``_eod``）入口一次性读入后注入，
        全创板科创 1993 个标的共用同一份 DataFrame。
        历史 bug：每个 symbol 都重读 455MB parquet（1.75s/次）→ 58 分钟纯 I/O，
        19:00 的 ``_eod`` 根本无法在合理窗口完成。复用 lake 后整体扫描降至秒级。

    Why 收窄创板科创（不扫全市场）：
        颈线法 param_iter 基线口径（记忆 neckline-paramiter-baseline）——创板科创
        20cm 涨跌幅 + 流动性结构更契合颈线法形态学假设；主板/北交所不在该策略可交易池。
        实际环境若需扩池，按实际前缀在此调整（spec 红线：本过滤口径变更需同步基线重算）。
    """
    # lake 已由 _eod 入口 read_parquet 一次注入，此处仅做 symbol 前缀过滤（零 I/O）
    syms = lake.index.get_level_values("symbol").unique().tolist()
    return [s for s in syms if s.split(".")[0].startswith(("300", "301", "688", "689"))]


def load_df_upto(lake, symbol: str, date: str):
    """从已加载的 lake 取 symbol 截至 date 的前复权日线（严格因果 .loc[:date] · 无前视）。

    Args:
        lake:   ``_eod`` 入口一次性 ``pd.read_parquet`` 读入的 data_lake DataFrame
                （MultiIndex date,symbol）。本函数**不 read_parquet**，避免每 symbol 重读。
        symbol: 形如 "300001.SZ"（与 data_lake MultiIndex level="symbol" 一致）。
        date:   截断日（YYYY-MM-DD，_eod 传 T 日盘后日 today——见下方术语说明）。

    Returns:
        该 symbol 截至 date（含 date）的前复权日线 DataFrame（OHLCV，DatetimeIndex）；
        symbol 不在 data_lake → 返 None（调用方 None-check 跳过）。

    ⚠️ 性能不变量（Task 7b fix）：
        本函数**绝不 read_parquet**——从传入的 lake 做 xs 切片，全创板科创 universe
        复用同一份 DataFrame，1993 次 xs 从 1993 次 disk read 降为纯内存索引（毫秒级）。

    Why xs+sort_index+loc：
        - xs(level="symbol") 取单 symbol 切片（MultiIndex 标准范式）；
        - sort_index 保时间升序（ATR/MA 等时序算子前提）；
        - .loc[:date] 闭区间截断，防 today 之后的 K 线泄漏（前视偏差 = 回测致命）。
    """
    try:
        return lake.xs(symbol, level="symbol").sort_index().loc[:date]
    except KeyError:
        # symbol 不在 data_lake（新上市/退市/代码漂移）→ 返 None，调用方跳过
        return None


# ============================================================================
# plan Task 5（P0-5 cooldown 信号去重 · SSoT C2b）：扫最近 cooldown 自然日
# trade_event SIGNAL.formed_at 标的集（原扫 plan_*.json formed_at，C2b 切 DB）。
# ============================================================================
def load_recent_plan_symbols(days_back: int, today: str) -> set[str]:
    """扫最近 days_back 自然日（含 today）trade_event SIGNAL 的 symbol 集（按 meta.formed_at）。

    物理意图（plan Task 5 · SSoT C2b）：scan_live 无跨日去重，同形态被持续突破会连续多日
        触发信号 → 实盘连续挂单超额成交。spec §4.5：_eod scan 后查最近 cooldown 日 SIGNAL
        formed_at 标的集，同标的丢弃。C2b 切换真相源：plan_*.json formed_at（C2b 前）→
        trade_event SIGNAL.meta.formed_at（C2b 后，SSoT 红线：DB 是唯一真相源）。

    Why formed_at 是 cooldown 锚点（非 timestamp / 非 plan_date）：
        formed_at（信号突破日 T）=「该标的最近一次被识别为信号」的真实时间锚点；
        timestamp（clock.now 写入日）= T 日盘后（实际写入），跨日漂移会污染窗口；
        plan_date（T+1 计划生效日）晚一日，T+1 才生效的标的会被错算入 T 的窗口。
        唯一正确锚点 = formed_at。

    **致命日期轴·formed_at 时间戳坑（红线）**：
        meta.formed_at 落盘格式 = str(pd.Timestamp) = ``"2026-08-03 00:00:00"``（带时间戳，
        method_v0.py:268 ``W.index[-1]`` → plan.py:158 ``str(s.formed_at)``），非纯日期。
        list_signal_symbols_by_formed_at 内部用 ``substr(json_extract(meta,'$.formed_at'),1,10)``
        取前 10 字符匹配纯日期 IN 列表（数学验证见 state_store.list_signal_symbols_by_formed_at）。

    Why 自然日回溯而非交易日：
        cooldown 参数（exec_cfg["cooldown"]）本身是【交易日】单位（颈线法 EXEC_DEFAULTS），
        但本函数用自然日回溯是**保守上界**——自然日≥交易日数（含周末），故 cooldown=5
        交易日 ≈ 7 自然日；用 cooldown=5 自然日回溯可能漏掉周五+周末的 5 交易日窗口。
        取 days_back=cooldown+2（含周末余量）作为保守窗口，避免周末边界漏判。

    Args:
        days_back: 回溯自然日数（含 today），调用方传 cooldown+2 余量。
        today:     YYYY-MM-DD（_eod 调用时传 clock.today()）。

    Returns:
        最近 days_back 自然日发过 SIGNAL 的 symbol 集；DB 异常返空集
        （保守不去重——可能重复发信号，但比 cooldown 误杀或崩盘好；logger.exception 留痕）。
    """
    from datetime import datetime as _dt, timedelta as _td
    from trading import state_store
    today_dt = _dt.strptime(today, "%Y-%m-%d")
    # 回溯窗口：[today - days_back + 1, today]（含 today）→ 纯日期列表传 IN 参数化
    dates = [(today_dt - _td(days=i)).strftime("%Y-%m-%d") for i in range(days_back)]
    try:
        return state_store.list_signal_symbols_by_formed_at(dates)
    except Exception:
        # DB 异常（连接/损坏/锁）返空集：保守不去重（让所有新信号通过，由人审闸兜底），
        # 比 cooldown 误杀或 engine 崩盘好。logger.exception 留痕供运维定位。
        logger.exception("load_recent_plan_symbols 读 DB 失败返空集（cooldown 不去重）")
        return set()


def resolve_cooldown_days(experiments: list) -> int:
    """从 experiments 提取 cooldown（取所有实验里的最大值，保守去重）。

    Why 取 max 而非首个：多实验灰度场景下，不同实验可能配不同 cooldown（如新档 5、旧档 3）。
    按「最长 cooldown 跨日去重」最保守——避免新档信号被旧档短 cooldown 漏去重。
    缺失/异常返 0（不去重，向后兼容老链路）。
    """
    if not experiments:
        return 0
    try:
        cooldowns = []
        for exp in experiments:
            params = getattr(exp, "params", None) or {}
            cd = params.get("cooldown")
            if cd is not None and int(cd) > 0:
                cooldowns.append(int(cd))
        return max(cooldowns) if cooldowns else 0
    except Exception:
        logger.exception("resolve_cooldown_days 异常返 0（不去重）")
        return 0


# ============================================================================
# Task 7 U5 gate 下沉：完整性 gate 上下文加载 + 策略窗口解析（_eod 辅助）
# ============================================================================
def load_integrity_ctx(today: str):
    """加载完整性 gate 上下文：停牌区间 + 近 2 年 trade_days（fail-open）。

    物理意图（Task 7 U5 · 300214.SZ 漏采教训）：完整性 gate 从 scan_live 上提到 _eod
    后，_eod 需在 filter_universe_by_continuity 前加载 susp/trade_days。逻辑从原 scan_live
    内联的 ``_ensure_integrity_cache`` 搬出（模块级 cache 删除——_eod 每次盘后只调一次，
    无需跨调用缓存；若重复调用可再引入缓存）。

    fail-open 红线（与原 _ensure_integrity_cache:80-83 同口径）：
        加载失败（无文件/无 token/网络异常）→ 返 ({}, set()) 让 filter 放行。
        trade_days 空集 → check_window_continuity 的 expected 恒空 → missing 恒空 →
        ok=True 全放行，退回原行为（gate 是新增防护，失效时不阻断识别）。

    Returns:
        (susp_intervals, trade_days_set)：dict[str, set[str]] + set[str]。
    """
    try:
        from datetime import datetime as _dt, timedelta as _td
        from pathlib import Path as _Path
        from data.integrity import load_suspend_intervals_cached, fetch_trade_days
        start = (_dt.strptime(today, "%Y-%m-%d") - _td(days=730)).strftime("%Y-%m-%d")
        trade_days = fetch_trade_days(start, today)
        # susp 走 mtime 键缓存加载器（2026-08-19 W0）：旧路径每次全量读 parquet +
        # iterrows 重析 19.2 万行；向量化+同进程缓存后仅首次 ~0.2s。缺文件告警保留。
        susp_path = _Path("data_lake/suspend_d.parquet")
        if susp_path.exists():
            susp = load_suspend_intervals_cached(trade_days)
        else:
            logger.warning("suspend_d.parquet 缺失，完整性 gate 无停牌 ground-truth（全判漏采）")
            susp = {}
        return susp, trade_days
    except Exception as e:
        logger.warning("完整性 gate 上下文加载失败（fail-open 放行）：%s", e)
        return {}, set()


def resolve_id_window(strategy) -> int:
    """从策略实例解析识别窗口（颈线法 id_cfg["window"]）。

    Why 不硬编码：颈线法的 window 经 NecklineConfig 默认值 + cfg_override 覆盖后落在
    strategy.id_cfg["window"]（与原 scan_live:232 的 self.id_cfg["window"] 同源）。本函数
    安全兜底：策略无 id_cfg 属性或缺 window 键时返 DEFAULTS.window（60）。
    """
    try:
        w = getattr(strategy, "id_cfg", {}).get("window")
        if w and int(w) > 0:
            return int(w)
    except Exception:
        pass
    # 兜底：颈线法 DEFAULTS.window=60（与 method_v0.DEFAULTS 同口径）
    return 60


# ============================================================================
# 计划数据集反查（_pre_open_gate ③ 数据就绪段防御性双检用）
# ============================================================================
def plan_data_keys(plan: dict) -> set[str]:
    """从 plan 反推策略声明的数据集 key 并集（③ 数据就绪段防御性双检用）。

    物理意图（spec S3 · ③ 数据就绪段的「查哪些数据集」来源）：
        plan orders 携带 ``experiment_id``，经 ``resolve_active`` 反查
        ``strategy_name`` → ``build_strategy(name, params).required_data_keys``
        （Task 2 策略接口声明的依赖数据集），取并集。解析失败（无实验 / DB 锁 /
        策略未注册）→ 返 ``{"daily"}``（保守默认，③ 本就是防御性双检，回退默认
        不会误放行未就绪数据：daily 未就绪时 gate 仍会拦）。

    Why resolve_active 而非读 plan orders 内联策略名：plan orders 只存
        ``experiment_id``（归因字段），不存 ``strategy_name``——必须经 resolver
        反查才能拿到策略名 → build_strategy。Why 不缓存：pre_open 单进程每日仅
        一次调用，零缓存一致性成本。

    Args:
        plan: ``load_plan`` 返回的 dict（含 ``orders`` 列表，每项 ``experiment_id``）。

    Returns:
        数据集 key 并集（如 ``{"daily"}`` 或 ``{"daily", "moneyflow"}``）；
        解析失败/空 orders → ``{"daily"}``。

    T1 外迁注记：原 engine 实例方法 ``_plan_data_keys(self, plan)``，经验证不读 self
        状态（仅经 plan 参数 + 模块级 resolve_active/build_strategy/logger），改 free
        function 安全。engine 类留薄实例 wrapper ``_plan_data_keys(self, plan)`` 调本函数，
        保 ``patch("trading.engine.TradingEngine._plan_data_keys")`` +
        ``eng._plan_data_keys(plan)`` 测试命中（行为等价红线）。
    """
    keys: set[str] = set()
    try:
        from experiment.resolver import resolve_active
        from strategies.registry import build_strategy
        # ActiveExperiment 字段名是 experiment_id（非 .id，见 experiment/models.py:55）
        exp_map = {e.experiment_id: e for e in resolve_active()}
        for o in plan.get("orders", []):
            exp = exp_map.get(o.get("experiment_id"))
            if exp is not None:
                strat = build_strategy(exp.strategy_name, exp.params)
                keys |= set(strat.required_data_keys)
    except Exception:
        logger.exception("plan_data_keys 解析失败，回退默认 {daily}")
    return keys or {"daily"}


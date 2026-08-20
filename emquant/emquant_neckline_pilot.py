# -*- coding: utf-8 -*-
"""东财掘金·颈线策略单文件试点（组装产物，勿手改——改 pilot_body.py 后重跑 build_pilot.py）。
PARAMS_FINGERPRINT=3466a8fca0b554cb  生成物见 emquant/config/。
"""
from __future__ import annotations


# ============================ §0 参数区（export_snapshot 导出的定稿快照）============================
ID_PARAMS = {'breakout_vol_mult': 1.0, 'decay_tau': None, 'local_extrema_window': 5, 'max_h_atr': 5.0, 'min_bottoms': 3, 'min_rr': 2.0, 'min_suppression': 0.6, 'min_touches': 2, 'stop_atr_mult': 1.0, 'tp_h_mult': 2.5, 'window': 80}
EXEC_PARAMS = {'buy_limit_atr_mult': 0.5, 'cancel_thresh_mult': 2.0, 'commission_rate': 0.0003, 'cooldown': 8, 'max_holding': 20, 'max_wait': 8, 'stamp_rate': 0.0005, 'tp1_h_mult': 1.0, 'tp1_portion': 0.3, 'trailing_floor': 0.0, 'trailing_grace': 0, 'trailing_step': 0.0, 'transfer_rate': 1e-05}
TRADE_CFG = {'cancel_thresh_mult': 1.0, 'floor': 0.5, 'grace': 5, 'kelly_fraction': 0.25, 'kelly_hat': 0.0, 'max_holding': 15, 'max_wait': 5, 'pos_cap': 0.05, 'sizing_mode': 'fixed', 'step': 0.1, 'stop_atr_mult': 1.0, 'tp1_h_mult': 1.0, 'tp1_portion': 0.5, 'tp_h_mult': 2.0}
UNIVERSE = ['300308.SZ', '688825.SH', '300502.SZ', '688256.SH', '688836.SH', '300394.SZ', '688008.SH', '300750.SZ', '300476.SZ', '688525.SH', '688981.SH', '300408.SZ', '688012.SH', '300285.SZ', '688041.SH', '301308.SZ', '300274.SZ', '688498.SH', '688347.SH', '300136.SZ', '300604.SZ', '300433.SZ', '301526.SZ', '300223.SZ', '300475.SZ', '300059.SZ', '688072.SH', '300857.SZ', '300058.SZ', '688146.SH', '301217.SZ', '300666.SZ', '688766.SH', '300620.SZ', '688017.SH', '688521.SH', '688048.SH', '300570.SZ', '688126.SH', '688826.SH', '300757.SZ', '300548.SZ', '301511.SZ', '688313.SH', '300346.SZ', '300033.SZ', '300390.SZ', '300014.SZ', '301717.SZ', '688820.SH', '300395.SZ', '301396.SZ', '688120.SH', '300209.SZ', '688702.SH', '300054.SZ', '301205.SZ', '688797.SH', '688361.SH', '688808.SH', '688183.SH', '300319.SZ', '688627.SH', '688549.SH', '301666.SZ', '300418.SZ', '300672.SZ', '688630.SH', '688111.SH', '300442.SZ', '688629.SH', '300975.SZ', '301171.SZ', '688082.SH', '688167.SH', '688037.SH', '688110.SH', '300017.SZ', '301165.SZ', '300179.SZ', '300373.SZ', '301013.SZ', '688777.SH', '301583.SZ', '688362.SH', '300489.SZ', '300124.SZ', '688322.SH', '688396.SH', '300398.SZ', '300759.SZ', '688141.SH', '688519.SH', '301377.SZ', '300661.SZ', '300458.SZ', '300567.SZ', '300903.SZ', '688019.SH', '300450.SZ', '300037.SZ', '300776.SZ', '688234.SH', '688200.SH', '688300.SH', '300088.SZ', '688432.SH', '300751.SZ', '688195.SH', '300657.SZ', '300454.SZ', '300803.SZ', '300806.SZ', '688205.SH', '300782.SZ', '688668.SH', '688268.SH', '688409.SH', '301536.SZ', '300302.SZ', '688403.SH', '300503.SZ', '688002.SH', '688469.SH', '300438.SZ', '300085.SZ', '301358.SZ', '688635.SH', '688143.SH', '300870.SZ', '688507.SH', '688388.SH', '300184.SZ', '300260.SZ', '300139.SZ', '688249.SH', '300811.SZ', '301611.SZ', '301236.SZ', '301018.SZ', '688099.SH', '688596.SH', '300747.SZ', '301319.SZ', '688545.SH', '688025.SH', '300814.SZ', '301071.SZ', '300821.SZ', '688783.SH', '301486.SZ', '688308.SH', '688123.SH', '300738.SZ', '300568.SZ', '688257.SH', '301183.SZ', '688536.SH', '688235.SH', '300760.SZ', '688333.SH', '300164.SZ', '300364.SZ', '300131.SZ', '688172.SH', '300726.SZ', '688147.SH', '688802.SH', '301655.SZ', '688503.SH', '300115.SZ', '300679.SZ', '688795.SH', '301269.SZ', '688233.SH', '688676.SH', '301200.SZ', '300323.SZ', '300001.SZ', '300456.SZ', '300316.SZ', '300236.SZ', '301123.SZ', '300763.SZ', '688809.SH', '300339.SZ', '300042.SZ', '688548.SH', '300166.SZ', '688662.SH', '688411.SH', '300576.SZ', '300607.SZ', '688031.SH', '688301.SH', '300170.SZ', '688372.SH', '300769.SZ', '688700.SH', '300602.SZ', '300762.SZ', '300083.SZ', '688535.SH', '688331.SH', '300283.SZ', '301489.SZ', '301188.SZ', '300496.SZ', '300255.SZ', '688027.SH', '688220.SH', '688828.SH', '300342.SZ', '300623.SZ', '301128.SZ', '301707.SZ', '301392.SZ', '688800.SH', '300207.SZ', '300331.SZ', '300684.SZ', '300199.SZ', '300779.SZ', '300558.SZ', '300077.SZ', '301566.SZ', '300706.SZ', '300263.SZ', '300593.SZ', '300182.SZ', '300720.SZ', '688531.SH', '688387.SH', '688603.SH', '300383.SZ', '688106.SH', '300347.SZ', '688456.SH', '301292.SZ', '688158.SH', '300655.SZ', '688036.SH', '300953.SZ', '688585.SH', '688652.SH', '688213.SH', '688400.SH', '300835.SZ', '301005.SZ', '688385.SH', '688515.SH', '300843.SZ', '300613.SZ', '300322.SZ', '301150.SZ', '300265.SZ', '300748.SZ', '300420.SZ', '688729.SH', '301373.SZ', '688102.SH', '301550.SZ', '300328.SZ', '688047.SH', '688502.SH', '300174.SZ', '300024.SZ', '301338.SZ', '688052.SH', '688260.SH', '300671.SZ', '301389.SZ', '688449.SH', '301021.SZ', '688020.SH', '688390.SH', '688392.SH', '688122.SH', '688578.SH', '688559.SH', '688584.SH', '300201.SZ', '301362.SZ', '300566.SZ', '300480.SZ', '300534.SZ', '688343.SH', '301297.SZ', '300196.SZ', '300718.SZ', '301421.SZ', '301196.SZ', '688353.SH', '300142.SZ', '301099.SZ', '300102.SZ', '688169.SH', '300725.SZ', '300005.SZ', '300724.SZ']
PARAMS_FINGERPRINT = '3466a8fca0b554cb'
# 试点硬闸（spec FR3）：单日新挂 ≤2；单票市值 ≤5%；仿真账户固定。
PILOT_MAX_NEW_ORDERS_PER_DAY = 2
PILOT_MAX_POSITION_PCT = 0.05
PILOT_ACCOUNT_ID = 'e7cb55d6-04ab-4ea9-98fc-503d9f97d2a1'



# ============================ §1 识别内核（signal.py + method_v0.py 逐字块，C2）============================
# -*- coding: utf-8 -*-
"""Signal dataclass —— 颈线法信号统一封装（Layer2 阶段1 · 字段口径收敛）。

物理定位：
    收敛颈线法信号历史上两套 dict 字段口径：
      - 回测侧 ``TRADE_REQUIRED_KEYS``（scan_at 产出，含完整进出场闭环字段）；
      - 实盘侧 ``scan_live`` 字段（纯识别，仅 formed_at/neckline/bottom/entry_price/atr）。
    两套原本都是 ``list[dict]``，消费方靠字符串键 ``s["symbol"]`` 读，散落多处且无类型保护。
    本 dataclass 把两套字段并到**同一个 frozen 值对象**里，scan_at / scan_live 统一返
    ``list[Signal]``，signal_runner / backtest_replay 改读 dataclass 属性。

字段设计原则（极简 + 显式）：
    - 字段集 = scan_at ∪ scan_live ∪ 实验归因（_eod 注入）；
    - 两个入口按物理语义填字段，未涉及的字段保持 ``None`` 默认（不强行造值，不撒谎）：
        · scan_at 闭环填 entry_date/exit_date/exit_price/exit_reason/rr/holding_bars 等；
        · scan_live 纯识别只填 formed_at/neckline/bottom/entry_price/atr/breakout_date；
    - 归因字段（experiment_id / experiment_weight）默认值保证老链路零回归：
        experiment_id="" / experiment_weight=1.0（满仓口径）。
    - frozen=True：信号一经产出即不可变（spec §0「参数以不可变快照锁定」红线——
      止损价是实盘风险参数，跨实验串味 = 风险归因错配）。_eod 注入归因用
      ``dataclasses.replace`` 产出新 Signal，不在原对象上原地赋值。

不变量守卫：
    - 决策逻辑零改动：Signal 只封装返回，scan_symbol/scan_at/scan_live 的判定分支不动；
    - signal_runner 行为不变：改读 dataclass 属性后产出的 PlannedOrder 与改前一致
      （由 test_signal_runner* / T1 golden 守）。
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Signal:
    """颈线法信号值对象（识别 + 进出场 + 实验归因，一字段一义）。

    所有字段均可缺省（None / ""），由各生产方法按物理语义填——未填即代表该信号
    在此入口下不涉及该字段（如 scan_live 不涉及 exit_price，因为实盘 T-1 晚还没有
    未来 K 线来模拟出场）。消费方按各自需要读，缺失字段显式 None 兜底。
    """

    # ---- 标的 + 识别元信息（两入口共用）----
    symbol: str
    """标的代码（如 ``"600000.SH"``）。归因与下单路由的核心 key。"""

    signal_type: str = "neckline"
    """信号类型（颈线法固定 ``"neckline"``；保留字段供未来多策略 registry 分布统计）。"""

    formed_at: Any = None
    """信号形成日（颈线突破日，= detect 窗口末根 ``W.index[-1]``）。
    回测侧是 index label（pd.Timestamp / str）；实盘侧同义。"""

    # ---- 形态识别几何要素（两入口共用，止损/止盈计算依赖）----
    neckline: float | None = None
    """颈线价位 c*（顶部高点聚集定位 + 压制时长验证后的阻力位）。
    stop_price / take_profit 都以颈线为基准 ± N×ATR/H 算。"""

    bottom: float | None = None
    """形态谷底价（窗口最低点 min_price）。H = neckline - bottom 是风险报酬比标尺。"""

    entry_price: float | None = None
    """进场价。scan_at：simulate_exit 算出的挂单回踩成交价；scan_live：颈线价 c_star
    （挂单等回踩，breakout 当日只触发信号不追涨）。"""

    atr: float | None = None
    """信号日的 ATR（窗口对齐 id_cfg["window"]，非写死 14 天）。
    stop_price = neckline - stop_mult × ATR 依赖此值；signal_runner 优先用 signal 自身
    atr（C2 final-fix：防多实验同标的 atr_map 覆盖串味）。"""

    # ---- scan_live 实盘纯识别独有 ----
    breakout_date: Any = None
    """突破日（实盘纯识别用）。detect 内部只在末根突破时返，故 == formed_at；
    显式单列是防御层——未来 detect 若支持历史日回溯，靠此字段过滤只挂当日新信号。"""

    # ---- scan_at 回测一站式独有（simulate_exit 产出的进出场闭环）----
    entry_date: Any = None
    """实际进场日（挂单回踩成交日 buy_date；scan_live 无未来 K 线，不填）。"""

    exit_date: Any = None
    """离场日（止损/止盈/超时触发日）。回测统计 monthly_returns / trades 排序读此。"""

    exit_price: float | None = None
    """离场价（分级止盈两批加权均价，由 avg_pnl 反推）。回测 trades 流水读此。"""

    exit_reason: str | None = None
    """离场原因（stop_loss / tp1 / tp2 / timeout / skip_no_pullback / skip_target_met）。"""

    rr: float | None = None
    """盈亏比（风险倍数）。本字段语义重载，两入口按各自物理语义填：

    - **scan_live（detect-time 预期口径，R3 Task 2 2026-07-27）**：在形态识别阶段就
      填，rr = (tp2 − entry) / (entry − stop_price)，stop_price = 颈线 − N×ATR（与
      execute 层 base_stop 同口径）；供 PlannedOrder → order_dict → 钉钉 md 展示，
      让研究员 T-1 晚人审快速识别弱信号。
    - **scan_at（simulate_exit post-trade 实现口径）**：在回测模拟出场后填，rr =
      avg_pnl_pct / risk_pct（% / % = 风险倍数），与 caisen ``(exit−entry)/(entry−stop)``
      同语义；引擎统计层 win_rate / avg_rr 依赖此值做冠军择优。

    两口径物理含义一致（都是"风险倍数"），区别仅在 pre-trade 预期 vs post-trade 实现。
    消费方按入口约定读，跨入口混读会口径错配。"""

    holding_bars: int | None = None
    """持仓交易日数（exit_pos - buy_idx）。引擎统计 avg_holding_bars 读此。"""

    avg_pnl_pct: float | None = None
    """分级止盈 tp1_portion 加权平均收益率（%）。颈线法附加字段，详情展示用。"""

    # ---- 实验归因（_eod 注入，默认满仓口径保证老链路零回归）----
    experiment_id: str = ""
    """所属实验版本 ID（_eod 经 ``dataclasses.replace`` 注入；空串=老链路无归因）。"""

    experiment_weight: float = 1.0
    """资金权重（灰度分流；1.0=满仓口径，向后兼容老 signal_runner 调用）。"""

    # ---- 执行参数快照（参数单源收敛 · 2026-08-17）----
    exec_params: dict | None = None
    """产信号时的实验执行键全集（stop_atr_mult/tp_h_mult/tp1_h_mult/tp1_portion/
    max_wait/cancel_thresh_mult/max_holding/trailing 三件），由 detect_signal 从
    id_cfg+exec_cfg 抄录。物理意图：执行参数随信号**定终身**（对齐回测 simulate_exit
    静态 cfg 语义）——eod 装配价位、SIGNAL.meta 快照、_stoploss decide_cfg 逐单读此，
    消灭「计划侧实验参数 / 巡检侧 env 缺省」双源分叉。None=老链路（测试/构造直调），
    消费方 fallback env/内置缺省。frozen 契约：dict 引用不得原地 mutate。"""


def signal_to_dict(sig: Signal) -> dict:
    """Signal → dict（兼容老消费方 / JSON 落盘）。

    保留：trading_plan 落盘 / report 聚合等需要序列化的场景仍要 dict；新代码应直接
    读 dataclass 属性。所有字段一次性透出（含 None），不撒谎不省略。
    """
    from dataclasses import asdict
    return asdict(sig)


# -*- coding: utf-8 -*-
"""颈线法形态识别器 v0（最小版 · 逻辑验证用）。

物理定位：
    对 caisen 现行"拐点法"形态识别的范式替代实验——不依赖 zigzag 拐点提取，
    而是以颈线为核心、以价格聚集带为语言识别底部形态。

核心判定流程（压实后参数，零待定）：
    ① 窗口 W = 近 N 日（默认 60；20-120 区间待 replay 定标）
    ② 颈线 = 窗口内【顶部高点聚集】的价位（顶点连线定位）+【压制时长】验证
       （close<颈线的比例 ≥ min_suppression，价格长期被压在颈线下方才有效）
    ③ 底部 = 窗口最低点 min + [min, min+ATR] 内的离散局部极值低点（含 min ≥2 个）
    ④ 突破 = 末根收盘 close > 颈线 c*（信号触发）
    ⑤ 进场 = 颈线价 c*（挂单等回踩；close>c* 只触发信号，不追涨）
    ⑥ R3 实际口径盈亏比 rr = (tp2−entry)/(entry−stop_price)
       （stop_price = 颈线 − N×ATR，与 execute 层 base_stop 同口径；min_rr 验真实盈亏比）

交易要素（用户规则，持有期模拟见 neckline_backtest.py）：
    进场执行 = T+1 日收盘买入；止损 = 颈线 c*；止盈 = 50%@颈线+H，50%@颈线+2H；
    超时 = 15 日未达止盈收盘卖剩余。

风控边界（CLAUDE.md 极简 + 显式 + 防御性）：
    - 数据不足（< 窗口）/ ATR 无效 / 颈线或谷底异常 → 显式返 None；
    - 局部极值用左右各 w 根比较，排除窗口边界 w 根；
    - 窗口最低点强制纳入底部集合（anchor）。

用法：
    PYTHONIOENCODING=utf-8 python -u strategies/neckline/method_v0.py
"""

import math

import numpy as np
import pandas as pd
# P1（2026-08-13 · spec 2026-08-12-overall-optimization-design §2）：识别热路径向量化——
# local_extrema_mask 用滑动窗视图一次算全序列局部极值掩码（替代逐日窗口 Python 循环），
# numpy>=1.24 已锁（requirements.txt），零新增依赖。
from numpy.lib.stride_tricks import sliding_window_view

# Signal dataclass（Task 1 归位 strategies/neckline/signal.py）——detect_signal 装配
# 完整 Signal 返回。同包子相对 import（Layer2 Task 1.5 收口口径）。


# ============================================================================
# 压实后的参数（replay 定标项用默认值起步）
# ============================================================================
DEFAULTS = {
    "window": 60,              # ① 窗口（20-120 区间，起步 60）
    "min_touches": 2,          # ② 颈线由 ≥2 个顶部高点聚集连成（定位用，不要求频繁）
    "min_suppression": 0.6,    #    压制时长下限：≥60% 的 close 在颈线下方才算有效
    "local_extrema_window": 3, # ③ 局部极值左右各 3 根
    "min_bottoms": 2,          #    至少双底（含 min 在内）
    "breakout_vol_mult": 1.5,  #    突破带量 1.5×近5日均量（复用 caisen）
    "min_rr": 1.5,             # ⑥ 实际盈亏比下限（rr 实际口径 (tp2−entry)/(entry−stop_price)，min_rr 验真实盈亏比）
    "max_h_atr": 4.0,          # ⑦ 形态深度上限 H/ATR（实证：浅形态胜率51% vs 深形态27%，深=暴跌反弹）
    "stop_atr_mult": 1.0,      # ⑧ 止损 ATR 倍数（止损=颈线−N×ATR；参数化供迭代）
    "tp_h_mult": 2.0,          # ⑨ 止盈2 的 H 倍数（止盈2=颈线+N×H；参数化供迭代）
    "decay_tau": None,                 # ⑩ 颈线聚集时间衰减（日 exp(-dt/tau)）
                                      #    方案A(2026-07-19)修颈线漂移(513130 0.812→0.750)✓ 但全市场净-3.2点
                                      #    (29.4%→26.2%)：中小盘+3点(top100~400 10.3→13.3)但大盘拖累更大
                                      #    (近期颈线=弱阻力，大盘控盘弱失效)。当前最优等权29.4%，暂回None。
                                      #    颈线漂移问题真实但纯时间衰减非正解，留作后续(量加权或其他)。
}

# 顶部聚集的局部极值窗口（search_neckline 的 top_window——颈线顶部聚集口径固定，cfg 不可调）。
# P1 review-fix（2026-08-13）：向量化后该值散落 5 处（search_neckline 默认参 / 两处掩码
# 预计算 / fast path 边界零化 / backtest 预计算），收口单源防魔数漂移（等价红线）。
TOPS_WINDOW = 3


# ============================================================================
# 基元：ATR（自写避免依赖；原 caisen.patterns.zigzag_causal.compute_atr 同口径，
#       该模块已随 caisen 形态退役删除）
# ============================================================================
def compute_atr(high: pd.Series, low: pd.Series, close: pd.Series,
                window: int = 14) -> pd.Series:
    """ATR = TR 的 window 日均值（因果，min_periods=1 防早期 NaN）。

    TR（真实波幅）= max(当日H-当日L, |当日H-昨收|, |当日L-昨收|)，含跳空缺口。
    """
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.rolling(window, min_periods=1).mean()


def decay_weights_of(n: int, tau: float) -> np.ndarray:
    """len=n 衰减权重 exp(-((n-1)-i)/tau)（窗口相对 i 距窗口末根的天数，末根权重 1）。

    P1 review-fix（2026-08-13）：颈线聚集选位与衰减压制共用该权重序列，此前同式两套
    写法（np.arange(n)[::-1] 与 np.arange(n-1,-1,-1)）散落 3 处——收口单源防漂移。
    """
    return np.exp(-(np.arange(n - 1, -1, -1)) / tau)


# ============================================================================
# 局部极小值 / 极大值：离散拐点提取（避免每日 low/high 连续值的多计）
# ============================================================================
def local_minima(values, w: int):
    """局部极小值：某点比左右各 w 根都低（≤）即一个离散低点。排除首尾各 w 根。"""
    n = len(values)
    mins = []
    for i in range(w, n - w):
        left = values[i - w:i]
        right = values[i + 1:i + w + 1]
        if values[i] <= left.min() and values[i] <= right.min():
            mins.append(float(values[i]))
    return mins


def local_maxima(values, w: int):
    """局部极大值：某点比左右各 w 根都高（≥）即一个顶部高点。排除首尾各 w 根。"""
    n = len(values)
    maxs = []
    for i in range(w, n - w):
        left = values[i - w:i]
        right = values[i + 1:i + w + 1]
        if values[i] >= left.max() and values[i] >= right.max():
            maxs.append(float(values[i]))
    return maxs


def local_extrema_mask(values, w: int, kind: str = "min") -> np.ndarray:
    """全序列局部极值布尔掩码（P1 向量化基元 · 与 local_minima/local_maxima 位置语义逐位一致）。

    kind="min"：某点 <= 左右各 w 根（局部极小，对齐 local_minima）；kind="max"：>= 左右
    各 w 根（局部极大，对齐 local_maxima / search_neckline 内联 tops 检测）。True 位 =
    旧逐点循环会收集的位置——范围 [w, n-w)，即排除首尾各 w 根（旧 range(w, n-w) 语义）。

    向量化：sliding_window_view 一次构出全部长度为 w 的滑动窗，逐位取窗 max/min 与
    中心值比较。O(n) 内存 O(n×w) 计算，替代逐日窗口的 Python 级 O(n×w) 循环发射
    （P0-1 cProfile 实测 numpy reduce 叶子 1.455s 是 top-1 热点，即此循环的叶子开销）。

    NaN 语义：窗 max/min 用 numpy NaN 传播（与旧 values[i-w:i].min() 的 ndarray 口径
    一致——生产路径 detect 传的恒是 .values ndarray，NaN 邻居 → 比较 False → 不成极值）。
    短序列（n < 2w+1，旧 range 空循环）→ 全 False。

    注：旧 local_minima/local_maxima 函数保留（单测/外部脚本的行为锚），本掩码是
    fast path 的等价向量化版——两者一致性由 tests/test_p1_fast_path.py 直接对拍守护。
    """
    arr = values.values if hasattr(values, "values") else np.asarray(values)
    n = len(arr)
    mask = np.zeros(n, dtype=bool)
    if n < 2 * w + 1:
        return mask
    sw = sliding_window_view(arr, w)          # sw[j] = arr[j:j+w]，j ∈ [0, n-w]
    # 左窗 arr[i-w:i]（i ∈ [w, n-w)）→ sw[i-w]，j ∈ [0, n-2w)；右窗 arr[i+1:i+1+w]
    # → sw[i+1]，j ∈ [w+1, n-w]。max 用 >= / min 用 <= 与旧版严格对齐（并列极值都算）。
    if kind == "max":
        left = sw[:n - 2 * w].max(axis=1)
        right = sw[w + 1:n - w + 1].max(axis=1)
        ok = (arr[w:n - w] >= left) & (arr[w:n - w] >= right)
    else:
        left = sw[:n - 2 * w].min(axis=1)
        right = sw[w + 1:n - w + 1].min(axis=1)
        ok = (arr[w:n - w] <= left) & (arr[w:n - w] <= right)
    mask[w:n - w] = ok
    return mask


# ============================================================================
# 颈线搜索：顶部高点聚集定位 + 压制时长验证
# ============================================================================
def search_neckline(highs, closes, atr_val: float, min_touches: int, min_supp: float,
                    top_window: int = TOPS_WINDOW, decay_tau: float | None = None):
    """颈线 = 【顶部高点聚集】的价位（时间衰减加权定位）+ 【压制时长】验证。

    两步，角色严格分离：
      ① 定位（颈线在哪）：取窗口内【顶部高点（局部极大值）】，找它们聚集在哪个
         价位——即 ±ATR 带内含最多顶部高点的那个价位 c*。"顶点连成颈线"的本意。
      ② 验证（确认有效）：压制时长 = close<c* 的比例 ≥ min_supp。
         价格长期在颈线下方 = 阻力真实。

    为何不用"压制时长最大化"选位（旧版 bug）：
        c 越高 → close<c 越多 → 压制时长越大 → 选到窗口最高价附近，脱离真实阻力。
        压制时长只能当【验证】，不能当【选位标准】。选位必须用顶部聚集。

    时间衰减加权（2026-07-19 方案A，修颈线漂移 bug）：
        旧版等权聚集 Σ[顶部在±ATR带内]，固定窗口里旧高点（如 10-28 反弹顶，套牢盘
        已割肉=失效阻力）和近期高点（套牢盘还在=有效阻力）等权，旧高点污染颈线 +
        窗口滚动时颈线漂移（旧高点移出聚集中心，颈线下台阶"配合"假突破）。
        改 exp(−Δt/τ) 加权：近期顶部权重高（套牢盘还在），旧顶部淡出（套牢盘割肉）。
        Δt = 顶部到窗口末根（=当日）的天数，τ = 衰减常数（默认 30 日，主力近期记忆窗口）。
        聚集选位用加权 score，但要求等权 touches ≥ min_touches（聚集足够性，防衰减后
        单个近期顶部独占颈线）。decay_tau=None 退化为等权（兼容旧行为）。

    返回：(颈线价位 c, 压制时长 suppression)；无满足者返 (None, 0.0)。
    """
    # P1（2026-08-13）：旧版 inline tops 检测 + O(tops²) 双循环已下沉向量化内核
    # _neckline_cluster（与旧实现逐位等价，tests/test_p1_fast_path.py 随机扫场对拍守护）。
    # 本函数保留公开签名与语义契约（识别单源注释链），只做「掩码预计算 → 内核」薄包装。
    high_arr = highs.values if hasattr(highs, "values") else np.asarray(highs)
    close_arr = closes.values if hasattr(closes, "values") else np.asarray(closes)
    if len(high_arr) == 0:
        return None, 0.0
    tops_mask = local_extrema_mask(high_arr, top_window, kind="max")
    return _neckline_cluster(high_arr, close_arr, atr_val, min_touches, min_supp,
                             tops_mask, decay_tau)


def _neckline_cluster(highs_w, closes_w, atr_val, min_touches, min_supp,
                      tops_mask, decay_tau, decay_weights=None):
    """颈线聚集定位 + 压制验证的向量化内核（search_neckline 与 _detect_core_window 共用）。

    P1（2026-08-13 · spec §2.1）：替代旧 search_neckline 的 O(tops²) Python 双循环——
    tops×tops 外层差布尔矩阵 `D = |vals[:,None] - vals[None,:]| <= atr`，带内计数
    touches = D.sum(axis=1)，加权 score = D @ w_t（等权 w_t=1；衰减 w_t=exp(-Δt/τ)，
    Δt = 窗口末根(n-1) − 顶部窗口相对位置）。与旧实现逐位等价：

      - 首最大语义：旧 `if score > best_score and touches_eq >= min_touches` 顺序迭代
        严格更新 → 平局取位置靠前；向量化 `np.argmax(where(valid, scores, -inf))`
        对无效候选置 -inf、argmax 取首个最大——两者一致。
      - 压制时长：close<c* 的（衰减加权）比例 ≥ min_supp；等权退化为布尔计数/n。
      - 入参 tops_mask 为窗口相对布尔掩码（local_extrema_mask 产物，含 [w, n-w) 边界
        排除语义）；调用方（detect wrapper / scan_symbol fast loop）保证掩码与窗口对齐。

    返回 (颈线价位 c_star, 压制 suppression)；无满足者返 (None, 0.0)。
    """
    n = len(highs_w)
    tops_pos = np.flatnonzero(tops_mask)
    if len(tops_pos) < min_touches:
        return None, 0.0   # 顶部不够，连不成颈线
    tops_vals = highs_w[tops_pos]
    # 聚集布尔矩阵（对称）：带内 |t_i − t_j| <= atr
    D = np.abs(tops_vals[:, None] - tops_vals[None, :]) <= atr_val
    touches = D.sum(axis=1)
    use_decay = bool(decay_tau and decay_tau > 0)
    if use_decay:
        if decay_weights is None:
            # 衰减权重（窗口相对索引 i → exp(-((n-1)-i)/tau)，与旧 math.exp 逐位同口径）
            decay_weights = decay_weights_of(n, decay_tau)
        w_t = decay_weights[tops_pos]
    else:
        w_t = np.ones(len(tops_pos), dtype=np.float64)
    scores = D @ w_t
    valid = touches >= min_touches   # 等权 touches 是聚集足够性阈值（衰减不豁免）
    if not valid.any():
        return None, 0.0   # 无满足聚集足够性的价位
    best = int(np.argmax(np.where(valid, scores, -np.inf)))   # 首个最大（对齐严格 > 更新）
    c_star = float(tops_vals[best])

    if use_decay:
        weights = (decay_weights if decay_weights is not None
                   else decay_weights_of(n, decay_tau))
        sup_num = float(weights[closes_w < c_star].sum())
        suppression = float(sup_num / weights.sum())
    else:
        suppression = float((closes_w < c_star).sum() / n)
    if suppression < min_supp:
        return None, 0.0   # 压制时长不足，颈线无效
    return c_star, suppression


# ============================================================================
# 颈线法识别器主流程
# ============================================================================
def _detect_core_window(highs_w, lows_w, closes_w, vols_w, index_w, atr_val, cfg,
                        tops_mask_w=None, lows_mask_w=None, decay_weights=None):
    """detect 全守卫的数组内核（P1 · spec §2.1）——窗口切片视图 + 窗口相对掩码。

    与 detect_neckline_method 旧逐行逻辑逐位等价（三层守护：tests/test_p1_fast_path.py
    内核对拍 + tests/test_neckline_recognition.py 7 守卫 + P0-3 冻结基线 compare()）。
    入参均为「截至识别日的窗口」数组（长度 = cfg["window"]），掩码为窗口相对布尔掩码
    （local_extrema_mask 产物，含 [w, n-w) 边界排除语义）；None 则现场算（单次调用
    路径）。decay_weights 为 len=window 的衰减权重 exp(-((n-1)-i)/tau)，None 且
    decay_tau>0 时现场算（滚动扫描预计算复用，省每 T 重算 exp）。

    守卫顺序与旧版严格一致：ATR → 颈线（聚集+压制）→ 底部 → 突破 → 带量 → 深度 →
    盈亏比。任一不过返 None。
    """
    n = len(highs_w)
    if pd.isna(atr_val) or atr_val <= 0:
        return None

    if tops_mask_w is None:
        tops_mask_w = local_extrema_mask(highs_w, TOPS_WINDOW, kind="max")
    tau = cfg.get("decay_tau")
    if tau and tau > 0 and decay_weights is None:
        decay_weights = decay_weights_of(n, tau)

    # —— 1. 颈线搜索（顶部聚集定位 + 压制时长验证，时间衰减加权）——
    c_star, suppression = _neckline_cluster(
        highs_w, closes_w, atr_val, cfg["min_touches"], cfg["min_suppression"],
        tops_mask_w, tau, decay_weights)
    if c_star is None:
        return None  # 无有效颈线（顶部不足 或 压制时长不足）

    # —— 2. 底部（最低点 + 带内离散低点）——
    # 旧版 pandas lows.min() 是 skipna；数组侧 np.nanmin 同语义（生产 OHLCV 无 NaN，
    # 语义仍逐位对齐——等价陷阱清单第 3 条）。
    min_price = float(np.nanmin(lows_w))
    if lows_mask_w is None:
        lows_mask_w = local_extrema_mask(lows_w, cfg["local_extrema_window"], kind="min")
    lows_pos = np.flatnonzero(lows_mask_w)
    lvals = lows_w[lows_pos]
    lvals = lvals[(lvals >= min_price) & (lvals <= min_price + atr_val)]
    bottom_set = {round(min_price, 4)}
    bottom_set.update(round(float(b), 4) for b in lvals)
    if len(bottom_set) < cfg["min_bottoms"]:
        return None  # 不足双底

    # —— 3. 突破（收盘越过颈线 + 带量）——
    close_T = float(closes_w[-1])
    if close_T <= c_star:
        return None  # 未突破颈线
    vol_T = float(vols_w[-1])
    # 旧版 pandas tail(5).mean() 是 skipna；数组侧 np.nanmean 同语义
    vol5 = float(np.nanmean(vols_w[-5:]))
    if vol5 > 0 and vol_T < cfg["breakout_vol_mult"] * vol5:
        return None  # 突破未带量

    # —— 4. 交易要素（颈线 + 最低点 → 进场/止损/止盈/rr）——
    # 进场 = 颈线价 c*（挂单等回踩；close>c* 只触发信号，不追涨）。
    # 止盈用 H 几何标尺（形态目标位：颈线+1H 第一波、颈线+N×H 第二波），
    # N=cfg["tp_h_mult"] 参数化（默认 2.0，对齐 caisen plan.neckline_height_multiple=2）。
    entry = c_star
    H = c_star - min_price                          # 形态几何深度（tp 定位标尺）
    if H <= 0:
        return None
    # ⑦ 形态深度过滤：H/ATR > max_h_atr 视为"暴跌反弹"（深形态全市场实证胜率仅 27%）
    h_over_atr = H / atr_val
    if h_over_atr > cfg.get("max_h_atr", 4.0):
        return None
    take_profit_1 = c_star + H                      # 第一波满足（几何，颈线+1H）
    take_profit_2 = c_star + cfg["tp_h_mult"] * H   # 第二波满足（几何，颈线+N×H）
    # —— R3 实际止损 + 实际盈亏比（2026-07-27 Task 1）——
    # Why：旧版 rr=2H/H=2.0 是几何 sanity，止损用谷底（min_price）跟执行层 simulate_exit
    # 的实际止损（颈线−stop_atr_mult×ATR，base_stop）口径脱节——detect 说"风险=H"，
    # 执行层真止损在颈线−N×ATR（往往远高于谷底），min_rr 没把住真实风险收益。
    # 修正：detect 显式产出 stop_price（与执行层同口径），rr 用实际盈亏比
    # (tp2−entry)/(entry−stop_price)，min_rr 验真实盈亏比。
    # 止盈仍用 H 几何标尺（形态目标位，与 caisen plan 对齐），止损改 ATR 波动标尺（风控口径）。
    stop_price = c_star - cfg["stop_atr_mult"] * atr_val   # 实际止损（执行层 base_stop 同口径）
    risk_dist = entry - stop_price
    if risk_dist <= 0:
        return None  # 防御：颈线 ≤ stop_price（ATR 异常放大），无风险距离无意义
    rr = (take_profit_2 - entry) / risk_dist              # 实际口径盈亏比（替换旧 2H/H 几何）
    if rr < cfg["min_rr"]:
        return None

    return {
        "formed_at": index_w[-1],
        "neckline": round(c_star, 3),
        "suppression": round(suppression, 3),
        "bottom": round(min_price, 3),
        "n_bottoms": len(bottom_set),
        "entry": round(entry, 3),
        "stop": round(min_price, 3),                # 谷底（保留，H 计算基准 + 形态参考）
        "stop_price": round(stop_price, 3),         # R3 实际交易止损（颈线−N×ATR，执行层 base_stop 同口径）
        "take_profit_1": round(take_profit_1, 3),
        "take_profit_2": round(take_profit_2, 3),
        "H": round(H, 3),
        "H_over_ATR": round(h_over_atr, 2),          # 形态深度（实证关键分水岭）
        "rr": round(rr, 3),                          # R3 实际口径盈亏比（替换旧几何 2H/H）
        "atr": round(atr_val, 3),
    }


def detect_neckline_method(df: pd.DataFrame, cfg: dict = DEFAULTS, atr_series=None):
    """对单标的 OHLCV 时序执行颈线法识别，返回候选 dict 或 None。

    atr_series: 可选预算 ATR 序列（回测滚动复用，避免每 T 重算 compute_atr）；
                None 则内部现算。

    P1（2026-08-13）：7 守卫逻辑已下沉数组内核 _detect_core_window，本函数是公开签名的
    薄包装（df 路径：实盘 scan_live / 回测 scan_at / 测试调用）——tail(window) 提取数组
    → 现场算极值掩码 → 调内核。研究侧 scan_symbol 走 detect_signal_fast（同一内核，
    全序列预计算掩码复用）——识别单源（内核一份，两侧共享）。
    """
    window = cfg["window"]
    if len(df) < window:
        return None

    W = df.tail(window)

    # ATR：外部传 atr_series（回测预算复用）则用末根；否则内部全序列算。
    # 窗口对齐 cfg["window"]（颈线识别窗口），尺度统一——形态在 window 天形成，
    # 衡量其波动尺度也应用 window 天，而非写死的 14 天短期 ATR。
    if atr_series is not None:
        atr_val = float(atr_series.iloc[-1])
    else:
        atr_val = float(compute_atr(df["high"], df["low"], df["close"], window=cfg["window"]).iloc[-1])
    if pd.isna(atr_val) or atr_val <= 0:
        return None

    highs = W["high"].values
    lows_w = W["low"].values
    closes_w = W["close"].values
    vols_w = W["volume"].values
    # 极值掩码（现场算：单次调用路径；滚动扫描走 detect_signal_fast 全序列预计算复用）
    tops_mask = local_extrema_mask(highs, TOPS_WINDOW, kind="max")
    lows_mask = local_extrema_mask(lows_w, cfg["local_extrema_window"], kind="min")
    return _detect_core_window(highs, lows_w, closes_w, vols_w, W.index, atr_val, cfg,
                               tops_mask, lows_mask)


# ============================================================================
# detect_signal：识别纯函数（Task 2 · U2 识别统一）
# ============================================================================
# 物理定位：
#     从 NecklineMethodStrategy.scan_live（strategies/neckline_method.py:218-298）
#     抽取的【纯识别函数】——ATR 全序列预算 → detect_neckline_method（含 R2 窗口已突破）
#     → R1 cancel_on close 口径预判 → 当日突破过滤 → 装配完整 Signal。
#
#     本函数是【已测但未挂接】的纯函数（Task 2 范围）——scan_live/scan_at/scan_symbol
#     的调用点改接在 Task 3 做（strangler 红线：从 scan_live 逻辑零改动抽取，不顺手优化）。
#
# 等价性红线（与 scan_live:218-299 逐位一致）：
#     - ATR 全序列预算用 id_cfg["window"]（颈线识别窗口，非写死 14）—— line 221-223
#     - detect_neckline_method(df_upto, id_cfg, atr_series=atr_full) —— line 228
#     - cancel_on 守卫用【close】（已是 close 口径，D9 实盘侧就位）—— line 252-258
#     - 当日突破过滤（formed_at == date，两侧 ISO 日期字符串归一）—— line 264-273
#     - Signal 装配（entry=颈线+buy_limit_mult×ATR, atr=ATR末值, rr=res["rr"]）—— line 279-299
def detect_signal(symbol, df_upto, id_cfg, exec_cfg, date, atr_full=None):
    """颈线法识别纯函数：从截至 date 的 df_upto 产出完整 Signal 或 None。

    逻辑零改动抽取自 NecklineMethodStrategy.scan_live:218-298（strangler 红线）。
    与 scan_live 的唯一差异：scan_live 返 ``list[Signal]``（实盘多信号容器约定），
    本函数返 ``Signal | None``（纯识别单信号，调用方 Task 3 按入口语义包装成 list/None）。

    无前视契约：
        df_upto 由调用方（_eod / scan_at）从 data_lake 加载该 symbol 截至 date 的前复权
        日线（截断于 date，不含 date 之后），atr 也在 df_upto 上算——严格因果。

    参数：
        symbol: 标的代码（Signal 归因用，如 "600000.SH"）
        df_upto: 该 symbol 截至 date 的前复权日线 DataFrame（OHLCV，DatetimeIndex）
        id_cfg: 识别层参数 dict（window/min_touches/...，与 DEFAULTS 同键集）
        exec_cfg: 执行层参数 dict（buy_limit_atr_mult/cancel_thresh_mult/...，
                  与 EXEC_DEFAULTS 同键集）
        date: 当前识别日（_eod 传 T-1 收盘日，str 或 pd.Timestamp 均可）
        atr_full: 可选预计算 ATR 全序列（窗口对齐 id_cfg["window"]，index 与 df_upto
            对齐；滚动扫描调用方预算一次按 T 截断传入，省每 T 全量重算，P1-6）。
            None → 内部自算（实盘/单次调用零改动，向后兼容）。

    返回：
        Signal（含 symbol/formed_at/breakout_date/neckline/bottom/entry_price/atr/rr）
        或 None（detect 无命中 / cancel_on close 守卫触发 / 非当日突破）。
    """
    # ATR 预算（窗口对齐 id_cfg["window"]，与 scan_at / precompute 同口径）。
    # 物理意图：颈线在 window 天形成，衡量其波动尺度也用 window 天，而非写死 14 天。
    # 截至此处仅用 df_upto（无前视），末根即 date 当日的 ATR。
    # P1-6：调用方可传预计算 atr_full（滚动扫描预算一次按 T 截断）避免每 T 全量重算；
    # 传入序列须与 df_upto 同 index 前缀（截断至 date），末值即 date 当日 ATR。
    if atr_full is None:
        atr_full = compute_atr(
            df_upto["high"], df_upto["low"], df_upto["close"], window=id_cfg["window"]
        )

    # 识别：detect_neckline_method（df_upto 截至 date，atr_series 末根对齐）。
    # detect 仅在末根突破时返回（内部 close_T = W["close"].iloc[-1] > c_star 才命中），
    # 故 res["formed_at"] == df_upto.index[-1] == date（正常路径）。detect 内部已含
    # R2 窗口已突破判定（窗口内已突破形态返 None），detect_signal 直接透传 None。
    res = detect_neckline_method(df_upto, id_cfg, atr_series=atr_full)
    if res is None:
        return None   # 早退与旧版一致：df_upto 空/无效时不访问 close 列（防 IndexError）

    # R1 cancel_on 守卫 + 当日突破过滤 + Signal 装配已下沉 _post_detect（P1 · spec §2.1）——
    # 与 detect_signal_fast（研究侧数组路径）共用同一装配闭包，识别装配单源防分叉。
    close_T = float(df_upto["close"].iloc[-1])
    atr_last = atr_full.iloc[-1]
    return _post_detect(symbol, res, id_cfg, exec_cfg, date, close_T, atr_last)


def _post_detect(symbol, res, id_cfg, exec_cfg, date, close_T, atr_last):
    """detect 之后的 R1 cancel_on 守卫 + 当日突破过滤 + Signal 装配（识别装配单源）。

    P1（2026-08-13 · spec §2.1）：从 detect_signal 抽取的共用后半段——detect_signal
    （df 路径：实盘 scan_live / 回测 scan_at）与 detect_signal_fast（数组路径：研究侧
    scan_symbol）共用，防「研究侧 fast path 与实盘 df 路径装配分叉」。语义与原
    detect_signal 后半段逐位一致：
      - cancel_on 守卫（close 口径，D9）：close_T ≥ 颈线+cancel_thresh_mult×H → None
      - 当日突破过滤：formed_at 与 date 归一短 ISO 比较（C1 类型对齐 fix）
      - 装配：entry_price 回退 `(res.get("atr") or 0.0)`；atr 字段回退 `res.get("atr")`
        ——两处回退口径不同（带 or 0.0 / 不带），勿合并。
    """
    if res is None:
        return None

    # R1 cancel_on 预判（close 口径，D9 实盘侧就位）：挡缺口1+4。
    # What：把 execute 层 cancel_on 撤单逻辑前移为识别期预判——避免实盘挂上废单后再撤
    #       的滑点/费率/状态机污染。识别期用【当日 close】判（T-1 晚只有完整收盘 K 线，
    #       没有次日盘中 high 可用，无前视），execute 层 simulate_exit 用盘中 high 判
    #       （high≥cancel_on 摸高即撤）——close≥cancel_on 蕴含 high≥cancel_on，故
    #       close 守卫是 high 守卫的保守近似（识别期更严，execute 层不漏挡）。
    # 参数复用：exec_cfg["cancel_thresh_mult"]（默认 1.0=颈线+H），与 execute 层撤单
    #          阈值同口径——识别层挡多少、执行层就撤多少。
    # 物理标尺：H = 颈线 - 谷底（形态几何深度，detect 已返回 res["bottom"]）。
    cancel_thresh = exec_cfg.get("cancel_thresh_mult")
    if cancel_thresh is not None:
        H = res["neckline"] - res["bottom"]
        cancel_on = res["neckline"] + cancel_thresh * H
        if close_T >= cancel_on:
            return None  # 涨幅已兑现，不产回踩挂单信号（挡冲天突破）

    # 当日突破过滤（防御层）：只挂当日新信号。
    # Why：detect 物理上只在末根突破时返，此处等于 date 是常态；但显式校验防 detect
    # 内部窗口语义未来变化（如支持历史日回溯）时把旧信号当新信号重吐占仓。
    # 类型对齐（C1 final-fix）：detect 返 formed_at 是 pd.Timestamp，date 可能是 str
    # （_eod 真实调用约定 strftime 出来）。pandas __ne__ 不像 __eq__ 做字符串解析，
    # Timestamp != str 恒 True → 所有真实信号被误判为历史信号丢弃 → 实盘静默死亡。
    # 两侧统一用 pd.Timestamp(...).strftime("%Y-%m-%d") 归一为短 ISO 比较才在任意类型
    # 组合下都能正确做物理同日判定。
    breakout_date = res.get("formed_at")
    if pd.Timestamp(breakout_date).strftime("%Y-%m-%d") != pd.Timestamp(date).strftime("%Y-%m-%d"):
        return None

    # Signal dataclass（实盘纯识别字段集，不掺 simulate_exit 的出场字段）。
    # entry_price：颈线 + buy_limit_atr_mult × ATR末值（对齐回测 simulate_exit:75
    # buy_limit=c_star+buy_limit_atr_mult×atr）。mult=0 退化颈线（零回归）；
    # atr 缺失/NaN 回退 res["atr"]（与 scan_live:289-290 同口径）。
    # atr：用 atr_full 末值（对齐 date 当日，供二期引擎算止损=颈线−N×ATR 用）。
    atr_ok = float(atr_last) if not pd.isna(atr_last) else None
    return Signal(
        symbol=symbol,
        signal_type="neckline",
        formed_at=res.get("formed_at"),
        breakout_date=res.get("formed_at"),
        neckline=res.get("neckline"),
        bottom=res.get("bottom"),
        entry_price=(res.get("neckline") or 0.0) + exec_cfg.get("buy_limit_atr_mult", 1.0) * (
            atr_ok if atr_ok is not None else (res.get("atr") or 0.0)),
        atr=atr_ok if atr_ok is not None else res.get("atr"),
        # R3 实际口径盈亏比透传（detect 已算 (tp2-entry)/(entry-stop_price)，
        # 基于颈线-N×ATR 止损 / 颈线+N×H 止盈的真实风险报酬比），供研究员 T-1 晚人审。
        rr=res.get("rr"),
        # 执行参数快照（参数单源收敛 · 2026-08-17）：跨 id_cfg（stop/tp 乘数）+
        # exec_cfg（执行键）抄录解析值。exec_cfg 本身 = {**EXEC_DEFAULTS, **实验覆盖}，
        # 故此处即「实验口径（有覆盖时）或回测默认（无覆盖时）」，消费链
        # （eod 装配 → SIGNAL.meta → _stoploss decide_cfg）据此定终身，env 只兜底。
        exec_params={
            "stop_atr_mult": id_cfg.get("stop_atr_mult"),
            "tp_h_mult": id_cfg.get("tp_h_mult"),
            "tp1_h_mult": exec_cfg.get("tp1_h_mult"),
            "tp1_portion": exec_cfg.get("tp1_portion"),
            "max_wait": exec_cfg.get("max_wait"),
            "cancel_thresh_mult": exec_cfg.get("cancel_thresh_mult"),
            "max_holding": exec_cfg.get("max_holding"),
            "trailing_grace": exec_cfg.get("trailing_grace", 0),
            "trailing_step": exec_cfg.get("trailing_step", 0.0),
            "trailing_floor": exec_cfg.get("trailing_floor"),
        },
    )


def detect_signal_fast(symbol, arr, pos, id_cfg, exec_cfg, date, atr_arr,
                       tops_mask=None, lows_mask=None, decay_weights=None):
    """fast path 识别（P1 · spec §2.1）：全序列数组 + 截至 pos 的窗口识别。

    与 detect_signal(symbol, df.iloc[:pos+1], id_cfg, exec_cfg, date,
                     atr_full=atr.iloc[:pos+1]) 逐位等价——研究侧 scan_symbol 逐日滚动
    扫描改走本入口（预算好的 arr/极值掩码/ATR/衰减权重复用，窗口切片=零拷贝视图），
    消除每 T 的 sym_df.iloc[:i+1] + atr_full.iloc[:i+1] O(n²) DataFrame 拷贝
    （P0-1 实测识别路径占 scan_symbol cumtime ~80% 主导，本函数即对症改造）。

    参数：
        arr:  全序列数组上下文 {"high"/"low"/"close"/"volume": ndarray, "index": DatetimeIndex}
        pos:  截至位置（含，识别日 = arr["index"][pos]）
        atr_arr: 预算好的 ATR 全序列（compute_atr 产物 to_numpy()，窗口对齐 id_cfg["window"]）
        tops_mask/lows_mask: 全序列极值掩码（local_extrema_mask 产物，调用方预计算复用；
            窗口切片与「在窗口上现场算掩码」逐位一致——局部比较只用窗内邻居，P0 交接注记）
        decay_weights: len=window 衰减权重（调用方预计算；None 时内核现场算）
    其余参数语义同 detect_signal（date 归一 ISO 比较，cancel_on close 守卫共用 _post_detect）。
    等价性由 tests/test_p1_fast_path.py（df 路径 == 数组路径）+ P0-3 冻结基线守护。
    """
    window = id_cfg["window"]
    if pos + 1 < window:
        return None   # 数据不足窗口（与 detect len(df)<window 守卫同口径）
    atr_val = float(atr_arr[pos])
    if pd.isna(atr_val) or atr_val <= 0:
        return None   # ATR 无效（与 detect 的 atr 守卫同口径）
    s = pos - window + 1
    highs_w = arr["high"][s:pos + 1]
    lows_w = arr["low"][s:pos + 1]
    closes_w = arr["close"][s:pos + 1]
    vols_w = arr["volume"][s:pos + 1]
    index_w = arr["index"][s:pos + 1]
    # 掩码边界裁剪（等价红线关键）：全序列掩码的 True 位含窗口首尾各 w 根的「边界区」
    # ——这些位置的局部极值判定用了窗口外的邻居，与旧版 detect 的极值循环（range(w, n-w)
    # 只取窗口相对 [w, n-w)）口径不同。正确裁剪 = 取窗口切片后**零化首尾边界区**（保持
    # 窗口相对坐标不变；若用 [s+w:pos-w+1] 裁切片则 flatnonzero 产出的是切片相对位置，
    # 与窗口相对坐标差 w 偏移——P0-3 冻结基线对拍曾抓出两种口径的分叉）。零化后与
    # 「在窗口上现场算掩码」（df 路径 detect_neckline_method 的口径）逐位一致。
    # .copy() 必加：基本切片是视图，写零会污染调用方预计算的全序列掩码。
    tops_slice = None
    if tops_mask is not None:
        tops_slice = tops_mask[s:pos + 1].copy()
        tops_slice[:TOPS_WINDOW] = False            # 窗口相对 [0,3) 边界排除
        tops_slice[-TOPS_WINDOW:] = False           # 窗口相对 [n-3, n) 边界排除
    w_ext = id_cfg["local_extrema_window"]
    lows_slice = None
    if lows_mask is not None:
        lows_slice = lows_mask[s:pos + 1].copy()
        lows_slice[:w_ext] = False        # 窗口相对 [0, w_ext) 边界排除
        lows_slice[-w_ext:] = False       # 窗口相对 [n-w_ext, n) 边界排除
    res = _detect_core_window(highs_w, lows_w, closes_w, vols_w, index_w, atr_val, id_cfg,
                              tops_slice, lows_slice, decay_weights)
    close_T = float(closes_w[-1])
    return _post_detect(symbol, res, id_cfg, exec_cfg, date, close_T, atr_val)


# ============================================================================
# 测试入口：单标的滚动 replay（每历史日重判，验证逻辑闭环）
# ============================================================================
def main():
    lake_path = "data_lake/a_shares_daily.parquet"
    if not os.path.exists(lake_path):
        print(f"[ERROR] 数据湖缺失：{lake_path}")
        return
    print(f"加载 {lake_path} ...")
    lake = pd.read_parquet(lake_path)

    symbol = "000001.SZ"
    try:
        sym_df = lake.xs(symbol, level="symbol").sort_index()
    except KeyError:
        print(f"[ERROR] 标的 {symbol} 不在湖中")
        return

    window = DEFAULTS["window"]
    print(f"标的={symbol}，总K线={len(sym_df)}，窗口={window}")
    print(f"参数：{DEFAULTS}\n")

    hits = []
    for i in range(window, len(sym_df)):
        sub = sym_df.iloc[: i + 1]
        res = detect_neckline_method(sub, DEFAULTS)
        if res is not None:
            res["symbol"] = symbol
            hits.append(res)

    print(f"=== 识别到 {len(hits)} 个颈线法形态 ===\n")
    for h in hits[-15:]:
        print(
            f"{h['formed_at'].date()} | 颈线={h['neckline']:<8} "
            f"压制={h['suppression']:<5} 底={h['bottom']:<8} "
            f"{h['n_bottoms']}底 | 进={h['entry']:<8} 止损={h['stop']:<8} "
            f"止盈2={h['take_profit_2']:<8} rr={h['rr']}"
        )

    if hits:
        print(f"\n[样例详情] 最近一个命中：")
        for k, v in hits[-1].items():
            print(f"  {k}: {v}")


if __name__ == "__main__":
    main()



# ============================ §2-§7 执行编排（pilot_body.py）============================
# -*- coding: utf-8 -*-
"""§2-§7 执行编排（pilot_body）——单文件的业务躯干（Task 5-8 逐节增补）。

物理定位：
    本文件是【源】而非生成物：build_pilot.py 把它整段拼到组装单文件的 §2-§7 区
    （§0 参数区/§1 识别内核在前）。试点执行编排按任务波次落进来后重跑组装器。

拼接纪律（Why——单文件位次约束，违者 SyntaxError 或 C2/C4 红线事故）：
    - 禁 `from __future__ import ...`：future import 只允许出现在组装文件顶部
      （head 已放），本段拼在模块中部，再出现即 SyntaxError；
    - 禁相对 import（`from .xxx import`）：单文件无包结构，相对 import 必炸；
    - 禁顶层 `import gm`（C4）：gm 只允许函数体内惰性 import——保证无 gm 环境
      可完整 import 单文件跑识别内核等价性测试（本任务阶段天然无 gm，纪律先行）；
    - 只许标准库（C3 同源）：不 import 仓库任何模块——单文件部署到掘金终端后无
      仓库上下文，任何仓库依赖都当场 ImportError。本段 §3/§4 全部 stdlib 实现。
"""
import csv
import json
import os
import tempfile
from datetime import date, datetime
from pathlib import Path


# ============================ §3 状态层（state.pkl 原子读写 + 人工风控双值文件）============================
# 物理布局（设计 §3：掘金腿没有 DB/钉钉/台账，state.pkl + 两个人工可编辑文件就是全部持久面）：
#   <BASE_DIR>/state/state.pkl         策略唯一持久状态（schema v1，见 _initial_state）
#   <BASE_DIR>/state/RISK_BLOCK.flag   人工风控开关（存在即拦增量；ADR-16 block_new_orders 的文件化身）
#   <BASE_DIR>/state/CAP.txt           人工仓位上限（ADR-16 max_total_position 的文件化身，缺省 1.0）
#   <BASE_DIR>/audit/audit_YYYYMMDD.csv 对拍审计（本地腿同机直接读，§7 逐行补全事件族）
# Why flag/cap 放 state/ 内：三件都是「运行时真值」，落 gitignore 的 state/ 目录，
# 仓库工作区零污染；人工触达路径（touch/编辑）由 README runbook 钉死。
BASE_DIR = Path(__file__).resolve().parent   # 产物自定位：仓库内跑落 emquant/{state,audit}/，
STATE_DIR = BASE_DIR / "state"               # 部署到掘金终端策略目录时自动落策略文件旁（零配置）
AUDIT_DIR = BASE_DIR / "audit"
CONFIG_DIR = BASE_DIR / "config"             # runtime.json（token/strategy_id/account）——Task 8 读
STATE_FILE = STATE_DIR / "state.pkl"         # 文件名沿用设计文档；内容是 JSON（见 save_state 的 Why）
RISK_BLOCK_FLAG = STATE_DIR / "RISK_BLOCK.flag"
CAP_FILE = STATE_DIR / "CAP.txt"


def _initial_state() -> dict:
    """state.pkl schema v1 的空白态（缺文件/首启时的落点）。

    schema v1（设计 §3，冻结——load_state 见到别的 version 一律拒载）：
        version    int      schema 版本号（未来迁移的哨兵，防静默猜结构）
        scan_done  set[str] 当日已扫描标记（"YYYY-MM-DD"，幂等防重扫——识别本身是纯
                          函数重扫零风险，重扫只产生重复 audit 噪声行）
        placed     dict[str, list[str]] {date: [cl_ord_id, ...]} 当日已挂（幂等防重挂，
                          对齐本地 has_order(OPEN)+UNIQUE 的防重意图，cl_ord_id 是订单主键）
        orders     dict[str, dict] {cl_ord_id: {symbol,date,price,qty,purpose,
                          exec_params,status,...}}（Task 7 生命周期判定消费）
        positions  dict[str, dict] {symbol: {entry_date,entry_price,qty,remaining_qty,
                          stop,tp1_price,tp1_done,tp2_price,trailing:{...},exec_params}}
    """
    return {"version": 1, "scan_done": set(), "placed": {}, "orders": {}, "positions": {}}


def load_state(path=None) -> dict:
    """读 state.pkl → 内存 dict；缺文件返空白态（首启语义）。

    反序列化规则：scan_done 在盘上是 JSON list（set 非 JSON 原生），载入即回转 set；
    placed/orders/positions 为 JSON 原生结构原样返回。

    Why 损坏即抛（fail-loud 而非静默重置）：save_state 的 tmp+rename 原子写已消灭
    「写到一半崩溃」这一唯一常态损坏源——文件仍坏只可能是磁盘损坏或人工误编辑。
    此时静默回空白态会把 orders/positions 一并抹掉 = 持仓裸奔（无止损管理）；
    抛错让策略停在启动期，人工核对柜台实况后处置（对齐本地引擎「宁停不裸奔」
    基调，同 pre_open DB 幂等读失败即 _CriticalHalt 的取舍）。version 不等 1
    或缺 v1 必备键同抛：schema 冻结 v1，未来升版必须显式迁移，绝不静默猜。

    可选 path 参数：默认 STATE_FILE；测试/编排传显式路径实现隔离（不污染真值区）。
    """
    p = Path(path) if path is not None else STATE_FILE
    if not p.exists():
        return _initial_state()
    raw = json.loads(p.read_text(encoding="utf-8"))            # 损坏 → JSONDecodeError 上抛（fail-loud）
    if not isinstance(raw, dict) or raw.get("version") != 1:
        seen = raw.get("version") if isinstance(raw, dict) else type(raw).__name__
        raise RuntimeError(f"state.pkl schema 异变（version={seen!r} != 1，冻结 v1 须显式迁移）：{p}")
    missing = [k for k in ("scan_done", "placed", "orders", "positions") if k not in raw]
    if missing:
        raise RuntimeError(f"state.pkl 缺 schema v1 必备键 {missing}（文件不完整，须人工核对）：{p}")
    raw["scan_done"] = set(raw["scan_done"])                   # 盘上 list → 内存 set（成员测 O(1)，防重语义天然）
    return raw


def save_state(state: dict, path=None) -> None:
    """state 落盘（tmp+rename 原子写，设计 §3 写入纪律）。

    Why 原子写：进程在写入中途被杀/断电，若直接覆写目标文件，崩溃点落在半途 =
    state 损坏 = 持仓/挂单记忆全丢。先写同目录临时文件、fsync、再 os.replace
    原子改名（同目录保证同一文件系统，rename 不可分割），消灭整类恢复场景。
    replace 失败（断电/被占用）时清理临时文件后原样上抛——不留垃圾 tmp（试点
    目录人工晨检要看，残留 .tmp 会当事故排查半天），也绝不吞错假成功。

    序列化（JSON 而非 pickle，尽管文件名叫 state.pkl）：pickle 对象绑定 Python
    版本/类路径，掘金终端与仓库的 Python 版本不保证一致；JSON 文本人工可检视、
    可跨版本手工修复，且 scan_done 的 set→list 转换显式可控。落盘前浅拷贝转换
    （不改动调用方 state 对象——编排层手里还持有它继续跑当轮）；scan_done 排序
    落盘保证同状态两次落盘字节一致（人工 diff/未来对拍友好）。
    """
    target = Path(path) if path is not None else STATE_FILE
    payload = dict(state)                                      # 浅拷贝：只改写 scan_done 一个键
    payload["scan_done"] = sorted(state.get("scan_done") or ())
    payload.setdefault("version", 1)                           # 兜底：防手工构造的 dict 落出无版本文件
    target.parent.mkdir(parents=True, exist_ok=True)           # 首启目录不存在就地创建（终端冷启动）
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".state.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False))   # 值不可序列化 → TypeError 上抛（写侧免费校验）
            f.flush()
            os.fsync(f.fileno())                               # 先刷盘再改名：封掉「rename 成功而数据仍在页缓存」的断电窗口
        os.replace(tmp_name, target)                           # 原子改名（Windows MoveFileEx REPLACE_EXISTING 同语义）
    except BaseException:
        try:
            os.unlink(tmp_name)                                # 失败清理残 tmp（晨检目录干净）
        except OSError:
            pass                                               # 清理失败无害：tmp 不会被任何读路径触达
        raise


def audit_log(event: str, audit_dir=None, **fields) -> None:
    """对拍审计逐行追加（audit_YYYYMMDD.csv）——§7 审计事件的物理写入原语在此先落位。

    行格式恒定三列：ts,event,detail（detail 为排序 JSON 字段包）。Why 塞 detail
    而非逐字段开列：审计事件族（CAP WARN/信号/挂单/撤单/成交/巡检动作……Task 8
    逐个补）键值不定，三列结构让 Excel 人工复核与文本 grep 两头都好使，新事件
    零迁移。Why CSV 而非 JSONL：设计 §2 降级清单明示「audit CSV 每日人工复核
    （替代钉钉 CRITICAL 链）」——晨检工具是 Excel/文本编辑器，CSV 是最大公约数。
    追加模式（审计行只增不改，完整性优先于整洁；崩溃留半行人工可辨，不做原子写）。
    """
    d = Path(audit_dir) if audit_dir is not None else AUDIT_DIR   # 调用时查模块常量：monkeypatch 即生效
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"audit_{date.today():%Y%m%d}.csv"                 # 按日分文件：晨检只看当天、复核期自然滚动
    with p.open("a", encoding="utf-8", newline="") as f:       # newline=""：csv 模块接管换行（Windows CRLF 可控）
        csv.writer(f).writerow([
            datetime.now().isoformat(timespec="seconds"), event,
            json.dumps(fields, ensure_ascii=False, sort_keys=True, default=str)])


def is_blocked(path=None) -> bool:
    """人工风控开关（ADR-16 block_new_orders 的文件化身）：RISK_BLOCK.flag 存在即 True。

    Why 只看存在性不看内容：开关的触达方式就是人工 touch/rm（回退 SOP 第一步），
    任何内容语义（0/1/off）都给「着急拦单」多加一步出错机会。与本地
    pre_open.py:478 同口径：block 只拦【增量】（挂新买单整体跳过），存量管理
    （撤昨日单/超期平仓/trailing）照常执行——编排层（Task 8 pre_open 阶段④）
    在「扫描后、挂单前」调用本函数，为 True 跳过挂单段而非整个 pre_open。
    """
    p = Path(path) if path is not None else RISK_BLOCK_FLAG
    return p.exists()


def read_cap(path=None) -> float:
    """人工仓位上限（ADR-16 max_total_position 的文件化身）：读 CAP.txt → [0.0, 1.0]。

    两分支刻意不对称（与本地 state_store.py:2016 resolve_risk_control 同口径）：
      - 缺文件 → 1.0（不限制）+ audit WARN：CAP.txt 是可选收紧项，试点默认态就是
        「不设总仓位上限」（另有单日 ≤2 + 单票 ≤5% 两道试点硬闸兜底）——缺文件是
        正常态不是事故，但 WARN 留痕让晨检能发现「以为设了 0.5 其实文件没了」的
        静默失效；
      - 内容非法（非数字/越界 [0,1]/NaN/不可读）→ 0.0（全拦）+ audit WARN：
        人工想收紧却写错（典型：把 50% 写成 50）时，最坏组合是系统静默按 50.0
        放行——fail-closed，宁可误拦人工改文件。与本地「值损坏视同 0 全跳」
        （I-4）逐字同义。
    NaN 防渗透：float('nan') 参与链式比较 0<=v<=1 恒 False，反向写法
    `not (0<=v<=1)` 恰把 nan 归入非法支（正向写法 `v<0 or v>1` 拦不住 nan）。
    """
    p = Path(path) if path is not None else CAP_FILE
    if not p.exists():
        audit_log("WARN", type="cap_missing", msg=f"CAP.txt 缺失，按缺省 1.0（不限制总仓位）运行：{p}")
        return 1.0
    try:
        v = float(p.read_text(encoding="utf-8").strip())       # strip：容忍人工编辑的首尾空白/换行
    except (OSError, ValueError):
        audit_log("WARN", type="cap_invalid", msg=f"CAP.txt 不可读/非数字，视同 0（全拦）fail-closed：{p}")
        return 0.0
    if not (0.0 <= v <= 1.0):                                  # 链式比较把 NaN 也判 False → 非法支
        audit_log("WARN", type="cap_invalid", msg=f"CAP.txt 值 {v!r} 越界 [0,1] 或 NaN，视同 0（全拦）：{p}")
        return 0.0
    return v


# ============================ §4 风控闸（ADR-16 双值 + 试点三硬闸合一预检）============================
def check_caps(sod_state, equity, positions_mv, open_buy_amount, price, qty, today, cap=None):
    """挂单前三闸合一预检（纯函数：不落盘、不查柜台——一切实况由调用方喂入）。

    闸序（任一不过即返 (False, 中文原因)，全过返 (True, "")；原因字符串直接进
    audit，人工晨检靠它定位是哪道闸拦的）：
      ① fail-closed 前置：equity/positions_mv/open_buy_amount 任一为 None，或
         equity<=0，或本单金额<=0 → 拒。上游查询失败（gm get_cash/get_positions
         拉取异常**静默返空**——Task 2 核对结论）必须显式传 None 进来，绝不拿
         0 冒充真值；「不知道占多少时宁可多拦不可盲放」对齐 pre_open.py:508-539
         的三查任一失败全跳。NaN 防渗透：判空判负全用反向写法 not (x>0)，nan
         参与比较恒 False → 自然落拒（正向写法拦不住 nan）。
      ② CAP 总仓位额度：本单金额 ≤ equity×CAP − 持仓市值 − 未终态买单占额。与
         pre_open.py:540 同式（`总权益×比例 − market_value − open_buy`）；其中
         open_buy_amount 口径与 state_store.py:1470 get_open_buy_amount 同义——
         未终态 buy 委托按 (qty−filled)×price 合计（成交部分已体现在持仓市值不
         重复扣；卖单是退出方向不占增量额度）。「逐单扣减」由调用方承担：每挂出
         一单把本单金额累进 open_buy_amount 再喂下一单（对齐 pre_open 循环侧）。
      ③ 单日新挂 ≤ PILOT_MAX_NEW_ORDERS_PER_DAY（试点硬闸 FR3，§0 写死 2）：
         当日 placed 已达上限 → 拒（试点期规模闸，验收后可放开）。
      ④ 单票金额 ≤ PILOT_MAX_POSITION_PCT×equity（试点硬闸 FR3，§0 写死 5%）：
         一单一票，单票新增敞口即本单金额；同票次日补挂的聚合敞口由 ② 总闸兜底。
         挂单量公式 qty=⌊equity×pos_cap/entry/100⌋×100 本就按 5% 定尺，本闸是
         对「定尺漂移/人工误用」的二次核验。
    恰等边界一律放行：② 与 ④ 的比较均用严格 >（对齐 pre_open.py:602
    `_order_amt > _pos_quota` 才拦——恰好吃满额度是合法满仓，不是违规）。
    cap 参数：默认 None → 现场读 CAP.txt（read_cap 内嵌 WARN 审计留痕）；测试与
    编排层可显式注入数值，免文件依赖。
    """
    # ① fail-closed 前置：先归一再判（None 守卫在前，防 float(None) 直接 TypeError；
    #    数值脏成非数字串则 float() 抛错上抛——编排层 bug 该炸就炸，不静默吞）
    eq = None if equity is None else float(equity)
    mv = None if positions_mv is None else float(positions_mv)
    ob = None if open_buy_amount is None else float(open_buy_amount)
    amount = float(price) * float(qty)
    if eq is None or mv is None or ob is None or not (eq > 0):
        return False, (f"fail-closed：权益/持仓/挂额查询不完整（equity={equity!r} "
                       f"positions_mv={positions_mv!r} open_buy={open_buy_amount!r}），当日不挂")
    if not (amount > 0):
        return False, f"委托参数残缺（price={price!r} qty={qty!r}，金额非正），拒挂"
    # ② CAP 总仓位额度（pre_open.py:540 同式；open_buy 口径 state_store.py:1470 同义）
    c = read_cap() if cap is None else float(cap)
    quota = eq * c - mv - ob
    if amount > quota:
        return False, (f"总仓位额度不足：本单 {amount:.2f} > 余量 {quota:.2f}"
                       f"（equity×CAP{c:g}−持仓{mv:.2f}−已挂{ob:.2f}）")
    # ③ 单日新挂上限（试点硬闸 FR3）
    placed_today = sod_state.get("placed", {}).get(today, [])
    if len(placed_today) >= PILOT_MAX_NEW_ORDERS_PER_DAY:
        return False, (f"单日新挂已达试点上限 {PILOT_MAX_NEW_ORDERS_PER_DAY}"
                       f"（placed[{today}] 共 {len(placed_today)} 单）")
    # ④ 单票金额上限（试点硬闸 FR3）
    sym_cap = PILOT_MAX_POSITION_PCT * eq
    if amount > sym_cap:
        return False, f"单票金额 {amount:.2f} 超试点上限 {PILOT_MAX_POSITION_PCT:.0%}×equity={sym_cap:.2f}"
    return True, ""


def run_pilot():
    """试点主入口（Task 8 实现：五阶段事件编排——预开/开盘/盘中巡检/收盘/盘后）。"""
    raise NotImplementedError("Task 8 实现")


if __name__ == "__main__":
    run_pilot()

"""Task 11：CreditRegime 宏观信贷状态机 —— 日频 +1/0/-1 判别 + 无前视红线守护。

设计意图（为什么要有这套测试）：
    CreditRegime 是整个宏观 CTA 体系的【宏观锚】（Epic 2 因子之首）：
      - 网关(T14) 据此对持仓做【宏观否决】（收缩态禁止开新多）；
      - 前端(T16) 据此展示驾驶舱红/黄/绿宏观灯。
    故其正确性 = 整个执行链的风控正确性，必须用注入数据 + 显式断言锁死三条契约：
      1) 社融↑ + M1M2 剪刀差为正 + DR007 下行 → 宽信用扩张(+1)；
      2) 社融↓ + 剪刀差非正 + DR007 上行 → 紧信用收缩(-1)；
      3) **无前视红线**：compute(D) 只用 D 及之前的数据，D 之后塞入的"扩张"
         绝不能污染 D 当日的判断（前视偏差会让回测完美、实盘直接崩盘）。

macro 湖读取约定：
    本测试通过 macro_df 显式注入，绕开真实 parquet 湖；
    生产侧由 CreditRegime._series 内部经 DataLakeReader._lakes["macro"] 读取
    （macro 湖是 DatetimeIndex，无 symbol 层，与 daily/minute 的 MultiIndex 不同）。
"""
import pandas as pd

# RED 阶段：factors/macro_regime.py 尚未创建 → ImportError 即首次失败信号。
# GREEN 阶段：模块创建后此处正常 import。
from factors.macro_regime import CreditRegime


# --------------------------------------------------------------
# 构造：40 个工作日的合成宏观 DataFrame（DatetimeIndex，无 symbol 层）
# --------------------------------------------------------------

def _build_macro(*, shrzgm, m1m2_gap, dr007, periods: int = 40) -> pd.DataFrame:
    """构造与 sync_macro_credit 落盘结构一致的合成宏观湖（用于注入）。

    index 为工作日（对齐 A 股交易日），列为 shrzgm/M1M2_gap/dr007；
    严格 DatetimeIndex（非 MultiIndex），匹配 macro 湖的真实结构。
    periods 用于控制序列长度（默认 40，可缩短以测样本不足分支）。
    """
    idx = pd.date_range("2024-01-01", periods=periods, freq="B")
    macro = pd.DataFrame(index=idx)
    macro["shrzgm"] = shrzgm
    macro["M1M2_gap"] = m1m2_gap
    macro["dr007"] = dr007
    return macro


# --------------------------------------------------------------
# 契约 1：扩张态
# --------------------------------------------------------------

def test_expansion_when_credit_up_and_rates_down():
    """社融扩张 + M1M2 剪刀差为正 + DR007 下行 → compute 返回 +1（宽信用）。

    物理意图：社融↑ 代表实体融资需求旺盛，M1 增速 > M2 代表资金活化（活期
    存款上行、企业投资活跃），DR007 下行代表银行间流动性宽松——三者共振即
    宏观宽信用状态，对应【扩张/积极】信号 +1。
    """
    idx = pd.date_range("2024-01-01", periods=40, freq="B")
    macro = _build_macro(
        shrzgm=[100 + i for i in range(40)],          # 社融单调上行（扩张）
        m1m2_gap=[1.0] * 40,                          # 剪刀差恒正（资金活化）
        dr007=[2.5 - i * 0.01 for i in range(40)],    # 利率单调下行（宽松）
    )
    r = CreditRegime(macro_df=macro)
    assert r.compute(idx[-1]) == 1


# --------------------------------------------------------------
# 契约 2：收缩态
# --------------------------------------------------------------

def test_contraction_when_credit_down_and_rates_up():
    """社融收缩 + M1M2 剪刀差为负 + DR007 上行 → compute 返回 -1（紧信用）。

    物理意图：社融↓ 代表实体融资萎缩，M1 增速 < M2 代表资金沉淀为定期/储蓄
    （企业不愿投资），DR007 上行代表银行间流动性收紧——三者共振即宏观紧信用
    状态，对应【收缩/防御】信号 -1。
    """
    idx = pd.date_range("2024-01-01", periods=40, freq="B")
    macro = _build_macro(
        shrzgm=[200 - i for i in range(40)],          # 社融单调下行（收缩）
        m1m2_gap=[-1.0] * 40,                         # 剪刀差恒负（资金沉淀）
        dr007=[2.0 + i * 0.01 for i in range(40)],    # 利率单调上行（收紧）
    )
    r = CreditRegime(macro_df=macro)
    assert r.compute(idx[-1]) == -1


# --------------------------------------------------------------
# 契约 3：无前视红线（最关键的量化风控红线）
# --------------------------------------------------------------

def test_no_lookahead_only_uses_past():
    """compute(D) 只用 D 及之前的数据（D 之后的扩张不影响）。

    红线：compute(D) 必须用 .loc[:D] 严格时间门控，D 之后塞入的任何值
    都不可被感知。否则即【前视偏差】——把未来信息提前泄漏给历史日，
    回测曲线会完美但实盘直接崩盘（典型未来函数陷阱）。

    构造：全 0 中性宏观态，在【末尾日】(idx[-1]) 塞入社融=9999；
    若 compute(D=idx[20]) 仍受末尾日 9999 影响 → 说明读到了未来值 → 前视泄露。
    """
    idx = pd.date_range("2024-01-01", periods=40, freq="B")
    macro = _build_macro(
        shrzgm=[100] * 40,
        m1m2_gap=[0.0] * 40,
        dr007=[2.0] * 40,
    )
    # 在 D 之后（末尾日）人为插入"虚假扩张"，compute(D) 不应感知
    macro.loc[idx[-1], "shrzgm"] = 9999
    r = CreditRegime(macro_df=macro)
    d = idx[20]
    # D 当日的判断：D 及之前全中性、末尾日 9999 扩张在 D 之后 → 必须 0。
    # 旧弱断言 `in (0,1,-1)` 过弱（compute 本就只能返这三者，前视泄露也过），
    # 升级为严格 == 0 守住前视红线。
    assert r.compute(d) == 0


def test_no_lookahead_neutral_when_future_only_expansion():
    """无前视强化：D 之前全中性、所有"扩张"都在 D 之后 → compute(D) 必须=0。

    构造前半段（D 及之前）严格中性、后半段（D 之后）剧烈扩张，
    compute(D) 必须仍判定为 0（中性）——任何对后半段扩张的感知都是前视泄露。
    """
    idx = pd.date_range("2024-01-01", periods=40, freq="B")
    macro = _build_macro(
        shrzgm=[100] * 40,
        m1m2_gap=[0.0] * 40,
        dr007=[2.0] * 40,
    )
    # D = idx[20]：D 之后（idx[21:]）剧烈扩张，但 compute(D) 不应感知
    for i in range(21, 40):
        macro.iloc[i, macro.columns.get_loc("shrzgm")] = 100 + (i - 20) * 10
        macro.iloc[i, macro.columns.get_loc("M1M2_gap")] = 5.0
        macro.iloc[i, macro.columns.get_loc("dr007")] = 2.0 - (i - 20) * 0.05
    r = CreditRegime(macro_df=macro)
    d = idx[20]
    # D 及之前全中性 → 必须 0；若返回 1 说明读到了 D 之后的扩张数据 → 前视泄露
    assert r.compute(d) == 0


# --------------------------------------------------------------
# 契约 4：样本不足（< 20 日）→ 安全返 0（中性）
# --------------------------------------------------------------

def test_returns_zero_when_insufficient_history():
    """数据不足 20 个观测 → 安全返 0，避免小样本趋势误判。

    物理意图：20 日（≈1 个月工作日）是判断"趋势"的最小可信样本；
    不足 20 日时统计无意义，强制返 0（中性）守护执行层不做激进动作。
    """
    idx = pd.date_range("2024-01-01", periods=10, freq="B")  # 仅 10 日 < 20
    macro = _build_macro(
        shrzgm=[100 + i for i in range(10)],
        m1m2_gap=[1.0] * 10,
        dr007=[2.5 - i * 0.01 for i in range(10)],
        periods=10,
    )
    r = CreditRegime(macro_df=macro)
    assert r.compute(idx[-1]) == 0


# --------------------------------------------------------------
# 契约 5：单例 get_default 双重检查锁
# --------------------------------------------------------------

def test_get_default_singleton():
    """get_default() 在同进程内返回同一实例（双重检查锁保证线程安全）。

    单例语义：网关(T14)/前端(T16) 共用同一 CreditRegime 实例，避免重复
    构造与 macro 湖重复加载；多线程下双重检查锁防止竞态下创建多个实例。
    """
    # 重置单例（防止其他测试污染本断言的"同一实例"语义）
    CreditRegime._instance = None
    try:
        a = CreditRegime.get_default()
        b = CreditRegime.get_default()
        assert a is b
    finally:
        # 清理：避免污染后续测试的 get_default 单例（生产侧 _macro 仍由 lifespan 注入）
        CreditRegime._instance = None

# -*- coding: utf-8 -*-
"""内核逐字等价（C2 红线）：组装文件中的 signal/method_v0 与仓库真身逐字节一致
（仅允许删 `from .signal import Signal` 行与提升 __future__ 两变换）。

物理定位：
    emquant 单文件试点的全部可信度押在一条等价链上——「仓库识别内核」与「掘金侧
    单文件识别内核」对同一段行情必须产出同一个 Signal。若组装过程（手工誊抄/
    顺手重构/字节漂移）引入任何非白名单差异，双轨对照即从「同一策略的两次执行」
    退化为「两个相似策略」，对照结论作废。本文件四层钉死：
      ① 字节等价：normalize（两侧各删两条白名单行）后 substring 比对——
         kernel 源码逐行完整出现在组装产物里；
      ② 可 import（C4）：gm 打毒丸（sys.modules['gm']=None）后单文件仍可完整
         exec——pilot 段不得顶层 import gm，无 gm 环境可跑等价测试；
      ③ 行为等价（随机游走）：同 df/同参数双跑，字段级一致（无信号侧 None==None）；
      ④ 行为等价（确定性命中）：按快照实验参数量身构造的 80 根颈线形态，
         repo/单文件两侧必须都命中且全字段一致——防 ③ 退化为两侧恒 None 的
         弱断言（None==None 永真，只有命中态才能证明数值装配无漂移）；
      ⑤ 组装幂等：build 纯函数无时间戳，两次产物逐字节一致且 == 已提交产物
         （生成物被手改立即暴露）。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ARTIFACT = ROOT / "emquant" / "emquant_neckline_pilot.py"
BUILD_SCRIPT = ROOT / "emquant" / "build_pilot.py"

# C2 白名单变换的两条目标行（build_pilot._strip 与本文件 _normalize 必须同集）
FUTURE = "from __future__ import annotations"
SIGNAL_IMPORT = "from .signal import Signal"


def _import_artifact():
    """按文件位置 exec 组装产物为独立模块。

    必须先注册进 sys.modules 再 exec：§1 内核的 Signal 是 dataclass + future
    annotations（PEP 563 字符串注解），dataclasses 解析字符串注解要回查
    sys.modules[cls.__module__]——不注册则 exec 当场 AttributeError(NoneType)。
    每次覆写同名条目 = 每次全新执行（产物更新后不读缓存）。
    """
    spec = importlib.util.spec_from_file_location("emquant_neckline_pilot", ARTIFACT)
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m


def _normalize(src: str) -> str:
    """白名单变换的逆：两侧各删 __future__ 行与 Signal 相对 import 行后必须逐行相等。

    删除语义刻意收紧为【整行 strip 后与白名单字符串完全相等才删】——不做子串/
    前缀匹配，防归一化过松吞掉伪装行（如 `from .signal import SignalX` 不得被误删，
    必须原样留在两侧参与比对）。
    """
    drop = (FUTURE, SIGNAL_IMPORT)
    return "\n".join(l for l in src.splitlines() if l.strip() not in drop)


# ============================================================================
# ① 字节等价（C2 红线）
# ============================================================================
def test_kernel_byte_equivalent():
    art = ARTIFACT.read_text(encoding="utf-8")
    sig = (ROOT / "strategies/neckline/signal.py").read_text(encoding="utf-8")
    mv0 = (ROOT / "strategies/neckline/method_v0.py").read_text(encoding="utf-8")
    # 白名单命中次数钉死：仓库侧每条恰好 1 行（signal.py 无相对 import 行）——
    # 若内核新增第二条相对 import/__future__，次数断言先炸，逼人工扩白名单评审，
    # 而非被 _strip/_normalize 静默吸收（白名单意外扩格 = C2 事故）。
    assert sig.count(FUTURE) == 1
    assert mv0.count(FUTURE) == 1
    assert mv0.count(SIGNAL_IMPORT) == 1
    # 组装侧相对 import 行必须删净（C2 变换①）；__future__ 只允许存活于文件顶部
    # （变换②提升，§1 内核区内出现即语法错误，import 自会拦）。
    assert art.count(SIGNAL_IMPORT) == 0
    assert _normalize(sig) in _normalize(art)          # signal 段逐字在
    assert _normalize(mv0) in _normalize(art)          # method_v0 段逐字在


# ============================================================================
# ② 无 gm 可 import（C4 红线）
# ============================================================================
def test_artifact_importable_without_gm():
    # 毒丸口径（与 runbook 无 gm 验证命令同式）：sys.modules['gm']=None 使任何
    # `import gm` 立即 ImportError——在装有 gm 的 .venv_emquant 里同样具备杀伤力，
    # 否则「产物碰巧没 import gm」与「gm 在场被静默 import 成功」无法区分。
    sys.modules["gm"] = None
    try:
        m = _import_artifact()                          # C4：无 gm 也能 import
        assert hasattr(m, "detect_signal") and hasattr(m, "Signal")
    finally:
        del sys.modules["gm"]                           # 清毒丸，不污染同进程其他用例


# ============================================================================
# ③ 行为等价：随机游走合成数据双跑（无信号侧 None==None 亦过）
# ============================================================================
def test_detect_signal_behavior_equal():
    import pandas as pd, numpy as np
    repo_det = _repo_detect_signal()
    m = _import_artifact()
    rng = np.random.default_rng(42)                     # 合成上行突破形态
    n = 120; base = 10.0                                # n=120 > window=80，实验口径窗够长
    close = base + np.cumsum(rng.normal(0.002, 0.02, n))
    high = close * (1 + np.abs(rng.normal(0, 0.008, n))); low = close * (1 - np.abs(rng.normal(0, 0.008, n)))
    vol = np.full(n, 1e6) * (1 + np.abs(rng.normal(0, 0.3, n)))
    idx = pd.date_range("2026-05-01", periods=n, freq="B")
    df = pd.DataFrame({"open": close * 0.999, "high": high, "low": low, "close": close, "volume": vol}, index=idx)
    # 两侧同 df/同参数（§0 快照注入的实验实弹口径）——差异只可能来自代码本身
    a = repo_det("600000.SH", df, m.ID_PARAMS, m.EXEC_PARAMS, idx[-1])
    b = m.detect_signal("600000.SH", df, m.ID_PARAMS, m.EXEC_PARAMS, idx[-1])
    assert (a is None) == (b is None)
    if a is not None:
        for k in ("symbol", "neckline", "bottom", "entry_price", "atr", "rr"):
            assert getattr(a, k) == getattr(b, k)


# ============================================================================
# ④ 行为等价：确定性命中形态（快照实验参数量身构造，两侧必须都出信号）
# ============================================================================
def _df_positive_case():
    """80 根确定性颈线形态（专为快照 ID_PARAMS 量身设计，无随机性）。

    设计推演（对照 _detect_core_window 七守卫，快照实验口径 window=80）：
        顶部聚集：顶高点 100.5/101/100/99.5 四处 local max（±3 窗），带内互距
                  ≤1.5 < ATR≈4.2 → touches=4 ≥ min_touches=2，颈线=首个最优=100.5；
        压制：    全窗 close ≤99 < 100.5（仅末根 104 突破）→ 压制≈79/80 ≥ 0.6；
        底部：    四谷低点 90/91/92/93 递升（±5 窗 local min，位置 12/28/44/60 均在
                  [5,75) 掩码区内），带内 [90,90+ATR] 离散值 ≥3 = min_bottoms=3；
        突破带量：末根 close=104 > 100.5，vol=500 ≥ 1.0×vol5=180（breakout_vol_mult=1）；
        深度：    H=10.5，H/ATR≈2.5 ≤ max_h_atr=5（非暴跌反弹）；
        盈亏比：  rr=2.5×H/ATR≈6.2 ≥ min_rr=2（真实口径 (tp2−entry)/(entry−stop)）；
        cancel_on：close_T=104 < 颈线+2.0×H=121.5（exec cancel_thresh_mult=2）。
    关键锚点间线性插值成 OHLC；open=前根 close（无跳空，TR 纯振幅可控）。
    """
    import pandas as pd
    # 锚点（pos, low, high, close）：谷顶交替的锯齿波 + 末段爬升至颈线下沿
    kpts = [
        (0, 94.0, 98.0, 96.0),
        (12, 90.0, 95.0, 91.0),   # 谷1（low=90=窗口最低 → bottom 锚）
        (20, 96.0, 100.5, 98.0),  # 顶1（high=100.5 → 颈线带）
        (28, 91.0, 96.0, 92.0),   # 谷2
        (36, 97.0, 101.0, 99.0),  # 顶2
        (44, 92.0, 97.0, 93.0),   # 谷3
        (52, 96.0, 100.0, 98.0),  # 顶3
        (60, 93.0, 98.0, 94.0),   # 谷4
        (68, 97.0, 99.5, 98.5),   # 顶4
        (76, 94.5, 96.5, 95.5),   # 末段回踩蓄势
        (78, 97.5, 99.5, 98.0),   # 爬升至颈线下沿（未提前突破）
    ]

    def _interp(pos):
        for (p0, *v0), (p1, *v1) in zip(kpts, kpts[1:]):
            if p0 <= pos <= p1:
                t = (pos - p0) / (p1 - p0)
                return tuple(a + (b - a) * t for a, b in zip(v0, v1))
        raise ValueError(pos)

    rows = []
    for i in range(80):
        if i == 79:  # 突破根：close=104 越颈线 + 带量 500（其余根恒量 100）
            rows.append((103.0, 106.0, 99.0, 104.0, 500.0))
            continue
        low, high, close = _interp(i)
        open_ = rows[-1][3] if rows else (high + low) / 2
        rows.append((open_, high, low, close, 100.0))
    idx = pd.date_range("2026-04-01", periods=80, freq="B")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=idx)


def test_detect_signal_positive_case_equal():
    """命中态全字段对拍：repo 与单文件对同一形态必须产出同一个 Signal。

    Why 必须命中（不容 None==None 蒙混）：随机游走用例大概率两侧皆 None——等价性
    只被「走过完整守卫链后双 None」弱证明；本用例强制穿透七守卫到 Signal 装配
    （neckline/bottom/entry_price=颈线+buy_limit_mult×ATR/rr/exec_params 抄录），
    数值装配任何一位漂移立即不等。
    """
    import pandas as pd
    repo_det = _repo_detect_signal()
    m = _import_artifact()
    df = _df_positive_case()
    a = repo_det("300308.SZ", df, m.ID_PARAMS, m.EXEC_PARAMS, df.index[-1])
    b = m.detect_signal("300308.SZ", df, m.ID_PARAMS, m.EXEC_PARAMS, df.index[-1])
    assert a is not None, "确定性命中形态被 repo 拒绝——快照参数或形态构造漂移，须重新核对"
    assert b is not None, "单文件侧拒绝同形态：C2 等价破裂（repo 命中而组装件不命中）"
    # formed_at/突破日锚定（突破根 == 窗口末根，两侧同锚）
    assert pd.Timestamp(a.formed_at) == df.index[-1]
    assert pd.Timestamp(b.breakout_date) == df.index[-1]
    # 逐字段对拍（识别几何 + 进场 + 风险标尺 + 实际盈亏比 + 执行参数定终身快照）
    for k in ("symbol", "signal_type", "neckline", "bottom", "entry_price", "atr", "rr"):
        assert getattr(a, k) == getattr(b, k), f"字段 {k} 分叉：repo={getattr(a, k)!r} vs art={getattr(b, k)!r}"
    assert a.exec_params == b.exec_params


# ============================================================================
# ⑤ 组装幂等 + 生成物未手改
# ============================================================================
def _import_build():
    """按文件位置加载组装器（emquant/ 非包成员，与产物同款 file-location import）。

    同款先注册 sys.modules 再 exec（Task 5+ 的 §3 状态层若在组装器侧引入
    dataclass 同样依赖注册；此处统一范式防复发）。
    """
    spec = importlib.util.spec_from_file_location("build_pilot_under_test", BUILD_SCRIPT)
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m


def _repo_detect_signal():
    """repo 侧识别函数（等价性对拍的参照腿）。

    双环境双路径（Why——两套 venv 的依赖面不同，参照腿必须都能站住）：
      - .venv310（仓库全依赖）：直接 `from strategies.neckline.method_v0 import …`
        走真实包 __init__ 链——最强路径，顺带证明包上下文无恙；
      - .venv_emquant（最小试点环境：gm+numpy+pandas，刻意无 pydantic 等仓库依赖）：
        `strategies/__init__ → schema.py → pydantic` 链不可执行，退回【合成父包 +
        文件位置加载同一份 signal.py/method_v0.py】——同一源文件同一代码，
        内核等价证据不打折，同时保住「gm 在场双环境全绿」的共存验证。
    合成父包名独立于真实包名（repo_neckline_ref），不触碰/不污染 sys.modules 里
    可能已存在的真实 strategies 包（全量套跑时已被其他测试导入）。
    """
    try:
        from strategies.neckline.method_v0 import detect_signal
        return detect_signal
    except ImportError:
        import types
        parent = "repo_neckline_ref"
        if parent not in sys.modules:
            pkg = types.ModuleType(parent)
            pkg.__path__ = []                       # 标记包语义（相对 import 解析用）
            sys.modules[parent] = pkg

        def _loc(name: str, path: Path):
            # 先注册再 exec：signal.py 的 dataclass+future 注解解析回查 sys.modules
            spec = importlib.util.spec_from_file_location(f"{parent}.{name}", path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = mod
            spec.loader.exec_module(mod)
            return mod

        _loc("signal", ROOT / "strategies/neckline/signal.py")
        return _loc("method_v0", ROOT / "strategies/neckline/method_v0.py").detect_signal


def test_build_idempotent(tmp_path):
    """同输入两次 build 逐字节一致（纯拼接无时间戳）+ 与已提交产物一致（防手改）。"""
    build = _import_build()
    p1 = build.build(tmp_path / "once.py")
    p2 = build.build(tmp_path / "twice.py")
    assert p1.read_bytes() == p2.read_bytes(), "build 非幂等：产物混入了非确定内容（时间戳/随机）"
    # read_text 两侧换行归一（Windows CRLF 检出不影响内容比对）
    assert p1.read_text(encoding="utf-8") == ARTIFACT.read_text(encoding="utf-8"), (
        "重跑 build 与已提交产物不一致：生成物被手改，或源（内核/快照/pilot_body）变更后未重跑 build"
    )

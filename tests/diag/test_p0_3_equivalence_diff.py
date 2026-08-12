# -*- coding: utf-8 -*-
"""P0-3 等价性 diff 比对函数单测（P1 验收基建）。

物理意图（Why）：P1 向量化必须逐信号字段级零差异。compare_signals 是 P1 验收门的纯函数
核心——本测试用合成信号列表钉死其语义：相同→零 mismatch；单字段变→恰好一条 mismatch
且字段名/旧值/新值正确。CI 安全：不依赖 data_lake（合成信号）。
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from diag.p0_3_equivalence_diff import compare_signals  # noqa: E402


def _sig(symbol, date, entry=10.0, avg_pnl=2.5, exit_reason="tp2",
         neckline=100.0, tp2=120.0):
    """造一条最小信号 dict（compare_signals 只读 CANONICAL 字段）。"""
    return {"symbol": symbol, "signal_date": date, "entry": entry,
            "avg_pnl_pct": avg_pnl, "exit_reason": exit_reason,
            "neckline": neckline, "tp2": tp2}


def test_identical_signals_zero_mismatch():
    """两份完全相同的信号 → compare_signals 返空 list（P1 等价即此断言）。"""
    a = [_sig("300001.SZ", "2025-03-03"), _sig("688001.SH", "2025-04-07")]
    assert compare_signals(a, [dict(x) for x in a]) == []


def test_field_change_yields_one_mismatch_with_field_name():
    """单条信号 entry 变 → 恰好一条 mismatch，含 symbol/date/field/旧值/新值。"""
    a = [_sig("300001.SZ", "2025-03-03", entry=10.0)]
    b = [_sig("300001.SZ", "2025-03-03", entry=10.5)]  # entry 10.0 → 10.5
    mm = compare_signals(a, b)
    assert len(mm) == 1
    m = mm[0]
    assert m["symbol"] == "300001.SZ" and m["signal_date"] == "2025-03-03"
    assert m["field"] == "entry"
    assert m["baseline"] == 10.0 and m["current"] == 10.5


def test_missing_signal_in_current_is_mismatch():
    """current 缺一条信号（识别变少）→ 记为 missing mismatch。"""
    a = [_sig("300001.SZ", "2025-03-03"), _sig("688001.SH", "2025-04-07")]
    b = [_sig("300001.SZ", "2025-03-03")]  # 少 688001
    mm = compare_signals(a, b)
    assert len(mm) == 1
    assert mm[0]["symbol"] == "688001.SH" and mm[0]["field"] == "__missing__"


def test_extra_signal_in_current_is_mismatch():
    """current 多一条信号（识别变多）→ 记为 extra mismatch。"""
    a = [_sig("300001.SZ", "2025-03-03")]
    b = [_sig("300001.SZ", "2025-03-03"), _sig("688001.SH", "2025-04-07")]
    mm = compare_signals(a, b)
    assert len(mm) == 1
    assert mm[0]["symbol"] == "688001.SH" and mm[0]["field"] == "__extra__"


def test_date_str_vs_date_obj_no_mismatch():
    """str() 类型归一：baseline JSON 往返的 date-str 与 current 现场 date-obj 须判等无 mismatch。

    物理意图（Why）：record_baseline 用 json.dumps(default=str) 序列化，日期变 str；
    compare() 重跑 scan_symbol 现场 produce datetime.date。compare_signals 必须两侧归一
    （str()）判等，否则 signal_date/exit_date 全报假阳性（P1 验收门静默失效）。
    """
    from datetime import date
    baseline = [{"symbol": "300001.SZ", "signal_date": "2025-07-21"}]      # JSON 往返 str
    current = [{"symbol": "300001.SZ", "signal_date": date(2025, 7, 21)}]   # 现场产 date
    assert compare_signals(baseline, current) == []

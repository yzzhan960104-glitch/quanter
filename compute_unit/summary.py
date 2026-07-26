# -*- coding: utf-8 -*-
"""人可读摘要:从 Result 按 inner calmar 选 top-N → 钉钉友好中文文本。

物理意图(spec §3.3 路径 2):Mac 跑完不回库,只生成几百字摘要供人 AirDrop + 手机钉钉转发。
出站信息量从「全批 result.json ~50KB」压到「top-N 摘要几百字」,绕开钉钉消息大小限制。

排序键:inner calmar 降序(与 discovery 冠军排序同口径,spec §15 开放问题②)。
仅展示 status=ok 的 trial(failed/degenerate 跳过)。含 inner+outer calmar/max_dd/ann/n,
人按「outer calmar 高 + max_dd 小 + n 足够」综合判断(与 discovery judging 口径一致,
信息隔离:outer 不反馈搜索但供人参考)。
"""
from __future__ import annotations

from compute_unit.protocol import Result


def _fmt_metrics(m: dict, prefix: str = "") -> str:
    """metrics dict → 单行简短中文(n/年化/calmar/回撤)。空 dict 安全。"""
    if not m:
        return f"{prefix}无数据"
    return (
        f"{prefix}n={m.get('n', 0)} "
        f"年化{m.get('ann', 0)*100:+.1f}% "
        f"calmar={m.get('calmar', 0):.2f} "
        f"回撤{m.get('max_dd', 0)*100:.1f}%"
    )


def summarize(result: Result, top_n: int = 3) -> str:
    """Result → top-N 中文摘要文本(钉钉友好,纯文本无 markdown 特殊字符)。

    top-N 按 inner calmar 降序,仅 ok 的 trial。末尾附 failed/degenerate 计数(供诊断)。
    """
    ok = [r for r in result.results if r.status == "ok"]
    ok.sort(key=lambda r: r.inner.get("calmar", 0.0), reverse=True)
    top = ok[:top_n]

    lines = [
        f"【Mac 计算单元回测摘要】task={result.task_id} 共{len(result.results)}组",
        f"git={result.git_commit[:8]} parquet={result.parquet_sha256[:8]}…",
        f"跑批时间 {result.ran_at}",
        "",
    ]
    if not top:
        lines.append("无 ok 结果(全 failed/degenerate,请查 result.json 的 error 字段)")
    else:
        lines.append(f"▼ top-{len(top)}(按 inner calmar 降序)")
        for i, r in enumerate(top, 1):
            lines.append(f"{i}. trial={r.trial_id}")
            lines.append(f"   {_fmt_metrics(r.inner, 'inner ')}")
            lines.append(f"   {_fmt_metrics(r.outer, 'outer ')}")
            lines.append(f"   总笔数 n_total={r.n_total}")
            lines.append("")
    # 诊断计数(failed/degenerate 数量,供判断批质量)
    n_failed = sum(1 for r in result.results if r.status == "failed")
    n_degen = sum(1 for r in result.results if r.status == "degenerate")
    if n_failed or n_degen:
        lines.append(f"(诊断:failed {n_failed} / degenerate {n_degen})")
    return "\n".join(lines)

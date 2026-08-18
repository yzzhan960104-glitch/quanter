# -*- coding: utf-8 -*-
"""运维入口统一 stdio UTF-8 治理。

背景:Windows cp936 管道/重定向下,Python 默认以 cp936(GBK) 编码 stdout/stderr。
任何 emoji 字符(✅/⚠️/🎯/❌ 等)的 print 会抛
`UnicodeEncodeError: 'gbk' codec can't encode character ...`,
直接崩掉 schtasks 调起的 bat → python 运维入口。

用法(在每个 CLI 入口的 `__main__`/main() 顶部加两行):

    from infra.pyio import force_utf8_stdout
    force_utf8_stdout()

配合 bat 侧 `set PYTHONUTF8=1` 双保险——本模块兜底处理未设环境变量的
直跑场景(`python -m ops.data_pipeline` 等)。

幂等:重复调用无副作用,异常静默(绝不因 stdio 治理反致入口崩溃)。

P2-A 扩展(根治 stderr emoji 崩):已退役的 compute_unit CLI(2026-08-18,ADR-17)的
`print(...❌..., file=sys.stderr)` 在 GBK 管道下同样会崩——失败路径正是最需要输出
的场景。force_utf8_stdout 现在同时治 stdout + stderr(原仅 stdout)。
"""
from __future__ import annotations

import sys


def force_utf8_stdout() -> None:
    """stdout + stderr 非 UTF-8 时 reconfigure 成 UTF-8(errors="replace" 兜底)。

    幂等:已是 UTF-8 时直接返回(no-op)。reconfigure 抛任何异常都静默吞掉——
    本函数是"治理护栏",绝不能反致入口崩溃。

    设计选择:
    - 同时治 stdout 和 stderr(stderr 在失败路径常带 emoji 错误信息,如
      已退役 compute_unit 的 `❌ 环境漂移`;GBK 管道下崩在错误输出处尤其伤——
      失败时连诊断信息都看不到)。原仅治 stdout 的设计在 emoji 仅出现在成功路径
      (`✅`) 时成立,引入 ❌ 等失败路径 emoji 后必须扩展。
    - 用 `errors="replace"` 而非 "strict":极端情况下用 ? 替代不可编码字符,
      保证 print 永不抛(运维入口"能跑完"比"输出完美"更重要)。
    """
    for stream_name in ("stdout", "stderr"):
        try:
            stream = getattr(sys, stream_name, None)
            if stream is None:
                continue
            enc = getattr(stream, "encoding", "") or ""
            if enc and enc.lower() not in ("utf-8", "utf8"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            # 静默:治理护栏绝不反致入口崩溃(reconfigure 在某些自定义 stream
            # 类型上可能缺失或不支持,如 pytest capture 已包装的 TextIOWrapper)。
            pass


# 向后兼容别名(语义已扩展为 stdio,旧调用方无需改动)。
force_utf8_stdio = force_utf8_stdout

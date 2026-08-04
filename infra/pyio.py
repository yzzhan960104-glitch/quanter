# -*- coding: utf-8 -*-
"""运维入口统一 stdout UTF-8 治理。

背景:Windows cp936 管道/重定向下,Python 默认以 cp936(GBK) 编码 stdout。
任何 emoji 字符(✅/⚠️/🎯 等)的 print 会抛
`UnicodeEncodeError: 'gbk' codec can't encode character ...`,
直接崩掉 schtasks 调起的 bat → python 运维入口。

用法(在每个 CLI 入口的 `__main__`/main() 顶部加两行):

    from infra.pyio import force_utf8_stdout
    force_utf8_stdout()

配合 bat 侧 `set PYTHONUTF8=1` 双保险——本模块兜底处理未设环境变量的
直跑场景(`python -m ops.data_pipeline` 等)。

幂等:重复调用无副作用,异常静默(绝不因 stdout 治理反致入口崩溃)。
"""
from __future__ import annotations

import sys


def force_utf8_stdout() -> None:
    """stdout 非 UTF-8 时 reconfigure 成 UTF-8(errors="replace" 兜底)。

    幂等:已是 UTF-8 时直接返回(no-op)。reconfigure 抛任何异常都静默吞掉——
    本函数是"治理护栏",绝不能反致入口崩溃。

    设计选择:
    - 仅治 stdout,不动 stderr(stderr 在运维场景一般可读性优先,且 GBK 下
      traceback 内 emoji 罕见;避免副作用扩散)。
    - 用 `errors="replace"` 而非 "strict":极端情况下用 ? 替代不可编码字符,
      保证 print 永不抛(运维入口"能跑完"比"输出完美"更重要)。
    """
    try:
        enc = getattr(sys.stdout, "encoding", "") or ""
        if enc and enc.lower() not in ("utf-8", "utf8"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        # 静默:治理护栏绝不反致入口崩溃(reconfigure 在某些自定义 stdout
        # 类型上可能缺失或不支持,如 pytest capture 已包装的 TextIOWrapper)。
        pass

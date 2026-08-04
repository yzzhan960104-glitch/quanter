# -*- coding: utf-8 -*-
"""GBK 输出治理冒烟测试。

回归 Windows cp936 管道/重定向下 emoji print(✅/⚠️)抛 UnicodeEncodeError
崩溃运维入口的 bug。验证 `infra.pyio.force_utf8_stdout()` 把 stdout reconfigure
成 UTF-8 后,emoji print 能在 GBK 环境下幸存。
"""
import os
import subprocess
import sys


def test_emoji_print_survives_gbk_pipe():
    """cp936 管道下 emoji print 不抛 UnicodeEncodeError(回归 GBK 崩溃)。

    模拟 schtasks 调起 bat → python 的真实场景:PYTHONIOENCODING=gbk
    (Windows 默认 cp936),stdout 被管道/重定向接管。无 force_utf8_stdout 时,
    print('✅') 会抛 UnicodeEncodeError: 'gbk' codec can't encode character。
    """
    code = (
        "import sys; sys.path.insert(0, r'F:\\quanter'); "
        "from infra.pyio import force_utf8_stdout; force_utf8_stdout(); "
        "print('✅ ok')"
    )
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "gbk"
    env.pop("PYTHONUTF8", None)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, env=env, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr
    assert "✅" in r.stdout


def test_force_utf8_stdout_is_idempotent():
    """force_utf8_stdout 幂等:多次调用不抛、不改变已 UTF-8 的 stdout。"""
    import io
    from infra.pyio import force_utf8_stdout

    buf = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", write_through=True)
    saved = sys.stdout
    sys.stdout = buf
    try:
        force_utf8_stdout()
        force_utf8_stdout()
        assert buf.encoding.lower() in ("utf-8", "utf8")
    finally:
        sys.stdout = saved

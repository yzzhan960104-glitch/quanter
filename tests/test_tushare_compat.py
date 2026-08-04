# -*- coding: utf-8 -*-
"""tushare 凭证层回归：set_token 不得落盘 ~/tk.csv（并发同步子进程争文件 → PermissionError）。"""


def test_get_pro_does_not_write_tk_csv(monkeypatch, tmp_path):
    """get_pro 只写内存 token，不产生 ~/tk.csv（2026-08-05 fund_nav/fund_share/monthly 失败根因）。"""
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    from data import _tushare_compat as tc
    monkeypatch.setattr(tc, "_PRO_CACHE", None)
    monkeypatch.setattr(tc, "get_credential", lambda *a, **k: "test-token")
    monkeypatch.setattr(tc.ts, "pro_api", lambda: object())
    tc.get_pro()
    assert not (tmp_path / "tk.csv").exists(), "set_token 不得把 token 落盘到 ~/tk.csv"


def test_ensure_token_does_not_write_tk_csv(monkeypatch, tmp_path):
    """ensure_token（pro_bar 模块级入口）同样不得落盘。"""
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    from data import _tushare_compat as tc
    monkeypatch.setattr(tc, "get_credential", lambda *a, **k: "test-token")
    assert tc.ensure_token() == "test-token"
    assert not (tmp_path / "tk.csv").exists()

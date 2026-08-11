# -*- coding: utf-8 -*-
"""tushare 凭证层回归 + T15 代码层防复发测试。

- set_token 不得落盘 ~/tk.csv（并发同步子进程争文件 → PermissionError）
- T15 决策 C：tushare 域名加入 NO_PROXY（防代理软件复发，2026-08-11）
"""
import os

import data._tushare_compat as tc


def test_get_pro_does_not_write_tk_csv(monkeypatch, tmp_path):
    """get_pro 只写内存 token，不产生 ~/tk.csv（2026-08-05 fund_nav/fund_share/monthly 失败根因）。"""
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(tc, "_PRO_CACHE", None)
    monkeypatch.setattr(tc, "get_credential", lambda *a, **k: "test-token")
    monkeypatch.setattr(tc.ts, "pro_api", lambda: object())
    tc.get_pro()
    assert not (tmp_path / "tk.csv").exists(), "set_token 不得把 token 落盘到 ~/tk.csv"


def test_ensure_token_does_not_write_tk_csv(monkeypatch, tmp_path):
    """ensure_token（pro_bar 模块级入口）同样不得落盘。"""
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(tc, "get_credential", lambda *a, **k: "test-token")
    assert tc.ensure_token() == "test-token"
    assert not (tmp_path / "tk.csv").exists()


# ============ T15 代码层防复发（决策 C · 2026-08-11）============


def test_harden_no_proxy_adds_tushare_hosts(monkeypatch):
    """_harden_no_proxy 把 tushare 域名加入 NO_PROXY（防代理复发）。"""
    monkeypatch.delenv("NO_PROXY", raising=False)
    tc._harden_no_proxy()
    no_proxy_list = os.environ["NO_PROXY"].split(",")
    assert "api.waditu.com" in no_proxy_list
    assert "api.tushare.pro" in no_proxy_list


def test_harden_no_proxy_idempotent_preserves_user_config(monkeypatch):
    """已配 NO_PROXY 时不覆盖，仅追加缺失的 tushare 域名；重复调用幂等。"""
    monkeypatch.setenv("NO_PROXY", "example.com,foo.bar")
    tc._harden_no_proxy()
    no_proxy = os.environ["NO_PROXY"]
    assert "example.com" in no_proxy.split(",")  # 用户配置保留
    assert "foo.bar" in no_proxy.split(",")
    assert "api.waditu.com" in no_proxy.split(",")  # tushare 追加
    # 重复调用幂等（不重复追加同一域名）
    tc._harden_no_proxy()
    assert os.environ["NO_PROXY"].count("api.waditu.com") == 1


def test_harden_no_proxy_effective_even_with_all_proxy_set(monkeypatch):
    """ALL_PROXY 设置（代理软件复发场景）时，NO_PROXY 仍加入 tushare 域名（直连白名单）。

    场景：代理软件重启写 ALL_PROXY=socks5://失效端口 → quanter 进程继承 →
    但 NO_PROXY 含 tushare 域名 → requests 命中直连 → tushare 不受害。
    """
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:5001")  # 模拟代理复发
    monkeypatch.delenv("NO_PROXY", raising=False)
    tc._harden_no_proxy()
    assert "api.waditu.com" in os.environ["NO_PROXY"].split(",")


def test_harden_no_proxy_case_insensitive(monkeypatch):
    """NO_PROXY 已含大写域名（API.WADITU.COM）时不重复追加小写 api.waditu.com。

    场景：代理软件/用户可能写大写域名，严格比对会重复追加同名异体。lower+strip 比对防重复。
    """
    monkeypatch.setenv("NO_PROXY", "API.WADITU.COM")
    tc._harden_no_proxy()
    no_proxy_list = os.environ["NO_PROXY"].split(",")
    assert "API.WADITU.COM" in no_proxy_list  # 用户大写配置保留
    assert "api.waditu.com" not in no_proxy_list  # 小写不重复追加（大写已覆盖）
    assert "api.tushare.pro" in no_proxy_list  # 未覆盖的仍追加

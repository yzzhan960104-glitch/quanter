# -*- coding: utf-8 -*-
"""tushare pro 测试替身单源（2026-08-19 测试库精简 W2 收口）。

原 7 个测试文件各自复制整块 `_FakePro + fake_pro fixture + 注册表隔离`（~70 行/份，
macro 文件头注释自认「conftest 未抽取」）——本模块收口为单源，各文件保留薄壳 fixture。

Why 独立模块而非 conftest：fixture 走 conftest 自动发现，但类与 patch 助手需要被
测试文件显式 import 组合（默认数据/目标模块因文件而异），tests 包化后可直接 import。
"""
from __future__ import annotations

import pandas as pd


class FakePro:
    """tushare pro 替身：按 api_name 返回可控 DataFrame，记录调用序列供断言。

    Why __getattr__：pro 接口方法（pro.income / pro.stock_basic ...）在运行时由
    tushare DataApi 动态分发，测试替身用 __getattr__ 一次性兜底所有 api_name，
    避免逐方法硬编码；同时记录调用序列供断言「shard 已存在即跳过」等行为。

    Args:
        data: {api_name: DataFrame} 默认数据（None → 空表；测试内可继续
              fake._data[...] = ... 覆盖或 fake.calls 断言调用）。
    """

    def __init__(self, data: dict | None = None):
        self.calls = []                                  # 记录 (api_name, kwargs)
        self._data = data if data is not None else {}

    def set(self, api, df):
        """注入某 api 的返回数据（原 macro_credit/dataset_registry 变体的便捷入口）。"""
        self._data[api] = df

    def __getattr__(self, api_name):
        def _call(**kwargs):
            self.calls.append((api_name, kwargs))
            return self._data.get(api_name, pd.DataFrame())
        return _call


class _NoRateLimit:
    """限频器替身：acquire 直通（不消耗真实令牌桶）。"""
    def acquire(self, n=1.0):
        return None


class _AlwaysPassBreaker:
    """熔断器替身：永远放行 + record_* 静默（不触发 OPEN 拒绝路径）。"""
    def allow_request(self):
        return True

    def record_success(self):
        return None

    def record_failure(self):
        return None


def install_fake_pro(monkeypatch, fake: FakePro, module_targets=()) -> None:
    """把 FakePro 短路进 _fetch_with_guard 链路（get_pro + 限频器 + 熔断器三道闸门）。

    Why 三道全 patch：sync 族经 rate_limiter → breaker → get_pro 串联，任一未被
    mock 都会触达真实 tushare/网络（.env 有 token 时静默命中真 API）。

    Why 双绑 get_pro（原 Task 2 修复的隔离漏洞，收口保注释）：data/tushare_sync.py
    顶部 `from data._tushare_compat import get_pro` 把函数对象绑到模块命名空间，
    仅 patch 源模块不改已绑定引用——旧实现下测试静默穿透真实 Tushare，跨文件
    套件污染（读到残留 shard → 断言 flaky）。故默认双绑：
      ① data._tushare_compat.get_pro（源模块）
      ② data.tushare_sync.get_pro（已绑定副本）

    Args:
        module_targets: 追加的模块级绑定（如 ("data.tools.sync_macro_credit",)）——
            该族脚本同样 `from ... import get_pro` 模块级绑定，只 patch 源模块拦不住。
            对应模块的限频/熔断器一并 patch（各脚本 import 各自的实例引用）。
    """
    monkeypatch.setattr("data._tushare_compat.get_pro", lambda: fake)
    monkeypatch.setattr("data.tushare_sync.get_pro", lambda: fake)
    monkeypatch.setattr("data.tushare_sync.tushare_rate_limiter", _NoRateLimit())
    monkeypatch.setattr("data.tushare_sync.tushare_breaker", _AlwaysPassBreaker())
    for mod in module_targets:
        monkeypatch.setattr(f"{mod}.get_pro", lambda: fake)
        monkeypatch.setattr(f"{mod}.tushare_rate_limiter", _NoRateLimit())
        monkeypatch.setattr(f"{mod}.tushare_breaker", _AlwaysPassBreaker())

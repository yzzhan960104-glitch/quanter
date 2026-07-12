# -*- coding: utf-8 -*-
"""统一 Tushare pro 接口入口：优先代理 tnskhdata（10000 积分），回退直连 tushare。

设计意图（反黑盒 + 可回退）：
- tnskhdata 是 tushare 的代理库（import tushare 改地址，API 完全兼容），积分更充足
  （10000 vs 直连账户常不足 2000，stock_basic/daily_basic 等接口需 2000+）。
- 本 helper 由 TNSKHDATA_TOKEN 环境变量决定走代理还是直连，所有需要 pro 接口的地方
  （sync_fundamentals / TushareDataFetcher）统一经此获取，避免 import 散落、便于切换。
- 凭证优先级：TNSKHDATA_TOKEN（代理）> TUSHARE_TOKEN（直连兜底，积分可能不足）。

实测（2026-07，代理 10000 积分）：stock_basic/daily_basic/daily/fina_indicator/trade_cal
全部解锁，全市场 5534 标的可拉。
"""
from __future__ import annotations

import os


def _proxy_tokens() -> list[str]:
    """代理 token 列表（TNSKHDATA_TOKEN 逗号分隔，支持多 token 负载均衡/冗余）。

    Why 多 token：单一代理 token 存在限频/失效风险，多 token 轮询分散压力 + 互为冗余；
    环境变量逗号分隔便于运维增删（主,备），无需改代码。
    """
    raw = os.getenv("TNSKHDATA_TOKEN", "").strip()
    return [t.strip() for t in raw.split(",") if t.strip()]


def _use_proxy() -> bool:
    """是否走代理 tnskhdata（至少一个代理 token 配置即启用）。"""
    return len(_proxy_tokens()) > 0


# 模块级轮询索引：多 token 轮询分散限频压力 + 单 token 失效自动切下一个
_token_index = 0


def get_pro():
    """返回 tushare pro 接口（代理多 token 轮询，回退直连 tushare）。

    多 token 轮询：每次 get_pro 取下一个 token（_token_index 递增取模），分散限频压力。
    单 token 失效时，调用方重新 get_pro 即自动切下一个（无需重启）。

    返回 pro 实例：pro.stock_basic / pro.daily_basic / pro.daily / pro.fina_indicator /
    pro.trade_cal ... 代理与直连 API 完全兼容，调用方无感知。
    """
    global _token_index
    tokens = _proxy_tokens()
    if tokens:
        token = tokens[_token_index % len(tokens)]
        _token_index += 1
        import tnskhdata as ts  # 代理：API 兼容 tushare，token 直传 pro_api
        return ts.pro_api(token)
    # 回退直连 tushare（TUSHARE_TOKEN，积分可能不足，仅兜底）
    from config import get_credential
    import tushare as ts
    ts.set_token(get_credential("tushare", "token"))
    return ts.pro_api()


def ts_module():
    """返回底层 ts 模块（tnskhdata 或 tushare），供需要 ts.xxx 静态方法（如 pro_bar）的场景。

    Why 单独导出模块：pro_bar 等接口在 ts 模块上而非 pro 实例上（pro_api 返回的 DataApi
    无 pro_bar 方法），调用方需直接拿 ts 模块。
    """
    if _use_proxy():
        import tnskhdata as ts
        return ts
    import tushare as ts
    return ts


def ensure_token() -> str:
    """设置 ts 模块全局 token（供 pro_bar 等模块级函数），返回所用 token。

    Why 单独提供：pro_bar 是 ts 模块级函数（用全局 token，非 pro 实例方法），调 ts.pro_bar
    前必须 set_token；本函数用当前轮询 token 配置，保证代理/直连一致 + 多 token 轮询。
    """
    global _token_index
    tokens = _proxy_tokens()
    if tokens:
        token = tokens[_token_index % len(tokens)]
        _token_index += 1
        import tnskhdata as ts
        ts.set_token(token)
        return token
    from config import get_credential
    import tushare as ts
    token = get_credential("tushare", "token")
    ts.set_token(token)
    return token


def source_name() -> str:
    """当前数据源名（'tnskhdata' 代理 或 'tushare' 直连），供日志/展示。"""
    return "tnskhdata" if _use_proxy() else "tushare"

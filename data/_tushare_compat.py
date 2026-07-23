# -*- coding: utf-8 -*-
"""统一 Tushare pro 接口入口：纯直连 tushare 官方 SDK。

历史（Why 纯直连）：
- 2026-07 之前曾用 tnskhdata 代理库（import tushare 改地址、API 兼容，10000 积分）
  作为 tushare pro 主通道，直连 tushare 兜底。当时积分受限（直连账户 <2000，
  stock_basic/daily_basic 等接口需 2000+），故引入代理 + 多 token 轮询/冗余。
- 2026-07-24 代理 token 失效 + 直连 tushare 已切到新 token（积分充足、直接走官方
  API），代理双轨彻底废弃。本 helper 删代理实现（_proxy_tokens/_use_proxy/_token_index/
  import tnskhdata/双轨分支），简化为纯直连 tushare SDK，单一来源、零分叉。

铁律：``get_pro`` / ``source_name`` / ``ts_module`` / ``ensure_token`` 4 函数签名
**保持不变**——它们被 calendar / tushare_sync / TushareDataFetcher / 各 sync 脚本
多处调用，签名变更会扩散冲击。本文件只换「实现内核」、不换「对外契约」。
"""
from __future__ import annotations

from config import get_credential
import tushare as ts


def get_pro():
    """返回 tushare pro 接口实例（纯直连 tushare 官方 SDK）。

    Why 直连：2026-07-24 废弃代理后唯一通道，token 走 config.get_credential 统一
    凭证层（与 .env 的 TUSHARE_TOKEN 一致），set_token 后 pro_api 取实例。
    调用方（tushare_sync / TushareDataFetcher 等）通过 ``pro.stock_basic`` /
    ``pro.daily`` / ``pro.daily_basic`` 等访问，对代理/直连无感知。
    """
    ts.set_token(get_credential("tushare", "token"))
    return ts.pro_api()


def ts_module():
    """返回底层 tushare 模块，供需要 ts.xxx 静态方法（如 pro_bar）的场景使用。

    Why 单独导出模块：pro_bar 等接口挂在 ts 模块上、而非 pro 实例上（pro_api 返回
    的 DataApi 无 pro_bar 方法），调用方需直接拿 ts 模块才能调 ts.pro_bar。
    纯直连后恒返 tushare 模块。
    """
    return ts


def ensure_token() -> str:
    """设置 tushare 模块全局 token（供 pro_bar 等模块级函数），返回 token。

    Why 单独提供：pro_bar 是 ts 模块级函数（用全局 token、非 pro 实例方法），
    调 ts.pro_bar 前必须 set_token；本函数用 config 凭证层 token，保证与
    get_pro / ts_module 三者 token 口径一致。
    """
    token = get_credential("tushare", "token")
    ts.set_token(token)
    return token


def source_name() -> str:
    """当前数据源名（恒返 ``'tushare'``），供日志/展示统一标识。

    Why 恒返：2026-07-24 废弃代理后不再有双轨，source 仅 tushare 一源；保留函数
    形态以兼容已有调用方（fetcher/sync 脚本日志），未来引入多源再扩展。
    """
    return "tushare"

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

import os

# ============================================================================
# T15 代码层防复发（2026-08-11 · 决策 C 补全）
# ============================================================================
# tushare 域名加入 NO_PROXY，防代理软件（v2ray/clash）复发重写 ALL_PROXY 致 tushare
# 走失效代理（T14 事故重演）。requests 默认 trust_env=True，读 NO_PROXY——命中域名即
# 跳过 ALL_PROXY/HTTP_PROXY 直连。T15.md 决策 B 已删注册表 ALL_PROXY（环境层根治），
# 本层为代码兜底（决策 C），与环境根治形成双层防线。代理软件复发时 quanter 不再受害。
# 详见 docs/superpowers/specs/2026-08-11-t15-tushare-proxy-hardening-design.md。
_TUSHARE_HOSTS = ("api.waditu.com", "api.tushare.pro")


def _harden_no_proxy() -> None:
    """把 tushare 域名加入 NO_PROXY（幂等，不覆盖用户既有配置）。

    物理意图：tushare pro_api 底层用 requests（trust_env=True），若进程继承了系统代理
    env（ALL_PROXY/HTTP_PROXY），requests 会把 tushare 请求走代理——代理失效时全失败
    （T14 事故根因）。NO_PROXY 是 requests 的「直连白名单」，命中域名即跳过代理。
    本函数在模块加载时把 tushare 域名加入 NO_PROXY，使代理 env 对 tushare 调用无效
    （代理软件复发写 ALL_PROXY 也不影响 tushare 直连）。

    幂等：仅在域名未存在时追加，保留用户既有 NO_PROXY 配置（如其他服务需直连的域名）。
    """
    no_proxy = os.environ.get("NO_PROXY", "")
    existing = no_proxy.split(",") if no_proxy else []
    missing = [h for h in _TUSHARE_HOSTS if h not in existing]
    if missing:
        os.environ["NO_PROXY"] = ",".join(filter(None, [no_proxy] + missing))


# 模块加载即生效（首次 import 时设置，进程内持久；calendar/sync/fetcher 多处 import 复用）
_harden_no_proxy()

from config import get_credential
import tushare as ts


# 缓存 pro 实例：get_pro 每次调用都 set_token 会重写 ~/tk.csv（tushare 把 token 落盘），
# 多进程并发同步（server sweep / 手动触发 / 增量 cron 同时跑）时互相争文件句柄 →
# PermissionError 导致同步整批失败。token 进程内恒定，pro 实例缓存后每进程只写一次。
_PRO_CACHE = None


def _set_token_mem(token: str) -> None:
    """设置 tushare 模块全局 token，不落盘 ~/tk.csv。

    物理意图（2026-08-05 fund_nav/fund_share/monthly 失败根因）：tushare 的
    ``set_token`` 会把 token 写入 ``~/tk.csv``；多进程并发同步（server sweep /
    手动触发 / 增量 cron 同时跑）各自 set_token 互争同一文件句柄 →
    PermissionError → 整批数据集失败。tushare 的 ``pro_api`` / ``pro_bar`` 实际
    只读内存 ``ts._token``，落盘纯属 SDK 副作用，故只写内存即可（进程内恒定，
    进程间零共享文件）。
    """
    ts._token = token


def get_pro():
    """返回 tushare pro 接口实例（纯直连 tushare 官方 SDK）。

    Why 直连：2026-07-24 废弃代理后唯一通道，token 走 config.get_credential 统一
    凭证层（与 .env 的 TUSHARE_TOKEN 一致），set_token 后 pro_api 取实例。
    调用方（tushare_sync / TushareDataFetcher 等）通过 ``pro.stock_basic`` /
    ``pro.daily`` / ``pro.daily_basic`` 等访问，对代理/直连无感知。
    """
    global _PRO_CACHE
    if _PRO_CACHE is None:
        _set_token_mem(get_credential("tushare", "token"))
        _PRO_CACHE = ts.pro_api()
    return _PRO_CACHE


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
    _set_token_mem(token)
    return token


def source_name() -> str:
    """当前数据源名（恒返 ``'tushare'``），供日志/展示统一标识。

    Why 恒返：2026-07-24 废弃代理后不再有双轨，source 仅 tushare 一源；保留函数
    形态以兼容已有调用方（fetcher/sync 脚本日志），未来引入多源再扩展。
    """
    return "tushare"

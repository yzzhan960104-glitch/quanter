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

import concurrent.futures
import os

# ============================================================================
# 外部 SDK 调用超时兜底（Task G4 · 韧性链复活 · 2026-08-13）
# ============================================================================
# 物理意图（Why 此层兜底存在）：tushare pro_api / fredapi 底层用 requests（默认无
# timeout），当对端 TCP 挂起（半开连接 / GFW 注入 RST / 对端 hold 住不回 FIN）时，
# requests.read() 阻塞在 socket.recv **不抛异常**——上层 CircuitBreaker / 退避重试
# 永远等不到异常触发 → 韧性链被旁路（熔断不跳闸、record_failure 不计、退避不重试），
# 同步主循环被一个挂起接口拖死。
#
# 解法：用独立线程池跑 SDK 调用，主线程 future.result(timeout) 到点抛 TimeoutError，
# 让上层 except 能捕获 → record_failure → 熔断/退避链复活。对齐既有范式：
#   - data/clients/akshare_client.py::_call_ak（_AK_TIMEOUT=30s，ThreadPoolExecutor）
#   - data/clients/alpha_vantage_client.py（httpx.AsyncClient timeout=15.0）
#
# 阈值选 30s（Why）：tushare/FRED 正常调用 <5s；30s 兜底覆盖大数据集分页 + 网络抖动；
# akshare 同量级已生产验证。env TUSHARE_CALL_TIMEOUT 可覆盖便于压测/测试调小不真等。
# 命名沿用 brief 契约（虽然 helper 通用，但 tushare 是主消费者 + 向后兼容）。
_CALL_TIMEOUT = float(os.getenv("TUSHARE_CALL_TIMEOUT", "30"))

# 独立线程池（Why 不用 with ThreadPoolExecutor as ex：with 退出会 shutdown(wait=True)
# 阻塞至挂起线程完成，反而把超时绕回成阻塞——违反"挂起可被打断"的初衷）。
# 模块级单例 + max_workers=4：挂起线程随主线程放弃 future 留在池里继续跑（Python 无法
# 真 kill 线程），但主线程已抛 TimeoutError 向上交差。4 worker 容纳偶发挂起不堵新调用。
# 对齐 akshare _ak_executor 同范式（已生产验证）。
_sdk_timeout_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="sdk-timeout")


def _call_with_timeout(fn, *args, timeout=None, **kwargs):
    """线程池包裹外部 SDK 调用 + 超时兜底，让 TCP 挂起可观测化（抛 TimeoutError）。

    Why 本函数：tushare pro_api / fredapi 底层 requests 无 timeout，TCP 挂起时不抛异常
    → 上层 CircuitBreaker/退避重试链被旁路（永远等不到异常）。本包裹在独立线程跑 fn，
    主线程 future.result(timeout) 到点抛 TimeoutError → 上层 except 捕获 → record_failure
    → 韧性链复活。

    Args:
        fn: 外部 SDK 调用（如 pro.daily / fred.get_series）。
        *args, **kwargs: fn 的位置/关键字参数（透传）。
        timeout: 显式覆盖 _CALL_TIMEOUT（per-call 自定义阈值，如 FRED/calendar 可传不同值）。

    Returns:
        fn 的返回值（透传）。

    Raises:
        TimeoutError: fn 在 timeout 秒内未完成（消息含 "timeout" + "超时" 双关键词，
            确保 tushare_sync._classify_exc 的 "超时" 命中 + FRED except 的 "timeout" 命中）。
        Exception: fn 自身抛出的任何异常原样透传（不被吞，不影响上层异常分类）。

    固有限制（无法消除）：超时仅放弃【等待】，底层 fn 线程仍在跑（Python 无真 kill 线程），
    挂起线程占用一个池槽位直至对端真断连。max_workers=4 容纳偶发挂起；持续挂起会耗尽
    槽位导致 submit 阻塞——但此时同步主循环早已因 TimeoutError 走熔断 OPEN 停打，
    不会继续投新任务（熔断层兜底）。
    """
    t = timeout if timeout is not None else _CALL_TIMEOUT
    fut = _sdk_timeout_executor.submit(fn, *args, **kwargs)
    try:
        return fut.result(timeout=t)
    except concurrent.futures.TimeoutError as e:
        # 双关键词消息（"timeout" + "超时"）：让 _classify_exc（查"超时"）和 FRED except
        # （查 "timeout"）都能命中 → 归基础设施异常走 record_failure / transient 退避。
        raise TimeoutError(f"外部 SDK 调用超时 (timeout after {t}s)") from e


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
    # 大小写不敏感 + trim 比对：NO_PROXY 域名本应小写，但用户/代理软件可能写大写
    # （API.WADITU.COM），严格比对会重复追加同名异体。lower+strip 比对防重复，写入
    # 保留原 no_proxy（用户配置原样）+ 追加规范小写 missing。
    existing_lower = {h.strip().lower() for h in no_proxy.split(",") if h.strip()}
    missing = [h for h in _TUSHARE_HOSTS if h.lower() not in existing_lower]
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

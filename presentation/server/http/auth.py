# -*- coding: utf-8 -*-
"""API 鉴权依赖（B-1：HTTPBearer token + 可选 IP 白名单）。

物理定位（CLAUDE.md 量化风控·安全审查）：
    修复 B-1——全部交易 API 零认证裸奔。本模块提供 require_write 依赖，挂在可触发
    真实下单/熔断/落盘/起子进程的敏感 router（trading/caisen/data/review）上，
    在请求进入业务逻辑前完成身份校验。

部署语义（环境变量驱动，零外部依赖 · DG-G2 fail-closed 铁律）：
    - QUANTER_API_TOKEN 未配置 + AUTO_TRADE_MODE=live：**fail-closed**——直接 raise 401，
      绝不放行。Why：live 模式（实盘）下单 API 裸奔 = 局域网/同机任意进程可无认证触发真单，
      「忘配 token」必须从「静默放行+WARNING」翻转为「硬拒」（DG-G2 安全审查裁决）。
    - QUANTER_API_TOKEN 未配置 + 非 live（dry_run/开发/CI）：放行 + WARNING。
      Why 放行：dry_run 不触真单（影子模式），维持放行语义避免破坏本地开发/CI（既有 API
      测试不设 token）；fail-closed 仅作用于 live 模式。
    - 配置后：受保护路由的请求必须携带 Authorization: Bearer <token>，常量时间比较
      （secrets.compare_digest）防时序侧信道攻击。
    - QUANTER_ALLOWED_IPS 可选（逗号分隔）：配置则额外校验来源 IP，纵深防御
      （即便 token 泄漏，非白名单 IP 仍被拒）。

设计原则（CLAUDE.md 极简 + 显式）：
    - 单一依赖函数 require_write，无中间件黑盒，路由层显式声明受保护面；
    - 不引入 JWT/OAuth 重型框架（单用户/小团队部署，Bearer 静态 token 足够）；
    - 失败显式 401/403（不静默放行），符合「显式优于隐式」。
"""
from __future__ import annotations

import logging
import os
import secrets
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_logger = logging.getLogger(__name__)

# auto_error=False：缺 Authorization 头时不自动 403，交由 require_write 统一裁决
# （token 未配置的开发态应放行，不能被 HTTPBearer 默认行为误拒）。
_bearer = HTTPBearer(auto_error=False)


def _configured_token() -> Optional[str]:
    """从环境变量读取已配置的 API token（未配置返 None）。"""
    tok = os.environ.get("QUANTER_API_TOKEN")
    return tok if tok else None


def require_write(
    request: Request,
    cred: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> None:
    """敏感 router 鉴权依赖（挂在 trading/training/data/review/ops 上）。

    校验顺序（DG-G2 fail-closed 铁律）：
        1. token 未配置 + live → **raise 401**（fail-closed，防默认裸奔）；
        2. token 未配置 + 非 live → 开发态放行 + WARNING（dry_run/CI 不阻断）；
        3. token 已配置 + Bearer 缺失/不匹配 → 401（secrets.compare_digest 常量时间比较）；
        4. QUANTER_ALLOWED_IPS 配置且来源 IP 不在白名单 → 403。

    返回 None（FastAPI 依赖仅用于副作用/拦截，无返回值消费）。
    """
    tok = _configured_token()
    if not tok:
        # DG-G2 fail-closed：live 模式（实盘）无 token = 硬拒，防默认裸奔。
        # Why 仅 live 翻转：dry_run 是影子模式（不触真单），维持放行+WARNING 避免破坏
        # 本地开发/CI；live 触真单，必须显式配 token 才能进入受保护路由。
        if os.getenv("AUTO_TRADE_MODE", "dry_run") == "live":
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "live 模式必须配置 QUANTER_API_TOKEN（fail-closed，拒绝无鉴权下单）",
            )
        # dry_run/开发态：token 未配置，放行但提醒生产必须配置。
        _logger.warning(
            "QUANTER_API_TOKEN 未配置，API 处于【无鉴权开发态】——"
            "live 模式将 fail-closed 拒起，生产部署必须配置该环境变量（B-1 / DG-G2）"
        )
        return None

    # token 已配置：强制 Bearer 校验。compare_digest 常量时间比较防时序攻击。
    if cred is None or not secrets.compare_digest(str(cred.credentials), tok):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "无效或缺失 API token")

    # 可选 IP 白名单（纵深防御：token 泄漏时仍限制来源 IP）。
    allowed_ips = os.environ.get("QUANTER_ALLOWED_IPS")
    if allowed_ips:
        client_ip = request.client.host if request.client else ""
        whitelist = {ip.strip() for ip in allowed_ips.split(",") if ip.strip()}
        if client_ip and client_ip not in whitelist:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, f"来源 IP 未授权：{client_ip}"
            )

    return None


def require_read_cookie(request: Request) -> None:
    """SSE 只读鉴权依赖（挂在 logs_router 上）。

    Why cookie 而非 Bearer（DG-G2 裁决）：
        ``/logs/stream`` 是 SSE 长连接，前端用 ``EventSource`` 订阅。EventSource API
        **无法自定义请求头**（不能加 ``Authorization: Bearer``），Bearer 鉴权在此场景
        失效。DG-G2 裁决走 **cookie**（``Cookie: quanter_ro=<token>``）：
          - 同源自动携带（前端 document.cookie 设置后，EventSource 请求自动带上）；
          - 不进 URL / access log（query 参数 ``?token=xxx`` 会进 nginx/uvicorn access
            log 泄露，cookie 走 Header 不进 access log 的 query 段）。

    校验顺序（与 require_write 同源 fail-closed 铁律）：
        1. token 未配置 + live → raise 401（fail-closed，防 SSE 裸奔）；
        2. token 未配置 + 非 live → 放行 + WARNING（dry_run/开发/CI 不阻断）；
        3. cookie ``quanter_ro`` 缺失/不匹配 → 401（compare_digest 常量时间比较）。
    """
    tok = _configured_token()
    if not tok:
        # DG-G2 fail-closed：与 require_write 同源——live 无 token = 硬拒，dry_run 放行。
        if os.getenv("AUTO_TRADE_MODE", "dry_run") == "live":
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "live 模式日志流需 cookie 鉴权（fail-closed，请配置 QUANTER_API_TOKEN）",
            )
        _logger.warning(
            "QUANTER_API_TOKEN 未配置，SSE 日志流处于【无鉴权开发态】——"
            "live 模式将 fail-closed 拒起（B-1 / DG-G2）"
        )
        return None

    # cookie 值与 token 常量时间比较（防时序侧信道爆破，与 require_write 的 Bearer 校验同构）。
    # Why ``or ""``：cookie 缺失时 ``.get`` 返 None，compare_digest(None, str) 会 TypeError；
    # 空串归一后必不等于非空 tok，安全落入「不匹配 → 401」分支。
    cookie_val = request.cookies.get("quanter_ro") or ""
    if not secrets.compare_digest(cookie_val, tok):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "日志流 cookie 无效")

    return None

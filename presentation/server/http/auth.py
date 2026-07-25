# -*- coding: utf-8 -*-
"""API 鉴权依赖（B-1：HTTPBearer token + 可选 IP 白名单）。

物理定位（CLAUDE.md 量化风控·安全审查）：
    修复 B-1——全部交易 API 零认证裸奔。本模块提供 require_write 依赖，挂在可触发
    真实下单/熔断/落盘/起子进程的敏感 router（trading/caisen/data/review）上，
    在请求进入业务逻辑前完成身份校验。

部署语义（环境变量驱动，零外部依赖）：
    - QUANTER_API_TOKEN 未配置：开发态，依赖放行但每次请求打 WARNING（生产必须配置）。
      Why 放行：避免破坏本地开发/CI（既有 API 测试不设 token）；生产部署须显式配置。
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
    """敏感 router 鉴权依赖（挂在 trading/caisen/data/review 上）。

    校验顺序：
        1. token 未配置 → 开发态放行 + WARNING；
        2. Bearer 缺失/不匹配 → 401（secrets.compare_digest 常量时间比较）；
        3. QUANTER_ALLOWED_IPS 配置且来源 IP 不在白名单 → 403。

    返回 None（FastAPI 依赖仅用于副作用/拦截，无返回值消费）。
    """
    tok = _configured_token()
    if not tok:
        # 开发态：token 未配置，放行但提醒生产必须配置。
        _logger.warning(
            "QUANTER_API_TOKEN 未配置，API 处于【无鉴权开发态】——"
            "生产部署必须配置该环境变量（B-1）"
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

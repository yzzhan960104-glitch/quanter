# -*- coding: utf-8 -*-
"""兼容垫片（Step1 迁移）：通知管理器已迁至 infra.notifier。

strangler 铁律①：保留旧模块路径转发，所有 `from core.notifier import X`
（NotificationManager/fire_and_forget/DingTalkChannel/build_default_manager/...）
零改动。新代码请用 `from infra.notifier import ...`。
"""
from infra.notifier import *  # noqa: F401,F403  —— 一网打尽全部公开符号
# 显式再导出 import * 可能漏的（被 __all__ 排除或下划线开头但被外部引用的）：
from infra.notifier import (  # noqa: F401
    NotificationManager, fire_and_forget, DingTalkChannel, build_default_manager,
)

# -*- coding: utf-8 -*-
"""caisen.training_dingtalk 参数审查机器人（Spec 3 §7）。

================================================================
迁移说明（2026-07-16，dws-migration Task 4，权威）
================================================================
原「webhook 推 + dingtalk-stream 收审核」双通道中的 **stream 收审核已删**：
@审核消息现已改走 dws dev connect 桥（scripts/dingtalk_review_bridge.py →
POST /api/v1/training/review → orchestrator.submit_review），不再由本模块的
dingtalk-stream SDK 被动收。本模块仅保留 webhook 主动推报告（training loop 仍在用）。

保留下列实体（@接收改 dws 桥后仍是必需）：
  - ReviewBotConfig：webhook/app 凭证装配（app_* 字段对 webhook 推送本身非必需，但
    from_env 软降级门控沿用「app_key/secret/staff 三件套缺一 → None」语义，避免改
    门控条件连锁影响 server/main.py lifespan 与既有测试断言）。
  - DingTalkNotifier：webhook 推 Markdown 报告（TrainingNotifier Protocol 的 push）。
  - _NoopNotifier：凭证未配时 orchestrator 的软降级哑通知器。

已删（死代码）：ReviewChatbotHandler / _run_stream / start_review_bot +
              `import dingtalk_stream` / `from dingtalk_stream import ...`。

主动推 webhook 物理要点（不变）：
  - 群自定义机器人是单向推（webhook + 加签 secret 两值即可），满足「训练后推报告」场景。
  - 加签复用 core/notifier.py:DingTalkChannel._sign（HMAC-SHA256+base64+urlencode）。
  - errcode 校验复用 DingTalkChannel._validate_response（HTTP 200 + errcode!=0 才是真失败，
    钉钉群机器人最易静默丢失的真实失败模式）。
  - 文本清洗 clean_markdown_for_dingtalk（原 bridge/replier.py，已内联本模块；
    dws-migration Task 5 bridge/ 退役后切断 bridge 依赖）。

全局红线：全中文注释；极简（urllib，复用现成加签/校验/清洗，不造轮子）。
"""
from __future__ import annotations

import json
import logging
import re
import urllib.request
import urllib.error
from dataclasses import dataclass
from typing import Optional

# 复用现成设施：加签/errcode 校验（core/notifier）。
# 注：原文本清洗函数 clean_markdown_for_dingtalk 从 bridge/replier.py 内联而来
# （dws-migration Task 5：bridge/ 自研全退役，本模块是 clean_* 的唯一存活用户，
# 故连同其依赖的 3 个正则常量一并内联，切断对 bridge 的依赖）。
from core.notifier import DingTalkChannel

logger = logging.getLogger(__name__)

# webhook POST 超时（秒）。10s 足够（钉钉群机器人在国内 <1s 回包），过长会反拖 loop 主流程。
_HTTP_TIMEOUT = 10

# ================================================================
# 钉钉 Markdown 文本清洗（原 bridge/replier.py，内联）
# ================================================================
# Why 清洗：钉钉群机器人 Markdown 仅支持 #/##/###、**粗**、*斜*、>引用、-列表、
# [链接](url)、![图](url)；不支持 <font>、表格 |、---分隔线、复杂代码块。
# 训练报告常含这些，直接发会被钉钉渲染成乱码或截断。
# 钉钉不支持的 HTML 标签（剥离标签保留内文）
_FONT_TAG = re.compile(r"<font[^>]*>(.*?)</font>", re.IGNORECASE | re.DOTALL)
# 通用 HTML 标签清理（<br> 转换行，其余剥离；保留 b/strong/i/em/code）
_OTHER_TAGS = re.compile(r"</?(?!b>|strong>|i>|em>|code>)[a-zA-Z][^>]*>")
# Markdown 表格分隔行（|---|---|）。
# 收紧正则：必须含至少一个 |（表格特征），避免误删纯分隔字符的正常文本行
# （如 Markdown 的 --- 水平线、或 ": : : :" 这种无 | 文本）。
# lookahead (?=.*\|) 保证行内至少一个 |，主匹配体只允许分隔字符。
_TABLE_SEPARATOR = re.compile(r"^(?=.*\|)[\s:|-]+$", re.MULTILINE)


def clean_markdown_for_dingtalk(text: str) -> str:
    """剥离钉钉不支持的 Markdown / HTML，保留可渲染部分。

    物理步骤：
      1) <font>...</font> → 内文（钉钉不支持 color 标签）。
      2) <br> → 换行；其余陌生 HTML 标签剥离（钉钉 Markdown 渲染器不认）。
      3) 表格处理：保留数据行 | 作竖线视觉分隔（钉钉不渲染表格，"| a | b |"
         比 " a  b " 可读），仅删分隔行（|---|---| 无信息量且钉钉原样显示成乱码）。
    """
    text = _FONT_TAG.sub(r"\1", text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = _OTHER_TAGS.sub("", text)
    text = _TABLE_SEPARATOR.sub("", text)
    return text.strip()


# ================================================================
# 1. ReviewBotConfig —— 凭证装配（from_env，软降级）
# ================================================================

@dataclass(frozen=True)
class ReviewBotConfig:
    """参数审查机器人配置（环境变量装配，凭证绝不硬编码）。

    历史遗留字段语义（dws-migration Task 4 后）：
      - app_key/app_secret：原供 dingtalk-stream 收审核用，现已删除；字段保留仅为
        不改 from_env 软降级门控（见模块顶部「迁移说明」），实际推送链路不读它们。
      - webhook/webhook_secret：群自定义机器人凭证，webhook 推报告真正在用。
        webhook 可空 → push 软降级 no-op；webhook_secret 可空 → 裸发不加签。

    软降级门控（from_env）：app_key/app_secret/allowed_staff_ids 三者缺一 → 返 None
    （门控条件沿用 dws 迁移前语义，避免连锁改 lifespan/测试；机器人整体不装配但不阻断 uvicorn）。
    webhook/webhook_secret 缺失不影响装配（仅推送降级为 no-op）。
    """
    app_key: str
    app_secret: str
    webhook: str               # 可空 → DingTalkNotifier.push 软降级为 no-op
    webhook_secret: str        # 可空 → 裸发（不加签）
    allowed_staff_ids: tuple   # 白名单 staffId（防他人触发训练消耗算力）

    @classmethod
    def from_env(cls) -> Optional["ReviewBotConfig"]:
        """从 REVIEW_* 环境变量装配。stream 三件套缺一 → 返 None（软降级）。"""
        import os
        app_key = os.getenv("REVIEW_APP_KEY", "").strip()
        app_secret = os.getenv("REVIEW_APP_SECRET", "").strip()
        webhook = os.getenv("REVIEW_WEBHOOK", "").strip()
        webhook_secret = os.getenv("REVIEW_WEBHOOK_SECRET", "").strip()
        raw = os.getenv("REVIEW_ALLOWED_STAFF_IDS", "")
        staff = tuple(s.strip() for s in raw.split(",") if s.strip())

        # stream 收审核必需 app_key/secret/staff；缺 → None 软降级（不阻断 uvicorn）
        if not app_key or not app_secret or not staff:
            logger.info(
                "REVIEW_APP_KEY/SECRET/STAFF_IDS 未完整配置，"
                "参数审查机器人不装配（软降级）"
            )
            return None
        return cls(
            app_key=app_key,
            app_secret=app_secret,
            webhook=webhook,
            webhook_secret=webhook_secret,
            allowed_staff_ids=staff,
        )


# ================================================================
# 2. DingTalkNotifier —— webhook 推报告（实现 TrainingNotifier Protocol）
# ================================================================

class DingTalkNotifier:
    """webhook 推 Markdown（群机器人）。

    实现 TrainingNotifier Protocol 的 push(loop_id, text)。
    加签复用 DingTalkChannel._sign，errcode 校验复用 DingTalkChannel._validate_response。
    webhook 未配 → push 软降级为 no-op（仅 warning 日志），不抛、不阻断 loop 主流程。
    """

    def __init__(self, cfg: ReviewBotConfig) -> None:
        self._cfg = cfg

    def push(self, loop_id: str, text: str) -> None:
        """主动推 Markdown 报告到群机器人。

        物理流程：
          1) webhook 空 → 软降级 no-op（凭证只配了 stream 收审核时走此路）。
          2) clean_markdown_for_dingtalk 清洗（剥 <font>/<br>/表格分隔行等钉钉不支持项）。
          3) title 取首行去掉 # 前缀的前 40 字（钉钉群机器人 Markdown title 必填）。
          4) webhook_secret 非空 → 加签（复用 DingTalkChannel._sign，拼 timestamp=&sign= 到 url）。
          5) urllib POST（不引 requests/aiohttp，极简）。
          6) DingTalkChannel._validate_response 校验 errcode（HTTP 200 + errcode!=0 才是真失败）。

        失败仅记 warning（推送是附属通道，不应反拖垮 loop 主流程）。
        """
        if not self._cfg.webhook:
            logger.warning(
                "REVIEW_WEBHOOK 未配，无法推送 loop=%s（软降级 no-op）", loop_id
            )
            return
        try:
            cleaned = clean_markdown_for_dingtalk(text)
            # title：首行去 # 前缀，截前 40 字；空则兜底「训练报告」
            title = cleaned.split("\n")[0].lstrip("# ").strip()[:40] or "训练报告"
            payload = {
                "msgtype": "markdown",
                "markdown": {"title": title, "text": cleaned},
            }
            url = self._cfg.webhook
            # 加签：secret 非空才加（复用 DingTalkChannel._sign 的 HMAC-SHA256+base64+urlencode）
            if self._cfg.webhook_secret:
                ts, sign = DingTalkChannel._sign(self._cfg.webhook_secret)
                url = f"{url}&timestamp={ts}&sign={sign}"
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(url, data=body, method="POST")
            req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            # HTTP 200 + errcode!=0 才是真失败（钉钉群机器人最易静默丢失的真实失败模式）
            DingTalkChannel._validate_response(data)
            logger.info("钉钉审查机器人推送成功 loop=%s title=%s", loop_id, title)
        except Exception as exc:  # noqa: BLE001
            # 推送是附属通道：任何失败（网络/加签错/errcode!=0）仅 warning，不反拖垮 loop
            logger.warning("钉钉审查机器人推送失败 loop=%s：%s", loop_id, exc)


# ================================================================
# 3. _NoopNotifier —— 凭证未配时的软降级替身
# ================================================================

class _NoopNotifier:
    """凭证未配（from_env 返 None）时 orchestrator 用的哑通知器。

    push 静默 no-op（logger.debug，不触网、不抛）。
    保证 orchestrator 无条件装配 notifier 时的安全降级。"""

    def push(self, loop_id: str, text: str) -> None:  # noqa: D401
        logger.debug("_NoopNotifier 静默丢弃 push loop=%s（凭证未配）", loop_id)

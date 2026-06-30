"""GLM（智谱）大模型客户端：情感打分，强制结构化输出，全异常降级中性。

设计要点（Why 集中说明）：
- 为何用标准 openai SDK + base_url 覆盖：智谱 GLM 的 OpenAI 兼容端点与
  openai Python SDK 协议层完全对齐，复用成熟 SDK 比手写 httpx 更稳
  （重试/流式/JSON mode 均内建），且与未来任何 OpenAI 兼容端点
  （如 DeepSeek、Moonshot、本地 vLLM）一键平替，避免供应商锁定。
- 为何 response_format=json_object + pydantic 双保险：LLM 输出本质不可信，
  仅靠 prompt 约束易出格式漂移（多余前缀/markdown 包裹）。response_format
  在协议层强制 JSON 模式（第一保险），pydantic 校验 score 取值范围
  （第二保险）——任一环节失败均落入降级分支，绝不让脏值污染下游策略。
- 为何全异常降级中性（绝不上抛）：情感因子是另类 alpha，不应让一次
  限频/超时/脏响应拖垮整个回测/实盘流水线。降级为 score=0.0 等价于
  “该条新闻无信息”，与因子缺失同语义，策略层无感。
- 为何凭证缺失不阻断启动：开发机/CI 无 ZHIPU_API_KEY 是常态，
  单例在启动期优雅降级（_client=None），后续调用一律返回中性，
  保持 lifespan 与全量测试脱网可跑。
"""
from __future__ import annotations

import logging
import os
import threading

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# 系统提示词：强约束 LLM 仅输出 {"score","reasoning"} 两字段 JSON。
# 显式声明取值域与语义，降低模型自由发挥导致格式漂移的概率。
SYS_PROMPT = (
    "你是冷酷客观的量化分析师。对给定财经新闻给出情绪打分。"
    "仅输出 JSON：{\"score\": 介于 [-1.0, 1.0] 的浮点, \"reasoning\": 一句话理由}。"
    "score>0 偏多、<0 偏空、0 中性。严禁输出 JSON 以外任何字符。"
)


class SentimentResult(BaseModel):
    """情感打分结构。score ∈ [-1.0, 1.0]。

    pydantic 的 ge/le 约束在 model_validate_json 阶段强制校验，
    任何越界（如 LLM 幻觉输出 1.5）直接抛 ValidationError，
    由上层 try/except 捕获后降级为中性——结构性兜底。
    """
    score: float = Field(ge=-1.0, le=1.0)
    reasoning: str = ""


class GLMClient:
    """GLM 情感打分单例。

    生命周期：
    - 启动期 lifespan 调 get_instance() 完成首次装配（建 AsyncOpenAI client）。
    - 业务期 async analyze_sentiment(news_text) 复用单例 client 打分。
    - 凭证缺失时 _client=None，进入降级模式（一律返回中性）。
    """

    _instance: "GLMClient | None" = None
    _lock = threading.Lock()  # 双重检查锁，线程安全

    @classmethod
    def get_instance(cls) -> "GLMClient":
        """双重检查锁单例，仿 NotificationManager.get_default。"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        from config import LLM_CONFIG
        self._model = LLM_CONFIG["model"]
        self._timeout = LLM_CONFIG["timeout"]
        key = os.getenv("ZHIPU_API_KEY", "")
        self._enabled = bool(key)
        if self._enabled:
            # 延迟 import：仅在有凭证时才引入 openai SDK，
            # 避免 CI/无 SDK 环境下的硬依赖（虽然 T1 已装 openai，
            # 但保持解耦习惯：缺凭证即不持任何外部 client 句柄）。
            from openai import AsyncOpenAI
            # base_url 覆盖：把 openai SDK 的默认端点指向智谱 GLM 兼容端点。
            self._client = AsyncOpenAI(base_url=LLM_CONFIG["base_url"], api_key=key)
        else:
            self._client = None
            logger.warning("ZHIPU_API_KEY 缺失，GLMClient 进入降级模式（一律返回中性）")

    async def analyze_sentiment(self, news_text: str) -> SentimentResult:
        """对单条新闻打分；全异常降级中性，绝不向外抛。

        降级分支覆盖：
        - 凭证缺失 / client 未装配 → 直接中性；
        - 网络超时 / 限频 / 5xx → openai SDK 抛异常被 try 捕获；
        - LLM 返回非 JSON / 字段缺失 / score 越界 → pydantic 抛
          ValidationError 被同一 try 捕获。
        统一回落 SentimentResult(0.0, "降级中性")，保证调用方永远拿到合法对象。
        """
        if not self._enabled or self._client is None:
            return SentimentResult(score=0.0, reasoning="凭证缺失，降级中性")
        try:
            resp = await self._client.chat.completions.create(
                model=self._model,
                # 协议层强制 JSON 模式（第一保险）：模型解码阶段约束只产出 JSON token。
                response_format={"type": "json_object"},
                messages=[{"role": "system", "content": SYS_PROMPT},
                          {"role": "user", "content": news_text}],
                timeout=self._timeout,
            )
            # pydantic 校验（第二保险）：解析 + score 范围约束一次完成。
            return SentimentResult.model_validate_json(resp.choices[0].message.content)
        except Exception as e:
            # 兜底：超时/限频/网络/JSON 非法/越界 全部降级中性，绝不抛。
            logger.warning("GLM 情感打分降级中性：%s", e)
            return SentimentResult(score=0.0, reasoning="降级中性")

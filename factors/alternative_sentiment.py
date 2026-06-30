"""新闻情绪因子：并发调用 GLM 打分，加权聚合当日情绪。

防御红线（Why 集中说明）：
- 为何 asyncio.gather(return_exceptions=True)：新闻列表本质是多条独立 LLM
  调用，若任一条抛错就用默认 return_exceptions=False 会让 gather 抛出
  使整批失败——单条新闻偶发限频/超时绝不能拖垮当日因子计算。
  return_exceptions=True 把异常作为结果元素返回而非上抛，单条失败不炸整批。
- 为何 isinstance(r, SentimentResult) 类型过滤：return_exceptions=True 模式
  下，失败的协程在结果列表里是 Exception 实例而非 SentimentResult。
  必须用 isinstance 显式过滤，把异常结果剔除；否则后续 r.score 会再次抛
  AttributeError 炸掉聚合——这正是上一道防线的兜底。
- 为何全失败/空列表 → 0.0（中性）而非抛：情绪因子是另类 alpha，缺失语义
  等价于“无信息”，0.0 中性让策略层无感（与 GLMClient 自身降级中性语义对齐）。
  绝不让一次网络全挂把整个回测/实盘流水线拖垮。
"""
from __future__ import annotations

import asyncio
import logging

from core.llm_client import GLMClient, SentimentResult

logger = logging.getLogger(__name__)


class NewsSentimentFactor:
    """当日新闻情绪因子：并发打分 → 等权平均。

    构造期注入 client（默认取 GLMClient 单例，测试可注入 FakeClient）。
    """

    def __init__(self, client: GLMClient | None = None) -> None:
        # 默认复用单例 client（lifespan 装配，凭证缺失时自身已降级中性）；
        # 测试用例通过显式注入 client 隔离网络依赖。
        self._client = client or GLMClient.get_instance()

    async def compute_daily_score(self, news_list: list[str]) -> float:
        """并发打分 → 等权平均。

        - 空列表 → 0.0（无信息即中性）。
        - 单条失败被 gather 吞掉，仅取成功结果做平均。
        - 全失败 → 0.0（降级中性，绝不抛）。
        """
        # 空列表短路：避免空 gather + 后续除零。
        if not news_list:
            return 0.0
        # 并发打分：return_exceptions=True 让单条异常作为结果元素返回，
        # 不污染整批；await 一次拿到全部结果（成功+失败混合）。
        results = await asyncio.gather(
            *(self._client.analyze_sentiment(t) for t in news_list),
            return_exceptions=True,
        )
        # 类型过滤剔除异常结果：仅保留 SentimentResult 实例。
        # 异常实例无 .score 属性，不过滤会触发 AttributeError。
        scores = [r.score for r in results if isinstance(r, SentimentResult)]
        # 全失败兜底：无任何成功结果 → 降级中性 0.0，绝不抛。
        if not scores:
            logger.warning("当日新闻全部打分失败，情绪因子降级 0.0")
            return 0.0
        # 等权平均：每条新闻情绪权重相等，朴素算术平均。
        return float(sum(scores) / len(scores))

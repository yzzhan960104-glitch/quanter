"""NewsSentimentFactor：并发打分、单条失败不炸、全失败→0.0。"""
import asyncio
from factors.alternative_sentiment import NewsSentimentFactor
from core.llm_client import SentimentResult


class _FakeClient:
    def __init__(self, scores):
        self._scores = scores
    async def analyze_sentiment(self, text):
        # 模拟第 2 条抛错（被 gather return_exceptions 吞掉）
        if self._scores is None:
            raise RuntimeError("boom")
        return SentimentResult(score=self._scores.pop(0), reasoning="x")


def test_weighted_average_of_scores():
    f = NewsSentimentFactor(client=_FakeClient([0.6, 0.4]))
    s = asyncio.run(f.compute_daily_score(["a", "b"]))
    assert abs(s - 0.5) < 1e-9


def test_single_failure_does_not_crash():
    f = NewsSentimentFactor(client=_FakeClient([0.8]))  # 第 2 条会 index error
    s = asyncio.run(f.compute_daily_score(["a", "b"]))
    # 仅 1 条成功 → 取成功的 0.8
    assert abs(s - 0.8) < 1e-9


def test_all_failure_returns_zero():
    f = NewsSentimentFactor(client=_FakeClient(None))  # 全抛
    s = asyncio.run(f.compute_daily_score(["a", "b"]))
    assert s == 0.0


def test_empty_list_returns_zero():
    f = NewsSentimentFactor(client=_FakeClient([]))
    assert asyncio.run(f.compute_daily_score([])) == 0.0

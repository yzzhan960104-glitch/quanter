# -*- coding: utf-8 -*-
"""U1：APScheduler job_defaults 三参数硬化断言。

物理意图：裸 AsyncIOScheduler() 无 job_defaults——机器休眠/慢触发会 job 堆积重叠
（pre_open 跑超 9:22 与下次重叠双挂、stop_loss 30s 堆积补跑风暴）。三参数锁死。
"""
import pytest
from trading.engine import TradingEngine


def test_sched_job_defaults_hardened():
    """构造 engine 即断言 sched.job_defaults 含三参数（防回归到裸构造）。"""
    eng = TradingEngine()
    # APScheduler 3.x 把 job_defaults 存在私有属性 _job_defaults（无公开 job_defaults 属性）
    jd = eng.sched._job_defaults
    assert jd.get("max_instances") == 1, f"max_instances 应为 1（防重叠双挂），实得 {jd.get('max_instances')}"
    assert jd.get("misfire_grace_time") == 300, f"misfire_grace_time 应为 300s，实得 {jd.get('misfire_grace_time')}"
    assert jd.get("coalesce") is True, f"coalesce 应为 True（堆积合并一次），实得 {jd.get('coalesce')}"

# -*- coding: utf-8 -*-
"""training_loop 状态机单测。mock 回测 DB + analyzer + notifier，不碰真网络/真回测。"""
import json
from unittest.mock import MagicMock, patch

import pytest

from backtest.optimize import training_loop
from backtest.optimize import training_dingtalk   # 端到端测试 mock 其 urllib.request.urlopen（真 webhook 通道）
from backtest.optimize.training_loop import TrainingLoopOrchestrator, LoopBusyError


class FakeNotifier:
    """记录推送/回显，便于断言 loop 在正确时机调了 notifier。"""
    def __init__(self):
        self.pushed = []   # [(loop_id, text)]
    def push(self, loop_id, text):
        self.pushed.append((loop_id, text))


@pytest.fixture
def orch(monkeypatch, tmp_path):
    """装配一个用 tmp DB + fake notifier 的编排器。"""
    db = str(tmp_path / "loops.db")
    monkeypatch.setattr(training_loop.training_loops_db, "_DEFAULT_DB_PATH", db)
    training_loop.training_loops_db.init_db()
    monkeypatch.setattr(training_loop.replay_tasks_db, "_DEFAULT_DB_PATH",
                        str(tmp_path / "replay.db"))
    training_loop.replay_tasks_db.init_db()
    notifier = FakeNotifier()
    o = TrainingLoopOrchestrator(notifier)
    return o, notifier


def test_start_runs_round_then_awaits_review(orch, monkeypatch):
    """核心动线：start → 提交回测 → 轮询到 SUCCESS → analyze → AWAITING_REVIEW + 推报告。

    用 _step_once 手动推进状态机（不起 daemon 线程），可控可测。
    """
    o, notifier = orch
    loop_id = o.start({"start": "2020-01-01", "end": "2024-12-31", "universe": None,
                       "base_cfg": {"min_rr_ratio": 1.5}, "max_rounds": 3})
    # mock：提交回测后立刻把它标 SUCCESS + 写 report
    def fake_get_task(task_id, path=None):
        return {"task_id": task_id, "status": "SUCCESS",
                "report": {"n_hits": 10, "win_rate": 0.6, "avg_rr": 1.8,
                           "max_drawdown": -0.1, "annualized_return": 0.2,
                           "pattern_dist": {}, "trades": []}}
    monkeypatch.setattr(training_loop.replay_tasks_db, "get_task", fake_get_task)
    monkeypatch.setattr(training_loop.training_analyzer, "analyze_round",
                        lambda r, c, h: "## 报告：表现尚可")

    o._step_once(loop_id)   # RUNNING：提交回测 + 轮询到 SUCCESS → ANALYZING → AWAITING_REVIEW

    loop = training_loop.training_loops_db.get_loop(loop_id)
    assert loop["status"] == "AWAITING_REVIEW"
    assert loop["current_round"] == 1
    assert len(loop["history"]) == 1              # 第1轮统计已入 history
    assert notifier.pushed                         # 报告已推
    assert "报告" in notifier.pushed[-1][1]


def test_start_rejects_second_active_loop(orch):
    """concurrency=1 守卫：已有活跃 loop 再 start → LoopBusyError。"""
    o, _ = orch
    o.start({"start": "2020-01-01", "end": "2024-12-31", "base_cfg": {}, "max_rounds": 3})
    # 手动把第一个标活跃（start 落 IDLE，不在 list_active_loops 里）
    from backtest.optimize import training_loops_db
    lid = training_loops_db.list_loops()[0]["loop_id"]
    training_loops_db.update_loop(lid, status="RUNNING")
    with pytest.raises(LoopBusyError):
        o.start({"start": "2020-01-01", "end": "2024-12-31", "base_cfg": {}, "max_rounds": 3})


def test_confirm_rerun_accumulates_cfg(orch, monkeypatch):
    """CONFIRMING：parse→回显→「确认」→ 下一轮 cfg 累积 + 状态回 RUNNING。

    时序：主线程 _step_once 进 _handle_awaiting_review 阻塞等 event；子线程 sleep(0.2)
    后 submit_review 喂"重跑意图"唤醒第一次（进 _confirm 回显），_confirm 再等"确认"，
    子线程再 submit_review("确认") 唤醒第二次 → _apply_confirmed → RUNNING。
    验证 _confirm 的两段 wait 都能被 submit_review 正确唤醒（不靠加 sleep 硬等）。
    """
    o, notifier = orch
    loop_id = o.start({"start": "2020-01-01", "end": "2024-12-31", "base_cfg": {"min_rr_ratio": 1.5},
                       "max_rounds": 3})
    # 直接置 AWAITING_REVIEW 模拟已到人审关卡（跳过回测链路，聚焦 CONFIRMING）
    from backtest.optimize import training_loops_db
    training_loops_db.update_loop(loop_id, status="AWAITING_REVIEW", current_round=1)
    monkeypatch.setattr(training_loop.training_analyzer, "parse_review",
                        lambda t, c, **kw: {"cfg_override": {"max_holding_bars": 20}, "action": "rerun"})

    # 起一个线程模拟你两次回复：第一次触发 _confirm（parse 后回显），第二次「确认」落库。
    # _POLL_INTERVAL 默认 3s，测试里把它压到 0.05 让 wait 超时快、唤醒响应快（不硬等 3s）。
    monkeypatch.setattr(training_loop, "_POLL_INTERVAL", 0.05)
    import threading
    import time as _t
    replies = ["min_rr 改2.0 重跑", "确认"]
    def reply_later():
        for r in replies:
            _t.sleep(0.08)   # 等 daemon 进入 wait 阻塞
            o.submit_review(loop_id, r)
    threading.Thread(target=reply_later).start()
    o._step_once(loop_id)   # AWAITING_REVIEW → CONFIRMING → 等「确认」 → RUNNING

    loop = training_loops_db.get_loop(loop_id)
    assert loop["status"] == "RUNNING"
    assert loop["current_round"] == 1   # RUNNING 等 _handle_running 才 +1
    assert loop["current_cfg"] == {"min_rr_ratio": 1.5, "max_holding_bars": 20}  # 累积


def test_stop_interrupts_running_round(orch, monkeypatch):
    """I1 回归：stop() 必须能中断 RUNNING 态的回测轮询（驱动 I1 的 DB stop 检查）。

    场景：loop 进 RUNNING 后回测一直 PENDING（模拟长回测不终态）→ 主线程 stop(loop_id)
    → 断言 _handle_running 在 ≤2×_POLL_INTERVAL 内退出 + DB status==STOPPED + history
    未被 append（无 phantom 轮次污染）。

    时序：_POLL_INTERVAL 压到 0.05（同 CONFIRMING 测试范式），子线程 sleep(0.08) 后喊停，
    确保 daemon 已进入 _handle_running 的 wait 阻塞再 stop。若无 I1 的 DB stop 检查，
    _handle_running 会死等回测终态（本测 mock 永远 PENDING），测试会超时挂死。
    """
    o, notifier = orch
    loop_id = o.start({"start": "2020-01-01", "end": "2024-12-31", "universe": None,
                       "base_cfg": {"min_rr_ratio": 1.5}, "max_rounds": 3})
    # mock：回测永远 PENDING（长回测不终态）——逼出 _handle_running 的轮询循环
    monkeypatch.setattr(training_loop.replay_tasks_db, "get_task",
                        lambda task_id, path=None: {"task_id": task_id, "status": "PENDING"})
    monkeypatch.setattr(training_loop, "_POLL_INTERVAL", 0.05)

    import threading
    import time as _t
    def stop_later():
        _t.sleep(0.08)   # 等 daemon 进 _handle_running 的 wait 阻塞
        o.stop(loop_id)
    threading.Thread(target=stop_later).start()

    o._step_once(loop_id)   # IDLE→RUNNING→_handle_running 轮询 → 被 stop 中断退出

    from backtest.optimize import training_loops_db
    loop = training_loops_db.get_loop(loop_id)
    assert loop["status"] == "STOPPED"          # stop() 落库的终态
    assert loop["history"] == []                 # 无 phantom：未 append 任何轮次摘要
    # current_round 在 _handle_running 开头已 +1（提交回测时即记账，stop 中断不回滚）——
    # 这是可接受的（round 已开始跑过回测 task，即便被中断也计为发起过）


def test_full_roundtrip_with_dingtalk_notifier(monkeypatch, tmp_path):
    """端到端：loop→回测SUCCESS→analyze→push 报告→收审核→确认→下一轮 RUNNING。

    真实接线验证（区别于前 4 个用 FakeNotifier 的单测）：注入真实 DingTalkNotifier
    （mock urllib.request.urlopen 不触网），覆盖整条动线：
      RUNNING→ANALYZING→AWAITING_REVIEW（active_loop_id 正确路由）→ 收审核 CONFIRMING
      → 确认 → RUNNING(下一轮) + current_cfg 累积（min_rr_ratio: 1.5 → 2.0）。

    物理意图：验证 (1) active_loop_id property 返回当前 loop；(2) DingTalkNotifier.push
    走真实 webhook 通道（mock urllib 后的 json POST + errcode 校验）；(3) TrainingNotifier
    Protocol 与 loop 状态机正确接线——这是 Spec3「钉钉闭环」的端到端门控。
    """
    import threading
    import time as _t

    # ---- 1) tmp DB 隔离（同 orch fixture 范式，但不复用 fixture 以便插入真实 notifier）----
    db = str(tmp_path / "loops.db")
    monkeypatch.setattr(training_loop.training_loops_db, "_DEFAULT_DB_PATH", db)
    training_loop.training_loops_db.init_db()
    monkeypatch.setattr(training_loop.replay_tasks_db, "_DEFAULT_DB_PATH",
                        str(tmp_path / "replay.db"))
    training_loop.replay_tasks_db.init_db()

    # ---- 2) 装配真实 DingTalkNotifier（REVIEW_WEBHOOK 配上，触发 push 真走 urllib 分支）----
    # stream 三件套（APP_KEY/SECRET/STAFF_IDS）+ webhook 两件套（WEBHOOK/SECRET）全配齐，
    # 让 from_env 装配成功且 push 真正 POST（webhook 空 → push 软降级 no-op，测不到 urllib 路径）。
    monkeypatch.setenv("REVIEW_APP_KEY", "ak")
    monkeypatch.setenv("REVIEW_APP_SECRET", "sk")
    monkeypatch.setenv("REVIEW_WEBHOOK", "https://oapi.dingtalk.com/robot/send?access_token=X")
    monkeypatch.setenv("REVIEW_WEBHOOK_SECRET", "sec")
    monkeypatch.setenv("REVIEW_ALLOWED_STAFF_IDS", "s1")

    # ---- 3) mock urllib（webhook POST 响应）+ call 收集（端到端门控核心）----
    # DingTalkNotifier.push 在 webhook 方案下只发一次 POST（无 access_token 换取），
    # 响应体 {"errcode":0,"errmsg":"ok"} 经 DingTalkChannel._validate_response 视为成功。
    # 这里用上下文管理器协议 mock（with urlopen(...) as resp），匹配真实代码的 with 写法。
    #
    # 【I1 修法】call 收集是端到端门控的核心：DingTalkNotifier.push 用 `except Exception`
    # 吞失败 + webhook 空 no-op early return。若未来误删 REVIEW_WEBHOOK env（或 push 静默
    # 出错被吞），仅断言终态（status/cfg）会全绿——门控形同虚设。此处记录每次 urlopen 的
    # url + 解析 body 还原的 markdown text，后续断言 calls ≥1（报告真推了）+ 某次 text 含
    # CONFIRMING 草稿特征（_confirm 真发生了），锁住整条 webhook 通道真实工作。
    urlopen_calls = []   # [(url, markdown_text)]
    def fake_urlopen(req, *args, **kwargs):
        # 【M2 修法】*args/**kwargs 签名比 (req, timeout=None) 更稳——防未来 push 调用
        # urlopen 时传参方式变化（如加 cafile/capath/context 等 urllib 原生参数）导致 mock 漏匹配。
        url = req.full_url
        text = ""
        try:
            # body 是 DingTalkNotifier.push 构造的 json：{"msgtype":"markdown","markdown":{"title":..,"text":..}}
            body = req.data.decode("utf-8") if req.data else ""
            payload = json.loads(body) if body else {}
            text = payload.get("markdown", {}).get("text", "")
        except Exception:  # noqa: BLE001
            text = ""
        urlopen_calls.append((url, text))
        resp = MagicMock()
        resp.read.return_value = json.dumps({"errcode": 0, "errmsg": "ok"}).encode()
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        return resp
    monkeypatch.setattr(training_dingtalk.urllib.request, "urlopen", fake_urlopen)

    from backtest.optimize.training_dingtalk import DingTalkNotifier, ReviewBotConfig
    cfg = ReviewBotConfig.from_env()
    assert cfg is not None, "REVIEW_* 环境变量装配失败，DingTalkNotifier 无法构造"
    notifier = DingTalkNotifier(cfg)
    o = training_loop.TrainingLoopOrchestrator(notifier)

    # ---- 4) mock 回测/analyzer（回测直 SUCCESS；analyze 产报告；parse 返 rerun 改 min_rr）----
    monkeypatch.setattr(training_loop.replay_tasks_db, "get_task",
                        lambda tid, path=None: {"status": "SUCCESS",
                            "report": {"n_hits": 5, "win_rate": 0.5, "avg_rr": 1.0,
                                       "max_drawdown": -0.1, "annualized_return": 0.1,
                                       "pattern_dist": {}}})
    monkeypatch.setattr(training_loop.training_analyzer, "analyze_round",
                        lambda r, c, h: "报告")
    monkeypatch.setattr(training_loop.training_analyzer, "parse_review",
                        lambda t, c, **kw: {"cfg_override": {"min_rr_ratio": 2.0}, "action": "rerun"})

    # ---- 5) 起训练 loop（max_rounds=3，首轮 RUNNING→…→AWAITING_REVIEW）----
    loop_id = o.start({"start": "2020-01-01", "end": "2024-12-31",
                       "base_cfg": {"min_rr_ratio": 1.5}, "max_rounds": 3})

    # _POLL_INTERVAL 压到 0.05（同 Task3 范式）：AWAITING_REVIEW/CONFIRMING 的 wait 循环
    # 超时快、唤醒响应快，配合子线程 sleep(0.2) 触发 submit_review，时序稳定不 flaky。
    monkeypatch.setattr(training_loop, "_POLL_INTERVAL", 0.05)

    # 第一拍：IDLE→RUNNING→提交回测→SUCCESS→ANALYZING→AWAITING_REVIEW（一轮动线原子跑完）
    o._step_once(loop_id)
    # active_loop_id 必须正确路由到当前 loop（钉钉 handler 据此决定喂哪个 loop）
    assert o.active_loop_id == loop_id
    # 【I1 门控】AWAITING_REVIEW 后 urlopen 必被调 ≥1 次（报告真推到 webhook 了）。
    # 若未来误删 REVIEW_WEBHOOK env 或 push 被吞，urlopen_calls 为空——此处断言立即红。
    assert len(urlopen_calls) >= 1, "AWAITING_REVIEW 后 webhook 未被调（REVIEW_WEBHOOK 可能丢失或 push 静默 no-op）"

    # ---- 6) 子线程模拟钉钉 handler 调 submit_review（CONFIRMING 两段式确认）----
    # _step_once 第二拍进 _handle_awaiting_review 阻塞等 event；子线程 0.2s 后喂"重跑意图"
    # 唤醒第一次（_confirm 回显草稿），_confirm 再等"确认"，子线程再 0.2s 后喂"确认"唤醒第二次
    # → _apply_confirmed → 下一轮 RUNNING + current_cfg 累积 min_rr_ratio: 1.5 → 2.0。
    def review_later():
        _t.sleep(0.2)                       # 等 daemon 进 _handle_awaiting_review 的 wait 阻塞
        o.submit_review(loop_id, "min_rr 改2.0 重跑")  # 唤醒第一次 → _confirm 回显
        _t.sleep(0.2)                       # 等 _confirm 进第二次 wait 阻塞
        o.submit_review(loop_id, "确认")     # 唤醒第二次 → _apply_confirmed 落库
    threading.Thread(target=review_later, daemon=True).start()

    # 第二拍：AWAITING_REVIEW → CONFIRMING（parse+回显）→ 等「确认」→ RUNNING（下一轮）
    o._step_once(loop_id)

    # ---- 7) 断言终态：下一轮 RUNNING + current_cfg 累积生效 ----
    loop = training_loop.training_loops_db.get_loop(loop_id)
    assert loop["status"] == "RUNNING"
    assert loop["current_cfg"]["min_rr_ratio"] == 2.0   # rerun 累积（1.5 ∪ {min_rr_ratio:2.0}）

    # 【I1+M3 门控】CONFIRMING 草稿真推过 webhook：两次 submit_review 间无法插断言（子线程
    # 时序），故在终态后间接锁住——断言某次推送的 markdown text 含 _render_confirm 草稿特征词
    # （「请确认」/「动作」/「改参重跑」之一，见 training_loop._render_confirm）。若 _confirm
    # 未发生（如 submit_review 唤醒失败、parse 异常早退），草稿推送缺失——此处断言立即红。
    # 同时验证 _confirm 的 push(loop_id, draft) 走的也是真实 webhook 通道（urlopen_calls 收得到）。
    confirm_keywords = ("请确认", "动作", "改参重跑")
    has_confirm_draft = any(
        any(kw in text for kw in confirm_keywords)
        for _, text in urlopen_calls
    )
    assert has_confirm_draft, (
        f"未在任何 webhook 推送中检测到 CONFIRMING 草稿特征词 {confirm_keywords}——"
        f"_confirm 回显草稿可能未推送（CONFIRMING 未真实发生）。urlopen_calls={urlopen_calls}"
    )

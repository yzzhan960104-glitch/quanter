# -*- coding: utf-8 -*-
"""caisen.training_loop 训练 loop 状态机编排器（Spec 3 §4）。

物理定位：uvicorn 进程内 daemon 线程，concurrency=1（同时只一个活跃 loop）。
每轮：提交一个回测 task（复用 replay_tasks_db.create_task，由 Spec1 ReplayScheduler
派发）→ 轮询 get_task 等终态 → analyze_round 产报告 → AWAITING_REVIEW 推钉钉等你审核
→ submit_review(钉钉调) 驱动 CONFIRMING（parse_review + 回显确认）→ 下一轮或 STOPPED。

解耦：状态机只依赖 TrainingNotifier Protocol（push/reply）；钉钉实现在 training_dingtalk。

时序约定：_handle_running 内部把"提交回测→轮询→ANALYZING→analyze→AWAITING_REVIEW"
作为一轮原子动线跑完——回测 SUCCESS 的后续 ANALYZING/AWAITING_REVIEW 是同一轮的收尾，
拆到独立 _step_once 周期只会增加无意义的跨周期状态悬挂。FAILED/CANCELLED 直接落
AWAITING_REVIEW（失败也得人审决策）。故 daemon 单次 _step_once 即推进完整一轮（或卡在
AWAITING_REVIEW 等人审）。
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any, Dict, List, Optional, Protocol

from caisen import replay_tasks_db, training_analyzer, training_loops_db
# 复用 replay_tasks_db 的时间戳工具——loop 表的 started_at/finished_at 与 replay_tasks
# 同源时钟，零重复实现。brief Step3 代码漏了这两行 import，实现者补齐。
# Step3.4 follow-up：replay_tasks_db 已迁 caisen/infra/。直接走物理新路径，避免循环 import 下
# 顶层垫片对 ``from caisen.replay_tasks_db import name`` 的 from-import 取属性失效（详见
# training_loops_db.py:18 注释）。与 replay_tasks_db 实体同对象，零行为差异。
from caisen.infra.replay_tasks_db import _now_iso

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 3.0          # 轮询回测终态间隔（秒）


class LoopBusyError(Exception):
    """已有活跃 loop（concurrency=1 守卫）→ API 层转 422。"""


class TrainingNotifier(Protocol):
    """推送接口抽象（钉钉实现见 training_dingtalk；测试注入 fake）。"""
    def push(self, loop_id: str, text: str) -> None: ...


class TrainingLoopOrchestrator:
    """训练 loop 编排器（daemon 线程跑状态机 + submit_review 驱动人审关卡）。

    线程模型：单 daemon 线程串行推进活跃 loop（concurrency=1）。人审关卡用
    per-loop threading.Event 解除阻塞——submit_review set event，daemon 继续。
    """

    def __init__(self, notifier: TrainingNotifier):
        self._notifier = notifier
        self._thread: Optional[threading.Thread] = None
        self._stop_flag = threading.Event()
        # loop_id → 审核事件 + 待审文本（AWAITING_REVIEW 时 submit_review 唤醒）
        # 为什么用 dict 而非单值：concurrency=1 之下虽然同时只一个活跃 loop，但保留
        # loop_id 索引让 submit_review 能精确寻址（即便未来放开并发也无须改结构）。
        self._review_events: Dict[str, dict] = {}
        self._lock = threading.Lock()

    # ---- 公开 API ----
    @property
    def active_loop_id(self) -> Optional[str]:
        """当前活跃 loop_id（供 dws 桥把 @审核消息路由到正确 loop）。

        物理定位：@审核消息经 dws dev connect 桥（dingtalk_review_bridge.py →
        POST /api/v1/training/review → orchestrator.submit_review）进来时，需确定"喂给
        哪个 loop 的 submit_review"。concurrency=1 下同时只一个活跃 loop，取 list_active_loops
        首个即可；无活跃 loop 返 None（dws 桥据此忽略，防误触）。直接查 DB 而非缓存内存值——
        保证多入口（API start / dws 桥收消息）看到的活跃态一致，避免内存态与 DB 漂移。
        """
        active = training_loops_db.list_active_loops()
        return active[0]["loop_id"] if active else None

    def start_daemon(self) -> None:
        """启动 daemon 推进线程（lifespan 调；幂等）。"""
        if self._thread and self._thread.is_alive():
            return
        self._stop_flag.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="training-loop")
        self._thread.start()

    def stop_daemon(self) -> None:
        """停 daemon 线程（lifespan 关闭调；仅置 flag，等当前周期结束退出）。"""
        self._stop_flag.set()

    def start(self, req: dict) -> str:
        """提交训练 loop（concurrency=1 守卫：已有活跃 loop → LoopBusyError）。

        物理意图：一次只允许一个活跃 loop，避免两个 loop 并发回测撞算力/撞库。
        守卫查 list_active_loops（含 RUNNING/ANALYZING/AWAITING_REVIEW/CONFIRMING），
        任一在跑即拒。loop 落库时 status=IDLE，首轮 _step_once 才 IDLE→RUNNING。

        TOCTOU 防御：check（list_active_loops）+ create（create_loop）必须同在 self._lock
        内原子完成——否则两并发 start 都过空检查再各自 create，会出现两个 IDLE loop 并存，
        都被 daemon 点火成 RUNNING → 两个活跃 loop 同时回测（撞算力/撞库）。把 init_db 也
        移入锁内（幂等，无副作用），确保 create 前表一定就绪。
        """
        with self._lock:
            if training_loops_db.list_active_loops():
                raise LoopBusyError("已有活跃训练 loop（一次只允许一个）")
            training_loops_db.init_db()
            replay_tasks_db.init_db()
            loop_id = training_loops_db.create_loop(req)
        # 起一个审核 event 供首轮 AWAITING_REVIEW 用
        self._review_events[loop_id] = {"event": threading.Event(), "text": None}
        return loop_id

    def stop(self, loop_id: str) -> None:
        """人工喊停（钉钉 action=stop 或 API /stop）→ STOPPED + 解除阻塞。

        为什么 stop 也走 _wake：若 loop 正卡在 _handle_awaiting_review/_confirm 的
        ev.wait() 上，仅改 DB 状态不够，必须 set event 才能解除线程阻塞，否则会卡到
        下次 _POLL_INTERVAL 超时才发现——用哨兵 "__STOP__" 区分"停"与"审核文本"。
        """
        training_loops_db.update_loop(loop_id, status="STOPPED", finished_at=_now_iso())
        self._wake(loop_id, "__STOP__")

    def submit_review(self, loop_id: str, text: str) -> None:
        """钉钉 handler 收到你的审核 → 唤醒 AWAITING_REVIEW（CONFIRMING 流程由 daemon 处理）。

        物理定位：本方法由钉钉 webhook/stream handler 在另一线程调用（不在 daemon 线程），
        通过 threading.Event 跨线程把审核文本递给正阻塞在 _handle_awaiting_review 的 daemon。
        """
        self._wake(loop_id, text)

    # ---- daemon 主循环 ----
    def _loop(self) -> None:
        """daemon 线程主体：周期扫活跃 loop 各推进一步，直到 stop_daemon。"""
        while not self._stop_flag.is_set():
            try:
                for loop in training_loops_db.list_active_loops():
                    self._step_once(loop["loop_id"])
            except Exception:
                # 吞掉单轮异常保活：daemon 挂了所有 loop 都没人推，故宁可记日志继续。
                logger.exception("training-loop daemon 循环异常（吞掉继续）")
            self._stop_flag.wait(_POLL_INTERVAL)

    def _step_once(self, loop_id: str) -> None:
        """推进单个 loop 一个状态转移（daemon 每轮调一次；测试也可直调）。

        状态机（Spec §4）：
          IDLE             → RUNNING（_prime_if_idle，首轮启动）
          RUNNING          → _handle_running：提交回测+轮询终态 → SUCCESS 链 ANALYZING→
                             AWAITING_REVIEW（一轮动线原子完成）/ FAILED → AWAITING_REVIEW
          ANALYZING        → _handle_analyzing：analyze_round → AWAITING_REVIEW（推报告）
          AWAITING_REVIEW  → _handle_awaiting_review：阻塞等 submit_review → CONFIRMING
                             （parse+回显）→ 确认则 RUNNING/STOPPED
        本方法按当前 status 分派；_handle_running 会链式把 SUCCESS 的 ANALYZING/AWAITING
        一并跑完（同一轮回测的收尾不宜跨周期悬挂）。
        """
        loop = training_loops_db.get_loop(loop_id)
        if loop is None:
            return
        status = loop["status"]

        # IDLE → RUNNING：start() 落 IDLE，首轮 daemon 负责点火上轮。仅改状态，
        # 真正提交回测交给 RUNNING 分支（保持 _handle_running 入口不变量：进 RUNNING 即
        # 提交当轮回测 + current_round+1）。
        if status == "IDLE":
            training_loops_db.update_loop(loop_id, status="RUNNING",
                                          started_at=_now_iso())
            loop = training_loops_db.get_loop(loop_id)
            status = "RUNNING"

        if status == "RUNNING":
            self._handle_running(loop)
        elif status == "ANALYZING":
            self._handle_analyzing(loop)
        elif status == "AWAITING_REVIEW":
            self._handle_awaiting_review(loop)
        # CONFIRMING 在 _handle_awaiting_review 内联完成（parse+回显+等确认一气呵成）

    # ---- 状态处理 ----
    def _handle_running(self, loop: dict) -> None:
        """提交当轮回测 + 轮询终态 → ANALYZING（成功链到 AWAITING_REVIEW）/ AWAITING_REVIEW(失败)。

        轮次语义：进 RUNNING 即"开始跑第 current_round+1 轮"——current_round 从 0 起，
        首轮 _step_once 经 IDLE→RUNNING 到这里 round_n=1。写库后提交回测 task（复用
        replay_tasks_db.create_task 写 PENDING，Spec1 ReplayScheduler 派发，本处只轮询）。
        """
        loop_id = loop["loop_id"]
        round_n = loop["current_round"] + 1
        training_loops_db.update_loop(loop_id, current_round=round_n)
        task_id = replay_tasks_db.create_task({
            "start": loop["start"], "end": loop["end"],
            "universe": loop["universe"],
            "cfg_override": loop["current_cfg"],
        })
        # 轮询等终态（带 stop 检查，避免你 stop 后还死等）
        # 两道 stop 感知：(1) daemon 级 _stop_flag（lifespan 关闭）；(2) 本 loop 级 DB STOPPED
        # （你钉钉喊停）。stop(loop_id) 只 update DB + _wake review event，不 set _stop_flag，
        # 故这里必须显式查 DB——否则长回测（几十分钟~几小时）要等自然终态才退出，且 SUCCESS
        # 时 _on_round_success 会无视 STOPPED 继续 append_history + 推报告，污染已停 loop。
        # 与 _handle_awaiting_review 的 wait 循环对称：响应延迟 ≤ _POLL_INTERVAL。
        while not self._stop_flag.is_set():
            cur = training_loops_db.get_loop(loop_id)
            if cur is None or cur["status"] == "STOPPED":
                return   # 你喊停了，放弃本轮回测轮询（不 append history、不推报告）
            task = replay_tasks_db.get_task(task_id)
            if task is None:
                # 回测任务被清/丢失——不自动重提，交人审决策（重跑/改/停）
                training_loops_db.update_loop(loop_id, status="AWAITING_REVIEW",
                    pending_review="回测任务丢失，请重试/改/停",
                    error="replay task not found")
                self._notifier.push(loop_id, f"⚠️ 第{round_n}轮回测任务丢失，请回复「重跑/改…/停」。")
                return
            if task["status"] == "SUCCESS":
                self._on_round_success(loop_id, round_n, task["report"])
                # 链式收尾：回测 SUCCESS 后 analyze + 推报告 + AWAITING_REVIEW 同属本轮动线，
                # 不拆到下个 _step_once 周期（否则 SUCCESS→ANALYZING 中间态无意义悬挂一周期）。
                self._analyze_and_await(loop_id)
                return
            if task["status"] in ("FAILED", "CANCELLED"):
                # 失败交人审：不自动重试（重复消耗算力），把错误摊给你决策。
                training_loops_db.update_loop(loop_id, status="AWAITING_REVIEW",
                    pending_review=f"第{round_n}轮回测失败：{task.get('error','')}")
                self._notifier.push(loop_id,
                    f"⚠️ 第{round_n}轮回测失败：{task.get('error','')}\n请回复「重跑」或「改 字段=值 重跑」或「停」。")
                return
            # PENDING/RUNNING：未到终态，等下一拍再查（带 stop 响应，最长卡 _POLL_INTERVAL）
            self._stop_flag.wait(_POLL_INTERVAL)

    def _on_round_success(self, loop_id: str, round_n: int, report: dict) -> None:
        """回测 SUCCESS → 记历史摘要 → ANALYZING。

        为什么只存摘要不存整 trades：history 喂 GLM 看多轮趋势（参数越调越激进→回撤放大），
        完整 trades 会撑爆 context；6 字段统计足以支撑趋势判断。完整 trades 在
        replay_tasks.report 里随时可回查。
        """
        summary = {
            "round": round_n,
            "n_hits": report.get("n_hits", 0),
            "win_rate": report.get("win_rate", 0),
            "avg_rr": report.get("avg_rr", 0),
            "max_dd": report.get("max_drawdown", 0),
            "annualized": report.get("annualized_return", 0),
        }
        training_loops_db.append_history(loop_id, summary)
        training_loops_db.update_loop(loop_id, status="ANALYZING")

    def _analyze_and_await(self, loop_id: str) -> None:
        """ANALYZING 分支主体（RUNNING 成功后链式调用，也可被 _step_once ANALYZING 态直调）。

        单独抽方法：_handle_running 成功链路 + _step_once ANALYZING 分支都要跑这段，
        抽出来零重复。取最新 loop（前面 update 后 dict 已旧，必须重读拿新 history/cfg）。
        """
        loop = training_loops_db.get_loop(loop_id)
        last_report = self._last_report_summary(loop)  # 用 history 末轮摘要当 report 喂 analyze
        md = training_analyzer.analyze_round(
            last_report, loop["current_cfg"], loop["history"])
        training_loops_db.update_loop(loop_id, status="AWAITING_REVIEW",
                                      pending_review=md)
        header = (f"## 第{loop['current_round']}轮训练报告\n\n{md}\n\n---\n"
                  f"请回复你的审核（如「min_rr 改2.0 重跑」/「停」/「重置」）。")
        self._notifier.push(loop_id, header)

    def _handle_analyzing(self, loop: dict) -> None:
        """ANALYZING → analyze_round → AWAITING_REVIEW（推报告）。

        正常路径下 ANALYZING 由 _handle_running 成功链路内联跑完，不会单独被 _step_once
        命中；但 daemon 重启或异常中断可能留下 ANALYZING 悬挂态，故保留独立分支兜底推进。
        """
        self._analyze_and_await(loop["loop_id"])

    def _handle_awaiting_review(self, loop: dict) -> None:
        """AWAITING_REVIEW → 阻塞等 submit_review → CONFIRMING（parse+回显+等确认）。

        阻塞模型：per-loop threading.Event.wait(timeout=_POLL_INTERVAL) 循环——
        超时醒来只为查一次 DB 状态（你 stop 了就把 loop 标 STOPPED，这里识别后退出），
        没停就继续 wait。submit_review 一旦 set event，wait 立即返回 True，取 text 进 _confirm。
        """
        loop_id = loop["loop_id"]
        ev = self._review_events.get(loop_id)
        if ev is None:
            # 兜底：重启后内存 event 丢了但 DB 还在 AWAITING_REVIEW——重建一个，流程能续。
            ev = {"event": threading.Event(), "text": None}
            self._review_events[loop_id] = ev
        # 阻塞等审核（带周期 stop 检查；超时由 AWAITING_REVIEW 自身处理，见 §9）
        while not self._stop_flag.is_set():
            if ev["event"].wait(timeout=_POLL_INTERVAL):
                break
            # 超时醒来查 DB：你 stop 了（stop() 已置 STOPPED）→ 解除阻塞退出
            cur = training_loops_db.get_loop(loop_id)
            if cur and cur["status"] == "STOPPED":
                return
        text = ev["text"]
        ev["event"].clear()
        ev["text"] = None
        if text == "__STOP__":
            return   # stop() 已置 STOPPED
        self._confirm(loop_id, text)

    def _confirm(self, loop_id: str, text: str) -> None:
        """CONFIRMING：parse_review + 回显 + 等确认 → 下一轮/STOPPED/重等。

        两段式确认的物理意图：GLM parse 可能误解你的中文（"min_rr 提一点" → 改错字段），
        故先回显草稿（动作+改动+下轮 cfg）让你过目，你回「确认」才落库，回「不对」回
        AWAITING_REVIEW 重等。防止 parse 幻觉直接污染训练状态。
        """
        loop = training_loops_db.get_loop(loop_id)
        try:
            parsed = training_analyzer.parse_review(text, loop["current_cfg"])
        except training_analyzer.ParseError as e:
            # 解析失败/非法 → 回显报错，回 AWAITING_REVIEW 重等（不污染状态）
            training_loops_db.update_loop(loop_id, status="AWAITING_REVIEW")
            self._notifier.push(loop_id, f"❌ 没听懂：{e}\n请重新说明审核意图。")
            return
        # 回显草稿（推钉钉，等你回「确认」）
        draft = self._render_confirm(loop, parsed)
        training_loops_db.update_loop(loop_id, status="CONFIRMING", pending_review=draft)
        self._notifier.push(loop_id, draft)

        # 等你确认/否认（同 _handle_awaiting_review 的 wait 模型）
        ev = self._review_events[loop_id]
        while not self._stop_flag.is_set():
            if ev["event"].wait(timeout=_POLL_INTERVAL):
                break
            cur = training_loops_db.get_loop(loop_id)
            if cur and cur["status"] == "STOPPED":
                return
        confirm_text = (ev["text"] or "").strip()
        ev["event"].clear()
        ev["text"] = None
        if confirm_text == "__STOP__":
            return
        if "不" in confirm_text or "重新" in confirm_text:
            # 你说「不对/重新说」→ 回 AWAITING_REVIEW 重等审核
            training_loops_db.update_loop(loop_id, status="AWAITING_REVIEW")
            self._notifier.push(loop_id, "好的，请重新说明你的审核意图。")
            return
        # 确认 → 应用 action（rerun 累积 cfg / reset 回基准 / stop 终止）
        self._apply_confirmed(loop_id, parsed)

    def _apply_confirmed(self, loop_id: str, parsed: dict) -> None:
        """确认通过后落库下一轮状态。

        cfg 累积语义：rerun 时 new_cfg = current_cfg ∪ cfg_override（叠加，不清空之前轮的
        调整）——多轮渐进调参的物理意图。reset 则 current_cfg ← base_cfg（清空所有累积）。
        到达 max_rounds → STOPPED（防止无限训练）。
        """
        loop = training_loops_db.get_loop(loop_id)
        action = parsed["action"]
        round_n = loop["current_round"]
        if action == "reset":
            new_cfg = loop["base_cfg"]
        elif action == "stop":
            training_loops_db.update_loop(loop_id, status="STOPPED", finished_at=_now_iso())
            self._notifier.push(loop_id, f"🛑 训练已停止（共 {round_n} 轮）。")
            return
        else:   # rerun
            new_cfg = {**loop["current_cfg"], **parsed["cfg_override"]}
        if round_n >= loop["max_rounds"]:
            training_loops_db.update_loop(loop_id, status="STOPPED", finished_at=_now_iso())
            self._notifier.push(loop_id, f"✅ 已达 max_rounds={loop['max_rounds']}，训练结束（共 {round_n} 轮）。")
            return
        training_loops_db.update_loop(loop_id, status="RUNNING", current_cfg=new_cfg,
                                      pending_review=None)

    # ---- 辅助 ----
    def _render_confirm(self, loop: dict, parsed: dict) -> str:
        """回显草稿：上轮 cfg → 本轮改动 → 本轮完整 cfg + 动作。

        为什么显式回显下轮完整 cfg：让你一眼看到"确认后下一轮到底跑什么参数"，
        不必心算 current_cfg ∪ override——降低误确认概率。
        """
        action_zh = {"rerun": "改参重跑", "stop": "停止", "reset": "重置回基准"}[parsed["action"]]
        changes = "\n".join(f"- {k}: {loop['current_cfg'].get(k)} → {v}"
                            for k, v in parsed["cfg_override"].items()) or "- （无改动）"
        new_cfg = ({**loop["current_cfg"], **parsed["cfg_override"]}
                   if parsed["action"] == "rerun" else loop["base_cfg"])
        return (f"## 请确认第{loop['current_round']+1}轮\n\n"
                f"**动作**：{action_zh}\n\n**改动**：\n{changes}\n\n"
                f"**下轮完整 cfg**：\n```\n{json.dumps(new_cfg, ensure_ascii=False)}\n```\n\n"
                f"回复「确认」执行，或「不对」重新说明。")

    def _last_report_summary(self, loop: dict) -> dict:
        """把 history 末轮摘要还原成近似 report dict 喂 analyze_round。

        为什么需要还原：append_history 只存 6 字段摘要（控量），analyze_round 期望
        report dict 形态——这里反向拼回近似结构（pattern_dist 补空），让 analyze_round
        无需感知 history 摘要格式。完整 trades 不还原（GLM 趋势分析用不到）。
        """
        if not loop["history"]:
            return {}
        h = loop["history"][-1]
        return {"n_hits": h.get("n_hits"), "win_rate": h.get("win_rate"),
                "avg_rr": h.get("avg_rr"), "max_drawdown": h.get("max_dd"),
                "annualized_return": h.get("annualized"), "pattern_dist": {}}

    def _wake(self, loop_id: str, text: str) -> None:
        """submit_review/stop 共用的唤醒原语：存 text + set event。"""
        ev = self._review_events.get(loop_id)
        if ev is not None:
            ev["text"] = text
            ev["event"].set()

# -*- coding: utf-8 -*-
"""__main__ CLI 单测（trading 主流程）：dry-run / 幂等去重 / --force / 推送失败 / 无日期兜底。

market 下线后，默认 bot=trading。trading 分支取数走 _fetch_trading_snapshot（重 import），
测试 mock 它 + build_trading_brief，避免依赖真实 trading_service。
幂等文件隔离：monkeypatch last_brief_file 返回 tmp_path，不碰真实 logs/。
"""
import broadcast.__main__ as bm
from broadcast.brief import BriefResult


def _stub_trading(monkeypatch, date="2026-07-15"):
    """桩：reader/date/取数/brief 渲染，让 main 主流程不依赖真实 IO。"""
    monkeypatch.setattr(bm, "_load_reader", lambda: "fake_reader")
    monkeypatch.setattr(bm, "_latest_trade_date", lambda r: date)
    # mock trading 取数四件套（避免 import trading_service 重链路 + 真实网关）
    monkeypatch.setattr(bm, "_fetch_trading_snapshot",
                        lambda d: ([], None, None, {"mode": "live"}))
    # mock brief 渲染（主流程测幂等/push/last，不应依赖 brief 真渲染）
    monkeypatch.setattr(
        bm, "build_trading_brief",
        lambda *a, **k: BriefResult(date=date, markdown="### 每日交易播报\n样例正文"),
    )


def _isolate_last_file(monkeypatch, tmp_path, date="2026-07-15"):
    """把 trading 幂等文件重定向到 tmp_path（不碰真实 logs/.last_trading_brief）。"""
    f = tmp_path / ".last_trading_brief"
    monkeypatch.setattr(bm, "last_brief_file", lambda bot: f)
    return f


def test_main_dry_run_prints_and_pushes_dry(monkeypatch):
    _stub_trading(monkeypatch)
    pushed = []
    monkeypatch.setattr(bm, "push_brief", lambda *a, **k: pushed.append((a, k)) or True)
    rc = bm.main(["--dry-run"])
    assert rc == 0
    assert pushed and pushed[0][1].get("dry_run") is True
    assert "每日交易播报" in pushed[0][0][1]


def test_main_dedup_skips_when_already_broadcast(monkeypatch, tmp_path):
    _stub_trading(monkeypatch, date="2026-07-15")
    f = _isolate_last_file(monkeypatch, tmp_path)
    f.write_text("2026-07-15", encoding="utf-8")
    pushed = []
    monkeypatch.setattr(bm, "push_brief", lambda *a, **k: pushed.append(1) or True)
    rc = bm.main([])
    assert rc == 0
    assert pushed == []                      # 今日已播 → 跳过


def test_main_force_overrides_dedup(monkeypatch, tmp_path):
    _stub_trading(monkeypatch, date="2026-07-15")
    f = _isolate_last_file(monkeypatch, tmp_path)
    f.write_text("2026-07-15", encoding="utf-8")
    pushed = []
    monkeypatch.setattr(bm, "push_brief", lambda *a, **k: pushed.append(1) or True)
    rc = bm.main(["--force"])
    assert rc == 0
    assert pushed == [1]                     # --force 覆盖去重
    assert f.read_text(encoding="utf-8") == "2026-07-15"


def test_main_success_writes_last(monkeypatch, tmp_path):
    _stub_trading(monkeypatch, date="2026-07-15")
    f = _isolate_last_file(monkeypatch, tmp_path)
    monkeypatch.setattr(bm, "push_brief", lambda *a, **k: True)
    rc = bm.main([])
    assert rc == 0
    assert f.read_text(encoding="utf-8") == "2026-07-15"


def test_main_push_failure_no_last(monkeypatch, tmp_path):
    _stub_trading(monkeypatch, date="2026-07-15")
    f = _isolate_last_file(monkeypatch, tmp_path)
    monkeypatch.setattr(bm, "push_brief", lambda *a, **k: False)
    rc = bm.main([])
    assert rc == 2                           # 推送失败
    assert not f.exists()                    # 失败不写 last（下次重试）


def test_main_no_date_returns_1(monkeypatch):
    _stub_trading(monkeypatch, date=None)
    assert bm.main([]) == 1

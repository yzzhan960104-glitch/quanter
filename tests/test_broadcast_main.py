# -*- coding: utf-8 -*-
"""__main__ CLI 单测：dry-run / 幂等去重 / --force 覆盖 / 无日期兜底。"""
import broadcast.__main__ as bm
from broadcast.brief import BriefResult


def _stub_reader_funcs(monkeypatch, date="2026-07-15"):
    monkeypatch.setattr(bm, "_load_reader", lambda: "fake_reader")
    monkeypatch.setattr(bm, "_latest_trade_date", lambda r: date)
    # mock build_daily_brief：main 流程测试（去重/push/last_broadcast）不应依赖 brief 真访问 reader
    monkeypatch.setattr(
        bm, "build_daily_brief",
        lambda *a, **k: BriefResult(date=date, markdown="### 每日行情播报\n样例正文"),
    )


def test_main_dry_run_prints_and_pushes_dry(monkeypatch):
    _stub_reader_funcs(monkeypatch)
    pushed = []
    monkeypatch.setattr(bm, "push_brief", lambda *a, **k: pushed.append((a, k)) or True)
    rc = bm.main(["--dry-run"])
    assert rc == 0
    assert pushed and pushed[0][1].get("dry_run") is True
    # push 被 mock 不真 print，改验入参 markdown 含标题
    assert "每日行情播报" in pushed[0][0][1]


def test_main_dedup_skips_when_already_broadcast(monkeypatch, tmp_path):
    _stub_reader_funcs(monkeypatch, date="2026-07-15")
    monkeypatch.setattr(bm, "LAST_BC_FILE", tmp_path / ".last_broadcast")
    (tmp_path / ".last_broadcast").write_text("2026-07-15", encoding="utf-8")
    pushed = []
    monkeypatch.setattr(bm, "push_brief", lambda *a, **k: pushed.append(1) or True)
    rc = bm.main([])
    assert rc == 0
    assert pushed == []                      # 今日已播 → 跳过，不推


def test_main_force_overrides_dedup(monkeypatch, tmp_path):
    _stub_reader_funcs(monkeypatch, date="2026-07-15")
    f = tmp_path / ".last_broadcast"
    monkeypatch.setattr(bm, "LAST_BC_FILE", f)
    f.write_text("2026-07-15", encoding="utf-8")
    pushed = []
    monkeypatch.setattr(bm, "push_brief", lambda *a, **k: pushed.append(1) or True)
    rc = bm.main(["--force"])
    assert rc == 0
    assert pushed == [1]                     # --force 覆盖去重
    assert f.read_text(encoding="utf-8") == "2026-07-15"  # 成功后重写 last_broadcast


def test_main_success_writes_last_broadcast(monkeypatch, tmp_path):
    _stub_reader_funcs(monkeypatch, date="2026-07-15")
    f = tmp_path / ".last_broadcast"
    monkeypatch.setattr(bm, "LAST_BC_FILE", f)
    monkeypatch.setattr(bm, "push_brief", lambda *a, **k: True)
    rc = bm.main([])
    assert rc == 0
    assert f.read_text(encoding="utf-8") == "2026-07-15"


def test_main_push_failure_no_last_broadcast(monkeypatch, tmp_path):
    _stub_reader_funcs(monkeypatch, date="2026-07-15")
    f = tmp_path / ".last_broadcast"
    monkeypatch.setattr(bm, "LAST_BC_FILE", f)
    monkeypatch.setattr(bm, "push_brief", lambda *a, **k: False)
    rc = bm.main([])
    assert rc == 2                           # 推送失败
    assert not f.exists()                    # 失败不写 last_broadcast（下次重试）


def test_main_no_date_returns_1(monkeypatch):
    _stub_reader_funcs(monkeypatch, date=None)
    assert bm.main([]) == 1

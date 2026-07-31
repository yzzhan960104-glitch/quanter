import pytest
from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
async def test_run_brief_all_calls_three_bots():
    from ops.brief_all import run_brief_all, BOTS
    with patch("ops.brief_all.asyncio.create_subprocess_exec") as cse:
        proc = AsyncMock(); proc.wait.return_value = 0
        cse.return_value = proc
        rc = await run_brief_all()
        assert cse.call_count == len(BOTS)  # 三个 bot 各起一个子进程
        assert rc == 0


@pytest.mark.asyncio
async def test_run_brief_all_one_fail_returns_nonzero():
    from ops.brief_all import run_brief_all
    with patch("ops.brief_all.asyncio.create_subprocess_exec") as cse:
        procs = []
        for i, _ in enumerate(["trading", "strategy", "data"]):
            p = AsyncMock(); p.wait.return_value = 1 if i == 1 else 0
            procs.append(p)
        cse.side_effect = procs
        rc = await run_brief_all()
        assert rc == 1  # strategy 失败 → 非 0

"""迁移回归:data/tools/sync_*.py 的 sys.path 必须让 import config 可达。

背景:commit d5f396cc 把 11 个 sync_*.py 从 scripts/ 迁到 data/tools/,
但 sys.path.insert 的 dirname 深度没跟上(2 层 dirname → E:\\quanter\\data,
无 config/ → ModuleNotFoundError)。本测试复算每个脚本自身的 sys.path 逻辑
(3 层 dirname → E:\\quanter)后真跑 import config,锁死迁移正确性。
"""
import subprocess
import sys
from pathlib import Path

# 受影响 10 文件(迁移遗留 2 层 dirname,需为 3 层;sync_rate_data.py 已随旧 akshare
# 湖退役删除——shibor 走 Tushare 管线,lpr/cn_m 不再采集)
SYNC_SCRIPTS = [
    "data/tools/sync_tushare.py",
    "data/tools/scan_integrity.py",
    "data/tools/sync_data_lake.py",
    "data/tools/probe_tushare_fields.py",
    "data/tools/repair_gaps.py",
    "data/tools/sync_macro_credit.py",
    "data/tools/sync_incremental.py",
    "data/tools/probe_snapshot_fields.py",
    "data/tools/probe_rate_fields.py",
    "data/tools/sync_all_tushare.py",
]


def test_each_script_syspath_reaches_config():
    """迁移回归:每个 data/tools/sync_*.py 的 sys.path 须让 import config 可达。

    不真跑探测/同步逻辑(避免烧 Tushare 配额/连真 API),只用 subprocess 复算
    脚本自己 3 层 dirname 的 sys.path 后真 import config。
    """
    root = Path(__file__).resolve().parent.parent
    for rel in SYNC_SCRIPTS:
        script = root / rel
        assert script.exists(), f"{rel} 不存在"
        # 静态校验:脚本里 sys.path.insert 必须已是 3 层 dirname(迁移深度必须跟上)
        src = script.read_text(encoding="utf-8")
        assert "os.path.dirname(os.path.dirname(os.path.dirname" in src, (
            f"{rel} sys.path.insert 仍是 2 层 dirname(迁移遗留),应改 3 层"
        )
        # 动态校验:复算脚本自己的 sys.path 逻辑(3 层 dirname)后真 import config
        code = (
            "import sys, os; "
            f"__file__ = r'{script}'; "
            "sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))); "
            "import config; print('OK')"
        )
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert r.returncode == 0 and "OK" in r.stdout, (
            f"{rel} import config 失败: returncode={r.returncode} stderr={r.stderr}"
        )


def test_sync_daily_incremental_unchanged_correct():
    """sync_daily_incremental.py 已是 3 层 dirname(正确范式),本 task 不动它——回归保护。"""
    root = Path(__file__).resolve().parent.parent
    src = (root / "data/tools/sync_daily_incremental.py").read_text(encoding="utf-8")
    # 已是 3 层(正确),保持不变
    assert src.count("os.path.dirname(os.path.dirname(os.path.dirname") >= 1

# -*- coding: utf-8 -*-
"""全栈护栏一键编排（CI + 本地同源入口）。

物理定位（用户诉求「让护栏自动跑，而非手跑」的统一入口）：
    把分散的 4 项 fast gate 串成一条命令，CI（GitHub Actions）与本地
    `python scripts/run_checks.py` 跑的是同一份代码——杜绝「本地过了 CI 挂」的漂移。

fast gate 清单（秒~分钟级，push 必跑）：
    ① 端口一致性 check_ports.py     —— vite proxy 与后端 API_PORT 静态比对（防 ECONNREFUSED）
    ② 前后端契约 check_contracts.py —— openapi 端点集 ↔ api/*.ts 调用集 比对（防 404/契约漂移）
    ③ 后端单测 pytest               —— tests/ 全量（含 check_ports/check_contracts/auth/caisen...）
    ④ 前端类型检查 vue-tsc          —— web 类型守门（防 TS 类型回归）

不在 fast gate（慢、需服务编排，文档另述手动跑法）：
    - E2E（Playwright）：tests/e2e/caisen_token_path.py + with_server.py，本地按需手跑。

设计（反黑盒 / 跨平台）：
    - 纯标准库，零新依赖；sys.executable 跑 Python 类检查（规避「多解释器错位」——曾踩 playwright
      装在 .venv310 但 subprocess python 落到系统解释器的坑）；npm 类用 shell=True（Windows npm.cmd）。
    - 任一 gate 失败 → 总 exit 1（CI 阻断）；逐项中文报告便于定位。
    - stdout reconfigure utf-8：防 Windows GBK 终端崩中文/符号（E2E 曾因此 UnicodeEncodeError）。
"""
import subprocess
import sys

# 防 Windows GBK 终端编码崩（中文报告 + 部分符号）；非 Windows 无副作用。
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


# (名称, 命令, 是否 shell)。shell=False 用 list（python 类，sys.executable 精确控解释器）；
# shell=True 用字符串（npm 类，让系统 shell 解析 npm/npm.cmd）。
CHECKS = [
    ("① 端口一致性 check_ports", [sys.executable, "scripts/check_ports.py"], False),
    ("② 前后端契约 check_contracts", [sys.executable, "scripts/check_contracts.py"], False),
    ("③ 后端单测 pytest", [sys.executable, "-m", "pytest", "tests", "-q", "--tb=short"], False),
    ("④ 前端类型检查 vue-tsc", "npm --prefix web run typecheck", True),
    ("⑤ 前端组件/单测 vitest", "npm --prefix web run test", True),
]


def _run_one(name: str, cmd, shell: bool) -> int:
    """跑单个 gate，实时透传子进程输出，返回其 exit code。"""
    display = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
    print(f"\n{'=' * 64}\n▶ {name}\n  $ {display}\n{'=' * 64}", flush=True)
    rc = subprocess.run(cmd, shell=shell).returncode
    tag = "PASS" if rc == 0 else f"FAIL (exit {rc})"
    print(f"\n  → {name}: {tag}", flush=True)
    return rc


def main() -> int:
    print("全栈护栏编排（fast gate）：端口 / 契约 / 单测 / 类型", flush=True)
    results = [(name, _run_one(name, cmd, shell)) for name, cmd, shell in CHECKS]

    # 汇总
    print(f"\n{'=' * 64}\n汇总：", flush=True)
    for name, rc in results:
        mark = "PASS" if rc == 0 else "FAIL"
        print(f"  [{mark}] {name}", flush=True)

    failed = [name for name, rc in results if rc != 0]
    if failed:
        print(f"\n✗ {len(failed)} 项失败：{', '.join(failed)}", flush=True)
        print("  修复对应 gate 后重跑 python scripts/run_checks.py", flush=True)
        return 1
    print("\n全部通过，护栏放行。", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

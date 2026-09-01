# -*- coding: utf-8 -*-
"""公网发布（yzzhan.xin · Cloudflare Pages Direct Upload）。

链路（三步串行，任一步失败即止——不发布半新半旧的混合态）：
  ① ops.public_snapshot.build_snapshot（脱敏 JSON → web/public/data/）
  ② npm run build:public（vue-tsc + vite --mode public → dist/）
  ③ wrangler pages deploy dist（Direct Upload——不经 git：主仓库零改动，
     发布历史只存在 CF 账号侧，公众仅见当前版，无 git 历史持仓泄漏面）

首次使用（一次性）：
  - npm install（wrangler 已在 web devDependencies）
  - npx wrangler login（浏览器 OAuth 一次点击——本脚本会前置检测并提示）
  - 项目创建：脚本自动尝试 wrangler pages project create（已存在则跳过）

自定义域 yzzhan.xin：首次部署后到 CF 面板 Pages → 项目 → Custom domains
一键绑定（DNS 已在 CF，自动配 CNAME+SSL）；或 wrangler 已登录后走 API。

平移预案（α→β 国内云）：本脚本①②不变，③换 ossutil 上传——前端零改动。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "presentation" / "web"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

# CF 凭证走 .env（gitignored）：CLOUDFLARE_API_TOKEN（自定义 token=Pages Edit +
# yzzhan.xin Zone/DNS Edit，2026-09-01 创建）+ CLOUDFLARE_ACCOUNT_ID（token 未含
# User Details Read，whoami 拿不到账户列表——显式给 ID 绕开，wrangler 即可无头跑）。
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

PROJECT = "yzzhan-view"          # CF Pages 项目名（自定义域绑在它上面）
PROD_BRANCH = "main"


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 600) -> int:
    """跑子进程，UTF-8 直通输出；返回码非零上抛终止发布链。

    Why shutil.which：Windows 下 npm/npx 是 .cmd 垫片，CreateProcess 不做 PATH
    后缀解析（WinError 2）——run_checks.py 用 shell=True 解决同型问题，此处用
    which 显式解析（参数带路径时免引号地狱）。
    """
    import shutil
    exe = shutil.which(cmd[0]) or cmd[0]
    print(f"$ {' '.join(cmd)}" + (f"  (cwd={cwd})" if cwd else ""), flush=True)
    rc = subprocess.run([exe, *cmd[1:]], cwd=str(cwd) if cwd else None,
                        timeout=timeout).returncode
    if rc != 0:
        raise SystemExit(f"步骤失败 rc={rc}：{' '.join(cmd)}")
    return rc


def _wrangler(*args: str, timeout: int = 600) -> subprocess.CompletedProcess:
    import shutil
    exe = shutil.which("npx") or "npx"
    return subprocess.run([exe, "--no-install", "wrangler", *args],
                          cwd=str(WEB), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="公网发布 yzzhan.xin（快照→构建→CF Pages）")
    p.add_argument("--snapshot-only", action="store_true", help="只出快照不构建不发布")
    p.add_argument("--build-only", action="store_true", help="快照+构建，不发布")
    args = p.parse_args(argv)

    print("=== ① 快照（脱敏 JSON） ===")
    from ops.public_snapshot import build_snapshot
    summary = build_snapshot()
    print(f"[①ok] {summary['files']} 个文件 @ {summary['generated_at']}")
    if args.snapshot_only:
        return 0

    print("=== ② 公开构建（vue-tsc + vite --mode public） ===")
    _run(["npm", "--prefix", str(WEB), "run", "build:public"], timeout=600)
    if args.build_only:
        return 0

    print("=== ③ Cloudflare Pages Direct Upload ===")
    import os
    if not os.getenv("CLOUDFLARE_API_TOKEN"):
        print("\n[缺凭证] .env 未配置 CLOUDFLARE_API_TOKEN（CF 面板 → API Tokens → "
              "自定义 token：Pages Edit + Zone/DNS Edit on yzzhan.xin）\n")
        return 2
    chk = _wrangler("whoami")
    if "not authenticated" in (chk.stdout + chk.stderr).lower():
        print("\n[需要你一次点击] wrangler 未登录。请在 presentation/web 目录运行：\n"
              "    npx wrangler login\n"
              "浏览器会打开 Cloudflare 授权页 → 点 Allow → 回来重跑本脚本。\n")
        return 2
    # 项目不存在则创建（已存在时 create 报错——吞掉继续 deploy）
    create = _wrangler("pages", "project", "create", PROJECT,
                       "--production-branch", PROD_BRANCH)
    if create.returncode == 0:
        print(f"[③ok] 项目 {PROJECT} 已创建")
    else:
        print(f"[③] 项目已存在或创建跳过（{ (create.stderr or create.stdout).strip()[:80] }）")
    _run(["npx", "--no-install", "wrangler", "pages", "deploy", "dist",
          "--project-name", PROJECT, "--branch", PROD_BRANCH], cwd=WEB, timeout=900)
    print(f"\n[done] 已发布 → https://{PROJECT}.pages.dev （自定义域 yzzhan.xin "
          "首次绑定：CF 面板 → Pages → {PROJECT} → Custom domains → Set up a domain）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

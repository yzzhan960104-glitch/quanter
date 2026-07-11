# -*- coding: utf-8 -*-
"""前后端契约一致性护栏（preflight 脚本）。

Why 存在：前后端契约此前仅靠 web/src/api/*.ts 头注释人工对齐，端点路径/方法/参数名
漂移只能靠运行时 404/422 暴露（鉴权 token 缺口即此类潜伏问题的典型）。本脚本把
FastAPI 的权威 /openapi.json（后端真相源）与前端 api/*.ts 的 apiClient.<method>('<path>')
调用做静态比对，前端调用了后端不存在的端点即 sys.exit(1) 阻断，与 check_ports.py 同为
「源码静态比对护栏 + 单测」家族（check_ports 比端口，本脚本比契约）。

设计（反黑盒 / 极简，与 check_ports.py 同哲学）：
- 纯函数 parse_openapi_endpoints / parse_ts_calls / _norm_path + main(backend_spec, ts_files)，
  CLI 仅薄封装，单测喂假 openapi dict + tmp_path 造假 ts，不依赖 subprocess；
- 刻意不在单测路径 import server.main（拉 fastapi/uvicorn/celery 重依赖）；CLI 入口
  才进程内 import server.main:app 取权威 openapi，故挂在后端 CI / make verify-contracts，
  不挂前端 predev（前端开发机可能无后端依赖，与 check_ports.py 前端轻量诉求互补）。

参数归一红线：前端 TS 写 /plans/${planId}（模板字符串），后端 openapi 写 /plans/{plan_id}，
两者参数名不同但语义同一占位 → _norm_path 统一为 /plans/{} 再比对，避免参数名差异误报漂移。
"""
import re
import sys
from pathlib import Path
from typing import Iterable, Set, Tuple

# 项目根锚定：scripts/check_contracts.py → scripts/ → 项目根（与运行 cwd 无关）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_API_DIR = PROJECT_ROOT / "web" / "src" / "api"

# 前端调用提取：apiClient.<method>('<path>') / apiClient.<method>(`<path>`)。
# method ∈ get/post/put/patch/delete；path 用单/双/反引号包裹；反引号内 ${...} 为模板参数。
# `[^'\"`]+` 吃掉 path 字面量（含 ${...}/斜杠/字母数字），引号闭合即止。
_TS_CALL_RE = re.compile(
    r"apiClient\.(get|post|put|patch|delete)\(\s*['\"`]([^'\"`]+)['\"`]"
)

# openapi 规范：paths.<path> 下的 HTTP method 键用小写；非 method 键（parameters/summary 等）须排除。
_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


def _norm_path(path: str) -> str:
    """路径参数占位归一：前端 ${x} 与后端 {y} 统一为 {}。

    两步替换：先把前端模板参数 ${...} 转为 {}，再把 openapi 参数名 {plan_id} 转为 {}。
    第二步的 `{[^}]+}` 要求至少 1 个非}字符，故不会把第一步生成的空 {} 再消费（保持占位）。
    """
    path = re.sub(r"\$\{[^}]+\}", "{}", path)   # 前端 TS 模板参数 ${planId} → {}
    path = re.sub(r"\{[^}]+\}", "{}", path)      # openapi 参数名 {plan_id} → {}（含已归一的前端保持不变）
    return path


def parse_ts_calls(text: str) -> Set[Tuple[str, str]]:
    """从 TS 源码文本提取 apiClient.<method>('<path>') 调用集。

    返回 {(METHOD_UPPER, path_norm)}，path 已经 _norm_path 归一；无调用返空集（纯类型文件不误报）。
    """
    calls: Set[Tuple[str, str]] = set()
    for m in _TS_CALL_RE.finditer(text):
        method = m.group(1).upper()
        calls.add((method, _norm_path(m.group(2))))
    return calls


def parse_openapi_endpoints(spec: dict) -> Set[Tuple[str, str]]:
    """从 openapi dict 提取 (METHOD_UPPER, path_norm) 端点集。

    仅取 spec["paths"][<path>] 下的 HTTP method 键（忽略 parameters/summary 等非 method 键）；
    path 经 _norm_path 归一。spec 缺 paths 或 paths 空 → 返空集（main 据此判解析失败 exit 2）。
    """
    endpoints: Set[Tuple[str, str]] = set()
    paths = spec.get("paths") or {}
    for path, ops in paths.items():
        if not isinstance(ops, dict):
            continue
        for key in ops:
            if key.lower() in _HTTP_METHODS:
                endpoints.add((key.upper(), _norm_path(path)))
    return endpoints


def main(backend_spec: dict, ts_files: Iterable[Path]) -> int:
    """比对后端 openapi 端点集与前端 ts 调用集，返回 exit code。

    真相源：backend_spec（openapi dict，CLI 入口进程内取自 server.main:app.openapi()）。
    返回码：
      0 —— 前端所有调用都在后端端点集内（一致，静默放行）
      1 —— 漂移：存在「前端调用但后端无」的端点，stderr 中文逐条列出
      2 —— 解析失败：openapi 无 paths（后端异常）/ ts 文件读不到（与漂移区分，便于定位）
    """
    backend = parse_openapi_endpoints(backend_spec)
    if not backend:
        # openapi 无 paths → 后端路由未挂载 / import 失败 / spec 异常，与契约漂移明确区分
        print(
            "[契约护栏] 后端 openapi 无 paths，无法提取端点集（后端异常或路由未挂载）。",
            file=sys.stderr,
        )
        return 2

    # 合并所有 api/*.ts 的前端调用（多 facade 场景，前端 6 个文件合并比对）
    frontend: Set[Tuple[str, str]] = set()
    missing_files = []
    for f in ts_files:
        try:
            frontend |= parse_ts_calls(Path(f).read_text(encoding="utf-8"))
        except OSError as e:
            missing_files.append(f"{f} ({e})")
    if missing_files:
        print(f"[契约护栏] 无法读取前端 api 文件：{missing_files}", file=sys.stderr)
        return 2

    drift = frontend - backend
    if drift:
        # 漂移：前端调用了后端 openapi 不存在的端点，逐条列出便于定位
        lines = "\n".join(f"    {m} {p}" for m, p in sorted(drift))
        print(
            f"[契约护栏] 发现 {len(drift)} 处前后端契约漂移（前端调用但后端 openapi 无此端点）：\n"
            f"{lines}\n"
            f"  修复：核对 web/src/api/*.ts 的请求 URL/method 与 server/api/v1/*.py 路由装饰器，"
            f"使端点路径与方法对齐（注意路径参数名差异不影响，护栏已归一比对）。",
            file=sys.stderr,
        )
        return 1

    return 0


def _load_backend_spec_from_app() -> dict:
    """CLI 入口专用：进程内 import server.main:app，取权威 openapi dict。

    Why 进程内而非 HTTP 拉 /openapi.json：不依赖起 uvicorn、不占端口、CI 友好；
    代价是拉 fastapi/uvicorn 等重依赖，故仅 CLI 调用（单测喂 spec dict 绕开）。

    sys.path 注入：`python scripts/check_contracts.py` 时 sys.path[0]=scripts/，不含项目根
    → 必须显式加项目根才能 import server.main（与 server/core/config.py 的 PROJECT_ROOT
    sys.path 注入同款；此处延迟到 CLI 调用才加，避免污染单测路径）。
    """
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from server.main import app  # noqa: WPS433（延迟 import：隔离重依赖，仅 CLI 需要）
    return app.openapi()


if __name__ == "__main__":
    # CLI 入口：进程内取后端 openapi，glob 前端 api/*.ts，比对。
    ts_files = sorted(DEFAULT_API_DIR.glob("*.ts"))
    sys.exit(main(_load_backend_spec_from_app(), ts_files))

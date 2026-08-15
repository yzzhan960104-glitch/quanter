# -*- coding: utf-8 -*-
"""前后端契约一致性护栏（preflight 脚本）。

Why 存在：前后端契约此前仅靠 web/src/api/*.ts 头注释人工对齐，端点路径/方法/参数名
漂移只能靠运行时 404/422 暴露（鉴权 token 缺口即此类潜伏问题的典型）。本脚本把
FastAPI 的权威 /openapi.json（后端真相源）与前端 api/*.ts 的 apiClient.<method>('<path>')
调用做静态比对，前端调用了后端不存在的端点即 sys.exit(1) 阻断，与 check_ports.py 同为
「源码静态比对护栏 + 单测」家族（check_ports 比端口，本脚本比契约）。

第二道守卫 check_no_double_unwrap（CR-1 形状契约，2026-08-15 技术债波次）：URL 对账只保证
「端点存在」，管不住「响应形状」——曾发生 facade 写 `const { data } = await apiClient.get(...)`
对 client.ts 已剥壳拦截器产物二次解构，data === undefined，discovery 页静默空态成 HTTP 200
死页（CR-1）。故对 api/*.ts 加静态正则守卫，命中即与契约漂移同档 exit 1 阻断。

设计（反黑盒 / 极简，与 check_ports.py 同哲学）：
- 纯函数 parse_openapi_endpoints / parse_ts_calls / _norm_path / check_no_double_unwrap
  + main(backend_spec, ts_files)，CLI 仅薄封装，单测喂假 openapi dict + tmp_path 造假 ts，
  不依赖 subprocess；
- 刻意不在单测路径 import presentation.server.main（拉 fastapi/uvicorn/celery 重依赖）；CLI 入口
  才进程内 import presentation.server.main:app 取权威 openapi，故挂在后端 CI / make verify-contracts，
  不挂前端 predev（前端开发机可能无后端依赖，与 check_ports.py 前端轻量诉求互补）。

参数归一红线：前端 TS 写 /plans/${planId}（模板字符串），后端 openapi 写 /plans/{plan_id}，
两者参数名不同但语义同一占位 → _norm_path 统一为 /plans/{} 再比对，避免参数名差异误报漂移。

守卫扫描范围红线：check_no_double_unwrap 只吃 main/CLI 传入的 presentation/web/src/api/*.ts
（DEFAULT_API_DIR.glob("*.ts")），绝不扫组件/视图目录——组件层对本地对象解构 { data } 是
合法写法，扩面必误伤。
"""
import re
import sys
from pathlib import Path
from typing import Iterable, Set, Tuple

# 项目根锚定：ops/check_contracts.py → scripts/ → 项目根（与运行 cwd 无关）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_API_DIR = PROJECT_ROOT / "presentation" / "web" / "src" / "api"

# 前端调用提取：apiClient.<method>('<path>') / apiClient.<method>(`<path>`)。
# method ∈ get/post/put/patch/delete；path 用单/双/反引号包裹；反引号内 ${...} 为模板参数。
# `[^'\"`]+` 吃掉 path 字面量（含 ${...}/斜杠/字母数字），引号闭合即止。
_TS_CALL_RE = re.compile(
    r"apiClient\.(get|post|put|patch|delete)\(\s*['\"`]([^'\"`]+)['\"`]"
)

# 二次解构死页模式（CR-1）：`const { data } = await apiClient.<method>(...)`。
# 根因：client.ts 响应拦截器 `(response) => response.data` 已剥掉 axios 包壳，
# apiClient.get 运行时直接 resolve 业务 payload 本身——再解构 { data } 得 undefined，
# 视图层静默空态（HTTP 200 死页），无任何报错可循，只能静态正则前置拦截。
# `\s*` 兼容 `{ data }`/`{data}` 等空格写法；方法名不限定（get/post 均可能中招）。
_DOUBLE_UNWRAP_RE = re.compile(r"const\s*\{\s*data\s*\}\s*=\s*await\s+apiClient\.")

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


def check_no_double_unwrap(ts_paths: Iterable[Path]) -> list[str]:
    """CR-1 形状契约守卫：扫 api/*.ts，揪出对已剥壳 apiClient 的二次解构。

    Why 存在：client.ts 响应拦截器已 `(response) => response.data` 剥壳，apiClient.get
    运行时直接 resolve 业务 payload；facade 若再写 `const { data } = await apiClient.get(...)`
    则 data === undefined，页面静默空态成 HTTP 200 死页（CR-1 discovery 实案）。URL 对账
    只能保证端点存在，管不住响应形状——本守卫补上这一层，命中即 main exit 1 阻断。

    扫描范围红线：只应吃 presentation/web/src/api/*.ts（main/CLI 的 DEFAULT_API_DIR glob），
    不扫组件目录——组件层对本地对象解构 { data } 是合法写法，扩面必误伤。

    Args:
        ts_paths: 待扫 ts 文件路径集（与 URL 对账同一批文件，main 已确保可读）。

    Returns:
        违规描述列表（"文件名:行号: 源码行"），空列表 = 干净放行；读取失败也入列
        （读不到文件无法证清白，宁可误阻断不可静默放过——fail-closed）。
    """
    violations: list = []
    for f in ts_paths:
        try:
            text = Path(f).read_text(encoding="utf-8")
        except OSError as e:
            violations.append(f"{Path(f).name}（读取失败：{e}）")
            continue
        # 逐行扫而非全文 finditer：违规项带行号，报错直达病灶，省去二次定位
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _DOUBLE_UNWRAP_RE.search(line):
                violations.append(f"{Path(f).name}:{lineno}: {line.strip()}")
    return violations


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
    """比对后端 openapi 端点集与前端 ts 调用集，并扫二次解构死页模式，返回 exit code。

    真相源：backend_spec（openapi dict，CLI 入口进程内取自 presentation.server.main:app.openapi()）。
    返回码：
      0 —— 前端所有调用都在后端端点集内，且无二次解构违规（一致，静默放行）
      1 —— 漂移（前端调用但后端无的端点）或二次解构（CR-1 死页模式）任一命中，
            stderr 中文逐条列出（两类同轮全量报告）
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

    # T17（T1 ①遗留）：物化 list——ts_files 在 main 内被消费两遍（URL 对账循环 +
    # check_no_double_unwrap），调用方若传生成器，第二遍遍历为空 → 形状守卫静默
    # 永远 0 命中（守卫失效而非误报，最隐蔽）。一行物化把双消费变成安全语义。
    ts_files = list(ts_files)

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

    # 两道守卫并列：① URL/method 对账（契约漂移）② 响应形状对账（二次解构死页）。
    # 同轮全量报告再统一判 exit——避免「修一处、再跑一轮才见下一处」的挤牙膏式定位。
    drift = frontend - backend
    unwrap_violations = check_no_double_unwrap(ts_files)
    if drift:
        # 漂移：前端调用了后端 openapi 不存在的端点，逐条列出便于定位
        lines = "\n".join(f"    {m} {p}" for m, p in sorted(drift))
        print(
            f"[契约护栏] 发现 {len(drift)} 处前后端契约漂移（前端调用但后端 openapi 无此端点）：\n"
            f"{lines}\n"
            f"  修复：核对 web/src/api/*.ts 的请求 URL/method 与 presentation/server/api/v1/*.py 路由装饰器，"
            f"使端点路径与方法对齐（注意路径参数名差异不影响，护栏已归一比对）。",
            file=sys.stderr,
        )
    if unwrap_violations:
        # 二次解构（CR-1 形状契约）：client.ts 拦截器已剥壳，二次解构必得 undefined 死页
        lines = "\n".join(f"    {v}" for v in unwrap_violations)
        print(
            f"[契约护栏] 发现 {len(unwrap_violations)} 处对已剥壳 apiClient 的二次解构（CR-1 死页根因）：\n"
            f"{lines}\n"
            f"  根因：client.ts 响应拦截器 `(response) => response.data` 已剥掉 axios 包壳，"
            f"apiClient.get 运行时直接 resolve 业务 payload，再 `const {{ data }} = ...` 解构得 undefined，"
            f"页面静默空态（HTTP 200 死页）。\n"
            f"  修复：参照 trading.ts 直返姿势 `return apiClient.get('<url>')` + 显式返回类型，去掉解构。",
            file=sys.stderr,
        )
    if drift or unwrap_violations:
        return 1

    return 0


def _load_backend_spec_from_app() -> dict:
    """CLI 入口专用：进程内 import presentation.server.main:app，取权威 openapi dict。

    Why 进程内而非 HTTP 拉 /openapi.json：不依赖起 uvicorn、不占端口、CI 友好；
    代价是拉 fastapi/uvicorn 等重依赖，故仅 CLI 调用（单测喂 spec dict 绕开）。

    sys.path 注入：`python ops/check_contracts.py` 时 sys.path[0]=scripts/，不含项目根
    → 必须显式加项目根才能 import presentation.server.main（与 server/http/config.py 的 PROJECT_ROOT
    sys.path 注入同款；此处延迟到 CLI 调用才加，避免污染单测路径）。
    """
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from presentation.server.main import app  # noqa: WPS433（延迟 import：隔离重依赖，仅 CLI 需要）
    return app.openapi()


if __name__ == "__main__":
    # CLI 入口：进程内取后端 openapi，glob 前端 api/*.ts，比对。
    ts_files = sorted(DEFAULT_API_DIR.glob("*.ts"))
    sys.exit(main(_load_backend_spec_from_app(), ts_files))

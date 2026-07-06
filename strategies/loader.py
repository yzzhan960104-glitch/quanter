"""策略动态加载器（importlib）

启动时扫描 strategies/ 白名单目录下所有模块，收集带 name 的 BaseStrategy 子类。
安全红线：只扫描 strategies/ 目录（非任意路径）；要求类显式声明 name 才注册，
杜绝隐式/恶意加载。

参数 schema 下发：list() 返回策略元数据；get_schema(name) 返回 params_model 的
JSON Schema（含 ui 渲染提示），供前端动态渲染表单。

Why 反黑盒：用标准库 importlib + pkgutil + inspect 三件套平铺扫描，
不引入任何重型插件框架（如 pluggy）。注册逻辑仅一段 issubclass + getattr，
可读性高，便于审计。
"""
import importlib
import inspect
import pkgutil
from typing import Any, Dict, List, Type

from pydantic import BaseModel

from .base import BaseStrategy


class StrategyLoader:
    """策略注册中心（启动时扫描一次，后续只读）

    线程安全说明：lifespan 启动阶段单线程扫描注册；运行期 API 层只调用
    get/list/get_schema（纯读），无并发写，故无需加锁。
    """

    def __init__(self, package_name: str = "strategies"):
        # 默认扫描 strategies 包；构造参数化便于单元测试隔离
        self._package = package_name
        self._registry: Dict[str, Type[BaseStrategy]] = {}

    def scan(self) -> None:
        """扫描策略包，注册所有带 name 的 BaseStrategy 子类

        流程：
        1. importlib.import_module 加载包对象，取 __path__（包目录列表）
        2. pkgutil.iter_modules 枚举包内子模块名（不递归子包，避免误扫测试目录）
        3. 逐个 import 子模块，inspect.getmembers 拿到模块顶层定义的类
        4. 三重过滤：是 BaseStrategy 子类 + 不是基类本身 + 显式声明 name 属性
           —— 最后一条是安全红线，防止扫描到无 name 的辅助基类/混入类
        """
        pkg = importlib.import_module(self._package)
        for _, modname, _ in pkgutil.iter_modules(pkg.__path__):
            module = importlib.import_module(f"{self._package}.{modname}")
            for _, cls in inspect.getmembers(module, inspect.isclass):
                if (issubclass(cls, BaseStrategy)
                        and cls is not BaseStrategy
                        and getattr(cls, "name", None)):
                    # 用类属性 name 注册（universe/params 实例化时注入）
                    self._registry[cls.name] = cls

    def get(self, name: str) -> Type[BaseStrategy]:
        """按 name 获取策略类（不存在抛 KeyError，由路由层转 404）"""
        if name not in self._registry:
            raise KeyError(f"未注册的策略: {name}，可用: {list(self._registry.keys())}")
        return self._registry[name]

    def list_names(self) -> List[str]:
        """返回已注册策略名列表"""
        return list(self._registry.keys())

    def list(self) -> List[Dict[str, Any]]:
        """返回策略元数据（供 GET /api/v1/strategies 下拉框）

        Why getattr 默认值：universe 在基类声明为 ClassVar 但子类多在 __init__
        注入实例属性，类属性层面可能缺失；此处统一回退，保证前端拿到稳定结构。
        """
        return [
            {
                "name": name,
                "label": getattr(cls, "label", name),
                "universe": getattr(cls, "universe", []),
                # 层级三·拓扑白盒：composition/rhythm/capital_allocation 反射到前端
                #（StrategyArchitectView 执行计划图 + 因子 drill-down 反查引用消费）
                "composition": getattr(cls, "composition", {}) or {},
                "rhythm": getattr(cls, "rhythm", "日频"),
                "capital_allocation": getattr(cls, "capital_allocation", ""),
            }
            for name, cls in self._registry.items()
        ]

    def get_schema(self, name: str) -> Dict[str, Any]:
        """返回策略 params_model 的 JSON Schema（供 GET /strategies/{name}/schema）

        单一真相源：前端表单结构 = params_model.model_json_schema()，
        含 Pydantic Field 经 json_schema_extra 合并的 ui 渲染提示键（control/group/step）。
        """
        cls = self.get(name)
        params_model: Type[BaseModel] = cls.params_model
        return params_model.model_json_schema()

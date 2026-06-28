"""策略插件包

设计原则：
- 每个策略一个模块，继承 BaseStrategy
- StrategyLoader 启动时 importlib 扫描本目录自动注册
- 策略只产出 List[TargetWeightSignal]，与引擎/风控/broker 解耦
- 每个策略用 ClassVar params_model 声明可调参数（JSON Schema 真相源）
"""

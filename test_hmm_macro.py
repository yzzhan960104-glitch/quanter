# -*- coding: utf-8 -*-
"""
HMM 宏观状态识别模块独立测试

注意：运行前请先安装依赖：
    pip install hmmlearn scikit-learn
"""

import sys
sys.path.append(".")

from factors.hmm_macro import MacroRegimeHMM, test_hmm_macro_module

if __name__ == "__main__":
    try:
        test_hmm_macro_module()
    except ImportError as e:
        print(f"\n❌ 缺少依赖包：{e}")
        print("\n请运行以下命令安装依赖：")
        print("  pip install hmmlearn scikit-learn")
    except Exception as e:
        print(f"\n❌ 测试失败：{e}")
        raise
"""UI 补丁模块依赖方向回归：规则提供者不得反向依赖聚合编排层。

对应待解决问题清单第 10 项：ui_movie 收集 ui_navigation/sanitize 等模块的
规则，而后两者曾反向导入 ui_movie 的常量与执行能力形成依赖环。重构后
常量与执行能力下沉到 movie_identity，依赖方向应为
规则提供者/聚合层 -> movie_identity，且 movie_identity 不导入任何规则模块。
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

V1649370 = Path(__file__).resolve().parents[1] / 'src' / 'codol_rebuild' / 'adapters' / 'game' / 'v1649370'
# 规则提供者模块：只允许依赖 movie_identity，不得导入 ui_movie。
RULE_PROVIDERS = ['ui_navigation', 'sanitize', 'player_name', 'ui_theme',
                  'ui_weekly_rewards', 'ui_recharge', 'purchase']


def _imported_v1649370_modules(path: Path) -> set:
    """解析模块顶层 import，返回从 v1649370 包导入的模块名集合。"""
    tree = ast.parse(path.read_text(encoding='utf-8'))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            parts = node.module.split('.')
            if 'v1649370' in parts:
                index = parts.index('v1649370')
                if index + 1 < len(parts):
                    names.add(parts[index + 1])
                elif node.names:
                    names.update(alias.name for alias in node.names)
    return names


class MoviePatchDependencyTests(unittest.TestCase):
    """断常依赖环不复现，并验证转发后的公共接口行为不变。"""

    def test_rule_providers_do_not_import_ui_movie(self):
        # 规则提供者不得（含函数内延迟导入）依赖聚合层 ui_movie。
        for name in RULE_PROVIDERS:
            self.assertNotIn('ui_movie', _imported_v1649370_modules(V1649370 / f'{name}.py'),
                             f'{name} 反向依赖 ui_movie')

    def test_movie_identity_imports_no_rule_modules(self):
        # 执行器模块必须保持底层，不导入任何规则提供者或聚合层。
        imports = _imported_v1649370_modules(V1649370 / 'movie_identity.py')
        self.assertFalse(imports & (set(RULE_PROVIDERS) | {'ui_movie'}))

    def test_ui_movie_reexports_identity(self):
        # 兼容既有调用：常量与执行函数经 ui_movie 转发后指向同一对象。
        from codol_rebuild.adapters.game.v1649370 import movie_identity, ui_movie
        for attr in ('LOAD_SITE', 'LOAD_BYTES', 'RESOURCE_NAME', 'RESOURCE_SIZE',
                     'COMPRESSED_SIZE', 'RESOURCE_SHA256'):
            self.assertIs(getattr(ui_movie, attr), getattr(movie_identity, attr))
        self.assertIs(ui_movie.apply_rules, movie_identity.apply_rules)


if __name__ == '__main__':
    unittest.main()

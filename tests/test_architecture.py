"""架构验证：实际扫描新模块、依赖方向和运行路径。"""
import ast
import importlib
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[2]
PACKAGE=ROOT/'client/src/codol_rebuild'

def boundary(module):
    name=module.removeprefix('codol_rebuild.')
    for prefix in ('adapters.game.profile_format','adapters.game.v1649370','adapters.persistence','adapters.transport'):
        if name==prefix or name.startswith(prefix+'.'):return prefix
    return name.split('.')[0]

ALLOWED={
 'application':{'application','core'},
 'adapters.persistence':{'adapters.persistence','application','core'},
 'adapters.transport':{'adapters.transport','application','core'},
 'adapters.game.profile_format':{'adapters.game.profile_format','core'},
 'adapters.game.v1649370':{'adapters.game.v1649370','adapters.game.profile_format','application','runtime','analysis','core'},
 'runtime':{'runtime','platform','core'},
 'bootstrap':{'bootstrap','application','adapters.persistence','adapters.transport','adapters.game.profile_format','adapters.game.v1649370','runtime','platform','resources','core'},
 'core':{'core'},'resources':{'resources','core'},'platform':{'platform','core'},'analysis':{'analysis','core'},
}

class ArchitectureTests(unittest.TestCase):
    def test_version_packages_keep_dependencies_acyclic(self):
        """版本公共机制不反向依赖功能，功能不依赖会话装配，模块间不形成循环。"""
        version_root = PACKAGE/'adapters/game/v1649370'
        prefix = 'codol_rebuild.adapters.game.v1649370'
        module_paths = {
            prefix + '.' + '.'.join(path.relative_to(version_root).with_suffix('').parts): path
            for path in version_root.rglob('*.py') if path.name != '__init__.py'
        }
        dependencies = {module: set() for module in module_paths}
        for module, path in module_paths.items():
            source_group = path.relative_to(version_root).parts[0]
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8-sig'))):
                targets = []
                if isinstance(node, ast.Import):
                    targets = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    self.assertEqual(node.level, 0, str(path))
                    targets = [node.module or ''] + [f'{node.module}.{alias.name}' for alias in node.names]
                for target in targets:
                    if target not in module_paths:
                        continue
                    dependencies[module].add(target)
                    target_group = module_paths[target].relative_to(version_root).parts[0]
                    if path.parent == version_root:
                        self.assertEqual(module_paths[target].parent, version_root, f'{module} -> {target}')
                    if source_group != 'session':
                        self.assertNotEqual(target_group, 'session', f'{module} -> {target}')

        visited = set()
        def visit(module, chain):
            self.assertNotIn(module, chain, ' -> '.join((*chain, module)))
            if module in visited:
                return
            for target in sorted(dependencies[module]):
                visit(target, (*chain, module))
            visited.add(module)
        for module in sorted(dependencies):
            visit(module, ())

    def test_version_package_initializers_have_no_import_side_effects(self):
        """职责包只声明用途，导入包不能隐式装配其他模块或提供旧路径别名。"""
        for path in (PACKAGE/'adapters/game/v1649370').rglob('__init__.py'):
            statements = ast.parse(path.read_text(encoding='utf-8-sig')).body
            self.assertEqual(len(statements), 1, str(path))
            self.assertIsInstance(statements[0], ast.Expr, str(path))
            self.assertIsInstance(statements[0].value, ast.Constant, str(path))
            self.assertIsInstance(statements[0].value.value, str, str(path))

    def test_production_dependency_direction(self):
        files=list(PACKAGE.rglob('*.py'))
        self.assertGreater(len(files),40,'扫描目录错误，禁止空扫描通过')
        modules = {'codol_rebuild.' + '.'.join(path.relative_to(PACKAGE).with_suffix('').parts)
                   .removesuffix('.__init__') for path in files}
        for path in files:
            module='codol_rebuild.'+'.'.join(path.relative_to(PACKAGE).with_suffix('').parts)
            layer=boundary(module)
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8-sig'))):
                imports=([node.module or ''] if isinstance(node,ast.ImportFrom) else [a.name for a in node.names] if isinstance(node,ast.Import) else [])
                if isinstance(node,ast.ImportFrom):
                    self.assertEqual(node.level,0,str(path))
                    # from 包 import 子模块也形成真实依赖，不能只检查父包名称。
                    imports += [f'{node.module}.{alias.name}' for alias in node.names
                                if f'{node.module}.{alias.name}' in modules]
                for target in imports:
                    self.assertFalse(target.startswith(('verification','research')),str(path))
                    if target.startswith('codol_rebuild.') and layer in ALLOWED:
                        self.assertIn(boundary(target),ALLOWED[layer],f'{path}: {target}')
                        if layer=='adapters.game.v1649370' and boundary(target)=='runtime':
                            self.assertEqual(target,'codol_rebuild.runtime.contracts',str(path))
                        if layer in ('adapters.persistence','adapters.transport') and boundary(target)=='application':
                            self.assertTrue(target.endswith('.contracts'),str(path))
                    if layer=='application':
                        self.assertNotIn(target.split('.')[0],{'ctypes','sqlite3','subprocess','struct','socket','requests'},str(path))

    def test_owned_python_modules_have_chinese_purpose(self):
        for folder in ('client/src','client/tools','client/tests','verification/client/tools'):
            for path in (ROOT/folder).rglob('*.py'):
                self.assertRegex(ast.get_docstring(ast.parse(path.read_text(encoding='utf-8-sig'))) or '',r'[\u4e00-\u9fff]',str(path))

    def test_paths_stay_at_existing_data_root(self):
        from codol_rebuild.core.paths import REBUILD_ROOT,PROJECT_ROOT
        self.assertEqual(PROJECT_ROOT,ROOT)
        self.assertEqual(REBUILD_ROOT,ROOT/'client')

    def test_no_old_packages_or_dynamic_imports(self):
        for path in PACKAGE.rglob('*.py'):
            text=path.read_text(encoding='utf-8-sig')
            for prefix in ('codol_rebuild.accounts','codol_rebuild.storage','codol_rebuild.profiles','codol_rebuild.client','codol_rebuild.runtime.offline_debug'):
                self.assertNotIn(prefix,text,str(path))
        for path in PACKAGE.rglob('*.py'):
            parts=path.relative_to(PACKAGE).with_suffix('').parts
            importlib.import_module('codol_rebuild.'+'.'.join(parts))

    def test_runtime_has_no_game_layout_import(self):
        for path in (PACKAGE/'runtime').glob('*.py'):
            text=path.read_text(encoding='utf-8')
            self.assertNotIn('v1649370',text,str(path))
            self.assertNotIn('local_account',text,str(path))

    def test_session_bridge_respects_profile_state_owner(self):
        """会话桥通过档案桥的生命周期接口协作，不能越过接口修改其状态。"""
        path = PACKAGE/'adapters/game/v1649370/session/session_bridge.py'
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            owner = node.value
            if not (isinstance(owner, ast.Attribute) and owner.attr == 'profiles'
                    and isinstance(owner.value, ast.Name) and owner.value.id == 'self'):
                continue
            self.assertNotIsInstance(node.ctx, ast.Store, f'{path}:{node.lineno}')
            self.assertFalse(node.attr.startswith('_'), f'{path}:{node.lineno}')
            self.assertNotIn(node.attr, {'account', 'schemas', 'catalog', 'inventory', 'service'},
                             f'{path}:{node.lineno}')

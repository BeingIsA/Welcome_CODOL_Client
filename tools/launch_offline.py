"""1649370 离线预览入口：已显示角色选择，进入大厅和对局尚未验证。

从当前普通用户 Python 进程调用现有宿主；仅临时断网守卫请求系统 UAC。
--dry-run 只验证输入并显示计划，不创建会话、不运行客户端、不弹 UAC。
资源库与基础 overlay 分别交给宿主校验；本工具不合并或覆盖二者。
"""
from __future__ import annotations

import argparse
import ast
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sys
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from codol_rebuild.adapters.game.v1649370.observation_points import BREAKPOINTS
IMAGE_SHA256 = '55f067ac24e7d5ea9bea5acd46a1514eb1b7780a627b73c2094f41133dce3ffd'
PATCH_BYTES = (
    ('0x4fdadc', '6858793e0368bcd8db00e845ee2700cccccccccc', '6858793e036858793e0368bcd8db00e840ee2700'),
    ('0x77eea5', 'e8a6f8ebff', '33c0909090'),
    ('0x7e993d', 'e8de8ce5ff', '9090909090'),
    ('0xe579f0', '18cddb00', '4c08dc00'),
    ('0xdbccf8', '7365742032343531', '7365742032343339'),
)


def host_options(source: Path) -> set[str]:
    """只读检查宿主公开参数；不导入 Win32 宿主或创建进程。"""
    tree = ast.parse(source.read_text(encoding='utf-8-sig'))
    return {argument.value for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'add_argument' for argument in node.args
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
            and argument.value.startswith('--')}


def build_plan(options: argparse.Namespace) -> dict:
    image = ROOT / 'bin/v1649370/codol1649370-offline.exe'
    patches = ROOT / 'resources/v1649370/offline-menu-patches.json'
    overlay = ROOT / 'resources/v1649370/offline-resources'
    deps = ROOT / 'resources/v1649370/runtime-deps'
    host = ROOT / 'src/codol_rebuild/bootstrap/launch.py'
    for path in (image, patches, host, ROOT / 'bin/offline_guard.exe'):
        if not path.is_file():
            raise ValueError(f'离线入口缺少文件：{path}')
    for path in (overlay, deps):
        if not path.is_dir():
            raise ValueError(f'离线入口缺少目录：{path}')
    if hashlib.sha256(image.read_bytes()).hexdigest() != IMAGE_SHA256:
        raise ValueError('离线镜像与已验证 1649370 镜像指纹不符')
    rows = json.loads(patches.read_text(encoding='utf-8'))
    if (not isinstance(rows, list) or len(rows) != len(PATCH_BYTES)
            or any(not isinstance(row, dict) for row in rows)
            or [(row.get('va'), row.get('before'), row.get('after')) for row in rows] != list(PATCH_BYTES)):
        raise ValueError('要求已验证的五项补丁：地址、修改前后字节必须完全一致')
    session = datetime.now().strftime('offline-%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8]
    supply_resources = bool(options.supply_resources or options.prewarm_run)
    arguments = ['--name', session]
    required = {'--name', '--image', '--patches', '--overlay', '--breakpoint'}
    if options.seconds is None:
        arguments.append('--until-close')
        required.add('--until-close')
    else:
        arguments += ['--seconds', str(options.seconds)]
        required.add('--seconds')
    arguments += ['--image', str(image), '--patches', str(patches),
                  '--overlay', str(overlay)]
    if getattr(options,'verify_resources',False):
        arguments.append('--verify-resources');required.add('--verify-resources')
    if getattr(options,'prepare_only',False):
        arguments.append('--prepare-only');required.add('--prepare-only')
    if supply_resources:
        arguments.append('--supply-resources')
        required.add('--supply-resources')
    if options.offline_menu_preview:
        arguments.append('--offline-menu-preview')
        required.add('--offline-menu-preview')
    if getattr(options,'local_profile',False):
        arguments.append('--local-profile')
        required.add('--local-profile')
    if getattr(options, 'server_url', None):
        from codol_rebuild.adapters.transport.http import normalize_server
        if not getattr(options, 'local_profile', False):
            raise ValueError('--server-url 需要 --local-profile')
        arguments += ['--server-url', normalize_server(options.server_url),
                      '--server-timeout', str(options.server_timeout)]
        required.update(('--server-url', '--server-timeout'))
        if options.server_identity:
            arguments += ['--server-identity', str(options.server_identity.resolve())]
            required.add('--server-identity')
    if options.prewarm_run:
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', options.prewarm_run):
            raise ValueError('--prewarm-run 必须是已有会话名，不接受路径或别名')
        allowed = (ROOT / 'workspaces/offline-runs').resolve()
        previous = (allowed / options.prewarm_run).resolve()
        if previous.parent != allowed or not (previous / 'resource-supply.jsonl').is_file():
            raise ValueError('--prewarm-run 必须指向项目会话中的资源供应记录')
        arguments += ['--prewarm-run', options.prewarm_run]
        required.add('--prewarm-run')
    if options.capture_schema:
        arguments.append('--capture-schema')
        required.add('--capture-schema')
    library_roots = [overlay.resolve()]
    for flag, requested in (('--resource-library', options.resource_library),
                            ('--loose-library', options.loose_library)):
        if requested is None:
            continue
        library = requested.resolve()
        if not library.is_dir() or not any(library.is_relative_to(base.resolve()) for base in (ROOT / 'resources/v1649370', ROOT / 'workspaces')):
            raise ValueError(flag + ' 必须是本项目固定资源目录或 workspaces 内的资源库目录')
        if any(library == other or library.is_relative_to(other) or other.is_relative_to(library)
               for other in library_roots):
            raise ValueError('图片库、松散资源库和基础 overlay 目录不能重叠')
        library_roots.append(library)
        arguments += [flag, str(library)]
        required.add(flag)
    for point in BREAKPOINTS:
        arguments += ['--breakpoint', point]
    arguments += ['--', '+set', '2439', '0']
    missing = sorted(required - host_options(ROOT / 'src/codol_rebuild/bootstrap/config.py'))
    return {'scope': ('server login/create/leave; gameplay not implemented' if getattr(options,'server_url',None) else 'local profile; offline room' if getattr(options,'local_profile',False) else 'offline character-selection preview; lobby and gameplay not verified'),
            'local_profile': getattr(options,'local_profile',False),
            'dry_run': options.dry_run, 'main_menu_verified': False, 'session': session,
            'offline_menu_preview': options.offline_menu_preview,
            'supply_resources': supply_resources,
            'capture_schema': options.capture_schema,
            'output_directory': str(ROOT / 'workspaces/offline-runs' / session),
            'lifetime': 'until-close' if options.seconds is None else 'bounded',
            'seconds': options.seconds, 'python': sys.executable,
            'host_identity': '当前 Python 用户和权限；入口不请求提升宿主',
            'uac': '仅临时断网守卫；客户端使用低权限令牌且守卫未就绪时不恢复执行',
            'resource_library_policy': '资源变更时完整校验，未变资源复用目录；配置独立保存；与 overlay 冲突时拒绝启动',
            'missing_host_options': missing, 'entrypoint': 'codol_rebuild.bootstrap.launch.main',
            'arguments': arguments}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    lifetime = parser.add_mutually_exclusive_group()
    lifetime.add_argument('--until-close', action='store_true', help='运行直到关闭或致命错误（默认）')
    lifetime.add_argument('--seconds', type=int, help='改为 1..600 秒有时限的诊断运行')
    parser.add_argument('--prewarm-run', help='从已有会话资源记录重新核验并预热；只接受会话名')
    parser.add_argument('--resource-library', type=Path, help='已构建 IWI 图片库路径，宿主在启动前校验并复用共享缓存')
    parser.add_argument('--loose-library', type=Path, help='已构建模型、音频和指定配置库路径，宿主在启动前校验并复用共享缓存')
    parser.add_argument('--offline-menu-preview', action=argparse.BooleanOptionalAction, default=False,
                        help='启用已验证到角色选择页的原生 UI 激活；不创建在线资料或库存')
    parser.add_argument('--supply-resources', action='store_true',
                        help='启用文件缺失断点和单次资源补件，仅用于诊断（默认关闭）')
    parser.add_argument('--capture-schema', action='store_true',
                        help='在前端帧只读扫描档案 DDL 指纹并导出结构定义（不写目标进程）')
    parser.add_argument('--local-profile', action='store_true', help='启用本地档案适配（实验验证中）')
    parser.add_argument('--server-url', help='连接业务服务器，例如 http://127.0.0.1:8080')
    parser.add_argument('--server-timeout', type=float, default=5.0, help='业务请求超时秒数（0.1～30）')
    parser.add_argument('--server-identity', type=Path, help='覆盖身份文件路径，供隔离验证使用')
    parser.add_argument('--verify-resources',action='store_true',help='强制资源 SHA-256 全量复核')
    parser.add_argument('--prepare-only',action='store_true',help='仅准备并测量启动目录，不运行游戏')
    parser.add_argument('--dry-run', action='store_true', help='只显示计划及检查支持情况，不启动')
    options = parser.parse_args(argv)
    if not 0.1 <= options.server_timeout <= 30:
        parser.error('--server-timeout 必须为 0.1～30 秒')
    if options.local_profile:options.offline_menu_preview=True
    if options.seconds is not None and not 1 <= options.seconds <= 600:
        parser.error('--seconds 必须在 1 至 600 秒之间')
    try:
        plan = build_plan(options)
        print(json.dumps(plan, ensure_ascii=False, indent=2), flush=True)
        if plan['missing_host_options']:
            raise ValueError('宿主尚不支持所需参数，拒绝启动：' + ', '.join(plan['missing_host_options']))
        if options.dry_run:
            return 0
        # 保持当前用户/令牌，不启动额外提升 shell；仅宿主内部的守卫使用 runas。
        sys.path.insert(0, str(ROOT / 'src'))
        from codol_rebuild.bootstrap.launch import main as debug_main
        return debug_main(plan['arguments'])
    except (OSError, ValueError, SyntaxError) as error:
        print(json.dumps({'error': str(error), 'main_menu_verified': False}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

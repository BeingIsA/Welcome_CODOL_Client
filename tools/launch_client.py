"""1649370 客户端启动入口：本地档案及可选服务端登录、创建和退出房间。

从当前普通用户 Python 进程调用现有宿主；仅临时断网守卫请求系统 UAC。
--dry-run 只验证输入并显示计划，不创建会话、不运行客户端、不弹 UAC。
统一使用完整原生资源树与可复用程序目录。
"""
from __future__ import annotations

import argparse
import configparser
import ast
from datetime import datetime
import hashlib
import json
from pathlib import Path
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
    requested_native = getattr(options, 'native_library', None)
    native_library = (requested_native or ROOT / 'resources/v1649370/game').resolve()
    image = ROOT / 'bin/v1649370/codol1649370-offline.exe'
    patches = ROOT / 'resources/v1649370/offline-menu-patches.json'
    host = ROOT / 'src/codol_rebuild/bootstrap/launch.py'
    for path in (image, patches, host, ROOT / 'bin/offline_guard.exe'):
        if not path.is_file():
            raise ValueError(f'离线入口缺少文件：{path}')
    if not native_library.is_dir():
        raise ValueError(f'离线入口缺少目录：{native_library}')
    from codol_rebuild.resources.native_library import read_manifest
    if not native_library.is_relative_to((ROOT / 'resources/v1649370').resolve()):
        raise ValueError('完整原生库必须位于项目固定版本资源目录')
    read_manifest(native_library)
    if hashlib.sha256(image.read_bytes()).hexdigest() != IMAGE_SHA256:
        raise ValueError('离线镜像与已验证 1649370 镜像指纹不符')
    rows = json.loads(patches.read_text(encoding='utf-8'))
    if (not isinstance(rows, list) or len(rows) != len(PATCH_BYTES)
            or any(not isinstance(row, dict) for row in rows)
            or [(row.get('va'), row.get('before'), row.get('after')) for row in rows] != list(PATCH_BYTES)):
        raise ValueError('要求已验证的五项补丁：地址、修改前后字节必须完全一致')
    session = datetime.now().strftime('offline-%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8]
    arguments = ['--name', session]
    required = {'--name', '--image', '--patches', '--breakpoint'}
    if getattr(options, 'trace_native_exit', True):
        arguments.append('--trace-native-exit')
        required.add('--trace-native-exit')
    else:
        arguments.append('--no-trace-native-exit')
        required.add('--no-trace-native-exit')
    if options.seconds is None:
        arguments.append('--until-close')
        required.add('--until-close')
    else:
        arguments += ['--seconds', str(options.seconds)]
        required.add('--seconds')
    # 由原生流程衔接启动动画与界面，避免额外菜单命令打断动画显示。
    arguments += ['--image', str(image), '--patches', str(patches)]
    arguments += ['--native-library', str(native_library)]
    required.add('--native-library')
    if getattr(options,'verify_resources',False):
        arguments.append('--verify-resources');required.add('--verify-resources')
    if getattr(options,'prepare_only',False):
        arguments.append('--prepare-only');required.add('--prepare-only')
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
    if options.capture_schema:
        arguments.append('--capture-schema')
        required.add('--capture-schema')
    for point in BREAKPOINTS:
        arguments += ['--breakpoint', point]
    arguments += ['--', '+set', '2439', '0']
    if getattr(options, 'game_server_ip', None):
        delimiter = arguments.index('--')
        arguments[delimiter:delimiter] = ['--game-server-ip', options.game_server_ip, '--game-server-port', str(options.game_server_port)]
        required.update(('--game-server-ip', '--game-server-port'))
    if getattr(options, 'game_network_mode', None):
        delimiter = arguments.index('--')
        arguments[delimiter:delimiter] = ['--game-network-mode', options.game_network_mode,
            '--game-bind-ip', options.game_bind_ip, '--game-bind-port', str(options.game_bind_port)]
        required.update(('--game-network-mode', '--game-bind-ip', '--game-bind-port'))
    missing = sorted(required - host_options(ROOT / 'src/codol_rebuild/bootstrap/config.py'))
    return {'scope': ('server room and single-player ready/start; Cross Fire / war only' if getattr(options,'server_url',None) else 'local profile; offline room' if getattr(options,'local_profile',False) else 'offline character-selection preview; lobby and gameplay not verified'),
            'local_profile': getattr(options,'local_profile',False),
            'dry_run': options.dry_run, 'main_menu_verified': False, 'session': session,
            'offline_menu_preview': options.offline_menu_preview,
            'capture_schema': options.capture_schema,
            'output_directory': str(ROOT / 'workspaces/offline-runs' / session),
            'lifetime': 'until-close' if options.seconds is None else 'bounded',
            'seconds': options.seconds, 'python': sys.executable,
            'host_identity': '当前 Python 用户和权限；入口不请求提升宿主',
            'uac': '仅临时断网守卫；客户端使用低权限令牌且守卫未就绪时不恢复执行',
            'resource_library_policy': '完整原生资源树；启动前校验，仅联接顶层目录；玩家设置独立保存',
            'missing_host_options': missing, 'entrypoint': 'codol_rebuild.bootstrap.launch.main',
            'arguments': arguments}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--trace-native-exit', action='store_true', default=True, help='采集原生无效参数及退出调用栈（默认开启）')
    parser.add_argument('--no-trace-native-exit', action='store_false', dest='trace_native_exit', help='关闭原生退出诊断')
    lifetime = parser.add_mutually_exclusive_group()
    lifetime.add_argument('--until-close', action='store_true', help='运行直到关闭或致命错误（默认）')
    lifetime.add_argument('--seconds', type=int, help='改为 1..600 秒有时限的诊断运行')
    parser.add_argument('--native-library', type=Path, help='完整原生资源根；默认 resources/v1649370/game')
    parser.add_argument('--offline-menu-preview', action=argparse.BooleanOptionalAction, default=False,
                        help='启用已验证到角色选择页的原生 UI 激活；不创建在线资料或库存')
    parser.add_argument('--capture-schema', action='store_true',
                        help='在前端帧只读扫描档案 DDL 指纹并导出结构定义（不写目标进程）')
    parser.add_argument('--local-profile', action='store_true', help='启用本地档案适配（实验验证中）')
    parser.add_argument('--server-config', type=Path, help='大厅与游戏服务器INI配置')
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
        if options.server_config:
            from codol_rebuild.bootstrap.config import read_server_config
            configured_url, endpoint, network = read_server_config(options.server_config)
            if not options.server_url: options.server_url = configured_url
            if endpoint: options.game_server_ip, options.game_server_port = endpoint
            if network: options.game_network_mode, options.game_bind_ip, options.game_bind_port = network
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
    except (OSError, ValueError, SyntaxError, configparser.Error) as error:
        print(json.dumps({'error': str(error), 'main_menu_verified': False}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

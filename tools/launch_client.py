"""1649370 客户端启动入口：本地档案及可选服务端登录、创建和退出房间。

从当前普通用户 Python 进程调用现有宿主；仅临时断网守卫请求系统 UAC。
--dry-run 只验证输入并显示计划，不创建会话、不运行客户端、不弹 UAC。
默认读取随客户端交付的终版原包，并复用已装配的程序目录。
"""
from __future__ import annotations

import argparse
import configparser
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
import uuid


try:
    __compiled__
except NameError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from codol_rebuild.core.paths import REBUILD_ROOT as ROOT
from codol_rebuild.bootstrap.config import build_parser
from codol_rebuild.adapters.game.v1649370.observation_points import BREAKPOINTS
IMAGE_SHA256 = '55f067ac24e7d5ea9bea5acd46a1514eb1b7780a627b73c2094f41133dce3ffd'
PATCH_BYTES = (
    ('0x4fdadc', '6858793e0368bcd8db00e845ee2700cccccccccc', '6858793e036858793e0368bcd8db00e840ee2700'),
    ('0x77eea5', 'e8a6f8ebff', '33c0909090'),
    ('0x7e993d', 'e8de8ce5ff', '9090909090'),
    ('0xe579f0', '18cddb00', '4c08dc00'),
    ('0xdbccf8', '7365742032343531', '7365742032343339'),
    # 跳过横纵灵敏度的下限覆盖，保留原生缩放与浮点栈清理。
    ('0x5b1809', '75', 'eb'),
    ('0x5b1816', '75', 'eb'),
)


def build_plan(options: argparse.Namespace) -> dict:
    requested_native = getattr(options, 'native_library', None)
    image = ROOT / 'bin/v1649370/codol1649370-offline.exe'
    patches = ROOT / 'manifests/offline-menu-patches.json'
    for path in (image, patches, ROOT / 'bin/offline_guard.exe'):
        if not path.is_file():
            raise ValueError(f'离线入口缺少文件：{path}')
    if requested_native is None:
        from codol_rebuild.resources.packed_library import INDEX_NAME, read_packed_index
        from codol_rebuild.resources.packed_view import PACKED_READER_FILES
        resource_root = ROOT / 'resources/v1649370/packed'
        reader_directory = ROOT / 'bin/v1649370/packed-reader'
        if not (resource_root / INDEX_NAME).is_file():
            raise ValueError(f'缺少游戏资源。请将配套资源放入 {resource_root}，其中须包含 {INDEX_NAME}。')
        for path in (reader_directory / name for name in PACKED_READER_FILES):
            if not path.is_file():
                raise ValueError(f'客户端缺少随包交付的文件：{path}')
        # 正常启动由装配步骤读取一次索引。仅预检时也要核实来源文件齐备。
        if options.dry_run:
            read_packed_index(resource_root, verify_resources=getattr(options, 'verify_resources', False))
        resource_policy = '项目自带终版 IFS 原包；首次读取整文件填充，退出后释放'
    else:
        from codol_rebuild.resources.native_library import read_manifest
        resource_root = requested_native.resolve()
        if not resource_root.is_dir() or not resource_root.is_relative_to((ROOT / 'resources/v1649370').resolve()):
            raise ValueError('完整原生库必须位于项目固定版本资源目录')
        read_manifest(resource_root, verify_resources=False)
        resource_policy = '开发对照用完整展开库；顶层目录联接'
    if hashlib.sha256(image.read_bytes()).hexdigest() != IMAGE_SHA256:
        raise ValueError('离线镜像与已验证 1649370 镜像指纹不符')
    rows = json.loads(patches.read_text(encoding='utf-8'))
    if (not isinstance(rows, list) or len(rows) != len(PATCH_BYTES)
            or any(not isinstance(row, dict) for row in rows)
            or [(row.get('va'), row.get('before'), row.get('after')) for row in rows] != list(PATCH_BYTES)):
        raise ValueError('要求已验证的补丁清单：地址、修改前后字节必须完全一致')
    session = datetime.now().strftime('offline-%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8]
    arguments = ['--name', session]
    if getattr(options, 'trace_native_exit', True):
        arguments.append('--trace-native-exit')
    else:
        arguments.append('--no-trace-native-exit')
    if options.seconds is None:
        arguments.append('--until-close')
    else:
        arguments += ['--seconds', str(options.seconds)]
    # 由原生流程衔接启动动画与界面，避免额外菜单命令打断动画显示。
    arguments += ['--image', str(image), '--patches', str(patches)]
    if requested_native is not None:
        arguments += ['--native-library', str(resource_root)]
    if getattr(options,'verify_resources',False):
        arguments.append('--verify-resources')
    if getattr(options,'prepare_only',False):
        arguments.append('--prepare-only')
    if options.offline_menu_preview:
        arguments.append('--offline-menu-preview')
    if getattr(options,'local_profile',False):
        arguments.append('--local-profile')
    if getattr(options, 'server_url', None):
        from codol_rebuild.adapters.transport.http import normalize_server
        if not getattr(options, 'local_profile', False):
            raise ValueError('--server-url 需要 --local-profile')
        arguments += ['--server-url', normalize_server(options.server_url),
                      '--server-timeout', str(options.server_timeout)]
        arguments += ['--client-port', str(options.client_port)]
        if options.server_identity:
            arguments += ['--server-identity', str(options.server_identity.resolve())]
    if options.capture_schema:
        arguments.append('--capture-schema')
    for point in BREAKPOINTS:
        arguments += ['--breakpoint', point]
    arguments += ['--', '+set', '2439', '0']
    build_parser().parse_args(arguments)
    return {'scope': ('server room and single-player ready/start; Cross Fire / war only' if getattr(options,'server_url',None) else 'local profile; offline room' if getattr(options,'local_profile',False) else 'offline character-selection preview; lobby and gameplay not verified'),
            'local_profile': getattr(options,'local_profile',False),
            'dry_run': options.dry_run, 'main_menu_verified': False, 'session': session,
            'offline_menu_preview': options.offline_menu_preview,
            'capture_schema': options.capture_schema,
            'output_directory': str(ROOT / 'workspaces/offline-runs' / session),
            'lifetime': 'until-close' if options.seconds is None else 'bounded',
            'seconds': options.seconds,
            **({'host_executable': str(ROOT / 'CODOL.exe')} if '__compiled__' in globals()
               else {'python': sys.executable}),
            'host_identity': '当前 Python 用户和权限；入口不请求提升宿主',
            'uac': '仅临时断网守卫；客户端使用低权限令牌且守卫未就绪时不恢复执行',
            'resource_library_root': str(resource_root), 'resource_library_policy': resource_policy,
            'missing_host_options': [], 'entrypoint': 'codol_rebuild.bootstrap.launch.main',
            'arguments': arguments}


def report_start_error(message: str) -> None:
    """命令行输出机器可读错误，双击发行版时通过 Windows 提示用户。"""
    stream = sys.stderr if sys.stderr is not None else sys.stdout
    if stream is not None:
        print(json.dumps({'error': message, 'main_menu_verified': False}, ensure_ascii=False),
              file=stream)
    elif '__compiled__' in globals():
        from ctypes import windll
        windll.user32.MessageBoxW(None, message, 'CODOL 启动失败', 0x10)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--trace-native-exit', action='store_true', default=True, help='采集原生无效参数及退出调用栈（默认开启）')
    parser.add_argument('--no-trace-native-exit', action='store_false', dest='trace_native_exit', help='关闭原生退出诊断')
    lifetime = parser.add_mutually_exclusive_group()
    lifetime.add_argument('--until-close', action='store_true', help='运行直到关闭或致命错误（默认）')
    lifetime.add_argument('--seconds', type=int, help='改为 1..600 秒有时限的诊断运行')
    parser.add_argument('--native-library', type=Path, help='开发对照用的展开资源目录；省略时读取随项目交付的原包')
    parser.add_argument('--offline-menu-preview', action=argparse.BooleanOptionalAction, default=False,
                        help='启用已验证到角色选择页的原生 UI 激活；不创建在线资料或库存')
    parser.add_argument('--capture-schema', action='store_true',
                        help='在前端帧只读扫描档案 DDL 指纹并导出结构定义（不写目标进程）')
    parser.add_argument('--local-profile', action='store_true', help='启用本地档案适配（实验验证中）')
    parser.add_argument('--client-port', type=int, default=30001, help='客户端本地UDP端口')
    parser.add_argument('--server-config', type=Path, help='大厅与游戏服务器INI配置')
    parser.add_argument('--server-url', help='连接业务服务器，例如 http://127.0.0.1:8080')
    parser.add_argument('--server-timeout', type=float, default=5.0, help='业务请求超时秒数（0.1～30）')
    parser.add_argument('--server-identity', type=Path, help='覆盖身份文件路径，供隔离验证使用')
    parser.add_argument('--verify-resources',action='store_true',help='强制资源 SHA-256 全量复核')
    parser.add_argument('--prepare-only',action='store_true',help='仅准备并测量启动目录，不运行游戏')
    parser.add_argument('--dry-run', action='store_true', help='只显示计划及检查支持情况，不启动')
    if '__compiled__' in globals():
        # 双击发行版 EXE 时沿用 start_client.cmd 的档案和服务器配置。
        parser.set_defaults(local_profile=True, server_config=ROOT / 'config/server.ini')
    options = parser.parse_args(argv)
    if not 0.1 <= options.server_timeout <= 30:
        parser.error('--server-timeout 必须为 0.1～30 秒')
    if options.local_profile:options.offline_menu_preview=True
    if options.seconds is not None and not 1 <= options.seconds <= 600:
        parser.error('--seconds 必须在 1 至 600 秒之间')
    try:
        if options.server_config:
            from codol_rebuild.bootstrap.config import read_server_config
            configured_url, options.client_port = read_server_config(options.server_config)
            if not options.server_url: options.server_url = configured_url
        plan = build_plan(options)
        print(json.dumps(plan, ensure_ascii=False, indent=2), flush=True)
        if options.dry_run:
            return 0
        # 保持当前用户/令牌，不启动额外提升 shell；仅宿主内部的守卫使用 runas。
        from codol_rebuild.bootstrap.launch import main as debug_main
        exit_code = debug_main(plan['arguments'])
        if exit_code and sys.stdout is None and sys.stderr is None:
            report_start_error(f"本次运行失败，详细记录见 {Path(plan['output_directory']) / 'result.json'}")
        return exit_code
    except (OSError, ValueError, SyntaxError, configparser.Error) as error:
        report_start_error(str(error))
        return 1


if __name__ == '__main__':
    if '__compiled__' in globals():
        # 与源码入口的 -X utf8 一致，重定向日志时也能保留中文。
        for stream in (sys.stdout, sys.stderr):
            if stream is not None:
                stream.reconfigure(encoding='utf-8')
    raise SystemExit(main())

"""离线运行参数解析；不创建进程。"""
import argparse
import configparser
import ipaddress
from pathlib import Path
from urllib.parse import urlsplit
from codol_rebuild.core.paths import REBUILD_ROOT as ROOT, accounts_dir

def parse_args(argv=None):
    """显式单次命令，所有会话放项目目录；不会把未验收窗口判成成功。"""
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image',type=Path,default=ROOT/'bin/v1649370/codol1649370-offline.exe')
    parser.add_argument('--name',required=True)
    parser.add_argument('--verify-resources',action='store_true',help='强制资源 SHA-256 全量复核')
    parser.add_argument('--prepare-only',action='store_true',help='仅准备运行目录并计时，不创建客户端或守卫')
    lifetime=parser.add_mutually_exclusive_group()
    lifetime.add_argument('--seconds',type=int,default=30)
    lifetime.add_argument('--until-close',action='store_true')
    parser.add_argument('--patches',type=Path)
    parser.add_argument('--native-library',type=Path,default=ROOT/'resources/v1649370/game',help='完整原生资源根，只联接顶层目录')
    parser.add_argument('--trace-files',action='store_true')
    parser.add_argument('--trace-native-exit', action='store_true', default=True, help='启用一次性原生退出诊断断点（默认开启）')
    parser.add_argument('--no-trace-native-exit', action='store_false', dest='trace_native_exit', help='关闭原生退出诊断')
    parser.add_argument('--offline-menu-preview',action='store_true')
    parser.add_argument('--local-profile',action='store_true',help='Experimental build-specific local profile bridge')
    parser.add_argument('--client-port', type=int, default=30001, help='客户端本地UDP端口')
    parser.add_argument('--server-url',help='启用服务端登录与房间接口，例如 http://127.0.0.1:8080')
    parser.add_argument('--server-timeout',type=float,default=5.0,help='业务请求超时秒数（0.1～30）')
    parser.add_argument('--server-identity',type=Path,
                        default=accounts_dir()/'server-identities.json',
                        help='服务器身份文件；验证时指定隔离路径')
    parser.add_argument('--capture-schema',action='store_true',
                        help='在前端帧只读扫描 DDL 指纹并导出档案结构定义（不写目标进程）')
    parser.add_argument('--breakpoint',action='append',default=[])
    parser.add_argument('argument',nargs='*')
    args=parser.parse_args(argv)
    args.server_address = None
    if not 1 <= args.client_port <= 65535:
        parser.error('客户端UDP端口必须为1～65535')
    if args.server_url:
        from codol_rebuild.adapters.transport.http import normalize_server
        try:
            args.server_url=normalize_server(args.server_url)
            args.server_address = server_ipv4(urlsplit(args.server_url).hostname)
        except ValueError as error:parser.error(str(error))
        if not args.local_profile:parser.error('--server-url 需要 --local-profile')
    if not 0.1 <= args.server_timeout <= 30:parser.error('业务请求超时须为 0.1～30 秒')
    if args.local_profile:args.offline_menu_preview=True
    if not 1<=args.seconds<=600:parser.error('单次观测时长必须在1至600秒之间')
    return args


def server_ipv4(address):
    """大厅、原生对局及守卫共享同一个明确的服务器IPv4。"""
    parsed_address = ipaddress.IPv4Address(address)
    if not 0 < int(parsed_address) < 0xE0000000:
        raise ValueError('服务器必须为具体单播IPv4地址')
    return str(parsed_address)


def read_server_config(path):
    """读取服务器HTTP端点和客户端UDP端口，不维护第二份服务器地址。"""
    configuration = configparser.ConfigParser(interpolation=None)
    with Path(path).open(encoding='utf-8-sig') as stream:
        configuration.read_file(stream)
    server_address = server_ipv4(configuration.get('server', 'ip'))
    server_port = configuration.getint('server', 'port', fallback=8080)
    client_port = configuration.getint('client', 'port', fallback=30001)
    if not 1 <= server_port <= 65535 or not 1 <= client_port <= 65535:
        raise ValueError('服务器HTTP端口及客户端UDP端口必须为1～65535')
    return f'http://{server_address}:{server_port}', client_port

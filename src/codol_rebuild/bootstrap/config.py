"""离线运行参数解析；不创建进程。"""
import argparse
import configparser
import ipaddress
from pathlib import Path
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
    parser.add_argument('--offline-menu-preview',action='store_true')
    parser.add_argument('--local-profile',action='store_true',help='Experimental build-specific local profile bridge')
    parser.add_argument('--game-server-ip')
    parser.add_argument('--game-server-port', type=int)
    parser.add_argument('--game-network-mode', choices=('local',))
    parser.add_argument('--game-bind-ip')
    parser.add_argument('--game-bind-port', type=int)
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
    if (args.game_server_ip is None) != (args.game_server_port is None):
        parser.error('游戏服务器地址与端口必须同时提供')
    if args.game_server_ip is not None:
        try: args.game_server_ip, args.game_server_port = game_endpoint(args.game_server_ip, args.game_server_port)
        except ValueError as error: parser.error(str(error))
    if any(value is not None for value in (args.game_network_mode, args.game_bind_ip, args.game_bind_port)):
        if not args.game_network_mode or args.game_bind_ip is None or args.game_bind_port is None or args.game_server_ip is None:
            parser.error('本地网络模式需要完整绑定地址、端口及已启用游戏服务器端点')
        try: args.game_bind_ip, args.game_bind_port = game_endpoint(args.game_bind_ip, args.game_bind_port)
        except ValueError as error: parser.error(str(error))
    if args.server_url:
        from codol_rebuild.adapters.transport.http import normalize_server
        try:args.server_url=normalize_server(args.server_url)
        except ValueError as error:parser.error(str(error))
        if not args.local_profile:parser.error('--server-url 需要 --local-profile')
    if not 0.1 <= args.server_timeout <= 30:parser.error('业务请求超时须为 0.1～30 秒')
    if args.local_profile:args.offline_menu_preview=True
    if not 1<=args.seconds<=600:parser.error('单次观测时长必须在1至600秒之间')
    return args


def game_endpoint(address, port):
    """游戏UDP端点仅允许本机回环IPv4，不执行DNS或连接操作。"""
    parsed_address = ipaddress.IPv4Address(address)
    parsed_port = int(port)
    if not parsed_address.is_loopback or not 1 <= parsed_port <= 65535:
        raise ValueError('游戏服务器必须为回环IPv4地址及1～65535端口')
    return str(parsed_address), parsed_port


def read_server_config(path):
    """按分节读取大厅与游戏端点；未配置游戏分节时保持断网。"""
    configuration = configparser.ConfigParser(interpolation=None)
    with Path(path).open(encoding='utf-8-sig') as stream:
        configuration.read_file(stream)
    lobby_address = configuration.get('server', 'ip')
    lobby_port = configuration.getint('server', 'port', fallback=8080)
    if not lobby_address or not 1 <= lobby_port <= 65535:
        raise ValueError('大厅服务器地址或端口无效')
    endpoint = None
    if configuration.getboolean('game_server', 'enabled', fallback=False):
        endpoint = game_endpoint(configuration.get('game_server', 'ip'), configuration.get('game_server', 'port'))
    network = None
    if configuration.has_section('game_network'):
        mode = configuration.get('game_network', 'mode')
        if mode != 'local' or endpoint is None:
            raise ValueError('本轮仅支持local网络模式，且须启用游戏服务器端点')
        bind_address, bind_port = game_endpoint(configuration.get('game_network', 'bind_ip'),
                                                 configuration.get('game_network', 'bind_port'))
        network = (mode, bind_address, bind_port)
    return f'http://{lobby_address}:{lobby_port}', endpoint, network

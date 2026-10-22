"""离线运行参数解析；不创建进程。"""
import argparse
from pathlib import Path
from codol_rebuild.core.paths import REBUILD_ROOT as ROOT

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
    parser.add_argument('--overlay',type=Path)
    parser.add_argument('--resource-library',type=Path)
    parser.add_argument('--loose-library',type=Path)
    parser.add_argument('--trace-files',action='store_true')
    parser.add_argument('--supply-resources',action='store_true')
    parser.add_argument('--queue-main-menu',action='store_true')
    parser.add_argument('--offline-menu-preview',action='store_true')
    parser.add_argument('--local-profile',action='store_true',help='Experimental build-specific local profile bridge')
    parser.add_argument('--server-url',help='启用服务端登录与房间接口，例如 http://127.0.0.1:8080')
    parser.add_argument('--server-timeout',type=float,default=5.0,help='业务请求超时秒数（0.1～30）')
    parser.add_argument('--server-identity',type=Path,default=ROOT/'data/accounts/server-identities.json',
                        help='服务器身份文件；验证时指定隔离路径')
    parser.add_argument('--capture-schema',action='store_true',
                        help='在前端帧只读扫描 DDL 指纹并导出档案结构定义（不写目标进程）')
    parser.add_argument('--prewarm-run')
    parser.add_argument('--breakpoint',action='append',default=[])
    parser.add_argument('argument',nargs='*')
    args=parser.parse_args(argv)
    if args.server_url:
        from codol_rebuild.adapters.transport.http import normalize_server
        try:args.server_url=normalize_server(args.server_url)
        except ValueError as error:parser.error(str(error))
        if not args.local_profile:parser.error('--server-url 需要 --local-profile')
    if not 0.1 <= args.server_timeout <= 30:parser.error('业务请求超时须为 0.1～30 秒')
    if args.local_profile:args.offline_menu_preview=True
    if args.prewarm_run:args.supply_resources=True
    if args.supply_resources:args.trace_files=True
    if not 1<=args.seconds<=600:parser.error('单次观测时长必须在1至600秒之间')
    return args

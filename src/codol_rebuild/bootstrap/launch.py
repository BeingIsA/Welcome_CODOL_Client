"""离线运行组合入口：创建依赖、注入版本桥并启动运行会话。"""
import json, sqlite3, time, traceback
from codol_rebuild.core.paths import REBUILD_ROOT as ROOT
from codol_rebuild.bootstrap.config import parse_args
from codol_rebuild.bootstrap.resource_setup import prepare, FORBIDDEN
from codol_rebuild.adapters.persistence.sqlite_accounts import AccountRepository
from codol_rebuild.adapters.game.v1649370.profile_codec import AccountCodec
from codol_rebuild.application.accounts.service import AccountSession
from codol_rebuild.adapters.game.v1649370.session_bridge import GameSessionBridge
from codol_rebuild.runtime.session import ProcessSession, SessionLog
from codol_rebuild.runtime.debug_loop import DebugLoop


def run(args):
    """装配单次运行；所有失败均由会话清理并生成结果。"""
    reporter=SessionLog(ROOT/'workspaces/offline-runs',args.name)
    run_dir,stage,log=reporter.run_dir,reporter.stage,reporter.data
    record,phase_done=reporter.record,reporter.phase_done
    def account_factory(schemas,tables,catalog):
        return AccountSession(AccountRepository(ROOT/'data/accounts/local.sqlite3'),
                              AccountCodec(schemas,tables,catalog))
    process=ProcessSession(args,run_dir,stage,log,record,phase_done,ROOT/'bin/offline_guard.exe')
    process.settings_errors=(OSError,ValueError,sqlite3.Error)
    bridge=None
    multiplayer=None
    try:
        prepared=prepare(args,run_dir,stage,log,phase_done)
        if getattr(args,'prepare_only',False):
            log['prepared_only']=True
            return log
        if getattr(args, 'server_url', None):
            from codol_rebuild.application.multiplayer.service import MultiplayerSession
            from codol_rebuild.adapters.transport.http import HttpGateway
            from codol_rebuild.adapters.transport.websocket import WebSocketConnection
            from codol_rebuild.adapters.persistence.server_identity import JsonIdentityStore
            from codol_rebuild.platform.machine_identity import fingerprint
            gateway = HttpGateway(args.server_url, args.server_timeout)
            multiplayer = MultiplayerSession(gateway, JsonIdentityStore(args.server_identity),
                                             gateway.server, fingerprint(),
                                             realtime=WebSocketConnection(gateway.server, args.server_timeout))
            record({'multiplayer_server': gateway.server})
        bridge=GameSessionBridge(args,run_dir,prepared['image'],prepared['pe'],log,record,phase_done,
                                 process,account_factory,(OSError,ValueError,sqlite3.Error), multiplayer=multiplayer)
        debugger=DebugLoop(process,bridge,prepared['pe'],prepared['supplier'],FORBIDDEN)
        process.start(prepared['exe'],prepared['settings'])
        debugger.run()
    except Exception as error:
        log['error']=str(error);log['traceback']=traceback.format_exc()
    finally:
        try:
            if bridge and process.is_running() and not log.get('error') and log.get('stop_reason') not in ('fatal_dialog','unhandled_exception'):
                try:bridge.checkpoint(force=True)
                except (OSError,ValueError) as error:log['local_profile_save_error']=str(error)
        finally:
            try:process.close()
            finally:
                if multiplayer:
                    try:multiplayer.close()
                    except Exception as error:
                        log['multiplayer_cleanup_error']=str(error)
                reporter.close()
    return log


def main(argv=None):
    """保留原宿主参数和退出码语义。"""
    result=run(parse_args(argv))
    print(json.dumps({k:v for k,v in result.items() if k not in ('events','modules','traceback','patches')},ensure_ascii=False,indent=2))
    cleanup=result.get('guard_cleanup',{})
    failed=(result.get('error') or result.get('stop_reason') in ('unhandled_exception','forbidden_module_before_initialization','fatal_dialog')
            or result.get('exit_code',0)!=0 or result.get('child_terminated') is False
            or result.get('guard_cleanup_timeout') or cleanup.get('error') or cleanup.get('engine_close_status',0)!=0)
    return 1 if failed else 0

if __name__=='__main__':raise SystemExit(main())

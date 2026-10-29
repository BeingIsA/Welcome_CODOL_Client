"""离线运行组合入口：创建依赖、注入版本桥并启动运行会话。"""
import json, sqlite3, time, traceback
from codol_rebuild.core.paths import REBUILD_ROOT as ROOT, accounts_dir
from codol_rebuild.bootstrap.config import parse_args
from codol_rebuild.bootstrap.logging_setup import session_logging
from codol_rebuild.bootstrap.resource_setup import prepare, FORBIDDEN
from codol_rebuild.adapters.persistence.sqlite_accounts import AccountRepository
from codol_rebuild.adapters.game.v1649370.accounts.profile_codec import AccountCodec
from codol_rebuild.application.accounts.service import AccountSession
from codol_rebuild.adapters.game.v1649370.session.session_bridge import GameSessionBridge
from codol_rebuild.runtime.session import ProcessSession, SessionLog
from codol_rebuild.runtime.debug_loop import DebugLoop


def run(args):
    """装配单次运行；所有失败均由会话清理并生成结果。"""
    if getattr(args, 'game_network_mode', None) == 'local':
        from codol_rebuild.adapters.game.v1649370.matches.network_initialization import native_arguments
        args.argument += native_arguments(args.game_bind_ip, args.game_bind_port)
    reporter=SessionLog(ROOT/'workspaces/offline-runs',args.name)
    try:
        with session_logging(reporter.run_dir):
            return run_session(args, reporter)
    finally:
        reporter.close()


def run_session(args, reporter):
    """在已配置日志的会话中装配并清理客户端依赖。"""
    run_dir,stage,log=reporter.run_dir,reporter.stage,reporter.data
    record,phase_done=reporter.record,reporter.phase_done
    accounts = accounts_dir()
    def account_factory(schemas,tables,catalog):
        return AccountSession(AccountRepository(accounts/'local.sqlite3'),
                              AccountCodec(schemas,tables,catalog))
    process=ProcessSession(args,run_dir,stage,log,record,phase_done,ROOT/'bin/offline_guard.exe')
    process.settings_errors=(OSError,ValueError,sqlite3.Error)
    bridge=None
    multiplayer=None
    online_session = None
    match_session = None
    rename_session=None
    chat_session = None
    broadcast_session = None
    realtime_dispatcher = None
    runtime_lease = None
    try:
        prepared=prepare(args,run_dir,stage,log,phase_done)
        runtime_lease = prepared.get('runtime_lease')
        if runtime_lease is not None:
            process.stage = runtime_lease.path
            process.runtime_lease = runtime_lease
        if getattr(args,'prepare_only',False):
            log['prepared_only']=True
            return log
        if getattr(args, 'server_url', None):
            from codol_rebuild.application.room.service import RoomSession
            from codol_rebuild.application.session.service import OnlineSession
            from codol_rebuild.adapters.transport.http import HttpGateway
            from codol_rebuild.adapters.transport.websocket import WebSocketConnection
            from codol_rebuild.adapters.persistence.server_identity import JsonIdentityStore
            from codol_rebuild.platform.machine_identity import fingerprint
            gateway = HttpGateway(args.server_url, args.server_timeout)
            from codol_rebuild.application.accounts.rename import RenameSession
            rename_session = RenameSession(
                HttpGateway(args.server_url, min(args.server_timeout, 5.0)))
            from codol_rebuild.application.chat.service import ChatSession
            from codol_rebuild.application.broadcast.service import BroadcastSession
            from codol_rebuild.application.realtime.dispatch import EventInbox, RealtimeDispatcher
            realtime_connection = WebSocketConnection(gateway.server, args.server_timeout)
            realtime_dispatcher = RealtimeDispatcher(realtime_connection)
            room_inbox, broadcast_inbox, chat_inbox = EventInbox(), EventInbox(), EventInbox()
            realtime_dispatcher.register(('chat_message',), chat_inbox)
            realtime_dispatcher.register(
                ('room_settings_updated', 'room_members_updated', 'room_kicked', 'room_closed'), room_inbox)
            realtime_dispatcher.register(('broadcast',), broadcast_inbox)
            broadcast_session = BroadcastSession(
                HttpGateway(args.server_url, args.server_timeout), broadcast_inbox,
                lambda: realtime_connection.status().value == 'connected')
            identity_store = JsonIdentityStore(args.server_identity)
            multiplayer = RoomSession(gateway, identity_store, gateway.server, inbox=room_inbox)
            if getattr(args, 'game_server_ip', None):
                from codol_rebuild.application.matches.service import MatchSession
                match_inbox = EventInbox()
                realtime_dispatcher.register(('room_start_updated', 'realtime_reconnected'), match_inbox)
                match_session = MatchSession(HttpGateway(args.server_url, 30), multiplayer.confirmed_room_id, match_inbox)
            online_session = OnlineSession(gateway, identity_store, gateway.server, fingerprint(),
                                           multiplayer, realtime_connection)
            chat_session = ChatSession(realtime_connection, chat_inbox, multiplayer.confirmed_room_id)
            record({'multiplayer_server': gateway.server})
        bridge=GameSessionBridge(args,run_dir,prepared['image'],prepared['pe'],log,record,phase_done,
                                 process,account_factory,(OSError,ValueError,sqlite3.Error), multiplayer=multiplayer,
                                 rename_session=rename_session, broadcast_session=broadcast_session,
                                 realtime_dispatcher=realtime_dispatcher, chat_session=chat_session, online_session=online_session,
                                 match_session=match_session)
        debugger=DebugLoop(process,bridge,prepared['pe'],FORBIDDEN)
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
                if online_session:
                    try:online_session.close()
                    except Exception as error:
                        log['multiplayer_cleanup_error']=str(error)
                if rename_session:
                    try:rename_session.close()
                    except Exception as error:
                        log['rename_cleanup_error']=str(error)
                if match_session:
                    try:match_session.close()
                    except Exception as error:
                        log['match_cleanup_error'] = str(error)
                if broadcast_session:
                    try:broadcast_session.close()
                    except Exception as error:
                        log['broadcast_cleanup_error'] = str(error)
                if runtime_lease is not None:
                    runtime_lease.close()
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

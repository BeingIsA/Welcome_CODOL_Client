"""房间用例编排：后台串行请求、身份保存和不确定写入恢复。"""
import threading
import logging
import time
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from codol_rebuild.application.room.contracts import (
    Completion, Gateway, IdentityRecord, IdentityStore, RoomError, RealtimeConnection, valid_room_id,
)


class RoomSession:
    """原生线程只提交与消费结果；身份、房间状态只在单个工作线程中改变。"""
    def __init__(self, gateway: Gateway, store: IdentityStore, server: str, fingerprint: str,
                 realtime: RealtimeConnection | None = None):
        self._gateway, self._store = gateway, store
        self._server, self._fingerprint = server, fingerprint
        self._record = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='codol-http')
        self._pending = None
        self._closed = False
        self._realtime = realtime
        self._is_owner = None
        # 设置同步与主流程互不阻塞，但共用单工作线程保证串行。
        self._settings_pending = None
        self._list_pending = None
        self._list_cancel = threading.Event()
        self._list_request = None
        self._members_pending = None
        self._kick_pending = None
        self._departure_pending = None
        self._team_pending = None

    def begin_change_team(self, room_id, team):
        """共用串行工作线程提交换队，未消费的请求不会重复发送。"""
        if (self._closed or self._pending is not None or self._team_pending is not None
                or self._departure_pending is not None or self._members_pending is not None
                or self._kick_pending is not None):
            return False
        self._team_pending = self._executor.submit(self._execute_change_team, room_id, team)
        return True

    def _execute_change_team(self, room_id, team):
        """成功读取成员档案供既有回显使用；失败不重试请求。"""
        try:
            record = self._record
            if record is None or record.room_id != room_id:
                raise RoomError('ROOM_NOT_FOUND', '当前房间已失效')
            data = self._gateway.change_team(record.player_id, room_id, team)
            self._load_profiles(data, record.player_id)
            return Completion('change_team', data=data)
        except RoomError as error:
            return Completion('change_team', error=error)
        except (KeyError, TypeError, ValueError) as error:
            return Completion('change_team', error=RoomError('INVALID_RESPONSE', str(error)))

    def poll_change_team(self):
        """一次性消费结果并释放通道，失败也允许后续用户操作。"""
        if self._team_pending is None or not self._team_pending.done():
            return None
        pending_team, self._team_pending = self._team_pending, None
        return pending_team.result()

    def begin_kick(self, room_id, target_player_id):
        """串行提交踢人；不在客户端提前移除成员，也不自动重试写请求。"""
        if self._closed or self._pending is not None or self._kick_pending is not None or self._departure_pending is not None or self._team_pending is not None:
            return False
        self._kick_pending = self._executor.submit(self._execute_kick, room_id, target_player_id)
        return True

    def _execute_kick(self, room_id, target_player_id):
        """提交菜单命令并加载返回成员资料，权限由服务端判断。"""
        try:
            record = self._record
            data = self._gateway.kick(record.player_id, room_id, target_player_id)
            self._load_profiles(data, record.player_id)
            return Completion('kick', data=data)
        except RoomError as error:
            return Completion('kick', error=error)
        except (KeyError, TypeError, ValueError) as error:
            return Completion('kick', error=RoomError('INVALID_RESPONSE', '踢人响应无效：'+str(error)))

    def poll_kick(self):
        """非阻塞消费一次踢人完成结果。"""
        if self._kick_pending is None or not self._kick_pending.done():
            return None
        pending, self._kick_pending = self._kick_pending, None
        return pending.result()

    def begin(self, operation: str, username: str, *, max_players: int = 4,
              password: str | None = None, settings: dict | None = None,
              room_id: str | None = None, profile: dict | None = None) -> bool:
        """提交单个用例，重复点击不会重复发送请求。

        设置同步与主流程（登录/创建/退出）互不阻塞：原生改设置在本地
        立即生效，这里只做异步补交，失败不影响房间状态。
        """
        if self._closed or self._departure_pending is not None:
            return False
        if operation == 'settings':
            if self._settings_pending is not None:
                return False
            self._settings_pending = self._executor.submit(
                self._execute_settings, username, settings)
            return True
        if self._pending is not None:
            return False
        if operation not in ('login', 'create', 'join', 'leave'):
            raise ValueError('未知联机用例')
        self._pending = self._executor.submit(self._execute, operation, username,
                                              max_players, password, settings, room_id, deepcopy(profile))
        return True

    def poll(self) -> Completion | None:
        """只在后台已完成时消费结果，不等待网络；主流程优先于设置同步。"""
        if self._pending is not None:
            if self._pending.done():
                pending, self._pending = self._pending, None
                return pending.result()
            return None
        if self._settings_pending is not None and self._settings_pending.done():
            pending, self._settings_pending = self._settings_pending, None
            return pending.result()
        return None

    def _save(self, record):
        # 即使磁盘写入失败，仍保留远端已生效的内存事实，供退出补偿使用。
        self._record = record
        self._store.save(self._server, record)

    def _clear_local_room_record(self):
        """清除自建的服务端房间记录；不发送请求，也不操作原生房间或界面。"""
        self._save(replace(self._record, room_id=None, uncertain_join=False,
                           uncertain_create=False))

    def poll_event(self) -> Completion | None:
        """统一消费实时消息，按类型交给对应业务处理函数。"""
        # 换队回包先消费，随后才读取事件快照，避免较旧回包覆盖较新成员列表。
        if self._closed or self._realtime is None or self._pending is not None or self._team_pending is not None:
            return None
        if self._departure_pending is not None:
            if not self._departure_pending.done():
                return None
            pending_departure, self._departure_pending = self._departure_pending, None
            return pending_departure.result()
        if self._members_pending is not None:
            if not self._members_pending.done():
                return None
            pending, self._members_pending = self._members_pending, None
            result = pending.result()
            if result.error is not None:
                return result
            if (result.data is not None and self._record is not None
                    and result.data['roomId'] == self._record.room_id):
                return result
            return None
        event = self._realtime.poll_event()
        if event is None:
            return None
        handlers = {'room_settings_updated': self._handle_room_settings_updated,
                    'room_members_updated': self._handle_room_members_updated}
        if event['type'] in ('room_kicked', 'room_closed'):
            return self._handle_room_departure(event['type'], event['payload'])
        handler = handlers.get(event['type'])
        return handler(event['payload']) if handler is not None else None

    def _handle_room_departure(self, event_type, payload):
        """服务端已结束本人的房间归属；只清理本地记录，不再次发送离房命令。"""
        if self._record is None or not self._record.room_id or self._record.room_id != payload.get('roomId'):
            return None
        self._departure_pending = self._executor.submit(
            self._complete_room_departure, event_type, payload['roomId'])
        return None

    def _complete_room_departure(self, event_type, room_id):
        """串行更新身份记录，持久化失败也不能保留已失效的房间展示。"""
        self._is_owner = None
        try:
            self._clear_local_room_record()
        except (OSError, ValueError):
            logging.getLogger(__name__).exception('被动离房记录保存失败')
        return Completion(event_type, data={'roomId': room_id})

    def _handle_room_members_updated(self, payload):
        """成员通知只触发后台权威快照查询，迟到通知不能直接改写本地成员。"""
        if self._record is not None and self._record.room_id == payload.get('roomId'):
            self._members_pending = self._executor.submit(self._refresh_members, self._record)
        return None

    def _refresh_members(self, record):
        """在业务工作线程读取成员与档案，原生适配只消费完成结果。"""
        try:
            data = self._gateway.room_detail(record.player_id, record.room_id)
            self._load_profiles(data, record.player_id)
            return Completion('room_members_updated', data=data)
        except RoomError as error:
            return Completion('room_members_updated', error=error)

    def _load_profiles(self, data, player_id):
        """按成员读取完整资料；原生缺失资料不能用其他玩家的数据代替。"""
        for member in data.get('members', []):
            member['profile'] = self._gateway.member_profile(
                player_id, data['roomId'], member['playerId'])

    def _handle_room_settings_updated(self, payload) -> Completion | None:
        """交付当前房间设置，离房后或其他房间的设置不再回显。"""
        if self._record is None or self._record.room_id != payload['roomId']:
            return None
        return Completion('room_settings_updated', data=payload)

    def begin_list(self, username: str) -> bool:
        """只保留最新刷新意图；与写用例分开消费，共用串行工作线程。"""
        if self._closed:
            return False
        self.cancel_list()
        self._list_request = username
        self._start_list()
        return True

    def _start_list(self):
        if self._list_pending is None and self._list_request is not None:
            username, self._list_request = self._list_request, None
            self._list_cancel = threading.Event()
            self._list_pending = self._executor.submit(
                self._execute_list, username, self._list_cancel)

    def cancel_list(self) -> None:
        """使在途列表失效；不取消建房、离房、设置或登录请求。"""
        self._list_cancel.set()
        self._list_request = None

    def poll_list(self) -> Completion | None:
        """旧批次完成后才启动最新请求，避免连续刷新堆积后台任务。"""
        if self._list_pending is None or not self._list_pending.done():
            return None
        result = self._list_pending.result()
        cancelled = self._list_cancel.is_set()
        self._list_pending = None
        self._start_list()
        if cancelled:
            return None if self._list_pending is not None else Completion('list_cancelled')
        return result

    def _execute_list(self, username, cancelled):
        """收齐分页再发布；拒绝循环游标和过大列表，不把截断结果当作完整列表。"""
        try:
            if cancelled.is_set():
                return Completion('list')
            if self._record is None:
                raise RoomError('NO_IDENTITY', '尚未完成启动登录')
            cursor, seen, rooms = None, set(), {}
            deadline = time.monotonic() + 20
            for _ in range(100):
                if cancelled.is_set():
                    return Completion('list')
                if time.monotonic() > deadline:
                    raise RoomError('LIST_TIMEOUT', '房间列表查询超时，请刷新重试')
                page = self._gateway.list_rooms(self._record.player_id, cursor)
                for room in page['rooms']:
                    rooms[room['roomId']] = room
                if len(rooms) > 1000:
                    raise RoomError('LIST_TOO_LARGE', '房间列表超过当前客户端容量')
                cursor = page['nextCursor']
                if cursor is None:
                    return Completion('list', data={'rooms': list(rooms.values())})
                if cursor in seen:
                    break
                seen.add(cursor)
            raise RoomError('INVALID_RESPONSE', '房间分页游标没有前进')
        except RoomError as error:
            return Completion('list', error=error)
        except (OSError, ValueError) as error:
            return Completion('list', error=RoomError('LOCAL_IDENTITY_ERROR', str(error)))

    def _login(self, username):
        if self._record is None:
            self._record = self._store.load(self._server)
        data = self._gateway.login(self._fingerprint, username,
                                   self._record.player_id if self._record else None)
        self._save(replace(self._record, player_id=data['playerId']) if self._record
                   else IdentityRecord(data['playerId']))
        if self._realtime is not None:
            self._realtime.start(self._record.player_id, self._fingerprint)

    def _recover_create(self):
        if not self._record.uncertain_create:
            return
        cursor, seen = None, set()
        for _ in range(100):
            page = self._gateway.list_rooms(self._record.player_id, cursor)
            owned = [room for room in page['rooms'] if room['ownerPlayerId'] == self._record.player_id]
            if owned:
                self._save(replace(self._record, room_id=owned[0]['roomId'], uncertain_create=False))
                return
            cursor = page['nextCursor']
            if cursor is None:
                self._save(replace(self._record, uncertain_create=False))
                return
            if cursor in seen:
                break
            seen.add(cursor)
        raise RoomError('RECOVERY_FAILED', '无法确认上次建房结果，请稍后重试')

    def _leave(self):
        self._recover_create()
        if self._record.room_id:
            try:
                if self._record.uncertain_join:
                    detail = self._gateway.room_detail(self._record.player_id, self._record.room_id)
                    members = detail.get('members')
                    if (detail.get('roomId') != self._record.room_id or not isinstance(members, list)
                            or not any(isinstance(m, dict) and m.get('playerId') == self._record.player_id
                                       for m in members)):
                        # 查询无成员不能证明在途 POST 未执行，保留记录供下次恢复。
                        raise RoomError('RECOVERY_PENDING', '上次入房结果尚未确认，请稍后重试', uncertain=True)
                self._gateway.leave(self._record.player_id, self._record.room_id)
            except RoomError as error:
                if error.code not in ('ROOM_NOT_FOUND', 'ROOM_CLOSED'):
                    raise
            self._clear_local_room_record()
        self._is_owner = None
        return {'left': True}

    def _join(self, room_id, username, password, profile=None):
        """记录目标后提交加入；响应丢失时查询成员确认，未确认保留退出恢复记录。"""
        if not valid_room_id(room_id):
            raise RoomError('INVALID_ROOM_ID', '请选择有效房间')
        if self._record.room_id and self._record.room_id != room_id:
            raise RoomError('ALREADY_IN_OTHER_ROOM', '请先退出当前房间')
        if self._record.uncertain_create:
            raise RoomError('RECOVERY_REQUIRED', '请先清理上次未确认的房间')
        previous = self._record
        self._save(replace(previous, room_id=room_id, uncertain_join=True))
        try:
            data = self._gateway.join(previous.player_id, room_id, username, password,
                                      **({'profile': profile} if profile is not None else {}))
        except RoomError as error:
            if not error.uncertain:
                self._save(previous)
                raise
            try:
                data = self._gateway.room_detail(previous.player_id, room_id)
            except RoomError:
                raise error
            if (data.get('roomId') != room_id or not isinstance(data.get('members'), list)
                    or not any(isinstance(m, dict) and m.get('playerId') == previous.player_id
                               for m in data['members'])):
                # 请求仍可能在服务端执行，不能因一次查询无成员就删除恢复记录。
                raise error
        self._save(replace(self._record, uncertain_join=False))
        self._is_owner = data.get('ownerPlayerId') == previous.player_id
        return data

    def _execute(self, operation, username, max_players, password, settings=None, room_id=None, profile=None):
        try:
            if operation == 'login':
                self._login(username)
            elif self._record is None:
                raise RoomError('NO_IDENTITY', '尚未完成启动登录')
            if operation == 'login':
                self._leave()  # 重启后清理上次未正常退出的房间。
                data = {'playerId': self._record.player_id, 'displayName': username}
            elif operation == 'leave':
                data = self._leave()
            elif operation == 'join':
                data = self._join(room_id, username, password, profile)
            else:
                if type(max_players) is not int or not 1 <= max_players <= 255:
                    raise RoomError('INVALID_MAX_PLAYERS', '房间人数无效')
                self._leave()
                self._save(replace(self._record, uncertain_create=True))
                try:
                    data = self._gateway.create(self._record.player_id, username,
                                                max_players, password, settings,
                                                **({'profile': profile} if profile is not None else {}))
                except RoomError as error:
                    if not error.uncertain:
                        self._save(replace(self._record, uncertain_create=False))
                    raise
                self._save(replace(self._record, room_id=data['roomId'], uncertain_create=False))
                self._is_owner = True
            if operation in ('create', 'join'):
                self._load_profiles(data, self._record.player_id)
            return Completion(operation, data=data)
        except RoomError as error:
            return Completion(operation, error=error)
        except (OSError, ValueError) as error:
            return Completion(operation, error=RoomError('LOCAL_IDENTITY_ERROR', str(error)))

    def _execute_settings(self, username, settings):
        """异步提交房内设置变更；每次意图只发送一次，不自动重试写入。"""
        try:
            if self._record is None:
                raise RoomError('NO_IDENTITY', '尚未完成启动登录')
            if self._is_owner is False:
                raise RoomError('FORBIDDEN', '只有房主可以修改房间设置')
            if settings is None or not isinstance(settings, dict) or not settings:
                raise RoomError('INVALID_SETTINGS', '设置数据无效')
            room_id = self._record.room_id if self._record else None
            if not room_id:
                raise RoomError('NO_ROOM', '当前不在房间内，设置未同步')
            data = self._gateway.update_settings(self._record.player_id, room_id, settings)
            return Completion('settings', data=data)
        except RoomError as error:
            return Completion('settings', error=error)
        except (OSError, ValueError) as error:
            return Completion('settings', error=RoomError('LOCAL_IDENTITY_ERROR', str(error)))

    def close(self) -> None:
        """进程结束后等待有限网络请求收尾；离房失败保留磁盘恢复标记。"""
        if self._closed:
            return
        self._closed = True
        self.cancel_list()
        def cleanup():
            if self._record is not None:
                self._leave()
        future = self._executor.submit(cleanup)
        try:
            future.result()
        finally:
            try:
                if self._realtime is not None:
                    self._realtime.close()
            finally:
                self._executor.shutdown(wait=True)

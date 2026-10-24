"""标准库 HTTP 玩家接口客户端；统一地址、超时、错误和响应契约检查。"""
import http.client
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from codol_rebuild.application.room.contracts import RoomError, valid_room_id


def normalize_server(value: str) -> str:
    """规范服务器根地址；保留 HTTPS 默认验证，不允许凭据或查询串。"""
    parts = urllib.parse.urlsplit(value.strip())
    if (parts.scheme not in ('http', 'https') or not parts.hostname or parts.username
            or parts.password or parts.query or parts.fragment or parts.path not in ('', '/')):
        raise ValueError('服务器地址须为 http(s)://主机[:端口]')
    port = parts.port
    host = parts.hostname.lower()
    if ':' in host:
        host = '[' + host + ']'
    suffix = f':{port}' if port and port != (443 if parts.scheme == 'https' else 80) else ''
    return f'{parts.scheme}://{host}{suffix}'


def _identifier(value, prefix):
    if prefix == 'rm' and valid_room_id(value):
        return value
    if not isinstance(value, str) or not re.fullmatch(prefix + r'_[A-Za-z0-9_-]{1,96}', value):
        raise ValueError('服务端身份或房间标识不符合契约')
    return value


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """写请求不自动重定向，以免改变方法或向其他地址发送身份。"""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HttpGateway:
    """不重试写请求；连接与响应异常显式标记结果不确定。"""
    def __init__(self, server, timeout=5.0):
        self.server = normalize_server(server)
        if not 0.1 <= timeout <= 30:
            raise ValueError('请求超时须为 0.1～30 秒')
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_NoRedirect())

    def _request(self, method, path, payload=None, player_id=None):
        headers = {'Accept': 'application/json', 'X-Request-Id': uuid.uuid4().hex}
        if player_id:
            headers['X-Player-Id'] = _identifier(player_id, 'pl')
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            headers['Content-Type'] = 'application/json; charset=utf-8'
        request = urllib.request.Request(self.server + path, body, headers, method=method)
        try:
            try:
                response = self._opener.open(request, timeout=self.timeout)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                raw = response.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise ValueError('接口响应过大')
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError('接口响应不是对象')
                if not 200 <= response.status < 300:
                    detail = data.get('error', {})
                    if not isinstance(detail, dict):
                        raise ValueError('接口错误响应不符合契约')
                    raise RoomError(str(detail.get('code', 'HTTP_ERROR')),
                                           str(detail.get('message', '服务端请求失败')),
                                           uncertain=response.status >= 500)
                if data.get('apiVersion') != '1' or not isinstance(data.get('data'), dict):
                    raise ValueError('接口版本或响应结构不符合契约')
                return data['data']
        except RoomError:
            raise
        except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException) as error:
            raise RoomError('NETWORK_ERROR', '无法完成服务端请求：' + str(error),
                                   uncertain=method == 'POST') from error

    def login(self, fingerprint, username, player_id):
        payload = {'fingerprint': fingerprint, 'username': username}
        if player_id:
            payload['playerId'] = player_id
        data = self._request('POST', '/api/v1/login', payload)
        try:
            _identifier(data.get('playerId'), 'pl')
            if not isinstance(data.get('displayName'), str) or type(data.get('firstLogin')) is not bool:
                raise ValueError('登录响应字段不完整')
            if player_id and data['playerId'] != player_id:
                raise ValueError('服务端返回了不同身份，拒绝覆盖原身份')
        except ValueError as error:
            raise RoomError('INVALID_RESPONSE', str(error), uncertain=True) from error
        return data

    def create(self, player_id, username, max_players, password, settings=None):
        payload = {'displayName': username, 'maxPlayers': max_players, 'password': password}
        if settings is not None:
            payload['settings'] = _settings_payload(settings)
        data = self._request('POST', '/api/v1/rooms', payload, player_id)
        try:
            _identifier(data.get('roomId'), 'rm')
            if (data.get('ownerPlayerId') != player_id or data.get('maxPlayers') != max_players
                    or not isinstance(data.get('members'), list)
                    or not any(isinstance(m, dict) and m.get('playerId') == player_id
                               and m.get('isOwner') is True for m in data['members'])):
                raise ValueError('房间响应与创建者或人数不匹配')
            if settings is not None and data.get('settings') != settings:
                raise ValueError('房间响应设置与提交值不匹配')
        except ValueError as error:
            raise RoomError('INVALID_RESPONSE', str(error), uncertain=True) from error
        return data

    def join(self, player_id, room_id, username, password):
        """只提交一次加入；响应损坏时保留可能已经入房的语义。"""
        room_id = _identifier(room_id, 'rm')
        data = self._request('POST', f'/api/v1/rooms/{room_id}/join',
                             {'displayName': username, 'password': password}, player_id)
        try:
            validate_room_snapshot(data, room_id)
            if not any(m['playerId'] == player_id for m in data['members']):
                raise ValueError('加入响应缺少当前玩家')
        except ValueError as error:
            raise RoomError('INVALID_RESPONSE', str(error), uncertain=True) from error
        return data

    def update_settings(self, player_id, room_id, settings):
        room_id = _identifier(room_id, 'rm')
        data = self._request('PATCH', f'/api/v1/rooms/{room_id}/settings',
                             {'settings': _settings_payload(settings)}, player_id)
        try:
            if data.get('settings') != _settings_payload(settings):
                raise ValueError('设置响应与提交值不匹配')
        except ValueError as error:
            raise RoomError('INVALID_RESPONSE', str(error), uncertain=True) from error
        return data

    def room_detail(self, player_id, room_id):
        room_id = _identifier(room_id, 'rm')
        data = self._request('GET', f'/api/v1/rooms/{room_id}', player_id=player_id)
        try:
            if data.get('roomId') != room_id:
                raise ValueError('房间详情响应不完整')
        except ValueError as error:
            raise RoomError('INVALID_RESPONSE', str(error)) from error
        return data

    def leave(self, player_id, room_id):
        room_id = _identifier(room_id, 'rm')
        data = self._request('POST', f'/api/v1/rooms/{room_id}/leave', {}, player_id)
        if type(data.get('left')) is not bool or type(data.get('roomClosed')) is not bool:
            raise RoomError('INVALID_RESPONSE', '离房响应字段不完整', uncertain=True)
        return data

    def list_rooms(self, player_id, cursor):
        query = urllib.parse.urlencode({'limit': 100, **({'cursor': cursor} if cursor else {})})
        data = self._request('GET', '/api/v1/rooms?' + query, player_id=player_id)
        try:
            if not isinstance(data.get('rooms'), list) or 'nextCursor' not in data:
                raise ValueError('房间列表响应不完整')
            if len(data['rooms']) > 100:
                raise ValueError('房间列表超出请求分页大小')
            if data['nextCursor'] is not None and not isinstance(data['nextCursor'], str):
                raise ValueError('房间游标无效')
            for room in data['rooms']:
                if not isinstance(room, dict):
                    raise ValueError('房间列表项无效')
                _identifier(room.get('roomId'), 'rm')
                _identifier(room.get('ownerPlayerId'), 'pl')
                for key, maximum in (('ownerDisplayName', 128), ('map', 256), ('mode', 256)):
                    value = room.get(key)
                    if (not isinstance(value, str) or len(value) > maximum
                            or '\0' in value):
                        raise ValueError('房间展示文本无效：' + key)
                if (type(room.get('maxPlayers')) is not int or not 1 <= room['maxPlayers'] <= 255
                        or type(room.get('playerCount')) is not int
                        or not 0 <= room['playerCount'] <= room['maxPlayers']
                        or type(room.get('hasPassword')) is not bool):
                    raise ValueError('房间人数或锁定状态无效')
        except ValueError as error:
            raise RoomError('INVALID_RESPONSE', str(error)) from error
        return data

def validate_room_snapshot(data, room_id):
    """校验入房快照，避免把不完整成员或错误房间发布给原生界面。"""
    if data.get('roomId') != room_id:
        raise ValueError('房间响应标识不匹配')
    owner = _identifier(data.get('ownerPlayerId'), 'pl')
    if (type(data.get('maxPlayers')) is not int or not 1 <= data['maxPlayers'] <= 255
            or type(data.get('playerCount')) is not int):
        raise ValueError('房间人数无效')
    members = data.get('members')
    if not isinstance(members, list) or not 1 <= len(members) <= data['maxPlayers']:
        raise ValueError('房间成员列表无效')
    ids = set()
    for member in members:
        if not isinstance(member, dict):
            raise ValueError('房间成员无效')
        identity = _identifier(member.get('playerId'), 'pl')
        if (identity in ids or type(member.get('isOwner')) is not bool
                or member['isOwner'] != (identity == owner)
                or not isinstance(member.get('displayName'), str)
                or '\0' in member['displayName'] or len(member['displayName']) > 128):
            raise ValueError('房间成员身份或昵称无效')
        ids.add(identity)
    if owner not in ids or data['playerCount'] != len(members):
        raise ValueError('房间成员数或房主不匹配')
    settings = _settings_payload(data.get('settings'))
    for key in ('map', 'mode'):
        if not isinstance(settings.get(key), str) or '\0' in settings[key]:
            raise ValueError('房间地图或模式无效')


def _settings_payload(settings):
    """校验设置快照外形并原样返回；键集与服务端契约一致。"""
    if not isinstance(settings, dict) or not settings or len(settings) > 64:
        raise ValueError('设置快照必须是1~64项的对象')
    for name, value in settings.items():
        if not isinstance(name, str):
            raise ValueError('设置字段名必须是文本')
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError(f'设置字段 {name} 的值类型无效')
    return settings

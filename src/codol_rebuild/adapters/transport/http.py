"""标准库 HTTP 玩家接口客户端；统一地址、超时、错误和响应契约检查。"""
from codol_rebuild.application.errors import OnlineError
import base64
import binascii
import http.client
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from codol_rebuild.application.room.contracts import valid_room_id

PLAYER_PROFILE_VERSION = 'codol-1649370'
PLAYER_PROFILE_SIZE = 32764


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


def _valid_inventory_item_id(item_id):
    return type(item_id) is int and 0 < item_id < 65535 and item_id not in (15400, 20000)


def validate_inventory(snapshot, *, uncertain=False):
    """完整仓库允许空清单，拒绝缺字段、重复物品和超出终版表示范围的数值。"""
    if not isinstance(snapshot, dict) or type(snapshot.get('revision')) is not int or snapshot['revision'] < 1:
        raise OnlineError('INVALID_RESPONSE', '服务器未返回有效的仓库版本', uncertain=uncertain)
    if any(type(snapshot.get(currency)) is not int or not 0 <= snapshot[currency] <= 2147483647
           for currency in ('gp', 'cp')):
        raise OnlineError('INVALID_RESPONSE', '服务器返回了无效余额', uncertain=uncertain)
    items = snapshot.get('items')
    if not isinstance(items, list):
        raise OnlineError('INVALID_RESPONSE', '服务器未返回完整物品清单', uncertain=uncertain)
    seen_ids = set()
    for item in items:
        if not isinstance(item, dict):
            raise OnlineError('INVALID_RESPONSE', '服务器返回了无效物品', uncertain=uncertain)
        item_id, quantity = item.get('itemId'), item.get('quantity')
        if (not _valid_inventory_item_id(item_id)
                or item_id in seen_ids or type(quantity) is not int or not 0 < quantity <= 2147483647):
            raise OnlineError('INVALID_RESPONSE', '服务器返回了无效的物品编号或数量', uncertain=uncertain)
        seen_ids.add(item_id)


def _validate_reward(reward):
    if not isinstance(reward, dict):
        raise OnlineError('INVALID_RESPONSE', '服务器未返回开箱奖励', uncertain=True)
    products = reward.get('product_ids')
    if (not isinstance(products, list) or not products
            or any(type(product_id) is not int or not 0 <= product_id <= 2147483647 for product_id in products)
            or (0 in products and products != [0])):
        raise OnlineError('INVALID_RESPONSE', '开箱回执的商品编号无效', uncertain=True)
    contents = reward.get('contents')
    if not isinstance(contents, list) or not contents:
        raise OnlineError('INVALID_RESPONSE', '开箱回执缺少奖励明细', uncertain=True)
    for content in contents:
        if not isinstance(content, dict):
            raise OnlineError('INVALID_RESPONSE', '奖励明细格式无效', uncertain=True)
        quantity = content.get('quantity')
        increase = content.get('quantity_delta')
        if (not _valid_inventory_item_id(content.get('item_id'))
                or type(quantity) is not int or not 1 <= quantity <= 2147483647
                or type(increase) is not int or not 0 <= increase <= quantity):
            raise OnlineError('INVALID_RESPONSE', '奖励物品编号或数量无效', uncertain=True)


def _validate_inventory_receipt(path, result, player_id, payload):
    if not isinstance(result, dict):
        raise OnlineError('INVALID_RESPONSE', '仓库事务回执必须是对象', uncertain=True)
    validate_inventory(result.get('inventory'), uncertain=True)
    if path == '/api/v1/players/me/supply-drop':
        _validate_reward(result.get('reward'))
    elif path == '/api/v1/players/me/rename':
        if (result.get('playerId') != player_id or not isinstance(result.get('displayName'), str)
                or result['displayName'] != payload['displayName']):
            raise OnlineError('INVALID_RESPONSE', '改名回执的身份或昵称与请求不符', uncertain=True)
    elif path == '/api/v1/broadcasts':
        if not isinstance(result.get('messageId'), str) or not result['messageId']:
            raise OnlineError('INVALID_RESPONSE', '喇叭回执缺少消息编号', uncertain=True)


class HttpGateway:
    """协议异常标明结果是否确定，仓库写入可凭同一操作编号重取回执。"""

    def send_broadcast(self, player_id: str, text: str, speaker_type: str) -> dict:
        """提交喇叭消耗与发送，广播展示由 WS 推送负责。"""
        return self._inventory_command('/api/v1/broadcasts', player_id,
                                       {'speakerType': speaker_type, 'text': text,
                                        'operationId': uuid.uuid4().hex})

    def kick(self, player_id, room_id, target_player_id):
        """调用玩家房主接口，不使用管理端接口。"""
        return self._request('POST', '/api/v1/rooms/'+_identifier(room_id, 'rm')+'/kick',
                             {'targetPlayerId': _identifier(target_player_id, 'pl')}, player_id)

    def change_team(self, player_id, room_id, team):
        """换队使用本人接口，由服务端校验目标名额。"""
        return self._request('PATCH', '/api/v1/rooms/'+_identifier(room_id, 'rm')+'/members/me/team',
                             {'team': team}, player_id)

    def start_match(self, player_id, room_id, banned_item_ids=()):
        """房主原生开局请求，结果资料由推送交付。"""
        return self._request('POST', '/api/v1/rooms/'+_identifier(room_id, 'rm')+'/start',
                             {'bannedItemIds': list(banned_item_ids)}, player_id)

    def broadcast_countdown(self, player_id, room_id, seconds):
        """转发原生房主倒计时命令。"""
        return self._request('POST', '/api/v1/rooms/'+_identifier(room_id, 'rm')+'/countdown',
                             {'seconds': seconds}, player_id)

    def change_ready(self, player_id, room_id, ready):
        """发送绝对准备状态，不在传输层切换状态或重试。"""
        return self._request('PATCH', '/api/v1/rooms/'+_identifier(room_id, 'rm')+'/members/me/ready',
                             {'ready': ready}, player_id)

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
        response_status = None
        try:
            try:
                response = self._opener.open(request, timeout=self.timeout)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                response_status = response.status
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
                    raise OnlineError(str(detail.get('code', 'HTTP_ERROR')),
                                           str(detail.get('message', '服务端请求失败')),
                                           uncertain=response.status >= 500)
                if data.get('apiVersion') != '1' or not isinstance(data.get('data'), dict):
                    raise ValueError('接口版本或响应结构不符合契约')
                return data['data']
        except OnlineError:
            raise
        except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException) as error:
            error_code = 'NETWORK_ERROR'
            detail = str(error)
            if path == '/api/v1/login':
                reason = error.reason if isinstance(error, urllib.error.URLError) else error
                if isinstance(reason, TimeoutError):
                    error_code = 'REQUEST_TIMEOUT'
                elif isinstance(error, ValueError) and response_status != 502:
                    error_code = 'INVALID_RESPONSE'
                if response_status is not None:
                    detail = f'HTTP {response_status}，{detail}'
            raise OnlineError(error_code, detail, uncertain=method in ('POST', 'PUT')) from error

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
            raise OnlineError('INVALID_RESPONSE', str(error), uncertain=True) from error
        return data

    def rename(self, player_id, display_name):
        """提交改名及扣卡，返回同一事务的确认结果。"""
        return self._inventory_command('/api/v1/players/me/rename', player_id,
                                       {'displayName': display_name, 'operationId': uuid.uuid4().hex})

    def inventory(self, player_id):
        """读取当前玩家的服务端仓库。"""
        snapshot = self._request('GET', '/api/v1/players/me/inventory', player_id=player_id)
        validate_inventory(snapshot)
        return snapshot

    def load_player_profile(self, player_id):
        """读取账号的配装和成长。版本零表示服务端尚无档案。"""
        result = self._request('GET', '/api/v1/players/me/profile', player_id=player_id)
        revision = result.get('revision')
        if type(revision) is not int or revision < 0 or 'profile' not in result:
            raise OnlineError('INVALID_RESPONSE', '服务器返回了无效的档案版本')
        profile = result['profile']
        if revision == 0 and profile is None:
            return 0, {}
        if (revision == 0 or not isinstance(profile, dict)
                or profile.get('contentVersion') != PLAYER_PROFILE_VERSION):
            raise OnlineError('INVALID_RESPONSE', '服务器返回的档案与客户端版本不匹配')
        records = {}
        for kind in ('public', 'secure'):
            encoded = profile.get(kind)
            if not isinstance(encoded, str) or len(encoded) != 4 * ((PLAYER_PROFILE_SIZE + 2) // 3):
                raise OnlineError('INVALID_RESPONSE', '服务器返回的档案不完整')
            try:
                records[kind] = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error) as error:
                raise OnlineError('INVALID_RESPONSE', '服务器返回的档案编码无效') from error
            if len(records[kind]) != PLAYER_PROFILE_SIZE:
                raise OnlineError('INVALID_RESPONSE', '服务器返回的档案长度无效')
        return revision, records

    def save_player_profile(self, player_id, expected_revision, blobs):
        """成对提交档案。回执丢失时使用相同版本和数据重取一次。"""
        profile = {kind: base64.b64encode(blobs[kind]).decode('ascii') for kind in ('public', 'secure')}
        payload = {'expectedRevision': expected_revision,
                   'profile': {**profile, 'contentVersion': PLAYER_PROFILE_VERSION}}
        for attempt in range(2):
            try:
                result = self._request('PUT', '/api/v1/players/me/profile', payload, player_id)
                revision = result.get('revision')
                if type(revision) is not int or revision != expected_revision + 1:
                    raise OnlineError('INVALID_RESPONSE', '服务器未确认所提交的档案版本', uncertain=True)
                return revision
            except OnlineError as error:
                if not error.uncertain or attempt == 1:
                    raise

    def purchase(self, player_id, sku_id, operation_id):
        return self._inventory_command('/api/v1/players/me/purchase', player_id,
                                       {'skuId': sku_id, 'operationId': operation_id})

    def open_supply_drop(self, player_id, crate_id, operation_id):
        return self._inventory_command('/api/v1/players/me/supply-drop', player_id,
                                       {'crateItemId': crate_id, 'operationId': operation_id})

    def _inventory_command(self, path, player_id, payload):
        """写请求回执丢失时使用原操作编号查询同一次成交。"""
        for attempt in range(2):
            try:
                result = self._request('POST', path, payload, player_id)
                _validate_inventory_receipt(path, result, player_id, payload)
                return result
            except OnlineError as error:
                if not error.uncertain or attempt == 1:
                    raise

    def create(self, player_id, username, max_players, password, settings=None, profile=None, *, room_name):
        payload = {'displayName': username, 'roomName': room_name, 'maxPlayers': max_players, 'password': password}
        if settings is not None:
            payload['settings'] = _settings_payload(settings)
        if profile is not None:
            payload['profile'] = profile
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
            raise OnlineError('INVALID_RESPONSE', str(error), uncertain=True) from error
        return data

    def join(self, player_id, room_id, username, password, profile=None):
        """只提交一次加入；响应损坏时保留可能已经入房的语义。"""
        room_id = _identifier(room_id, 'rm')
        data = self._request('POST', f'/api/v1/rooms/{room_id}/join',
                             {'displayName': username, 'password': password,
                              **({'profile': profile} if profile is not None else {})}, player_id)
        try:
            validate_room_snapshot(data, room_id)
            if not any(m['playerId'] == player_id for m in data['members']):
                raise ValueError('加入响应缺少当前玩家')
        except ValueError as error:
            raise OnlineError('INVALID_RESPONSE', str(error), uncertain=True) from error
        return data

    def update_settings(self, player_id, room_id, settings):
        room_id = _identifier(room_id, 'rm')
        data = self._request('PATCH', f'/api/v1/rooms/{room_id}/settings',
                             {'settings': _settings_payload(settings)}, player_id)
        try:
            if data.get('settings') != _settings_payload(settings):
                raise ValueError('设置响应与提交值不匹配')
        except ValueError as error:
            raise OnlineError('INVALID_RESPONSE', str(error), uncertain=True) from error
        return data

    def room_detail(self, player_id, room_id):
        room_id = _identifier(room_id, 'rm')
        data = self._request('GET', f'/api/v1/rooms/{room_id}', player_id=player_id)
        try:
            if data.get('roomId') != room_id:
                raise ValueError('房间详情响应不完整')
        except ValueError as error:
            raise OnlineError('INVALID_RESPONSE', str(error)) from error
        return data

    def member_profile(self, player_id, room_id, member_id):
        """读取同房玩家的完整档案；只解码传输外形，不访问原生进程。"""
        room_id = _identifier(room_id, 'rm')
        member_id = _identifier(member_id, 'pl')
        data = self._request('GET', f'/api/v1/rooms/{room_id}/members/{member_id}/profile',
                             player_id=player_id)
        if (data.get('roomId') != room_id or data.get('playerId') != member_id
                or 'profile' not in data):
            raise OnlineError('INVALID_RESPONSE', '成员档案响应身份不匹配')
        return data['profile']

    def update_profile(self, player_id, room_id, profile):
        """传送当前完整档案；确认身份不匹配视为结果未知。"""
        room_id = _identifier(room_id, 'rm')
        data = self._request('PUT', f'/api/v1/rooms/{room_id}/members/me/profile',
                             {'profile': profile}, player_id)
        if (data.get('roomId') != room_id or data.get('playerId') != player_id):
            raise OnlineError('INVALID_RESPONSE', '档案上传确认身份不匹配', uncertain=True)
        return data

    def leave(self, player_id, room_id):
        room_id = _identifier(room_id, 'rm')
        data = self._request('POST', f'/api/v1/rooms/{room_id}/leave', {}, player_id)
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
                for key, maximum in (('roomName', 255), ('ownerDisplayName', 128), ('map', 256), ('mode', 256)):
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
            raise OnlineError('INVALID_RESPONSE', str(error)) from error
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

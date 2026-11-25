"""1649370 登录结果适配：公共帧发起用例，沿既有门控和错误通知发布。"""
import hashlib
import struct
from codol_rebuild.application.session.contracts import SessionPort
from codol_rebuild.adapters.game.v1649370.native_state import checked_write, IDENTITY, LOGIN
from codol_rebuild.adapters.game.v1649370.accounts.player_name import username


LOGIN_FAILURE_MESSAGES = {
    'NETWORK_ERROR': '登录失败：暂时无法连接服务器，请稍后再试。',
    'REQUEST_TIMEOUT': '登录超时，请稍后再试。',
    'INVALID_RESPONSE': '登录失败：服务器返回了异常数据。',
}


class OnlineSessionBridge:
    """仅转换会话完成结果；身份和连接生命周期由应用用例负责。"""

    def __init__(self, process, port: SessionPort, state, connected, record, notify):
        self.process = process
        self.port = port
        self.state = state
        self.connected = connected
        self.notify = notify
        self.record = record
        self.started = False

    def poll(self):
        """公共主线程暂停点消费结果；失败投递原生提示且不自动重试。"""
        if not self.started:
            self.started = self.port.begin_login(username(self.process.read))
        result = self.port.poll_login()
        if result is None:
            return
        if result.error is not None:
            message = str(result.error)
            self.notify(LOGIN_FAILURE_MESSAGES.get(result.error.code, message))
            self.record({'multiplayer': 'login', 'success': False, 'error': message})
            return message
        identity = hashlib.sha256(result.data['playerId'].encode('utf-8')).digest()[:8]
        if identity == bytes(8):
            identity = b'\x01' + bytes(7)
        checked_write(self.process, self.state + IDENTITY, identity)
        self.connected(result.data['playerId'], result.data['displayName'])
        checked_write(self.process, self.state + LOGIN, struct.pack('<I', 1))
        self.record({'multiplayer': 'login', 'success': True})

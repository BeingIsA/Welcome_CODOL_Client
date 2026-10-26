"""1649370 登录结果适配：公共帧发起用例，沿既有门控和错误通知发布。"""
import hashlib
import struct
from codol_rebuild.application.session.contracts import SessionPort
from codol_rebuild.adapters.game.v1649370.native_state import checked_write, IDENTITY, LOGIN, MESSAGE, ACTION, POLL
from codol_rebuild.adapters.game.v1649370.frontend.notifications import display_message
from codol_rebuild.adapters.game.v1649370.accounts.player_name import username


class OnlineSessionBridge:
    """仅转换会话完成结果；身份和连接生命周期由应用用例负责。"""

    def __init__(self, process, port: SessionPort, state, connected, record):
        self.process = process
        self.port = port
        self.state = state
        self.connected = connected
        self.record = record
        self.started = False

    def poll(self):
        """公共主线程暂停点消费结果；失败保留原任务通知且不自动重试。"""
        if not self.started:
            self.started = self.port.begin_login(username(self.process.read))
        result = self.port.poll_login()
        if result is None:
            return
        if result.error is not None:
            message = display_message(result.error)
            message_bytes = message.encode('utf-8')[:510].decode('utf-8', errors='ignore').encode('utf-8') + b'\0'
            checked_write(self.process, self.state + MESSAGE, message_bytes)
            checked_write(self.process, self.state + ACTION, struct.pack('<I', 4))
            checked_write(self.process, self.state + POLL, struct.pack('<I', 1))
            self.record({'multiplayer': 'login', 'success': False, 'error': message})
            return
        identity = hashlib.sha256(result.data['playerId'].encode('utf-8')).digest()[:8]
        if identity == bytes(8):
            identity = b'\x01' + bytes(7)
        checked_write(self.process, self.state + IDENTITY, identity)
        self.connected(result.data['playerId'], result.data['displayName'])
        checked_write(self.process, self.state + LOGIN, struct.pack('<I', 1))
        self.record({'multiplayer': 'login', 'success': True})

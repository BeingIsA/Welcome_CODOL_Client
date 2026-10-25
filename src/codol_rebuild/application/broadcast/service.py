"""大小喇叭成功路径：HTTP 确认与 WS 消息保持独立。"""
from concurrent.futures import ThreadPoolExecutor
from codol_rebuild.application.broadcast.contracts import BroadcastGateway


class BroadcastSession:
    """使用登录结果中的身份，不承担登录、扣道具或失败恢复。"""

    def __init__(self, gateway: BroadcastGateway, messages, connection_available):
        self._gateway = gateway
        self._messages = messages
        self._connection_available = connection_available
        self.player_id = None
        self._pending = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='codol-broadcast')

    def available(self) -> bool:
        """身份由登录用例确认，连接可用性由装配时注入的查询提供。"""
        return self.player_id is not None and self._connection_available()

    def connected(self, player_id: str, display_name: str) -> None:
        """由登录完成通知交接已确认身份；昵称由服务端决定。"""
        self.player_id = player_id

    def begin(self, text: str, speaker_type: str) -> None:
        """提交单次请求；原生发送锁保证同一时刻只有一次发送。"""
        if self.player_id is None or self._pending is not None:
            raise ValueError('广播身份未就绪或前次发送尚未完成')
        self._pending = self._executor.submit(self._send, self.player_id, text, speaker_type)

    def _send(self, player_id: str, text: str, speaker_type: str) -> dict:
        """将请求类型随成功确认交回，HTTP 本身仅返回消息标识。"""
        confirmation = self._gateway.send_broadcast(player_id, text, speaker_type)
        return {**confirmation, 'speakerType': speaker_type}

    def poll_sent(self) -> dict | None:
        """只返回真实 HTTP 完成结果，不生成展示事件或伪造成功。"""
        if self._pending is None or not self._pending.done():
            return None
        completed, self._pending = self._pending, None
        return completed.result()

    def poll_message(self) -> dict | None:
        """展示只消费服务端大小喇叭推送。"""
        message = self._messages.poll_event()
        if message is None or message['payload']['speakerType'] not in ('large', 'small'):
            return None
        return message['payload']

    def close(self) -> None:
        """等待已有的有界 HTTP 请求结束，不重发。"""
        self._executor.shutdown(wait=True)

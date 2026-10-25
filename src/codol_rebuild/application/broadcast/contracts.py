"""广播应用端口：发送确认与推送展示分别交接。"""
from typing import Protocol


class BroadcastGateway(Protocol):
    """复用已认证身份发送大喇叭的通信端口。"""

    def send_broadcast(self, player_id: str, text: str) -> dict:
        """提交一次大喇叭，返回服务端消息标识，不重试。"""
        ...


class BroadcastPort(Protocol):
    """游戏适配使用的成功路径接口。"""

    def available(self) -> bool:
        """返回身份及推送连接是否就绪，供原生入口状态查询。"""
        ...

    def connected(self, player_id: str, display_name: str) -> None:
        """交接已确认的登录身份。"""
        ...

    def begin(self, text: str) -> None:
        """在后台提交正文，不在游戏线程等待请求。"""
        ...

    def poll_sent(self) -> dict | None:
        """消费发送确认；该确认不得插入聊天消息。"""
        ...

    def poll_message(self) -> dict | None:
        """消费一条服务端广播，用于原生展示。"""
        ...

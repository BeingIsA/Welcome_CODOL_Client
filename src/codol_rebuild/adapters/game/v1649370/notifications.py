"""固定版本原生单按钮通知：由调用方在主线程排队，前端帧负责发布。"""
import struct


class NativeNotifications:
    """只拥有通知文本及发布标记，不解释房间事件或菜单命令。"""

    def __init__(self, process):
        self.process = process
        self.buffer = None

    def install(self):
        """复用已确认的 cppShowNotification 包装入口，不修改其实现。"""
        if self.process.read(0x730920, 6) != bytes.fromhex('558bec83e4f8'):
            raise ValueError('原生通知 ABI 不匹配')
        self.buffer = self.process.allocate(4096)
        self.process.write(self.buffer, bytes(4096))
        publisher = self.process.allocate(4096)
        code = bytearray.fromhex('9c60 8bec 833d')
        code.extend(struct.pack('<I', self.buffer))
        code.extend(bytes.fromhex('00 7420 c705'))
        code.extend(struct.pack('<II', self.buffer, 0))
        code.extend(bytes.fromhex('83e4f0 b9'))
        code.extend(struct.pack('<I', self.buffer+128))
        code.append(0xe8)
        code.extend(struct.pack('<I', (0x730920-publisher-len(code)-4) & 0xffffffff))
        code.extend(bytes.fromhex('8be5 619d c3'))
        code[12] = len(code)-5-13
        self.process.write(publisher, bytes(code))
        self.process.executable(publisher, 4096)
        return publisher

    def message(self, text):
        """主线程排队一次通知；原生确认按钮沿用原有关闭行为。"""
        encoded_text = text.encode('utf-8')[:1000].decode('utf-8', errors='ignore').encode('utf-8')+b'\0'
        self.process.write(self.buffer+128, encoded_text)
        self.process.write(self.buffer, struct.pack('<I', 1))

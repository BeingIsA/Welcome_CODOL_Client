"""原生通知编码回归；内存替身不代表游戏界面验收。"""
import struct
import unittest
from unittest.mock import Mock

from codol_rebuild.adapters.game.v1649370.frontend.notifications import NativeNotifications


class NativeNotificationTests(unittest.TestCase):
    def make_notifications(self, language_value):
        """仅提供原生通知转换路径读取的语言对象。"""
        process = Mock()
        memory_words = {0x7ACE410: 0x25000000, 0x2500000C: language_value}
        process.read.side_effect = lambda address, length: struct.pack('<I', memory_words[address])
        notifications = NativeNotifications(process)
        notifications.buffer = 0x26000000
        return notifications, process

    def test_departure_messages_match_native_language_decoder(self):
        for language_value, encoding in ((0, 'utf-8'), (1, 'gbk')):
            for message in ('你已被移出房间。', '管理员已关闭房间。'):
                with self.subTest(language=language_value, message=message):
                    notifications, process = self.make_notifications(language_value)
                    notifications.message(message)
                    text_call, publish_call = process.write.call_args_list
                    self.assertEqual(text_call.args[0], notifications.buffer+128)
                    self.assertEqual(text_call.args[1][:-1].decode(encoding), message)
                    self.assertEqual(text_call.args[1][-1:], b'\0')
                    self.assertEqual(publish_call.args, (notifications.buffer, struct.pack('<I', 1)))

    def test_truncation_keeps_complete_characters_and_terminator(self):
        for language_value, encoding, expected in ((0, 'utf-8', 'A'+'中'*333),
                                                    (1, 'gbk', 'A'+'中'*499)):
            with self.subTest(language=language_value):
                notifications, process = self.make_notifications(language_value)
                notifications.message('A'+'中'*600)
                encoded_text = process.write.call_args_list[0].args[1]
                self.assertLessEqual(len(encoded_text), 1001)
                self.assertEqual(encoded_text[-1:], b'\0')
                self.assertEqual(encoded_text[:-1].decode(encoding), expected)

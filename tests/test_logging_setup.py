"""宿主日志回归：异常、轮转、会话隔离和重复初始化。"""
import logging
from pathlib import Path
import tempfile
import unittest

from codol_rebuild.bootstrap.logging_setup import session_logging


class SessionLoggingTests(unittest.TestCase):
    def test_exception_and_repeated_initialization(self):
        with tempfile.TemporaryDirectory() as directory:
            with session_logging(directory):
                with session_logging(directory):
                    logger = logging.getLogger('codol_rebuild.application.logging_test')
                    logger.info('唯一事件')
                    try:
                        raise ValueError('异常证据')
                    except ValueError:
                        logger.exception('操作失败')
            contents = (Path(directory) / 'client.log').read_text(encoding='utf-8')
            self.assertEqual(contents.count('唯一事件'), 1)
            self.assertIn('ValueError: 异常证据', contents)
            self.assertIn('Traceback', contents)
            self.assertIn('codol_rebuild.application.logging_test', contents)
            self.assertIn('MainThread', contents)

    def test_rotation_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            with session_logging(directory, max_bytes=160, backup_count=2):
                for event_number in range(20):
                    logging.getLogger('codol_rebuild.runtime').info('事件 %s %s', event_number, '内容' * 20)
            self.assertEqual(len(list(Path(directory).glob('client.log*'))), 3)
            self.assertIn('事件 19', (Path(directory) / 'client.log').read_text(encoding='utf-8'))

    def test_sequential_sessions_restore_configuration(self):
        package_logger = logging.getLogger('codol_rebuild')
        previous_handlers = list(package_logger.handlers)
        previous_level = package_logger.level
        with tempfile.TemporaryDirectory() as first_directory, tempfile.TemporaryDirectory() as second_directory:
            with self.assertRaisesRegex(RuntimeError, '退出'):
                with session_logging(first_directory):
                    package_logger.info('第一会话')
                    raise RuntimeError('退出')
            with session_logging(second_directory):
                package_logger.info('第二会话')
                package_logger.debug('默认隐藏')
            first_contents = (Path(first_directory) / 'client.log').read_text(encoding='utf-8')
            second_contents = (Path(second_directory) / 'client.log').read_text(encoding='utf-8')
            self.assertNotIn('第二会话', first_contents)
            self.assertNotIn('第一会话', second_contents)
            self.assertNotIn('默认隐藏', second_contents)
        self.assertEqual(package_logger.handlers, previous_handlers)
        self.assertEqual(package_logger.level, previous_level)

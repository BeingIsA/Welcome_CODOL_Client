"""宿主日志装配：管理单次会话的文件输出，不参与业务和原生事件记录。"""
from contextlib import contextmanager
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


@contextmanager
def session_logging(run_directory, *, max_bytes=5 * 1024 * 1024, backup_count=3):
    """为宿主模块提供 INFO 文件日志；同一会话重复进入时复用处理器。"""
    package_logger = logging.getLogger('codol_rebuild')
    log_path = (Path(run_directory) / 'client.log').resolve()
    for existing_handler in package_logger.handlers:
        if isinstance(existing_handler, RotatingFileHandler) and existing_handler.baseFilename == str(log_path):
            yield
            return
    file_handler = RotatingFileHandler(
        log_path, maxBytes=max_bytes, backupCount=backup_count, encoding='utf-8')
    file_handler.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)s %(name)s [%(threadName)s] %(message)s'))
    previous_level = package_logger.level
    package_logger.setLevel(logging.INFO)
    package_logger.addHandler(file_handler)
    try:
        yield
    finally:
        package_logger.removeHandler(file_handler)
        file_handler.close()
        package_logger.setLevel(previous_level)

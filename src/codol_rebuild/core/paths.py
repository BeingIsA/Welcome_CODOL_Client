"""统一项目路径锚点：模块移动不改变账号、资源、运行日志的磁盘位置。

账号、设置与服务器身份等持久用户数据统一存放在系统用户目录（%APPDATA%/CODOL/），
与程序目录解耦；程序目录仅保留可重建的固定资源、缓存与会话日志。
"""
import os
from pathlib import Path

REBUILD_ROOT = Path(__file__).resolve().parents[3]
PROJECT_ROOT = REBUILD_ROOT.parent

# 持久用户数据根目录：Windows 下取 %APPDATA%（漫游）下的 CODOL 子目录。
USER_DATA_ROOT = Path(os.environ['APPDATA']) / 'CODOL'


def accounts_dir() -> Path:
    """返回持久账号/设置/身份数据目录，并在不存在时自动创建。

    目录为 %APPDATA%/CODOL/data/accounts；parents=True 保证多级目录一次建齐，
    exist_ok=True 保证目录已存在时静默返回，不会因并发或重复调用报错。
    """
    directory = USER_DATA_ROOT / 'data' / 'accounts'
    directory.mkdir(parents=True, exist_ok=True)
    return directory

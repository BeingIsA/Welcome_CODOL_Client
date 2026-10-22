"""统一项目路径锚点：模块移动不改变账号、资源、运行日志的磁盘位置。"""
from pathlib import Path

REBUILD_ROOT = Path(__file__).resolve().parents[3]
PROJECT_ROOT = REBUILD_ROOT.parent

"""项目内直接运行的入口；无需安装包，也不修改系统 Python 搜索路径。"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from codol_rebuild.cli import main

if __name__ == "__main__":
    raise SystemExit(main())

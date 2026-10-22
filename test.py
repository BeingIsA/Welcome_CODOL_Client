"""在项目专用临时目录运行测试，输出可归档结果；不启动游戏或服务。"""
import argparse
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest


def main() -> int:
    """发现本项目测试并归档结果，不覆盖已保存的验收记录。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="可选结果 JSON；必须是新路径")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    temporary = (root.parent / "verification" / "client" / "workspaces" / "test-temp").resolve()
    if not temporary.is_relative_to(root.parent):
        raise RuntimeError("测试临时目录必须位于项目内")
    if args.output and args.output.exists():
        parser.error("输出已经存在，请选择新证据文件")
    temporary.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(temporary)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(root / "src"))
    suite = unittest.defaultTestLoader.discover(str(root / "tests"), pattern="test_*.py")
    captured = io.StringIO()
    result = unittest.TextTestRunner(stream=captured, verbosity=2).run(suite)
    print(captured.getvalue(), end="")
    payload = {"status": "passed" if result.wasSuccessful() else "failed",
               "tests_run": result.testsRun, "failures": len(result.failures),
               "errors": len(result.errors), "skipped": len(result.skipped),
               "scope": "unit tests; does not establish client launch or gameplay",
               "log": captured.getvalue()}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())

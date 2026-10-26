"""构建完整原生资源树；默认只生成清单和空间核算，--build 才发布文件。"""
import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / 'client/src'))
from codol_rebuild.resources.native_library import build_library


def main():
    """固定输入输出在本项目，报告构建进度及全部格式错误。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build', action='store_true', help='校验并发布全部原生资源')
    options = parser.parse_args()
    try:
        report = build_library(PROJECT_ROOT, publish=options.build,
                               progress=lambda value: print(json.dumps(value, ensure_ascii=False), flush=True))
        return 0 if not options.build or report['summary']['completed'] else 1
    except (OSError, ValueError, KeyError) as error:
        print(json.dumps({'error': str(error)}, ensure_ascii=False), flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

"""版本预检和独立副本的命令行入口；运行参数不触发游戏或驱动。"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path

from codol_rebuild.core.baseline import BaselineError, load_manifest, save_new_json, stage_inputs, verify_inputs

from codol_rebuild.core.paths import REBUILD_ROOT


def main(argv: list[str] | None = None) -> int:
    """执行 check 或 stage，保持机器可读输出与明确的失败退出码。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    topic_tools = {"fastfile": "resources.fastfile", "bootstrap": "analysis.pe_recovery", "assets": "resources.assets"}
    if argv and argv[0] in topic_tools:
        # 专题工具保留各自参数与失败阶段，统一入口仅负责显式分派。
        module = importlib.import_module("." + topic_tools[argv[0]], __package__)
        try:
            return module.main(argv[1:])
        except (OSError, ValueError, KeyError, TypeError) as error:
            print(json.dumps({"status": "error", "tool": argv[0], "error": str(error)}, ensure_ascii=False), file=sys.stderr)
            return 2
    parser = argparse.ArgumentParser(description="CODOL 1649370 版本预检与独立分析副本")
    parser.add_argument("--client", type=Path, default=REBUILD_ROOT.parent / "Original Client")
    parser.add_argument("--manifest", type=Path, default=REBUILD_ROOT / "manifests/build1649370.json")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="校验关键输入版本，不执行游戏")
    check.add_argument("--output", type=Path, help="可选的新证据文件，禁止覆盖")
    stage = commands.add_parser("stage", help="创建关键文件的独立分析副本")
    stage.add_argument("--destination", type=Path, required=True)
    commands.add_parser("fastfile", help="Fastfile 检查与严格解码；子命令后加 --help")
    commands.add_parser("bootstrap", help="LZ4/AP32 恢复与静态 PE 分析；子命令后加 --help")
    commands.add_parser("assets", help="Zone 中 RawFile 与网络常量表解析；子命令后加 --help")
    args = parser.parse_args(argv)
    if args.command in topic_tools:
        parser.error("专题工具名必须位于第一个参数，例如 run.py fastfile --help")
    try:
        manifest = load_manifest(args.manifest)
        if args.command == "check":
            result = verify_inputs(args.client, manifest)
            if args.output:
                output = args.output.resolve()
                if output.is_relative_to(args.client.resolve()):
                    raise BaselineError("证据不能写入原客户端目录")
                save_new_json(output, result)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["ready"] else 2
        result = stage_inputs(args.client, args.destination, manifest)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

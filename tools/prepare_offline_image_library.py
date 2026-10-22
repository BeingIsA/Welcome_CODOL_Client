#!/usr/bin/env python3
"""一次构建所有已固定新版 IWI；--plan-only 仅统计空间与候选。"""
from pathlib import Path
import argparse
import json
import sys
PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(PROJECT/'client/src'))
from codol_rebuild.resources.image_library import build_library

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan-only',action='store_true')
    args=parser.parse_args()
    try:
        result=build_library(PROJECT,plan_only=args.plan_only,
            progress=lambda item:print(json.dumps(item,ensure_ascii=False),flush=True))
        if args.plan_only: return 0 if result['space_sufficient'] else 2
        summary=result['summary'];print(json.dumps(summary,ensure_ascii=False),flush=True)
        return 2 if summary['error'] else 3 if summary['conflict'] else 0
    except (OSError,ValueError,KeyError,TypeError) as exc:
        print(json.dumps({'status':'error','reason':str(exc)},ensure_ascii=False),file=sys.stderr)
        return 2

if __name__=='__main__':raise SystemExit(main())

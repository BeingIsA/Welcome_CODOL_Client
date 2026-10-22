#!/usr/bin/env python3
"""一次准备同33启动包的严格类型限定非IWI数据资源库。"""
from pathlib import Path
import argparse,json,sys
PROJECT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(PROJECT/'client/src'))
from codol_rebuild.resources.loose_library import prepare_loose_library

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--plan-only',action='store_true');a=p.parse_args()
    try:
        result=prepare_loose_library(PROJECT,plan_only=a.plan_only,progress=lambda row:print(json.dumps(row,ensure_ascii=False),flush=True))
        if a.plan_only:return 0 if result['space_sufficient'] else 2
        summary=result['summary'];print(json.dumps(summary,ensure_ascii=False),flush=True)
        return 2 if summary['error'] else 3 if summary['conflict'] else 0
    except (OSError,ValueError,KeyError,TypeError) as error:
        print(json.dumps({'status':'error','reason':str(error)},ensure_ascii=False),file=sys.stderr);return 2

if __name__=='__main__':raise SystemExit(main())

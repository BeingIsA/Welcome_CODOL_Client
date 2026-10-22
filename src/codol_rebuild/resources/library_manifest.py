# SPDX-License-Identifier: AGPL-3.0-or-later
"""素材库清单完整性校验：版本绑定、完成状态与计数、字节数闭环的统一规则。"""
from __future__ import annotations

EXE_SHA = '36997674b18f2896de5e9d755686f6922af10708f355175714058d0a0172b537'


def validate_library_manifest(manifest: dict, library_kind: str) -> None:
    """校验已生成的素材库清单可安全用于批量启动；规则与生成端计数闭环一致。

    library_kind 传入 'iwi' 表示图片库，其余值须与清单内 library_kind 一致。
    校验失败抛出 ValueError，调用方不得继续挂载该库。
    """
    if manifest.get('schema') != 1 or manifest.get('source_exe_sha256') != EXE_SHA or manifest.get('source_resource_version') != 461 or (manifest.get('old_version_resources_used') is not False) or (manifest.get('summary', {}).get('completed') is not True):
        raise ValueError('资源库清单版本不符或尚未完成')
    if library_kind != 'iwi' and manifest.get('library_kind') != library_kind:
        raise ValueError('松散资源库类型不符')
    if not isinstance(manifest.get('issues'), list) or manifest['issues']:
        raise ValueError('资源库仍有未解决的冲突或校验错误，拒绝不完整批量启动')
    if not isinstance(manifest.get('resources'), list) or not manifest['resources']:
        raise ValueError('资源库清单没有有效条目')
    summary = manifest['summary']
    count = len(manifest['resources'])
    count_fields = ('total', 'processed', 'published', 'already_present', 'conflict', 'error', 'validated_output_bytes', 'expected_output_bytes')
    if any((type(summary.get(key)) is not int or summary[key] < 0 for key in count_fields)):
        raise ValueError('资源库汇总计数无效')
    if summary.get('status') != 'complete' or summary['total'] != count or summary['processed'] != count or (summary['published'] + summary['already_present'] != count) or (summary['conflict'] != 0) or (summary['error'] != 0):
        raise ValueError('资源库清单与批量完成计数不符')
    if any((not isinstance(entry, dict) or type(entry.get('size')) is not int or entry['size'] <= 0 for entry in manifest['resources'])):
        raise ValueError('资源库文件记录无效')
    expected_bytes = sum((entry['size'] for entry in manifest['resources']))
    if summary['validated_output_bytes'] != expected_bytes or summary['expected_output_bytes'] != expected_bytes:
        raise ValueError('资源库清单与批量完成字节数不符')

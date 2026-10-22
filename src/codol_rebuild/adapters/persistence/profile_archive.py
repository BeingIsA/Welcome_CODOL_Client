"""单份档案归档：JSON 校验、原子替换与备份回退；不负责生成解锁策略。

历史分析工具使用此格式，正式账号的双档案事务使用 sqlite_accounts。
"""
from __future__ import annotations
import base64
import hashlib
import json
import os
import time
from pathlib import Path
from codol_rebuild.application.accounts.contracts import ProfileArchiveFormat, ProfileFormatError

class ProfileStore:
    """本地档案 JSON 存档：base64 blob + sha256 + 原子替换 + .previous 备份。

    schema 身份参数化（不写死旧版常量）。读档主档→备份回退；坏档不静默覆盖，
    保存前先滚动备份旧档，再临时文件 + os.replace 原子落盘。
    """

    FORMAT = 1

    def __init__(self, path: str | Path, format: ProfileArchiveFormat):
        self.path = Path(path)
        self.format = format
        self.identity = format.identity

    def _row(self, blob: bytes, run: str | None) -> dict:
        return {'format': self.FORMAT, 'schema_version': self.identity['version'],
                'schema_checksum': self.identity['checksum'], 'schema_size': self.identity['size'],
                'size': len(blob), 'sha256': hashlib.sha256(blob).hexdigest(),
                'saved_at': time.time(), 'run': run,
                'profile_base64': base64.b64encode(blob).decode('ascii')}

    def _decode(self, path: Path) -> bytes:
        row = json.loads(path.read_text(encoding='utf-8'))
        if row.get('format') != self.FORMAT or row.get('schema_version') != self.identity['version'] \
                or row.get('schema_checksum') != self.identity['checksum'] or row.get('schema_size') != self.identity['size']:
            raise ProfileFormatError(f'存档 schema 身份不符：{path}')
        blob = base64.b64decode(row['profile_base64'], validate=True)
        if hashlib.sha256(blob).hexdigest() != row['sha256']:
            raise ProfileFormatError(f'存档校验和不符：{path}')
        self.format.validate(blob)
        return blob

    def load(self) -> tuple:
        """主档→备份回退读取；都无效则抛错并带上全部原因，绝不覆盖坏档。"""
        errors = []
        for candidate in (self.path, self.path.with_suffix('.previous.json')):
            if not candidate.exists():
                continue
            try:
                blob = self._decode(candidate)
            except (KeyError, TypeError, ValueError) as exc:
                errors.append(f'{candidate.name}: {exc}')
                continue
            return blob, {'path': str(candidate), 'recovered_from_backup': candidate != self.path,
                          'sha256': hashlib.sha256(blob).hexdigest(), 'ignored_errors': errors}
        if errors:
            raise ProfileFormatError('没有可用存档：' + '; '.join(errors))
        raise ProfileFormatError('存档不存在：' + str(self.path))

    def save(self, blob: bytes, run: str | None = None) -> dict:
        """原子保存：先滚动备份能通过校验的旧档，再临时文件 + os.replace 落盘。"""
        self.format.validate(blob)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                self._decode(self.path)
            except (KeyError, TypeError, ValueError):
                pass
            else:
                previous = self.path.with_suffix('.previous.json')
                pending = previous.with_suffix('.tmp')
                pending.write_bytes(self.path.read_bytes())
                os.replace(pending, previous)
        row = self._row(blob, run)
        pending = self.path.with_suffix('.tmp')
        with pending.open('w', encoding='utf-8') as stream:
            json.dump(row, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, self.path)
        return {'path': str(self.path), 'sha256': row['sha256'], 'saved_at': row['saved_at']}

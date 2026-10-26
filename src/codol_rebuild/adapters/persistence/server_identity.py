"""按服务器地址保存明文身份；原子替换且拒绝覆盖损坏文件。"""
from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import tempfile
from codol_rebuild.application.room.contracts import valid_room_id
from codol_rebuild.application.session.contracts import IdentityRecord


class JsonIdentityStore:
    """身份文件不与本地背包数据库耦合；一次客户端运行使用一个写入者。"""
    def __init__(self, path: Path):
        self.path = Path(path)

    def _read(self):
        if not self.path.exists():
            return {'version': 1, 'servers': {}}
        data = json.loads(self.path.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('servers'), dict):
            raise ValueError('服务器身份文件损坏，请保留文件并检查，不能自动换号')
        return data

    def load(self, server):
        row = self._read()['servers'].get(server)
        if row is None:
            return None
        if (not isinstance(row, dict) or not isinstance(row.get('player_id'), str)
                or not re.fullmatch(r'pl_[A-Za-z0-9_-]{1,96}', row['player_id'])
                or type(row.get('uncertain_create', False)) is not bool
                or type(row.get('uncertain_join', False)) is not bool
                or (row.get('room_id') is not None and
                    not valid_room_id(row['room_id']))):
            raise ValueError('保存的服务器身份记录无效')
        return IdentityRecord(row['player_id'], row.get('room_id'), row.get('uncertain_create', False),
                              row.get('uncertain_join', False))

    def save(self, server, record):
        data = self._read()
        data['servers'][server] = asdict(record)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=self.path.name + '.', suffix='.tmp', dir=self.path.parent)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

"""本机只保存一个玩家身份，房间恢复信息按服务器单独管理。"""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from codol_rebuild.application.room.contracts import valid_room_id
from codol_rebuild.application.session.contracts import IdentityRecord


class JsonIdentityStore:
    """一次客户端运行使用一个写入者。"""
    def __init__(self, path: Path):
        self.path = Path(path)
        self.recovery_path = self.path.with_name(self.path.name + '.rooms.json')

    @staticmethod
    def _server_key(server):
        return hashlib.sha256(server.encode('utf-8')).hexdigest()

    @staticmethod
    def _record(row):
        if (not isinstance(row, dict) or not isinstance(row.get('player_id'), str)
                or not re.fullmatch(r'pl_[A-Za-z0-9_-]{1,96}', row['player_id'])
                or type(row.get('uncertain_create', False)) is not bool
                or type(row.get('uncertain_join', False)) is not bool
                or (row.get('room_id') is not None and
                    not valid_room_id(row['room_id']))):
            raise ValueError('保存的服务器身份记录无效')
        return IdentityRecord(row['player_id'], row.get('room_id'), row.get('uncertain_create', False),
                              row.get('uncertain_join', False))

    @staticmethod
    def _room_fields(record):
        return {field: getattr(record, field) for field in ('room_id', 'uncertain_create', 'uncertain_join')
                if getattr(record, field)}

    def _recovery(self):
        if not self.recovery_path.exists():
            return {}
        recovery = json.loads(self.recovery_path.read_text(encoding='utf-8'))
        if not isinstance(recovery, dict):
            raise ValueError('房间恢复文件损坏')
        return recovery

    def load(self, server):
        if not self.path.exists():
            return None
        data = json.loads(self.path.read_text(encoding='utf-8'))
        identity = self._record(data)
        fields = self._recovery().get(self._server_key(server), {})
        return self._record({**fields, 'player_id': identity.player_id})

    def save(self, server, record):
        existing = self.load(server)
        if existing and existing.player_id != record.player_id:
            raise ValueError('保存的玩家身份与登录结果不一致')
        if existing is None and self.recovery_path.exists():
            self.recovery_path.unlink()
        recovery = self._recovery()
        server_key = self._server_key(server)
        fields = self._room_fields(record)
        if fields != recovery.get(server_key, {}):
            if fields:
                recovery[server_key] = fields
            else:
                recovery.pop(server_key)
            self._write(self.recovery_path, recovery)
        if existing is None:
            self._write(self.path, {'player_id': record.player_id})

    @staticmethod
    def _write(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

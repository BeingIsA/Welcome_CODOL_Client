"""账号持久化与事务一致性；本模块负责 sqlite_accounts。

Transactional account storage shared by local bootstrap and future services.

No client addresses or network dependencies. Account identity is private-server
identity. Public loadout and secure progression commit as one revision; callers
must supply the revision they read to prevent a stale client overwriting a save.
"""
from __future__ import annotations
from contextlib import closing
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
import time


from codol_rebuild.application.accounts.contracts import RevisionConflict


class AccountRepository:
    def __init__(self, path):
        self.path = Path(path)

    def _existing_read_connection(self):
        """离线工具只打开现有数据库，避免路径错误生成空存档。"""
        if not self.path.is_file():
            raise ValueError(f'未找到存档：{self.path}；请先启动游戏创建账号。')
        return sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)

    def inventory_accounts(self):
        """读取已有账号列表，不迁移或创建数据库表。"""
        with closing(self._existing_read_connection()) as database:
            return [row[0] for row in database.execute('SELECT name FROM accounts ORDER BY name')]

    def inventory_snapshot(self, name):
        """单次查询读取授权及修订号，拒绝没有初始化状态的历史账号。"""
        with closing(self._existing_read_connection()) as database:
            row = database.execute('SELECT accounts.revision, account_initialization.state '
                'FROM accounts LEFT JOIN account_initialization ON accounts.name=account_initialization.account '
                'WHERE accounts.name=?', (name,)).fetchone()
            if row is None or row[1] is None:
                raise ValueError('账号没有物品授权状态，不能直接编辑；不会自动重置存档。')
            return {'revision': row[0], 'state': json.loads(row[1])}

    def backup_inventory_database(self):
        """SQLite 在线备份包含 WAL 已提交内容，备份失败阻止后续解锁。"""
        from datetime import datetime
        directory = self.path.parent / 'item-unlocker-backups'
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / f'local-{datetime.now():%Y%m%d-%H%M%S-%f}-{secrets.token_hex(3)}.sqlite3'
        with closing(self._existing_read_connection()) as source_database:
            with closing(sqlite3.connect(destination)) as target_database:
                source_database.backup(target_database)
        return str(destination)

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db=sqlite3.connect(self.path, timeout=5)
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('CREATE TABLE IF NOT EXISTS accounts (name TEXT PRIMARY KEY, uid INTEGER UNIQUE NOT NULL, revision INTEGER NOT NULL, updated REAL NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS profiles (account TEXT NOT NULL REFERENCES accounts(name), kind TEXT NOT NULL, identity TEXT NOT NULL, blob BLOB NOT NULL, sha256 TEXT NOT NULL, PRIMARY KEY(account,kind))')
        db.execute('CREATE TABLE IF NOT EXISTS display_names (account TEXT PRIMARY KEY REFERENCES accounts(name), value TEXT NOT NULL, revision INTEGER NOT NULL, updated REAL NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS account_initialization (account TEXT PRIMARY KEY REFERENCES accounts(name), state TEXT NOT NULL)')
        db.commit()
        return db

    def identity(self, name='local'):
        if not name or len(name)>64:raise ValueError('Invalid local account name')
        with closing(self._connect()) as db, db:
            db.execute('INSERT OR IGNORE INTO accounts VALUES (?,?,0,?)',
                       (name,secrets.randbelow(2**62-1)+1,time.time()))
            uid,revision=db.execute('SELECT uid,revision FROM accounts WHERE name=?',(name,)).fetchone()
            return {'name':name,'uid':uid,'revision':revision}

    def load(self, name, identities):
        with closing(self._connect()) as db:
            db.execute('BEGIN')
            row=db.execute('SELECT uid,revision FROM accounts WHERE name=?',(name,)).fetchone()
            if row is None:raise ValueError('Unknown account')
            profiles={}
            for kind,identity,blob,sha in db.execute('SELECT kind,identity,blob,sha256 FROM profiles WHERE account=?',(name,)):
                if kind not in identities or json.loads(identity)!=identities[kind]:
                    raise ValueError('Saved profile build/schema mismatch')
                if hashlib.sha256(blob).hexdigest()!=sha:raise ValueError('Saved profile checksum mismatch')
                profiles[kind]=blob
            if profiles and set(profiles)!=set(identities):raise ValueError('Incomplete saved profile pair')
            initialization = db.execute('SELECT state FROM account_initialization WHERE account=?', (name,)).fetchone()
            result = {'uid':row[0],'revision':row[1],'profiles':profiles}
            if initialization is not None:
                result['initialization'] = json.loads(initialization[0])
            return result

    def save(self, name, revision, profiles, identities, initialization=None):
        if set(profiles)!=set(identities) or not profiles:raise ValueError('Incomplete profile transaction')
        with closing(self._connect()) as db, db:
            changed=db.execute('UPDATE accounts SET revision=revision+1, updated=? WHERE name=? AND revision=?',
                               (time.time(),name,revision)).rowcount
            if changed!=1:raise RevisionConflict('Account changed since last load')
            for kind,blob in profiles.items():
                db.execute('INSERT OR REPLACE INTO profiles VALUES (?,?,?,?,?)',
                           (name,kind,json.dumps(identities[kind],sort_keys=True),blob,hashlib.sha256(blob).hexdigest()))
            if initialization is not None:
                db.execute('INSERT INTO account_initialization VALUES (?,?) ON CONFLICT(account) DO UPDATE SET state=excluded.state',
                           (name, json.dumps(initialization, sort_keys=True)))
        return revision+1

    def save_inventory(self, name, revision, inventory_state):
        """购买仅提交库存与余额；版本冲突或写入失败时整笔回滚。"""
        with closing(self._connect()) as database, database:
            changed = database.execute(
                'UPDATE accounts SET revision=revision+1, updated=? WHERE name=? AND revision=?',
                (time.time(), name, revision)).rowcount
            if changed != 1:
                raise RevisionConflict('Account changed since last load')
            database.execute(
                'INSERT INTO account_initialization VALUES (?,?) ON CONFLICT(account) DO UPDATE SET state=excluded.state',
                (name, json.dumps(inventory_state, sort_keys=True)))
        return revision + 1

    def display_name(self, name):
        """昵称版本独立于配装存档；旧数据库无需改写原有表或blob。"""
        with closing(self._connect()) as db:
            db.execute('BEGIN')
            if db.execute('SELECT 1 FROM accounts WHERE name=?', (name,)).fetchone() is None:
                raise ValueError('Unknown account')
            row = db.execute('SELECT value,revision FROM display_names WHERE account=?', (name,)).fetchone()
            return {'value': row[0], 'revision': row[1]} if row else {'value': None, 'revision': 0}

    def save_display_name(self, name, revision, value, *, account_revision=None, initialization=None):
        """单事务比较并交换昵称，首次写入同样受并发版本保护。"""
        if type(revision) is not int or revision < 0 or not isinstance(value, str) or not value:
            raise ValueError('Invalid display name record')
        if (account_revision is None) != (initialization is None):
            raise ValueError('Incomplete display name inventory transaction')
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM accounts WHERE name=?', (name,)).fetchone() is None:
                raise ValueError('Unknown account')
            row = db.execute('SELECT revision FROM display_names WHERE account=?', (name,)).fetchone()
            if (row[0] if row else 0) != revision:
                raise RevisionConflict('昵称已由其他会话修改，请重启后重试')
            db.execute('INSERT INTO display_names VALUES (?,?,?,?) ON CONFLICT(account) DO UPDATE SET value=excluded.value,revision=excluded.revision,updated=excluded.updated',
                       (name, value, revision+1, time.time()))
            if initialization is not None:
                changed = db.execute('UPDATE accounts SET revision=revision+1, updated=? WHERE name=? AND revision=?',
                                     (time.time(), name, account_revision)).rowcount
                if changed != 1:
                    raise RevisionConflict('Account changed since last load')
                db.execute('INSERT INTO account_initialization VALUES (?,?) ON CONFLICT(account) DO UPDATE SET state=excluded.state',
                           (name, json.dumps(initialization, sort_keys=True)))
        return revision+1

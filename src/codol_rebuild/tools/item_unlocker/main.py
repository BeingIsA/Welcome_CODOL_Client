"""图形工具装配入口：连接目录、账号存储和批量解锁用例。"""
import json
import os
import sys
from pathlib import Path
from PySide6.QtWidgets import QApplication
from codol_rebuild.adapters.persistence.sqlite_accounts import AccountRepository
from codol_rebuild.application.accounts.item_unlock import ItemUnlockService
from codol_rebuild.tools.item_unlocker.environment import ensure_game_stopped
from codol_rebuild.tools.item_unlocker.window import UnlockWindow


def main():
    """自动定位当前系统用户存档；发行包内置目录，不依赖 Excel。"""
    resource_root = Path(sys._MEIPASS) if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[4]
    catalog = json.loads((resource_root / 'resources/item_catalog.json').read_text(encoding='utf-8'))
    database_path = Path(os.environ['APPDATA']) / 'CODOL/data/accounts/local.sqlite3'
    repository = AccountRepository(database_path)
    service = ItemUnlockService(repository, [row[0] for row in catalog], ensure_game_stopped)
    application = QApplication(sys.argv)
    application.setStyle('Fusion')
    window = UnlockWindow(catalog, repository, service, database_path)
    window.show()
    return application.exec()


if __name__ == '__main__':
    sys.exit(main())

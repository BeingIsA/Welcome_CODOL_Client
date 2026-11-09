"""双表物品选择界面；筛选和分页不改变待解锁清单。"""
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QPushButton, QSpinBox, QSplitter,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget, QHeaderView)


class UnlockWindow(QMainWindow):
    """界面仅管理选择状态，通过注入服务提交库存变更。"""

    def __init__(self, catalog, repository, service, database_path):
        super().__init__()
        self.setFont(QFont('Microsoft YaHei UI', 10))
        self.catalog = catalog
        self.catalog_by_id = {row[0]: row for row in catalog}
        self.repository = repository
        self.service = service
        self.pending_ids = set()
        self.owned_ids = set()
        self.account_ready = False
        self.filtered_rows = []
        self.setWindowTitle('CODOL · 物品解锁器')
        self.resize(1180, 850)
        self.setMinimumSize(900, 650)
        container = QWidget()
        self.setCentralWidget(container)
        layout = QVBoxLayout(container)
        title = QLabel('物品解锁器')
        title.setStyleSheet('font-size:24px;font-weight:600;color:#18344d;padding:8px 0;')
        layout.addWidget(title)
        path_label = QLabel(f'存档：{database_path}')
        path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(path_label)
        layout.addWidget(QLabel('关闭游戏后操作 · 点击上表加入待解锁清单 · 保存后重启游戏生效'))
        controls = QHBoxLayout()
        self.accounts = QComboBox()
        self.accounts.setMinimumWidth(140)
        controls.addWidget(QLabel('账号'))
        controls.addWidget(self.accounts)
        self.search = QLineEdit()
        self.search.setPlaceholderText('搜索中文名 / 内部名称 / 物品 ID')
        self.search.setClearButtonEnabled(True)
        controls.addWidget(self.search, 1)
        self.type_filter = QComboBox()
        self.type_filter.addItem('全部类型', '')
        for value in sorted({row[4] for row in catalog if row[4]}):
            self.type_filter.addItem(value, value)
        self.category_filter = QComboBox()
        controls.addWidget(self.type_filter)
        controls.addWidget(self.category_filter)
        refresh_button = QPushButton('刷新存档')
        refresh_button.clicked.connect(self.load_accounts)
        controls.addWidget(refresh_button)
        layout.addLayout(controls)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        splitter = QSplitter(Qt.Orientation.Vertical)
        upper = QWidget()
        upper_layout = QVBoxLayout(upper)
        upper_layout.setContentsMargins(0, 0, 0, 0)
        upper_layout.addWidget(QLabel('物品目录 · 点击一行加入下表'))
        self.catalog_table = self.make_table()
        self.catalog_table.cellClicked.connect(self.add_row)
        upper_layout.addWidget(self.catalog_table)
        paging = QHBoxLayout()
        self.previous_button = QPushButton('上一页')
        self.next_button = QPushButton('下一页')
        self.page_number = QSpinBox()
        self.page_number.setMinimum(1)
        self.page_size = QComboBox()
        self.page_size.addItems(['50', '100', '200'])
        self.page_size.setCurrentText('100')
        self.page_label = QLabel()
        self.previous_button.clicked.connect(lambda: self.page_number.setValue(self.page_number.value()-1))
        self.next_button.clicked.connect(lambda: self.page_number.setValue(self.page_number.value()+1))
        for widget in (self.previous_button, self.page_number, self.next_button, self.page_label):
            paging.addWidget(widget)
        paging.addStretch()
        paging.addWidget(QLabel('每页'))
        paging.addWidget(self.page_size)
        upper_layout.addLayout(paging)
        lower = QWidget()
        lower_layout = QVBoxLayout(lower)
        lower_layout.setContentsMargins(0, 0, 0, 0)
        self.pending_label = QLabel('待解锁清单 · 0 件')
        lower_layout.addWidget(self.pending_label)
        self.pending_table = self.make_table()
        lower_layout.addWidget(self.pending_table)
        actions = QHBoxLayout()
        remove_button = QPushButton('移除选中项')
        remove_button.clicked.connect(self.remove_selected)
        clear_button = QPushButton('清空清单')
        clear_button.clicked.connect(self.clear_pending)
        self.unlock_button = QPushButton('全部解锁')
        self.unlock_button.setStyleSheet('background:#2465a5;color:white;padding:10px 28px;font-weight:bold;')
        self.unlock_button.clicked.connect(self.unlock_pending)
        actions.addWidget(remove_button)
        actions.addWidget(clear_button)
        actions.addStretch()
        actions.addWidget(self.unlock_button)
        lower_layout.addLayout(actions)
        splitter.addWidget(upper)
        splitter.addWidget(lower)
        splitter.setSizes([420, 240])
        layout.addWidget(splitter, 1)
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(180)
        self.search_timer.timeout.connect(self.apply_filters)
        self.search.textChanged.connect(lambda: self.search_timer.start())
        self.type_filter.currentIndexChanged.connect(self.update_categories)
        self.category_filter.currentIndexChanged.connect(self.apply_filters)
        self.page_size.currentIndexChanged.connect(self.apply_filters)
        self.page_number.valueChanged.connect(self.render_page)
        self.accounts.currentTextChanged.connect(self.load_account)
        self.update_categories()
        self.load_accounts()

    def make_table(self):
        table = QTableWidget(0, 7)
        table.setHorizontalHeaderLabels(['物品 ID', '内部名称', '中文名', '品质', '类型', '分类', '状态'])
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.verticalHeader().hide()
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        table.horizontalHeader().setStretchLastSection(True)
        for column_index, width in enumerate([85, 260, 240, 80, 75, 80, 100]):
            table.setColumnWidth(column_index, width)
        return table

    def fill_table(self, table, rows):
        table.setUpdatesEnabled(False)
        table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            identifier = row[0]
            status = ('不可解锁' if identifier in (15400, 20000) else
                      '已拥有' if identifier in self.owned_ids else
                      '未拥有' if self.account_ready else '存档未就绪')
            for column_index, value in enumerate([*row, status]):
                cell = QTableWidgetItem(str(value or ''))
                cell.setData(Qt.ItemDataRole.UserRole, identifier)
                table.setItem(row_index, column_index, cell)
        table.setUpdatesEnabled(True)

    def update_categories(self):
        selected_type = self.type_filter.currentData()
        self.category_filter.blockSignals(True)
        self.category_filter.clear()
        self.category_filter.addItem('全部分类', '')
        for category in sorted({row[5] for row in self.catalog if row[5] and (not selected_type or row[4] == selected_type)}):
            self.category_filter.addItem(category, category)
        self.category_filter.blockSignals(False)
        self.apply_filters()

    def apply_filters(self):
        query = self.search.text().strip().casefold()
        selected_type = self.type_filter.currentData()
        category = self.category_filter.currentData()
        self.filtered_rows = [row for row in self.catalog
            if (not selected_type or row[4] == selected_type)
            and (not category or row[5] == category)
            and (not query or any(query in str(value).casefold() for value in row[:3]))]
        count = len(self.filtered_rows)
        self.page_number.blockSignals(True)
        self.page_number.setMaximum(max(1, (count+int(self.page_size.currentText())-1)//int(self.page_size.currentText())))
        self.page_number.setValue(1)
        self.page_number.blockSignals(False)
        self.render_page()

    def render_page(self):
        page = self.page_number.value()
        size = int(self.page_size.currentText())
        self.fill_table(self.catalog_table, self.filtered_rows[(page-1)*size:page*size])
        self.page_label.setText(f'/ {self.page_number.maximum()} 页 · 共 {len(self.filtered_rows):,} 件')
        self.previous_button.setEnabled(page > 1)
        self.next_button.setEnabled(page < self.page_number.maximum())

    def load_accounts(self):
        current_account = self.accounts.currentText()
        self.accounts.blockSignals(True)
        try:
            names = self.repository.inventory_accounts()
            self.accounts.clear()
            self.accounts.addItems(names)
            if current_account in names:
                self.accounts.setCurrentText(current_account)
        except Exception as error:
            self.accounts.clear()
            self.status.setText(str(error))
        finally:
            self.accounts.blockSignals(False)
        self.load_account()

    def load_account(self):
        self.account_ready = False
        self.owned_ids = set()
        try:
            if not self.accounts.currentText():
                raise ValueError('未找到可用账号。请先启动游戏创建存档，再点击刷新存档。')
            from codol_rebuild.application.accounts.policy import validate_initialization
            snapshot = self.repository.inventory_snapshot(self.accounts.currentText())
            self.owned_ids = set(validate_initialization(snapshot['state'])['owned_ids'])
            self.account_ready = True
            self.status.setText(f'当前账号：{self.accounts.currentText()} · 已拥有 {len(self.owned_ids):,} 件')
        except Exception as error:
            self.status.setText(str(error))
        self.render_page()
        self.render_pending()

    def add_row(self, row_index, column_index):
        identifier = self.catalog_table.item(row_index, 0).data(Qt.ItemDataRole.UserRole)
        if identifier not in (15400, 20000):
            self.pending_ids.add(identifier)
            self.render_pending()

    def render_pending(self):
        self.fill_table(self.pending_table, [self.catalog_by_id[identifier] for identifier in sorted(self.pending_ids)])
        self.pending_label.setText(f'待解锁清单 · {len(self.pending_ids)} 件 · 目标账号：{self.accounts.currentText() or "未选择"}')
        self.unlock_button.setEnabled(self.account_ready and bool(self.pending_ids))

    def remove_selected(self):
        for selected in self.pending_table.selectionModel().selectedRows():
            self.pending_ids.discard(self.pending_table.item(selected.row(), 0).data(Qt.ItemDataRole.UserRole))
        self.render_pending()

    def clear_pending(self):
        self.pending_ids.clear()
        self.render_pending()

    def unlock_pending(self):
        self.unlock_button.setEnabled(False)
        try:
            result = self.service.unlock(self.accounts.currentText(), self.pending_ids)
        except Exception as error:
            QMessageBox.warning(self, '未能解锁', str(error))
            self.load_account()
            return
        self.pending_ids.clear()
        self.load_account()
        message = f'已解锁 {result["added"]} 件，跳过已拥有 {result["skipped"]} 件。\n重新启动游戏后生效。'
        if result['backup']:
            message += f'\n备份：{result["backup"]}'
        QMessageBox.information(self, '处理完成', message)

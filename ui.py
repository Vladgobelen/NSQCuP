# ui.py
import sys
import os
import traceback
import logging
from pathlib import Path
from PyQt5.QtWidgets import (
    QApplication,
    QMainWindow,
    QVBoxLayout,
    QHBoxLayout,
    QWidget,
    QPushButton,
    QLabel,
    QCheckBox,
    QScrollArea,
    QFrame,
    QProgressBar,
    QSizePolicy,
    QMessageBox,
    QStackedWidget,
    QListWidget,
    QListWidgetItem,
    QLineEdit
)
from PyQt5.QtCore import Qt, QTimer, QSize
from PyQt5.QtGui import QIcon, QPalette, QColor, QFont, QFontDatabase
from addon_manager import AddonManager, ErrorHandler, AddonData
from voice_client_ui import VoiceChatUI

def get_base_path():
    """Получает базовый путь для ресурсов"""
    if getattr(sys, 'frozen', False):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))

class AddonUpdater(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Night Watch Updater")
        self.resize(550, 650)
        self.logger = logging.getLogger('AddonUpdater')
        self.logger.setLevel(logging.DEBUG)
        
        # Настройка логирования
        if not hasattr(self, 'logger_configured'):
            if not os.path.exists('logs'):
                os.makedirs('logs')
            
            fh = logging.FileHandler('logs/main_ui.log')
            fh.setLevel(logging.DEBUG)
            formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
            fh.setFormatter(formatter)
            self.logger.addHandler(fh)
            self.logger_configured = True

        self._setup_fonts()
        self._setup_ui()
        self._setup_manager()
        self._setup_theme()

    def _setup_fonts(self):
        font_db = QFontDatabase()
        try:
            # Попытка загрузить шрифт Arial
            font_id = font_db.addApplicationFont(":/fonts/arial.ttf")
            if font_id != -1:
                font_family = font_db.applicationFontFamilies(font_id)[0]
                self._font = QFont(font_family, 10)
            else:
                self._font = QFont("Arial", 10)
        except:
            self._font = QFont()
            self._font.setPointSize(10)

        QApplication.setFont(self._font)

    def _setup_ui(self):
        self.central_widget = QWidget()
        self.central_widget.setObjectName("centralWidget")
        self.setCentralWidget(self.central_widget)

        self.stacked_widget = QStackedWidget()
        main_layout = QVBoxLayout(self.central_widget)
        main_layout.addWidget(self.stacked_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        self.addon_widget = QWidget()
        self.voice_widget = VoiceChatUI(self)

        self.stacked_widget.addWidget(self.addon_widget)
        self.stacked_widget.addWidget(self.voice_widget)

        self._setup_addon_ui()
        self.logger.info("UI настройка завершена")

    def _setup_addon_ui(self):
        layout = QVBoxLayout(self.addon_widget)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        self._setup_game_panel(layout)

        sep = QFrame()
        sep.setObjectName("separator")
        sep.setFrameShape(QFrame.HLine)
        sep.setFrameShadow(QFrame.Sunken)
        layout.addWidget(sep)

        self._setup_addons_list(layout)

    def _setup_game_panel(self, parent_layout):
        panel = QWidget()
        panel.setObjectName("gamePanel")
        panel_layout = QHBoxLayout()
        panel.setLayout(panel_layout)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.setSpacing(8)

        self.game_status = QLabel("Проверка игры...")
        self.game_status.setFont(QFont("Arial", 10, QFont.Bold))

        self.launch_btn = QPushButton("Запустить игру")
        self.launch_btn.setFixedSize(140, 32)
        self.launch_btn.clicked.connect(self._launch_game)

        # Кнопка голосового чата с символом UTF-8
        self.voice_btn = QPushButton("🎤")
        self.voice_btn.setFont(QFont("Arial", 12))
        self.voice_btn.setFixedSize(36, 36)
        self.voice_btn.setToolTip("Голосовой чат")
        self.voice_btn.clicked.connect(self.show_voice_chat)

        panel_layout.addWidget(self.game_status)
        panel_layout.addWidget(self.launch_btn)
        panel_layout.addWidget(self.voice_btn)
        parent_layout.addWidget(panel)

        self._check_game()

    def _setup_addons_list(self, parent_layout):
        label = QLabel("Менеджер аддонов")
        label.setFont(QFont("Arial", 11, QFont.Bold))
        parent_layout.addWidget(label)

        scroll = QScrollArea()
        scroll.setObjectName("addonsScrollArea")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        content = QWidget()
        content.setObjectName("scrollContent")
        self.addons_layout = QVBoxLayout()
        content.setLayout(self.addons_layout)
        self.addons_layout.setSpacing(8)

        scroll.setWidget(content)
        parent_layout.addWidget(scroll, stretch=1)

    def _setup_manager(self):
        self.manager = AddonManager()
        self.manager.addons_layout = self.addons_layout
        self.manager.update_progress.connect(self._on_progress_update)
        self.manager.operation_finished.connect(self._on_operation_finished)
        self.manager.addon_update_available.connect(self._on_addon_update_available)

        self.error_handler = ErrorHandler()
        self.error_handler.error_occurred.connect(self._show_error_message)

        self.update_timer = QTimer()
        self.update_timer.timeout.connect(self._check_updates)
        self.update_timer.start(30000)

        self._load_addons()
        self.logger.info("Менеджер настроен")

    def _setup_theme(self):
        dark_palette = QPalette()
        dark_palette.setColor(QPalette.Window, QColor(45, 45, 45))
        dark_palette.setColor(QPalette.WindowText, Qt.white)
        dark_palette.setColor(QPalette.Base, QColor(30, 30, 30))
        dark_palette.setColor(QPalette.AlternateBase, QColor(45, 45, 45))
        dark_palette.setColor(QPalette.ToolTipBase, Qt.white)
        dark_palette.setColor(QPalette.ToolTipText, Qt.white)
        dark_palette.setColor(QPalette.Text, Qt.white)
        dark_palette.setColor(QPalette.Button, QColor(60, 60, 60))
        dark_palette.setColor(QPalette.ButtonText, Qt.white)
        dark_palette.setColor(QPalette.BrightText, Qt.red)
        dark_palette.setColor(QPalette.Link, QColor(42, 130, 218))
        dark_palette.setColor(QPalette.Highlight, QColor(42, 130, 218))
        dark_palette.setColor(QPalette.HighlightedText, Qt.black)
        dark_palette.setColor(QPalette.Disabled, QPalette.Text, Qt.darkGray)
        dark_palette.setColor(QPalette.Disabled, QPalette.ButtonText, Qt.darkGray)

        app = QApplication.instance()
        app.setPalette(dark_palette)

        self.setStyleSheet("""
            QWidget {
                color: #FFFFFF;
                background-color: #2D2D2D;
            }
            #centralWidget {
                background-color: #2D2D2D;
            }
            #separator {
                background-color: #555555;
            }
            #gamePanel {
                background-color: #3A3A3A;
                border-radius: 4px;
                padding: 8px;
            }
            #addonsScrollArea {
                background-color: #2D2D2D;
                border: 1px solid #555555;
                border-radius: 4px;
            }
            #scrollContent {
                background-color: #2D2D2D;
            }
            QCheckBox {
                color: #FFFFFF;
                spacing: 6px;
            }
            QCheckBox::indicator {
                width: 16px;
                height: 16px;
            }
            QCheckBox::indicator:unchecked {
                border: 1px solid #555555;
                background-color: #333333;
            }
            QCheckBox::indicator:checked {
                border: 1px solid #555555;
                background-color: #2A82DA;
            }
            QProgressBar {
                height: 4px;
                border-radius: 2px;
                background: #252525;
            }
            QProgressBar::chunk {
                background: #2A82DA;
                border-radius: 2px;
            }
            QPushButton {
                background-color: #3D3D3D;
                border: 1px solid #444;
                padding: 5px;
                min-width: 80px;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #4D4D4D;
            }
            QPushButton:pressed {
                background-color: #2D2D2D;
            }
            QPushButton:disabled {
                background-color: #333333;
                color: #888888;
            }
            QLabel {
                color: #FFFFFF;
            }
            QLabel[accessibleName="updateLabel"] {
                color: #8BC34A;
                font-style: italic;
            }
            QScrollArea {
                border: none;
            }
            QScrollBar:vertical {
                border: none;
                background: #2D2D2D;
                width: 10px;
                margin: 0px 0px 0px 0px;
            }
            QScrollBar::handle:vertical {
                background: #555555;
                min-height: 20px;
                border-radius: 4px;
            }
        """)

    def _check_game(self):
        game_exists = Path("Wow.exe").exists()
        status_text = "Готова к запуску" if game_exists else "Игра не найдена"
        self.game_status.setText(status_text)
        self.game_status.setStyleSheet(
            "color: #4CAF50;" if game_exists else "color: #F44336;"
        )
        self.launch_btn.setEnabled(game_exists)
        self.logger.info(f"Проверка игры: {status_text}")

    def _load_addons(self):
        for name, addon in self.manager.addons.items():
            self._add_addon_item(name, addon)
        self.logger.info("Аддоны загружены")

    def _add_addon_item(self, name: str, addon: AddonData):
        widget = QWidget()
        layout = QVBoxLayout()
        widget.setLayout(layout)
        layout.setContentsMargins(0, 0, 0, 0)

        top_row = QWidget()
        top_layout = QHBoxLayout()
        top_row.setLayout(top_layout)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(6)

        checkbox = QCheckBox(name)
        checkbox.setChecked(addon.installed)
        checkbox.stateChanged.connect(
            lambda state, n=name: self.manager.toggle_addon(n, state)
        )
        checkbox.setStyleSheet("font-weight: bold;")
        top_layout.addWidget(checkbox)

        update_label = QLabel()
        update_label.setAccessibleName("updateLabel")
        if name == "NSQC":
            update_label.setVisible(addon.needs_update)
            if addon.needs_update:
                update_label.setText("(Доступно обновление)")
        top_layout.addWidget(update_label)
        top_layout.addStretch()

        desc = QLabel(addon.description)
        desc.setStyleSheet("color: #AAAAAA;")
        desc.setWordWrap(True)

        progress = QProgressBar()
        progress.setRange(0, 100)
        progress.setTextVisible(False)
        progress.setVisible(False)

        layout.addWidget(top_row)
        layout.addWidget(desc)
        layout.addWidget(progress)

        widget.checkbox = checkbox
        widget.progress = progress
        widget.update_label = update_label
        widget.name = name

        self.addons_layout.addWidget(widget)

    def _on_progress_update(self, name: str, progress: float):
        for i in range(self.addons_layout.count()):
            w = self.addons_layout.itemAt(i).widget()
            if w and hasattr(w, "name") and w.name == name:
                w.progress.setValue(int(progress * 100))
                w.progress.setVisible(True)
                break

    def _on_operation_finished(self, name: str, success: bool):
        for i in range(self.addons_layout.count()):
            w = self.addons_layout.itemAt(i).widget()
            if w and hasattr(w, "name") and w.name == name:
                try:
                    addon = self.manager.addons[name]
                    w.progress.setVisible(False)

                    w.checkbox.blockSignals(True)
                    w.checkbox.setChecked(addon.installed)
                    w.checkbox.blockSignals(False)

                    if name == "NSQC":
                        w.update_label.setVisible(addon.needs_update)
                        w.update_label.setText(
                            "(Доступно обновление)" if addon.needs_update else ""
                        )

                    w.checkbox.update()
                    w.checkbox.repaint()
                except Exception as e:
                    self.logger.error(f"Ошибка обновления UI: {str(e)}")
                break

    def _on_addon_update_available(self, name: str):
        if name == "NSQC":
            self.manager.toggle_addon(name, True)

    def _show_error_message(self, message):
        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Critical)
        msg.setText("Произошла ошибка")
        msg.setInformativeText(message)
        msg.setWindowTitle("Ошибка")
        msg.exec_()
        self.logger.error(f"Показано сообщение об ошибке: {message}")

    def _check_updates(self):
        if "NSQC" in self.manager.addons:
            self.manager.check_nsqc_update()

    def _launch_game(self):
        self.logger.info("Запуск игры...")
        if not self.manager.launch_game():
            self._show_error_message("Не удалось запустить игру")
            self.logger.error("Не удалось запустить игру")

    def show_voice_chat(self):
        self.logger.info("Переход к голосовому чату")
        self.stacked_widget.setCurrentWidget(self.voice_widget)
        self.voice_widget.start_voice_client()

    def show_addon_manager(self):
        self.logger.info("Переход к менеджеру аддонов")
        self.voice_widget.stop_voice_client()
        self.stacked_widget.setCurrentWidget(self.addon_widget)
    
    def closeEvent(self, event):
        self.logger.info("Закрытие приложения")
        self.voice_widget.stop_voice_client()
        event.accept()
import os
import logging
import traceback
from PyQt5.QtCore import Qt, QTimer, QObject, pyqtSignal, QSize
from PyQt5.QtGui import QFont, QColor, QPalette
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QListWidget, QListWidgetItem, QLineEdit, QFrame, QScrollArea, QMessageBox,
    QTextEdit, QSplitter, QCheckBox, QSlider
)

# Локальные импорты
from voice_client_backend import VoiceClientBackend, pyaudio_available
from voice_client_constants import SERVER_ADDRESS, MIN_VOICE_THRESHOLD, MAX_VOICE_THRESHOLD, DEFAULT_VOICE_THRESHOLD, AGGRESSIVE_DTX_THRESHOLD


class VoiceChatUI(QWidget):
    def __init__(self, parent):
        super().__init__()
        self.parent = parent
        self.logger = logging.getLogger('VoiceChatUI')
        self.logger.setLevel(logging.DEBUG)
        self.voice_client = None
        self.is_connected = False
        self.is_talking = False
        self.participants_visible = False
        self.current_style = "telegram"  # или "discord"

        # Настройка логирования
        if not hasattr(self, 'logger_configured'):
            if not os.path.exists('logs'):
                os.makedirs('logs')
            fh = logging.FileHandler('logs/voice_ui.log')
            fh.setLevel(logging.DEBUG)
            formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
            fh.setFormatter(formatter)
            self.logger.addHandler(fh)
            self.logger_configured = True

        self.setup_ui()
        self.setup_theme()

    def setup_ui(self):
        self.main_layout = QVBoxLayout()
        self.setLayout(self.main_layout)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)

        # Создаем контейнеры для разных стилей
        self.telegram_container = QWidget()
        self.discord_container = QWidget()

        self.setup_telegram_ui()
        self.setup_discord_ui()

        self.main_layout.addWidget(self.telegram_container)
        self.discord_container.hide()

    def setup_telegram_ui(self):
        layout = QVBoxLayout(self.telegram_container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.setup_telegram_top_bar(layout)

        # Список участников (горизонтальный, скрываемый)
        self.setup_participants_bar(layout)

        # Область чата
        self.setup_chat_area(layout)

        # Нижняя панель с полем ввода
        self.setup_input_area(layout)

    def setup_discord_ui(self):
        layout = QHBoxLayout(self.discord_container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Левая панель (список участников и управление)
        left_panel = QWidget()
        left_panel.setFixedWidth(250)
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)

        # Заголовок сервера/комнаты
        server_header = QWidget()
        server_header.setFixedHeight(50)
        server_header_layout = QHBoxLayout(server_header)
        server_header_layout.setContentsMargins(15, 0, 15, 0)

        server_name = QLabel("Голосовой чат")
        server_name.setFont(QFont("Arial", 14, QFont.Bold))
        server_header_layout.addWidget(server_name)
        server_header_layout.addStretch()

        # Кнопка назад
        back_btn = QPushButton("←")
        back_btn.setFixedSize(30, 30)
        back_btn.clicked.connect(self.parent.show_addon_manager)
        back_btn.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                border: none;
                font-size: 16px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #3d3d3d;
                border-radius: 15px;
            }
        """)
        server_header_layout.addWidget(back_btn)

        left_layout.addWidget(server_header)

        # Список участников (вертикальный)
        participants_label = QLabel("Участники голосового канала")
        participants_label.setContentsMargins(15, 10, 15, 5)
        participants_label.setStyleSheet("color: #72767d; font-weight: bold;")
        left_layout.addWidget(participants_label)

        self.discord_participants_list = QListWidget()
        self.discord_participants_list.setStyleSheet("""
            QListWidget {
                background-color: #2f3136;
                border: none;
                color: #8e9297;
            }
            QListWidget::item {
                padding: 5px 15px;
                border-bottom: 1px solid #36393f;
            }
            QListWidget::item:selected {
                background-color: #36393f;
            }
        """)

        # Добавляем тестовых участников
        for i in range(5):
            item = QListWidgetItem(f"Участник {i+1}")
            self.discord_participants_list.addItem(item)

        left_layout.addWidget(self.discord_participants_list, 1)

        # Панель управления голосом
        voice_control = QWidget()
        voice_control.setFixedHeight(80)
        voice_control.setStyleSheet("background-color: #292b2f;")
        voice_layout = QVBoxLayout(voice_control)
        voice_layout.setContentsMargins(10, 10, 10, 10)

        # Индикатор подключения
        self.discord_status_label = QLabel("Не подключено")
        self.discord_status_label.setAlignment(Qt.AlignCenter)
        self.discord_status_label.setStyleSheet("color: #72767d; font-size: 12px;")
        voice_layout.addWidget(self.discord_status_label)

        # Кнопка микрофона
        self.discord_mic_btn = QPushButton("Выключить микрофон")
        self.discord_mic_btn.setCheckable(True)
        self.discord_mic_btn.setFixedHeight(30)
        self.discord_mic_btn.setStyleSheet("""
            QPushButton {
                background-color: #ed4245;
                border: none;
                border-radius: 4px;
                color: white;
                font-weight: bold;
            }
            QPushButton:checked {
                background-color: #43b581;
            }
            QPushButton:hover {
                background-color: #ed4245;
                opacity: 0.8;
            }
            QPushButton:checked:hover {
                background-color: #43b581;
                opacity: 0.8;
            }
            QPushButton:disabled {
                background-color: #4f545c;
            }
        """)
        self.discord_mic_btn.clicked.connect(self.toggle_microphone)
        self.discord_mic_btn.setEnabled(False)
        voice_layout.addWidget(self.discord_mic_btn)

        left_layout.addWidget(voice_control)
        layout.addWidget(left_panel)

        # Правая панель (чат)
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        # Заголовок чата
        chat_header = QWidget()
        chat_header.setFixedHeight(50)
        chat_header.setStyleSheet("background-color: #36393f; border-bottom: 1px solid #202225;")
        chat_header_layout = QHBoxLayout(chat_header)
        chat_header_layout.setContentsMargins(15, 0, 15, 0)

        chat_name = QLabel("Текстовый чат")
        chat_name.setFont(QFont("Arial", 14, QFont.Bold))
        chat_header_layout.addWidget(chat_name)
        chat_header_layout.addStretch()

        right_layout.addWidget(chat_header)

        # Область чата
        self.discord_chat_area = QTextEdit()
        self.discord_chat_area.setReadOnly(True)
        self.discord_chat_area.setStyleSheet("""
            QTextEdit {
                background-color: #36393f;
                border: none;
                padding: 15px;
                color: #dcddde;
                font-size: 14px;
            }
        """)

        # Добавляем тестовые сообщения
        self.discord_chat_area.append("<span style='color: #72767d;'>Добро пожаловать в голосовой чат!</span>")
        self.discord_chat_area.append("<span style='color: #fff;'><b>Участник 1:</b> Привет всем!</span>")
        self.discord_chat_area.append(
            "<span style='color: #fff; text-align: right; display: block;'><b>Вы:</b> Здравствуйте!</span>")

        right_layout.addWidget(self.discord_chat_area, 1)

        # Поле ввода сообщения
        input_widget = QWidget()
        input_widget.setFixedHeight(60)
        input_widget.setStyleSheet("background-color: #40444b;")
        input_layout = QHBoxLayout(input_widget)
        input_layout.setContentsMargins(15, 10, 15, 10)

        self.discord_message_input = QLineEdit()
        self.discord_message_input.setPlaceholderText("Введите сообщение...")
        self.discord_message_input.setStyleSheet("""
            QLineEdit {
                background-color: #484c52;
                border: 1px solid #000000;
                padding: 8px;
                border-radius: 4px;
                color: #dcddde;
            }
        """)
        self.discord_message_input.returnPressed.connect(self.send_discord_message)

        self.discord_send_btn = QPushButton("➤")
        self.discord_send_btn.setFixedSize(40, 40)
        self.discord_send_btn.setStyleSheet("""
            QPushButton {
                background-color: #5865f2;
                border: none;
                border-radius: 4px;
                color: white;
                font-size: 16px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #4752c4;
            }
            QPushButton:disabled {
                background-color: #4f545c;
            }
        """)
        self.discord_send_btn.clicked.connect(self.send_discord_message)

        input_layout.addWidget(self.discord_message_input)
        input_layout.addWidget(self.discord_send_btn)

        right_layout.addWidget(input_widget)
        layout.addWidget(right_panel, 1)

    def setup_telegram_top_bar(self, layout):
        top_bar = QWidget()
        top_bar.setFixedHeight(50)
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(10, 5, 10, 5)

        # Кнопка назад
        self.back_btn = QPushButton("←")
        self.back_btn.setFixedSize(40, 40)
        self.back_btn.clicked.connect(self.parent.show_addon_manager)
        self.back_btn.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                border: none;
                font-size: 18px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #3d3d3d;
                border-radius: 20px;
            }
        """)

        # Кнопка комнат
        self.rooms_btn = QPushButton("Комнаты")
        self.rooms_btn.setFixedHeight(40)
        self.rooms_btn.setStyleSheet("""
            QPushButton {
                background-color: #3498db;
                border: none;
                border-radius: 15px;
                color: white;
                font-size: 12px;
                padding: 0 10px;
            }
            QPushButton:hover {
                background-color: #2980b9;
            }
        """)

        # Название чата
        self.chat_title = QLabel("Голосовой чат")
        self.chat_title.setFont(QFont("Arial", 14, QFont.Bold))
        self.chat_title.setAlignment(Qt.AlignCenter)
        self.chat_title.mousePressEvent = self.toggle_participants

        # Кнопка настроек
        self.settings_btn = QPushButton("⚙")
        self.settings_btn.setFixedSize(40, 40)
        self.settings_btn.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                border: none;
                font-size: 18px;
            }
            QPushButton:hover {
                background-color: #3d3d3d;
                border-radius: 20px;
            }
        """)
        self.settings_btn.clicked.connect(self.show_settings)

        # Кнопка микрофона
        self.mic_btn = QPushButton("🎤")
        self.mic_btn.setFixedSize(40, 40)
        self.mic_btn.setCheckable(True)
        self.mic_btn.setEnabled(False)
        self.update_mic_button_style()
        self.mic_btn.clicked.connect(self.toggle_microphone)

        top_layout.addWidget(self.back_btn)
        top_layout.addWidget(self.rooms_btn)
        top_layout.addWidget(self.chat_title, 1)
        top_layout.addWidget(self.settings_btn)
        top_layout.addWidget(self.mic_btn)

        layout.addWidget(top_bar)

    def setup_settings_menu(self):
        """Создает меню настроек"""
        self.settings_menu = QWidget()
        self.settings_menu.setWindowTitle("Настройки голосового чата")
        self.settings_menu.setFixedSize(350, 300)
        self.settings_menu.setWindowFlags(Qt.Dialog)

        layout = QVBoxLayout(self.settings_menu)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(10)

        # Настройка DTX
        dtx_label = QLabel("DTX (Discontinuous Transmission):")
        dtx_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(dtx_label)

        dtx_desc = QLabel("Уменьшает трафик при молчании, но может снизить качество голоса")
        dtx_desc.setStyleSheet("color: #AAAAAA; font-size: 11px;")
        dtx_desc.setWordWrap(True)
        layout.addWidget(dtx_desc)

        self.dtx_checkbox = QCheckBox("Включить DTX")
        self.dtx_checkbox.setChecked(True)  # Включено по умолчанию
        self.dtx_checkbox.stateChanged.connect(self.toggle_dtx)
        layout.addWidget(self.dtx_checkbox)

        # Агрессивный режим DTX
        aggressive_label = QLabel("Агрессивный режим DTX:")
        aggressive_label.setStyleSheet("font-weight: bold; margin-top: 10px;")
        layout.addWidget(aggressive_label)

        aggressive_desc = QLabel("Сильнее подавляет фоновые шумы, но может обрезать тихий голос")
        aggressive_desc.setStyleSheet("color: #AAAAAA; font-size: 11px;")
        aggressive_desc.setWordWrap(True)
        layout.addWidget(aggressive_desc)

        self.aggressive_dtx_checkbox = QCheckBox("Включить агрессивный режим")
        self.aggressive_dtx_checkbox.setChecked(False)
        self.aggressive_dtx_checkbox.stateChanged.connect(self.toggle_aggressive_dtx)
        layout.addWidget(self.aggressive_dtx_checkbox)

        # Порог активации голоса
        threshold_label = QLabel("Чувствительность микрофона:")
        threshold_label.setStyleSheet("font-weight: bold; margin-top: 10px;")
        layout.addWidget(threshold_label)

        threshold_desc = QLabel("Регулирует порог активации голоса (меньше значение = выше чувствительность)")
        threshold_desc.setStyleSheet("color: #AAAAAA; font-size: 11px;")
        threshold_desc.setWordWrap(True)
        layout.addWidget(threshold_desc)

        threshold_layout = QHBoxLayout()

        self.threshold_slider = QSlider(Qt.Horizontal)
        self.threshold_slider.setRange(MIN_VOICE_THRESHOLD, MAX_VOICE_THRESHOLD)
        self.threshold_slider.setValue(DEFAULT_VOICE_THRESHOLD)
        self.threshold_slider.valueChanged.connect(self.update_voice_threshold)
        threshold_layout.addWidget(self.threshold_slider)

        self.threshold_value = QLabel(str(DEFAULT_VOICE_THRESHOLD))
        self.threshold_value.setFixedWidth(40)
        threshold_layout.addWidget(self.threshold_value)

        layout.addLayout(threshold_layout)

        # Кнопка закрытия
        close_btn = QPushButton("Закрыть")
        close_btn.clicked.connect(self.settings_menu.hide)
        layout.addWidget(close_btn)

        layout.addStretch()

    def toggle_dtx(self, state):
        """Включение/выключение DTX"""
        self.use_dtx = state == Qt.Checked
        if self.voice_client:
            self.voice_client.set_dtx(self.use_dtx)

    def toggle_aggressive_dtx(self, state):
        """Включение/выключение агрессивного режима DTX"""
        self.aggressive_dtx = state == Qt.Checked
        if self.voice_client:
            self.voice_client.set_aggressive_dtx(self.aggressive_dtx)

    def update_voice_threshold(self, value):
        """Обновление порога активации голоса"""
        self.threshold_value.setText(str(value))
        if self.voice_client:
            self.voice_client.set_voice_threshold(value)

    def setup_participants_bar(self, layout):
        """Создает панель участников (горизонтальный список)"""
        self.participants_widget = QWidget()
        self.participants_widget.setFixedHeight(60)
        self.participants_widget.hide()  # Скрываем по умолчанию

        participants_layout = QHBoxLayout(self.participants_widget)
        participants_layout.setContentsMargins(10, 5, 10, 5)
        participants_layout.setSpacing(10)

        # Заголовок участников
        participants_label = QLabel("Участники:")
        participants_label.setStyleSheet("font-weight: bold;")
        participants_layout.addWidget(participants_label)

        # Добавляем примеры участников
        for i in range(3):
            participant = QLabel(f"Игрок {i+1}")
            participant.setFixedSize(50, 50)
            participant.setAlignment(Qt.AlignCenter)
            participant.setStyleSheet("""
                QLabel {
                    background-color: #3498db;
                    border-radius: 25px;
                    color: white;
                    font-weight: bold;
                }
            """)
            participants_layout.addWidget(participant)

        participants_layout.addStretch()
        layout.addWidget(self.participants_widget)

    def setup_chat_area(self, layout):
        """Создает область чата"""
        self.chat_area = QTextEdit()
        self.chat_area.setReadOnly(True)
        self.chat_area.setPlaceholderText("Здесь будут отображаться сообщения...")

        # Добавляем тестовые сообщения для демонстрации
        self.add_message("Система", "Добро пожаловать в голосовой чат!", False)
        self.add_message("Игрок 1", "Привет всем!", False)
        self.add_message("Вы", "Здравствуйте!", True)

        layout.addWidget(self.chat_area, 1)

    def setup_input_area(self, layout):
        """Создает нижнюю панель с полем ввода"""
        input_widget = QWidget()
        input_widget.setFixedHeight(60)
        input_layout = QHBoxLayout(input_widget)
        input_layout.setContentsMargins(10, 5, 10, 5)

        # Поле ввода текста
        self.message_input = QLineEdit()
        self.message_input.setPlaceholderText("Введите сообщение...")
        self.message_input.returnPressed.connect(self.send_message)

        # Кнопка отправки
        self.send_btn = QPushButton("➤")
        self.send_btn.setFixedSize(40, 40)
        self.send_btn.setStyleSheet("""
            QPushButton {
                background-color: #3498db;
                border: none;
                border-radius: 20px;
                color: white;
                font-size: 16px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #2980b9;
            }
            QPushButton:disabled {
                background-color: #7f8c8d;
            }
        """)
        self.send_btn.clicked.connect(self.send_message)

        input_layout.addWidget(self.message_input)
        input_layout.addWidget(self.send_btn)

        layout.addWidget(input_widget)

    def resizeEvent(self, event):
        """Обработчик изменения размера окна"""
        width = event.size().width()

        if width >= 600 and self.current_style != "discord":
            self.current_style = "discord"
            self.telegram_container.hide()
            self.discord_container.show()
            self.main_layout.addWidget(self.discord_container)
        elif width < 600 and self.current_style != "telegram":
            self.current_style = "telegram"
            self.discord_container.hide()
            self.telegram_container.show()
            self.main_layout.addWidget(self.telegram_container)

        super().resizeEvent(event)

    def add_message(self, sender, message, is_me):
        """Добавляет сообщение в чат с правильным выравниванием"""
        if is_me:
            # Сообщение от себя - выравниваем по правому краю
            self.chat_area.append(f"<div style='text-align: right; color: #3498db;'><b>{sender}:</b> {message}</div>")
        else:
            # Сообщение от других - выравниваем по левому краю
            self.chat_area.append(f"<div style='text-align: left;'><b>{sender}:</b> {message}</div>")

        # Прокручиваем вниз
        self.chat_area.verticalScrollBar().setValue(
            self.chat_area.verticalScrollBar().maximum()
        )

    def send_message(self):
        message = self.message_input.text().strip()
        if message:
            self.add_message("Вы", message, True)
            self.message_input.clear()

    def send_discord_message(self):
        message = self.discord_message_input.text().strip()
        if message:
            self.discord_chat_area.append(f"<span style='color: #fff; text-align: right; display: block;'><b>Вы:</b> {message}</span>")
            self.discord_message_input.clear()

            # Прокручиваем вниз
            self.discord_chat_area.verticalScrollBar().setValue(
                self.discord_chat_area.verticalScrollBar().maximum()
            )

    def toggle_microphone(self):
        """Включение/выключение микрофона"""
        if self.current_style == "telegram":
            is_checked = self.mic_btn.isChecked()
        else:
            is_checked = self.discord_mic_btn.isChecked()

        if is_checked:
            self.start_talking()
        else:
            self.stop_talking()

        self.update_mic_button_style()

    def toggle_participants(self, event):
        """Показ/скрытие списка участников"""
        self.participants_visible = not self.participants_visible
        if self.participants_visible:
            self.participants_widget.show()
        else:
            self.participants_widget.hide()

    def update_mic_button_style(self):
        """Обновляет стиль кнопки микрофона в зависимости от состояния"""
        if not self.is_connected:
            # Отключено от сервера - серый
            mic_style = """
                background-color: #95a5a6;
                border: none;
                border-radius: 20px;
                font-size: 18px;
            """
            if self.current_style == "telegram":
                self.mic_btn.setStyleSheet(f"QPushButton {{ {mic_style} }}")
            else:
                self.discord_mic_btn.setStyleSheet("""
                    QPushButton {
                        background-color: #4f545c;
                        border: none;
                        border-radius: 4px;
                        color: white;
                        font-weight: bold;
                    }
                """)
                self.discord_status_label.setText("Не подключено")
                self.discord_status_label.setStyleSheet("color: #ed4245; font-size: 12px;")
        elif not self.is_talking:
            # Подключено, микрофон выключен - зеленый
            if self.current_style == "telegram":
                self.mic_btn.setStyleSheet("""
                    QPushButton {
                        background-color: #2ecc71;
                        border: none;
                        border-radius: 20px;
                        font-size: 18px;
                    }
                    QPushButton:hover {
                        background-color: #27ae60;
                    }
                """)
            else:
                self.discord_mic_btn.setStyleSheet("""
                    QPushButton {
                        background-color: #43b581;
                        border: none;
                        border-radius: 4px;
                        color: white;
                        font-weight: bold;
                    }
                    QPushButton:hover {
                        opacity: 0.8;
                    }
                """)
                self.discord_mic_btn.setText("Выключить микрофон")
                self.discord_status_label.setText("Подключено")
                self.discord_status_label.setStyleSheet("color: #43b581; font-size: 12px;")
        else:
            # Микрофон включен - красный
            if self.current_style == "telegram":
                self.mic_btn.setStyleSheet("""
                    QPushButton {
                        background-color: #e74c3c;
                        border: none;
                        border-radius: 20px;
                        font-size: 18px;
                    }
                    QPushButton:hover {
                        background-color: #c0392b;
                    }
                """)
            else:
                self.discord_mic_btn.setStyleSheet("""
                    QPushButton {
                        background-color: #ed4245;
                        border: none;
                        border-radius: 4px;
                        color: white;
                        font-weight: bold;
                    }
                    QPushButton:hover {
                        opacity: 0.8;
                    }
                """)
                self.discord_mic_btn.setText("Включить микрофон")
                self.discord_status_label.setText("Говорите...")
                self.discord_status_label.setStyleSheet("color: #ed4245; font-size: 12px;")

    def update_status(self, status):
        self.logger.info(f"Статус обновлен: {status}")

    def show_error(self, message):
        error_msg = f"Ошибка: {message}"
        if self.current_style == "telegram":
            self.add_message("Система", error_msg, False)
        else:
            self.discord_chat_area.append(f"<span style='color: #ed4245;'>Ошибка: {message}</span>")
        self.logger.error(error_msg)

    def toggle_connection(self):
        if self.is_connected:
            self.disconnect_from_server()
        else:
            self.connect_to_server()

    def connect_to_server(self):
        if not pyaudio_available:
            self.show_error("PyAudio не доступен")
            return

        try:
            if self.current_style == "telegram":
                self.add_message("Система", "Подключение к серверу...", False)
            else:
                self.discord_chat_area.append("<span style='color: #72767d;'>Подключение к серверу...</span>")

            # Создаем клиент
            self.voice_client = VoiceClientBackend()
            self.voice_client.status_update.connect(self.update_status)
            self.voice_client.log_message.connect(self.logger.info)
            self.voice_client.connection_update.connect(self.update_connection_status)
            self.voice_client.transmission_update.connect(self.update_transmission_status)

            # Подключаемся к серверу
            if self.voice_client.connect_to_server(SERVER_ADDRESS[0], SERVER_ADDRESS[1]):
                self.is_connected = True
                if self.current_style == "telegram":
                    self.mic_btn.setEnabled(True)
                else:
                    self.discord_mic_btn.setEnabled(True)
                self.update_mic_button_style()
                if self.current_style == "telegram":
                    self.add_message("Система", "Успешно подключено к серверу", False)
                else:
                    self.discord_chat_area.append("<span style='color: #43b581;'>Успешно подключено к серверу</span>")
            else:
                self.show_error("Не удалось подключиться к серверу")

        except Exception as e:
            self.show_error(f"Ошибка подключения: {str(e)}")
            self.logger.error(f"Ошибка подключения: {str(e)}")
            self.logger.error(traceback.format_exc())

    def disconnect_from_server(self):
        if self.voice_client:
            self.voice_client.disconnect_from_server()
            self.voice_client = None

        self.is_connected = False
        if self.current_style == "telegram":
            self.mic_btn.setEnabled(False)
            self.mic_btn.setChecked(False)
        else:
            self.discord_mic_btn.setEnabled(False)
            self.discord_mic_btn.setChecked(False)
        self.update_mic_button_style()
        if self.current_style == "telegram":
            self.add_message("Система", "Отключено от сервера", False)
        else:
            self.discord_chat_area.append("<span style='color: #72767d;'>Отключено от сервера</span>")

    def update_connection_status(self, connected):
        self.is_connected = connected
        if self.current_style == "telegram":
            self.mic_btn.setEnabled(connected)
            if not connected:
                self.mic_btn.setChecked(False)
        else:
            self.discord_mic_btn.setEnabled(connected)
            if not connected:
                self.discord_mic_btn.setChecked(False)
        self.update_mic_button_style()

    def update_transmission_status(self, transmitting):
        self.is_talking = transmitting
        if self.current_style == "telegram":
            self.mic_btn.setChecked(transmitting)
        else:
            self.discord_mic_btn.setChecked(transmitting)
        self.update_mic_button_style()

    def start_talking(self):
        if self.voice_client and self.is_connected:
            self.voice_client.set_transmitting(True)

    def stop_talking(self):
        if self.voice_client and self.is_connected:
            self.voice_client.set_transmitting(False)

    def show_settings(self):
        """Показывает меню настроек"""
        if not hasattr(self, 'settings_menu'):
            self.setup_settings_menu()
        self.settings_menu.show()

    def setup_theme(self):
        palette = QPalette()
        palette.setColor(QPalette.Window, QColor(30, 30, 30))
        palette.setColor(QPalette.WindowText, Qt.white)
        palette.setColor(QPalette.Base, QColor(40, 40, 40))
        palette.setColor(QPalette.AlternateBase, QColor(45, 45, 45))
        palette.setColor(QPalette.Text, Qt.white)
        palette.setColor(QPalette.Button, QColor(50, 50, 50))
        palette.setColor(QPalette.ButtonText, Qt.white)
        palette.setColor(QPalette.Highlight, QColor(65, 130, 210))
        palette.setColor(QPalette.HighlightedText, Qt.white)
        self.setPalette(palette)

        self.setStyleSheet("""
            QWidget {
                background-color: #1e1e1e;
                color: white;
            }
            QTextEdit {
                background-color: #252525;
                border: none;
                padding: 10px;
                color: white;
                font-size: 12px;
            }
            QLineEdit {
                background-color: #2d2d2d;
                border: 1px solid #444;
                padding: 8px;
                border-radius: 20px;
                color: white;
            }
            QScrollBar:vertical {
                background: #252525;
                width: 10px;
            }
            QScrollBar::handle:vertical {
                background: #444;
                min-height: 20px;
                border-radius: 4px;
            }
        """)

    def start_voice_client(self):
        """Запускает голосовой клиент - вызывается при переходе на вкладку"""
        self.connect_to_server()

    def stop_voice_client(self):
        """Останавливает голосовой клиент - вызывается при выходе с вкладки"""
        if self.is_connected:
            self.disconnect_from_server()

    def closeEvent(self, event):
        """Обработчик закрытия окна"""
        if self.is_connected:
            self.disconnect_from_server()
        event.accept()

import os
import logging
import traceback
from PyQt5.QtCore import Qt, QTimer, QObject, pyqtSignal, QSize
from PyQt5.QtGui import QFont, QColor, QPalette
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QListWidget, QListWidgetItem, QLineEdit, QFrame, QScrollArea, QMessageBox,
    QTextEdit, QSplitter, QCheckBox, QSlider, QSpacerItem, QSizePolicy, QStackedWidget, QGroupBox
)
# Локальные импорты
from voice_client_backend import VoiceClientBackend, pyaudio_available
from voice_client_constants import SERVER_ADDRESS, BITRATE, DTX_DEFAULT_ENABLED, DTX_DEFAULT_PACKET_LOSS_PERCENT  # Обновлен импорт


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
        self.resize_pending = False
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
        # Создаем QStackedWidget для управления переключением между режимами
        self.stacked_container = QStackedWidget()
        # Создаем контейнеры для разных стилей
        self.telegram_container = QWidget()
        self.discord_container = QWidget()
        # Настройка интерфейсов
        self.setup_telegram_ui()
        self.setup_discord_ui()
        # Добавляем контейнеры в стек
        self.stacked_container.addWidget(self.telegram_container)
        self.stacked_container.addWidget(self.discord_container)
        # Устанавливаем текущий режим
        self.current_style = "telegram"
        self.stacked_container.setCurrentIndex(0)
        # Добавляем стек в основной макет
        self.main_layout.addWidget(self.stacked_container)

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
        # Кнопка настроек (добавлена)
        self.discord_settings_btn = QPushButton("⚙")
        self.discord_settings_btn.setFixedSize(30, 30)
        self.discord_settings_btn.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                border: none;
                font-size: 16px;
            }
            QPushButton:hover {
                background-color: #3d3d3d;
                border-radius: 15px;
            }
        """)
        self.discord_settings_btn.clicked.connect(self.show_settings)
        server_header_layout.addWidget(self.discord_settings_btn)
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
        self.settings_menu.setFixedSize(350, 350)  # Увеличена высота для новых настроек
        self.settings_menu.setWindowFlags(Qt.Dialog)
        layout = QVBoxLayout(self.settings_menu)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(15)
        # Информация о битрейте
        bitrate_label = QLabel("Текущий битрейт:")
        bitrate_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(bitrate_label)
        bitrate_value = QLabel(f"{BITRATE // 1000} kbps (оптимизирован для голоса)")
        bitrate_value.setStyleSheet("color: #3498db;")
        layout.addWidget(bitrate_value)
        bitrate_info = QLabel("Битрейт установлен на 24 kbps для оптимального качества голоса")
        bitrate_info.setStyleSheet("color: #AAAAAA; font-size: 11px;")
        bitrate_info.setWordWrap(True)
        layout.addWidget(bitrate_info)

        # Новая группа настроек DTX
        dtx_group = QGroupBox("Настройки DTX (Discontinuous Transmission)")
        dtx_layout = QVBoxLayout(dtx_group)

        # Чекбокс для включения DTX
        self.dtx_checkbox = QCheckBox("Включить DTX")
        self.dtx_checkbox.setChecked(DTX_DEFAULT_ENABLED)  # Установка значения по умолчанию
        self.dtx_checkbox.stateChanged.connect(self.on_dtx_toggled)
        dtx_layout.addWidget(self.dtx_checkbox)

        # Слайдер для процента потерь пакетов
        self.dtx_slider_label = QLabel(f"Ожидаемый процент потерь пакетов: {DTX_DEFAULT_PACKET_LOSS_PERCENT}%")
        self.dtx_slider_label.setStyleSheet("color: #AAAAAA; font-size: 11px;")
        dtx_layout.addWidget(self.dtx_slider_label)

        self.dtx_slider = QSlider(Qt.Horizontal)
        self.dtx_slider.setRange(0, 100)
        self.dtx_slider.setValue(DTX_DEFAULT_PACKET_LOSS_PERCENT)  # Установка значения по умолчанию
        self.dtx_slider.valueChanged.connect(self.on_dtx_slider_changed)
        # Устанавливаем начальное состояние слайдера в зависимости от чекбокса
        self.dtx_slider.setEnabled(DTX_DEFAULT_ENABLED)
        dtx_layout.addWidget(self.dtx_slider)

        layout.addWidget(dtx_group)

        layout.addStretch()
        # Кнопка закрытия
        close_btn = QPushButton("Закрыть")
        close_btn.clicked.connect(self.settings_menu.hide)
        layout.addWidget(close_btn)

    def setup_participants_bar(self, layout):
        """Создает панель участников (горизонтальный список)"""
        self.participants_widget = QWidget()
        self.participants_widget.setFixedHeight(60)
        self.participants_widget.hide()
        participants_layout = QHBoxLayout(self.participants_widget)
        participants_layout.setContentsMargins(10, 5, 10, 5)
        participants_layout.setSpacing(10)
        participants_label = QLabel("Участники:")
        participants_label.setStyleSheet("font-weight: bold;")
        participants_layout.addWidget(participants_label)
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
        self.message_input = QLineEdit()
        self.message_input.setPlaceholderText("Введите сообщение...")
        self.message_input.returnPressed.connect(self.send_message)
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
        # Защита от рекурсивных вызовов
        if self.resize_pending:
            return
        self.resize_pending = True
        try:
            width = event.size().width()
            if width >= 600 and self.current_style != "discord":
                self.current_style = "discord"
                self.stacked_container.setCurrentIndex(1)
                self.update_mic_button_style()  # Добавлено
            elif width < 600 and self.current_style != "telegram":
                self.current_style = "telegram"
                self.stacked_container.setCurrentIndex(0)
                self.update_mic_button_style()  # Добавлено
        finally:
            self.resize_pending = False
            super().resizeEvent(event)

    def add_message(self, sender, message, is_me):
        """Добавляет сообщение в чат с правильным выравниванием"""
        if is_me:
            self.chat_area.append(f"<div style='text-align: right; color: #3498db;'><b>{sender}:</b> {message}</div>")
        else:
            self.chat_area.append(f"<div style='text-align: left;'><b>{sender}:</b> {message}</div>")
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
        # Обновляем состояние обеих кнопок независимо от стиля
        self.mic_btn.setEnabled(self.is_connected)
        self.discord_mic_btn.setEnabled(self.is_connected)
        if not self.is_connected:
            # Отключено от сервера - серый
            telegram_style = """
                QPushButton {
                    background-color: #95a5a6;
                    border: none;
                    border-radius: 20px;
                    font-size: 18px;
                }
            """
            discord_style = """
                QPushButton {
                    background-color: #4f545c;
                    border: none;
                    border-radius: 4px;
                    color: white;
                    font-weight: bold;
                }
            """
            # discord_status = "Не подключено"
            # discord_status_color = "#ed4245"
        elif not self.is_talking:
            # Подключено, микрофон выключен - КРАСНЫЙ (ожидание)
            telegram_style = """
                QPushButton {
                    background-color: #e74c3c;
                    border: none;
                    border-radius: 20px;
                    font-size: 18px;
                }
                QPushButton:hover {
                    background-color: #c0392b;
                }
            """
            discord_style = """
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
            """
            # discord_status = "Подключено"
            # discord_status_color = "#ed4245"
        else:
            # Микрофон включен - ЗЕЛЁНЫЙ (передача)
            telegram_style = """
                QPushButton {
                    background-color: #2ecc71;
                    border: none;
                    border-radius: 20px;
                    font-size: 18px;
                }
                QPushButton:hover {
                    background-color: #27ae60;
                }
            """
            discord_style = """
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
            """
        # Применяем стили к текущей активной кнопке
        if self.current_style == "telegram":
            self.mic_btn.setStyleSheet(telegram_style)
        else:
            self.discord_mic_btn.setStyleSheet(discord_style)

    def update_status(self, status):
        """Обновление статуса соединения"""
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
            # Подключаемся к сигналу обновления настроек DTX
            self.voice_client.dtx_settings_update.connect(self.update_dtx_ui_from_backend)
            # Подключаемся к серверу
            if self.voice_client.connect_to_server(SERVER_ADDRESS[0], SERVER_ADDRESS[1]):
                self.is_connected = True
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
            self.add_message("Система", "Отключено от сервера", False)
        else:
            self.discord_chat_area.append("<span style='color: #72767d;'>Отключено от сервера</span>")

    def update_connection_status(self, connected):
        self.is_connected = connected
        # Обновляем состояние обеих кнопок независимо от стиля
        self.mic_btn.setEnabled(connected)
        self.discord_mic_btn.setEnabled(connected)
        if not connected:
            # Сбрасываем состояние кнопок при отключении
            self.mic_btn.setChecked(False)
            self.discord_mic_btn.setChecked(False)
        self.update_mic_button_style()  # Обновляем стиль для текущей активной кнопки

    def update_transmission_status(self, transmitting):
        self.is_talking = transmitting
        if self.current_style == "telegram":
            self.mic_btn.setChecked(transmitting)
        else:
            self.discord_mic_btn.setChecked(transmitting)
        self.update_mic_button_style()

    def update_dtx_ui_from_backend(self, enabled: bool, packet_loss_percent: int):
        """Обновляет UI на основе настроек DTX, полученных от бэкенда"""
        if hasattr(self, 'dtx_checkbox') and hasattr(self, 'dtx_slider'):
            self.dtx_checkbox.blockSignals(True)
            self.dtx_slider.blockSignals(True)
            self.dtx_checkbox.setChecked(enabled)
            self.dtx_slider.setValue(packet_loss_percent)
            self.dtx_slider.setEnabled(enabled)
            self.dtx_slider_label.setText(f"Ожидаемый процент потерь пакетов: {packet_loss_percent}%")
            self.dtx_checkbox.blockSignals(False)
            self.dtx_slider.blockSignals(False)

    def on_dtx_toggled(self, state):
        """Обработчик изменения состояния чекбокса DTX"""
        if self.voice_client and self.is_connected:
            enabled = state == Qt.Checked
            self.voice_client.set_dtx(enabled)
            # Обновляем состояние слайдера
            self.dtx_slider.setEnabled(enabled)
            # Обновляем метку слайдера
            self.dtx_slider_label.setText(f"Ожидаемый процент потерь пакетов: {self.dtx_slider.value()}%")

    def on_dtx_slider_changed(self, value):
        """Обработчик изменения значения слайдера DTX"""
        if self.voice_client and self.is_connected:
            self.voice_client.set_dtx_packet_loss_percent(value)
            # Обновляем метку слайдера
            self.dtx_slider_label.setText(f"Ожидаемый процент потерь пакетов: {value}%")

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
            QGroupBox {
                font-weight: bold;
                border: 1px solid #444;
                border-radius: 5px;
                margin-top: 1ex; /* оставляем место для заголовка */
                padding-top: 10px; /* отступ внутри группы */
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top center; /* позиционируем заголовок по центру сверху */
                padding: 0 5px;
                background-color: #1e1e1e;
            }
            QSlider::groove:horizontal {
                border: 1px solid #999999;
                height: 8px; /* высота трека */
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #B1B1B1, stop:1 #c4c4c4);
                margin: 2px 0;
            }

            QSlider::handle:horizontal {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #b4b4b4, stop:1 #8f8f8f);
                border: 1px solid #5c5c5c;
                width: 18px;
                margin: -2px 0; /* увеличиваем размер ручки */
                border-radius: 3px;
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

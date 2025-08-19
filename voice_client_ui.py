# voice_client_ui.py
import os
import sys
import logging
import platform
import traceback
import socket
import threading
import time
import queue
import struct
import ctypes
from ctypes import c_ubyte, c_int32, c_int16, c_int, byref, POINTER, CFUNCTYPE, cdll
from collections import deque
from datetime import datetime
from PyQt5.QtCore import Qt, QTimer, QObject, pyqtSignal, QSize
from PyQt5.QtGui import QFont, QColor, QPalette, QIcon
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QListWidget, QListWidgetItem, QLineEdit, QFrame, QScrollArea, QMessageBox,
    QTextEdit, QSizePolicy, QSplitter
)

# Конфигурация
SAMPLE_RATE = 48000
CHANNELS = 1
FRAME_SIZE = 480
BUFFER_DURATION_MS = 200
KEEP_ALIVE_INTERVAL = 1.0
SERVER_ADDRESS = ('194.31.171.29', 38592)

# Попытка импорта PyAudio
try:
    import pyaudio
    pyaudio_available = True
except ImportError:
    pyaudio = None
    pyaudio_available = False

# Настройка логирования для бэкенда
def setup_backend_logging():
    if not os.path.exists('logs'):
        os.makedirs('logs')

    logger = logging.getLogger('VoiceClientBackend')
    logger.setLevel(logging.DEBUG)

    # Файловый обработчик
    fh = logging.FileHandler('logs/voice_backend.log')
    fh.setLevel(logging.DEBUG)

    # Консольный обработчик
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)

    # Форматтер
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    fh.setFormatter(formatter)
    ch.setFormatter(formatter)

    # Добавляем обработчики
    logger.addHandler(fh)
    logger.addHandler(ch)

    return logger

class VoiceClientBackend(QObject):
    status_update = pyqtSignal(str)
    log_message = pyqtSignal(str)
    connection_update = pyqtSignal(bool)
    transmission_update = pyqtSignal(bool)

    def __init__(self):
        super().__init__()
        self.is_transmitting = False
        self.is_connected = False
        self.running = False
        self.logger = setup_backend_logging()

        # Аудио буферы
        self.playback_buffer = deque(maxlen=SAMPLE_RATE * BUFFER_DURATION_MS // 1000)

        # Сеть
        self.socket = None
        self.server_address = None

        # Очереди для межпоточного обмена
        self.audio_queue = queue.Queue()
        self.network_queue = queue.Queue()

        # Аудио потоки
        self.audio_stream_in = None
        self.audio_stream_out = None
        self.pyaudio_instance = None

        # Статистика
        self.packets_sent = 0
        self.packets_received = 0
        self.last_stat_time = time.time()

        # Потоки
        self.threads = []

        # Opus
        self.opus = None
        self.encoder = None
        self.decoder = None

        # Инициализация Opus
        self._init_opus()

    def _init_opus(self):
        """Инициализация библиотеки Opus"""
        try:
            if platform.system() == 'Windows':
                lib_path = 'libopus.dll'
            else:
                lib_path = './libopus.so.0.10.1'

            self.opus = cdll.LoadLibrary(lib_path)
            self.logger.info(f"Opus библиотека загружена: {lib_path}")

            # Определяем прототипы функций
            self.opus.opus_encoder_create.restype = ctypes.c_void_p
            self.opus.opus_encoder_create.argtypes = [c_int32, c_int, c_int, ctypes.POINTER(c_int)]

            self.opus.opus_decoder_create.restype = ctypes.c_void_p
            self.opus.opus_decoder_create.argtypes = [c_int32, c_int, ctypes.POINTER(c_int)]

            self.opus.opus_encode.restype = c_int
            self.opus.opus_encode.argtypes = [ctypes.c_void_p, ctypes.POINTER(c_int16), c_int,
                                              ctypes.POINTER(c_ubyte), c_int32]

            self.opus.opus_decode.restype = c_int
            self.opus.opus_decode.argtypes = [ctypes.c_void_p, ctypes.POINTER(c_ubyte), c_int32,
                                              ctypes.POINTER(c_int16), c_int, c_int]

            # Константы Opus
            OPUS_APPLICATION_AUDIO = 2049

            # Создаем кодировщик
            error = c_int(0)
            self.encoder = self.opus.opus_encoder_create(SAMPLE_RATE, CHANNELS, OPUS_APPLICATION_AUDIO, byref(error))
            if error.value != 0:
                raise Exception(f"Ошибка создания кодировщика: {error.value}")

            # Создаем декодер
            error = c_int(0)
            self.decoder = self.opus.opus_decoder_create(SAMPLE_RATE, CHANNELS, byref(error))
            if error.value != 0:
                raise Exception(f"Ошибка создания декодера: {error.value}")

            self.logger.info("Opus кодек инициализирован успешно")

        except Exception as e:
            error_msg = f"Ошибка инициализации Opus: {str(e)}"
            self.logger.error(error_msg)
            self.opus = None

    def connect_to_server(self, server_ip, server_port):
        """Подключение к серверу"""
        try:
            if not self.opus:
                raise Exception("Opus не инициализирован")

            if not pyaudio_available:
                raise Exception("PyAudio не доступен")

            self.server_address = (server_ip, server_port)
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.socket.setblocking(False)

            self.logger.info(f"Попытка подключения к серверу {server_ip}:{server_port}")

            # Тестовый пакет для проверки соединения
            test_packet = b'\x00'
            self.socket.sendto(test_packet, self.server_address)
            self.logger.info("Отправлен тестовый пакет на сервер")

            self.is_connected = True
            self.running = True

            # Запускаем потоки
            self._start_threads()

            self.connection_update.emit(True)
            self.status_update.emit("Подключено к серверу")
            self.logger.info("Успешно подключено к серверу")
            return True

        except Exception as e:
            error_msg = f"Ошибка подключения: {str(e)}"
            self.logger.error(error_msg)
            self.logger.error(traceback.format_exc())
            self.connection_update.emit(False)
            return False

    def disconnect_from_server(self):
        """Отключение от сервера"""
        self.logger.info("Начало отключения от сервера")
        self.running = False
        self.is_connected = False

        # Останавливаем аудио потоки
        self._stop_audio_streams()

        # Останавливаем все потоки
        for thread in self.threads:
            if thread.is_alive():
                thread.join(timeout=1.0)

        self.threads = []

        if self.socket:
            self.socket.close()
            self.socket = None

        self.connection_update.emit(False)
        self.status_update.emit("Отключено от сервера")
        self.logger.info("Успешно отключено от сервера")

    def set_transmitting(self, transmitting):
        """Установка режима передачи"""
        self.is_transmitting = transmitting
        self.transmission_update.emit(transmitting)
        self.logger.info(f"Режим передачи: {transmitting}")

    def _init_audio(self):
        """Инициализация аудио"""
        if not pyaudio_available:
            return False

        try:
            self.pyaudio_instance = pyaudio.PyAudio()

            # Настройка аудио захвата
            self.audio_stream_in = self.pyaudio_instance.open(
                format=pyaudio.paInt16,
                channels=CHANNELS,
                rate=SAMPLE_RATE,
                input=True,
                frames_per_buffer=FRAME_SIZE,
                stream_callback=self._audio_callback
            )

            # Настройка аудио воспроизведения
            self.audio_stream_out = self.pyaudio_instance.open(
                format=pyaudio.paFloat32,
                channels=CHANNELS,
                rate=SAMPLE_RATE,
                output=True,
                frames_per_buffer=FRAME_SIZE
            )

            self.logger.info("Аудио потоки успешно инициализированы")
            return True

        except Exception as e:
            error_msg = f"Ошибка инициализации аудио: {str(e)}"
            self.logger.error(error_msg)
            self.logger.error(traceback.format_exc())
            return False

    def _stop_audio_streams(self):
        """Остановка аудио потоков"""
        try:
            if self.audio_stream_in and self.audio_stream_in.is_active():
                self.audio_stream_in.stop_stream()
                self.audio_stream_in.close()

            if self.audio_stream_out and self.audio_stream_out.is_active():
                self.audio_stream_out.stop_stream()
                self.audio_stream_out.close()

            if self.pyaudio_instance:
                self.pyaudio_instance.terminate()
                self.pyaudio_instance = None

            self.logger.info("Аудио потоки остановлены")
        except Exception as e:
            error_msg = f"Ошибка остановки аудио: {str(e)}"
            self.logger.error(error_msg)
            self.logger.error(traceback.format_exc())

    def _audio_callback(self, in_data, frame_count, time_info, status):
        """Callback для захвата аудио"""
        try:
            if self.is_transmitting and self.is_connected:
                # Кодируем в Opus
                encoded = (c_ubyte * 400)()

                # Преобразуем байты в массив int16
                pcm_data = (c_int16 * FRAME_SIZE).from_buffer_copy(in_data)

                # Кодируем
                result = self.opus.opus_encode(self.encoder, pcm_data, FRAME_SIZE, encoded, 400)

                if result > 0:
                    packet_data = bytes(encoded[:result])
                    self.network_queue.put(packet_data)
                    self.packets_sent += 1
                    self.logger.debug(f"Отправлен аудио пакет, размер: {result} байт")

            return (None, pyaudio.paContinue)
        except Exception as e:
            error_msg = f"Ошибка в audio callback: {str(e)}"
            self.logger.error(error_msg)
            self.logger.error(traceback.format_exc())
            return (None, pyaudio.paContinue)

    def _start_threads(self):
        """Запуск рабочих потоков"""
        # Инициализируем аудио
        if not self._init_audio():
            error_msg = "Не удалось инициализировать аудио"
            self.logger.error(error_msg)
            return False

        # Запускаем потоки
        threads = [
            threading.Thread(target=self._transmit_thread, name="TransmitThread"),
            threading.Thread(target=self._receive_thread, name="ReceiveThread"),
            threading.Thread(target=self._playback_thread, name="PlaybackThread"),
            threading.Thread(target=self._keepalive_thread, name="KeepAliveThread"),
        ]

        for thread in threads:
            thread.daemon = True
            thread.start()
            self.threads.append(thread)
            self.logger.info(f"Запущен поток: {thread.name}")

        return True

    def _transmit_thread(self):
        """Поток передачи данных"""
        self.logger.info("Поток передачи данных запущен")
        while self.running:
            try:
                data = self.network_queue.get(timeout=0.1)
                if self.socket and self.is_connected:
                    try:
                        self.socket.sendto(data, self.server_address)
                        self.logger.debug(f"Отправлен пакет на сервер, размер: {len(data)} байт")
                    except Exception as e:
                        self.logger.error(f"Ошибка отправки пакета: {str(e)}")
            except queue.Empty:
                continue
            except Exception as e:
                error_msg = f"Ошибка передачи: {str(e)}"
                self.logger.error(error_msg)
                self.logger.error(traceback.format_exc())
        self.logger.info("Поток передачи данных остановлен")

    def _receive_thread(self):
        """Поток приема данных"""
        self.logger.info("Поток приема данных запущен")
        while self.running:
            try:
                if self.socket:
                    try:
                        data, addr = self.socket.recvfrom(4000)
                        self.packets_received += 1
                        self.logger.debug(f"Получен пакет от {addr}, размер: {len(data)} байт")

                        # Игнорируем keep-alive пакеты
                        if len(data) <= 1:
                            continue

                        # Декодируем аудио
                        pcm_data = (c_int16 * FRAME_SIZE)()

                        # Создаем массив c_ubyte из полученных данных
                        c_ubyte_array = (c_ubyte * len(data)).from_buffer_copy(data)

                        result = self.opus.opus_decode(self.decoder, c_ubyte_array, len(data), pcm_data, FRAME_SIZE, 0)

                        if result > 0:
                            # Конвертируем в float32 и добавляем в буфер
                            audio_data = [sample / 32768.0 for sample in pcm_data[:result]]
                            self.audio_queue.put(audio_data)
                            self.logger.debug(f"Декодирован аудио пакет, размер: {result} семплов")
                        else:
                            self.logger.warning(f"Ошибка декодирования Opus: {result}")

                    except BlockingIOError:
                        time.sleep(0.001)
                    except ConnectionResetError as e:
                        error_msg = f"Соединение разорвано сервером: {str(e)}"
                        self.logger.error(error_msg)
                        self.running = False
                        self.is_connected = False
                        self.connection_update.emit(False)
                        self.status_update.emit("Соединение разорвано сервером")
                    except Exception as e:
                        error_msg = f"Ошибка приема: {str(e)}"
                        self.logger.error(error_msg)
                        self.logger.error(traceback.format_exc())
            except Exception as e:
                error_msg = f"Критическая ошибка в потоке приема: {str(e)}"
                self.logger.error(error_msg)
                self.logger.error(traceback.format_exc())
        self.logger.info("Поток приема данных остановлен")

    def _playback_thread(self):
        """Поток воспроизведения аудио"""
        self.logger.info("Поток воспроизведения аудио запущен")
        while self.running:
            try:
                audio_data = self.audio_queue.get(timeout=0.1)

                # Преобразуем в байты
                audio_bytes = struct.pack(f'{len(audio_data)}f', *audio_data)
                self.audio_stream_out.write(audio_bytes)
                self.logger.debug("Воспроизведен аудио пакет")

            except queue.Empty:
                # Воспроизводим тишину если данных нет
                silence = [0.0] * FRAME_SIZE
                audio_bytes = struct.pack(f'{FRAME_SIZE}f', *silence)
                try:
                    self.audio_stream_out.write(audio_bytes)
                except Exception as e:
                    if self.running:  # Логируем только если поток еще работает
                        self.logger.error(f"Ошибка воспроизведения тишины: {str(e)}")
            except Exception as e:
                error_msg = f"Ошибка воспроизведения: {str(e)}"
                self.logger.error(error_msg)
                self.logger.error(traceback.format_exc())
        self.logger.info("Поток воспроизведения аудио остановлен")

    def _keepalive_thread(self):
        """Поток отправки keep-alive пакетов"""
        self.logger.info("Поток keep-alive запущен")
        while self.running:
            if self.is_connected and not self.is_transmitting:
                try:
                    if self.socket:
                        self.socket.sendto(b'\x00', self.server_address)
                        self.logger.debug("Отправлен keep-alive пакет")
                except Exception as e:
                    error_msg = f"Ошибка отправки keep-alive: {str(e)}"
                    self.logger.error(error_msg)
            time.sleep(KEEP_ALIVE_INTERVAL)
        self.logger.info("Поток keep-alive остановлен")

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
        
        # Показываем Telegram стиль по умолчанию
        self.main_layout.addWidget(self.telegram_container)
        self.discord_container.hide()

    def setup_telegram_ui(self):
        """Настройка Telegram-стиля интерфейса"""
        layout = QVBoxLayout(self.telegram_container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Верхняя панель (Telegram-style)
        self.setup_telegram_top_bar(layout)

        # Список участников (горизонтальный, скрываемый)
        self.setup_participants_bar(layout)

        # Область чата
        self.setup_chat_area(layout)

        # Нижняя панель с полем ввода
        self.setup_input_area(layout)

    def setup_discord_ui(self):
        """Настройка Discord-стиля интерфейса"""
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
        self.discord_chat_area.append("<span style='color: #fff; text-align: right; display: block;'><b>Вы:</b> Здравствуйте!</span>")
        
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
        """Создает верхнюю панель в стиле Telegram"""
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

        # Кнопка настроек (заглушка)
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
            # Переключаемся на Discord-стиль
            self.current_style = "discord"
            self.telegram_container.hide()
            self.discord_container.show()
            self.main_layout.addWidget(self.discord_container)
        elif width < 600 and self.current_style != "telegram":
            # Переключаемся на Telegram-стиль
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
        """Обработчик отправки сообщения в Telegram-стиле"""
        message = self.message_input.text().strip()
        if message:
            self.add_message("Вы", message, True)
            self.message_input.clear()

    def send_discord_message(self):
        """Обработчик отправки сообщения в Discord-стиле"""
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
            if self.voice_client.connect_to_server("194.31.171.29", 38592):
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
        
        # Стили для Telegram-режима
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
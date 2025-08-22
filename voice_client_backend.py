import os
import sys
import uuid
import time
import threading
import queue
import logging
import socket
import struct
import traceback
import ctypes
import ctypes.util
from collections import defaultdict, deque

import pyaudio

from PyQt5.QtCore import QObject, pyqtSignal, QTimer

# Импортируем константы из вашего файла
from voice_client_constants import (
    SAMPLE_RATE, CHANNELS, FRAME_SIZE, BUFFER_DURATION_MS,
    KEEP_ALIVE_INTERVAL, SERVER_ADDRESS, OPUS_APPLICATION_VOIP,
    OPUS_SIGNAL_VOICE, BITRATE, JITTER_BUFFER_MAX_SIZE,
    JITTER_BUFFER_MIN_SIZE, JITTER_BUFFER_TARGET_SIZE,
    PLC_MAX_SKIP_FRAMES, CLIENT_ID_LEN
)

# --- Настройки логирования для backend ---
logger = logging.getLogger('VoiceClientBackend')
if not logger.handlers and not os.path.exists('logs'):
    os.makedirs('logs')
if not logger.handlers:
    logger.setLevel(logging.DEBUG)
    fh = logging.FileHandler('logs/voice_backend.log')
    fh.setLevel(logging.DEBUG)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    fh.setFormatter(formatter)
    ch.setFormatter(formatter)
    logger.addHandler(fh)
    logger.addHandler(ch)
# --- Конец настроек логирования ---

# Проверка доступности pyaudio
pyaudio_available = True
try:
    import pyaudio
except ImportError:
    pyaudio_available = False
    logger.error("PyAudio не найден. Голосовой клиент не будет работать.")

# --- Загрузка и инициализация libopus с помощью ctypes ---
opuslib = None
opus_available = False

# Определяем типы для ctypes
opus_int32 = ctypes.c_int32
opus_int16 = ctypes.c_int16
opus_uint32 = ctypes.c_uint32
opus_int8 = ctypes.c_int8
opus_uint8 = ctypes.c_uint8
opus_int64 = ctypes.c_int64
opus_uint64 = ctypes.c_uint64
# --- ИСПРАВЛЕНИЕ: добавлена недостающая строка для c_int_p ---
c_int_p = ctypes.POINTER(ctypes.c_int)
# --- ИСПРАВЛЕНИЕ: добавлена недостающая строка для c_short_p ---
c_short_p = ctypes.POINTER(ctypes.c_short)
# --- КОНЕЦ ИСПРАВЛЕНИЙ ---
c_ubyte_p = ctypes.POINTER(ctypes.c_ubyte)
c_uint_p = ctypes.POINTER(ctypes.c_uint)
c_ulong_p = ctypes.POINTER(ctypes.c_ulong)
c_ushort_p = ctypes.POINTER(ctypes.c_ushort)

OPUS_OK = 0
OPUS_BAD_ARG = -1
OPUS_BUFFER_TOO_SMALL = -2
OPUS_INTERNAL_ERROR = -3
OPUS_INVALID_PACKET = -4
OPUS_UNIMPLEMENTED = -5
OPUS_INVALID_STATE = -6
OPUS_ALLOC_FAIL = -7

OPUS_APPLICATION_VOIP = 2048
OPUS_APPLICATION_AUDIO = 2049
OPUS_APPLICATION_RESTRICTED_LOWDELAY = 2051

OPUS_SET_BITRATE_REQUEST = 4002
OPUS_SET_VBR_REQUEST = 10006
OPUS_SET_COMPLEXITY_REQUEST = 4010
OPUS_SET_SIGNAL_REQUEST = 4024

OPUS_SIGNAL_VOICE = 3001
OPUS_SIGNAL_MUSIC = 3002

MAX_PACKET_SIZE = 4000

try:
    # Попытка найти библиотеку Opus
    opuslib_path = ctypes.util.find_library("opus")
    if opuslib_path is None:
        # Попробуем распространенные имена
        for name in ['libopus', 'opus']:
            opuslib_path = ctypes.util.find_library(name)
            if opuslib_path:
                break
    if opuslib_path is None:
        # Если не найдено, попробуем прямые пути (например, для Windows)
        if os.name == 'nt':  # Windows
            # Предполагаем, что opus.dll находится рядом или в PATH
            potential_paths = ['opus.dll', 'libopus.dll', os.path.join(os.path.dirname(__file__), 'opus.dll')]
            for path in potential_paths:
                if os.path.exists(path):
                    opuslib_path = path
                    break
        elif os.name == 'posix':  # Linux/macOS
            # Предполагаем, что libopus.so или libopus.dylib находится рядом или в стандартных путях
            potential_paths = ['libopus.so', 'libopus.dylib']
            for path in potential_paths:
                if os.path.exists(path):
                    opuslib_path = path
                    break

    if opuslib_path:
        opuslib = ctypes.CDLL(opuslib_path)
        logger.info(f"libopus загружена из: {opuslib_path}")

        # Определение сигнатур функций
        # opus_encoder_create
        opuslib.opus_encoder_create.argtypes = (opus_int32, ctypes.c_int, opus_int32, c_int_p)
        opuslib.opus_encoder_create.restype = ctypes.c_void_p

        # opus_encoder_ctl
        # opus_encoder_ctl принимает переменное число аргументов, поэтому указываем только обязательные
        opuslib.opus_encoder_ctl.argtypes = (ctypes.c_void_p, opus_int32)  # Остальные аргументы передаются напрямую
        opuslib.opus_encoder_ctl.restype = opus_int32

        # opus_encode
        # --- ИСПРАВЛЕНИЕ: Используем c_short_p вместо c_ushort_p ---
        opuslib.opus_encode.argtypes = (ctypes.c_void_p, c_short_p, ctypes.c_int, c_ubyte_p, opus_int32)
        # --- КОНЕЦ ИСПРАВЛЕНИЯ ---
        opuslib.opus_encode.restype = opus_int32

        # opus_encoder_destroy
        opuslib.opus_encoder_destroy.argtypes = (ctypes.c_void_p,)
        opuslib.opus_encoder_destroy.restype = None

        # opus_decoder_create
        opuslib.opus_decoder_create.argtypes = (opus_int32, ctypes.c_int, c_int_p)
        opuslib.opus_decoder_create.restype = ctypes.c_void_p

        # opus_decoder_ctl
        # opus_decoder_ctl также принимает переменные аргументы
        opuslib.opus_decoder_ctl.argtypes = (ctypes.c_void_p, opus_int32)  # Остальные аргументы передаются напрямую
        opuslib.opus_decoder_ctl.restype = opus_int32

        # opus_decode
        # --- ИСПРАВЛЕНИЕ: Используем c_short_p вместо c_ushort_p для выходного буфера ---
        opuslib.opus_decode.argtypes = (ctypes.c_void_p, c_ubyte_p, opus_int32, c_short_p, ctypes.c_int, ctypes.c_int)
        # --- КОНЕЦ ИСПРАВЛЕНИЯ ---
        opuslib.opus_decode.restype = opus_int32

        # opus_decoder_destroy
        opuslib.opus_decoder_destroy.argtypes = (ctypes.c_void_p,)
        opuslib.opus_decoder_destroy.restype = None

        # opus_strerror
        opuslib.opus_strerror.argtypes = (ctypes.c_int,)
        opuslib.opus_strerror.restype = ctypes.c_char_p

        opus_available = True
    else:
        logger.error("libopus не найдена. Голосовой клиент не будет работать.")
except Exception as e:
    logger.error(f"Ошибка загрузки libopus: {e}")
    traceback.print_exc()


def opus_strerror(error_code):
    """Получает строковое описание ошибки Opus."""
    if opuslib and hasattr(opuslib, 'opus_strerror'):
        return opuslib.opus_strerror(error_code).decode('utf-8')
    else:
        return f"Opus error code: {error_code}"


class OpusEncoder:
    """Простая обертка над Opus C API для энкодера."""

    def __init__(self, fs, channels, application):
        self.fs = fs
        self.channels = channels
        self.application = application
        self.encoder_state = None
        self._create_encoder()

    def _create_encoder(self):
        if not opus_available or not opuslib:
            raise RuntimeError("libopus недоступна")

        err = ctypes.c_int()
        self.encoder_state = opuslib.opus_encoder_create(self.fs, self.channels, self.application, ctypes.byref(err))
        if err.value != OPUS_OK or not self.encoder_state:
            raise RuntimeError(f"Не удалось создать Opus энкодер: {opus_strerror(err.value)}")

        # Установка параметров (если нужно)
        # err = opuslib.opus_encoder_ctl(self.encoder_state, OPUS_SET_SIGNAL_REQUEST, OPUS_SIGNAL_VOICE)
        # if err.value != OPUS_OK:
        #     logger.warning(f"Не удалось установить OPUS_SIGNAL_VOICE: {opus_strerror(err.value)}")

        # err = opuslib.opus_encoder_ctl(self.encoder_state, OPUS_SET_BITRATE_REQUEST, BITRATE)
        # if err.value != OPUS_OK:
        #     logger.warning(f"Не удалось установить битрейт {BITRATE}: {opus_strerror(err.value)}")

    def encode(self, pcm_data, frame_size):
        """Кодирует PCM данные в Opus пакет."""
        if not self.encoder_state:
            raise RuntimeError("Энкодер не инициализирован")

        # --- ИСПРАВЛЕНИЕ: Создание массива c_short ---
        # Преобразуем байты PCM в массив c_short (знаковые 16-битные)
        # from_buffer_copy работает корректно с c_short для данных PyAudio paInt16
        pcm_array = (ctypes.c_short * (len(pcm_data) // 2)).from_buffer_copy(pcm_data)
        opus_data = (ctypes.c_ubyte * MAX_PACKET_SIZE)()

        # --- ИСПРАВЛЕНИЕ: Передача pcm_array (который автоматически преобразуется в c_short_p) ---
        result = opuslib.opus_encode(self.encoder_state, pcm_array, frame_size, opus_data, MAX_PACKET_SIZE)
        # --- КОНЕЦ ИСПРАВЛЕНИЯ ---

        if result < 0:
            raise RuntimeError(f"Ошибка кодирования Opus: {opus_strerror(result)}")

        # Возвращаем байты
        return bytes(opus_data[:result])

    def __del__(self):
        if self.encoder_state and opuslib and hasattr(opuslib, 'opus_encoder_destroy'):
            opuslib.opus_encoder_destroy(self.encoder_state)
            self.encoder_state = None


class OpusDecoder:
    """Простая обертка над Opus C API для декодера."""

    def __init__(self, fs, channels):
        self.fs = fs
        self.channels = channels
        self.decoder_state = None
        self._create_decoder()

    def _create_decoder(self):
        if not opus_available or not opuslib:
            raise RuntimeError("libopus недоступна")

        err = ctypes.c_int()
        self.decoder_state = opuslib.opus_decoder_create(self.fs, self.channels, ctypes.byref(err))
        if err.value != OPUS_OK or not self.decoder_state:
            raise RuntimeError(f"Не удалось создать Opus декодер: {opus_strerror(err.value)}")

    def decode(self, data, frame_size):
        """
        Декодирует Opus пакет в PCM данные.
        :param  Байты Opus данных или None для PLC.
        :param frame_size: Размер фрейма в сэмплах.
        :return: Байты PCM данных.
        """
        if not self.decoder_state:
            raise RuntimeError("Декодер не инициализирован")

        # --- ИСПРАВЛЕНИЕ: Создание выходного буфера как массива c_short ---
        # Opus декодирует в знаковые 16-битные целые. PyAudio paInt16 ожидает их же.
        pcm_data = (ctypes.c_short * (frame_size * self.channels))()
        # --- КОНЕЦ ИСПРАВЛЕНИЯ ---
        opus_data = None
        data_len = 0

        if data is not None:
            # Убедимся, что data - это bytes
            if isinstance(data, bytearray):
                data = bytes(data)
            opus_data = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
            data_len = len(data)

        # data может быть None для PLC, тогда opus_data будет None, data_len = 0
        # --- ИСПРАВЛЕНИЕ: Передача pcm_data (который автоматически преобразуется в c_short_p) ---
        result = opuslib.opus_decode(self.decoder_state, opus_data, data_len, pcm_data, frame_size, 0)
        # --- КОНЕЦ ИСПРАВЛЕНИЯ ---

        if result < 0:
            raise RuntimeError(f"Ошибка декодирования Opus: {opus_strerror(result)}")

        # Возвращаем байты
        # result содержит количество декодированных сэмплов (не байтов!)
        decoded_samples = result * self.channels
        # --- ИСПРАВЛЕНИЕ: Преобразование c_short массива в байты ---
        # from_buffer_copy создает копию из объекта ctypes, здесь мы создаем байты напрямую
        # bytes(pcm_data) не работает, нужно использовать другой способ
        # Можно использовать array.array или struct.pack, но самый прямой способ с ctypes:
        # ctypes.string_at(pcm_data, decoded_samples * ctypes.sizeof(ctypes.c_short)) - работает, но возвращает bytes
        # Но проще преобразовать в bytes через memoryview или array
        import array
        # Создаем array.array('h') из ctypes массива
        # array.array('h', pcm_data[:decoded_samples]) - не работает напрямую
        # Но можно создать array из bytes и затем скопировать
        # Или использовать memoryview
        # memoryview(pcm_data).cast('h')[:decoded_samples].tobytes() - не работает напрямую
        # Лучший способ: преобразовать в bytes через bytes(pcm_data.raw[:decoded_samples * 2])
        # Но правильнее через array или struct
        # array.array('h') и затем .tobytes()
        # temp_array = array.array('h')
        # temp_array.frombytes(ctypes.string_at(pcm_data, decoded_samples * 2))
        # return temp_array.tobytes()
        # Еще проще:
        return ctypes.string_at(pcm_data, decoded_samples * ctypes.sizeof(ctypes.c_short))
        # --- КОНЕЦ ИСПРАВЛЕНИЯ ---

    def __del__(self):
        if self.decoder_state and opuslib and hasattr(opuslib, 'opus_decoder_destroy'):
            opuslib.opus_decoder_destroy(self.decoder_state)
            self.decoder_state = None

# --- Конец интеграции с libopus ---


class VoiceClientBackend(QObject):
    """
    Backend для голосового клиента, поддерживающего групповые вызовы.
    Обрабатывает захват, кодирование, отправку, получение, декодирование,
    смешивание и воспроизведение аудио для множества участников.
    """
    # Сигналы для взаимодействия с UI
    status_update = pyqtSignal(str)  # Статусное сообщение (например, "Подключение...")
    log_message = pyqtSignal(str)    # Лог-сообщение
    connection_update = pyqtSignal(bool)  # Состояние подключения (True/False)
    transmission_update = pyqtSignal(bool)  # Состояние передачи (True/False)

    def __init__(self):
        super().__init__()
        if not pyaudio_available:
            raise RuntimeError("PyAudio недоступен")
        if not opus_available:
            raise RuntimeError("libopus недоступна")

        self.client_id = uuid.uuid4()
        self.client_id_bytes = self.client_id.bytes
        logger.info(f"Инициализация клиента с ID: {self.client_id}")

        self.server_ip = None
        self.server_port = None
        self.socket = None

        # --- Аудио параметры ---
        self.sample_rate = SAMPLE_RATE
        self.channels = CHANNELS
        self.frame_size = FRAME_SIZE  # Количество сэмплов на фрейм
        self.opus_frame_bytes = self.frame_size * self.channels * 2  # 16-bit

        # --- Состояния ---
        self.is_connected = False
        self.is_transmitting = False
        self._stop_event = threading.Event()
        self._transmit_event = threading.Event()

        # --- Потоки ---
        self.send_thread = None
        self.receive_thread = None
        self.playback_thread = None
        self.keepalive_timer = None

        # --- PyAudio ---
        self.pyaudio_instance = pyaudio.PyAudio()
        self.input_stream = None
        self.output_stream = None

        # --- Opus ---
        # Кодировщик для исходящего потока
        self.opus_encoder = OpusEncoder(self.sample_rate, self.channels, OPUS_APPLICATION_VOIP)
        # Можно попробовать установить параметры после создания, если нужно
        # try:
        #     # Пример установки сигнала (если поддерживается)
        #     # err = opuslib.opus_encoder_ctl(self.opus_encoder.encoder_state, OPUS_SET_SIGNAL_REQUEST, OPUS_SIGNAL_VOICE)
        #     # if err != OPUS_OK: logger.warning(...)
        # except: pass # Игнорируем ошибки установки

        # Словарь декодеров для входящих потоков {sender_uuid: decoder}
        self.opus_decoders = {}
        # Словарь буферов джиттера для входящих потоков {sender_uuid: JitterBuffer}
        self.jitter_buffers = {}

        # --- Счетчики ---
        self.sequence_number = 0

        # --- Очереди и буферы ---
        # Очередь для отправки пакетов (из capture/send в send_thread)
        self.send_queue = queue.Queue(maxsize=100)
        # Очередь для воспроизведения смешанного аудио (из receive/playback в playback_thread)
        self.playback_queue = queue.Queue(maxsize=JITTER_BUFFER_MAX_SIZE * 2)

        # --- Таймеры и мьютексы ---
        # Для безопасного доступа к словарям декодеров/буферов
        self.receivers_lock = threading.RLock()

        # --- Таймауты получателей ---
        self.receiver_last_activity = defaultdict(float)  # {uuid: timestamp}
        self.receiver_timeout = 60.0  # секунд

    def connect_to_server(self, ip, port):
        """Подключается к серверу и запускает потоки."""
        if self.is_connected:
            logger.warning("Клиент уже подключен")
            return True

        try:
            self.server_ip = ip
            self.server_port = port
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.socket.settimeout(0.1)  # Неблокирующий с таймаутом

            # Регистрация на сервере
            reg_packet = b"REGISTER:" + self.client_id_bytes
            self.socket.sendto(reg_packet, (self.server_ip, self.server_port))
            logger.info(f"Отправлен регистрационный пакет на {self.server_ip}:{self.server_port}")

            # Запуск потоков
            self._start_threads()

            self.is_connected = True
            self.connection_update.emit(True)
            self.status_update.emit("Подключен к серверу")
            self.log_message.emit(f"Успешно подключен к {self.server_ip}:{self.server_port}")
            logger.info(f"Клиент {self.client_id} подключен к {self.server_ip}:{self.server_port}")

            # Запуск keep-alive таймера
            self._start_keepalive()

            return True
        except Exception as e:
            logger.error(f"Ошибка подключения: {e}")
            self.log_message.emit(f"Ошибка подключения: {e}")
            self.status_update.emit("Ошибка подключения")
            self._cleanup_on_disconnect()
            return False

    def disconnect_from_server(self):
        """Отключается от сервера и останавливает потоки."""
        if not self.is_connected:
            return

        logger.info("Отключение клиента...")
        self.status_update.emit("Отключение...")
        self.log_message.emit("Отключение от сервера")

        # Остановка передачи
        self.set_transmitting(False)

        # Остановка потоков
        self._stop_event.set()

        # Ожидание завершения потоков
        threads_to_join = [self.send_thread, self.receive_thread, self.playback_thread]
        for thread in threads_to_join:
            if thread and thread.is_alive():
                thread.join(timeout=2.0)  # Таймаут 2 секунды
                if thread.is_alive():
                    logger.warning(f"Поток {thread.name} не завершился вовремя")

        # Остановка keep-alive таймера
        if self.keepalive_timer:
            self.keepalive_timer.stop()
            self.keepalive_timer = None

        # Закрытие сокета
        if self.socket:
            try:
                self.socket.close()
            except Exception as e:
                logger.error(f"Ошибка закрытия сокета: {e}")
            self.socket = None

        # Очистка ресурсов PyAudio/Opus
        self._cleanup_audio_resources()

        self.is_connected = False
        self.connection_update.emit(False)
        self.status_update.emit("Отключено")
        self.log_message.emit("Отключено от сервера")
        logger.info("Клиент отключен")

    def set_transmitting(self, transmitting):
        """Включает или выключает передачу аудио."""
        if not self.is_connected:
            logger.warning("Невозможно установить передачу: клиент не подключен")
            return

        if transmitting and not self.is_transmitting:
            logger.info("Начало передачи")
            self.is_transmitting = True
            self._transmit_event.set()
            self.transmission_update.emit(True)
            self.log_message.emit("Начало передачи голоса")

        elif not transmitting and self.is_transmitting:
            logger.info("Окончание передачи")
            self.is_transmitting = False
            self._transmit_event.clear()
            self.transmission_update.emit(False)
            self.log_message.emit("Окончание передачи голоса")

    def _start_threads(self):
        """Запускает рабочие потоки."""
        self._stop_event.clear()
        self.send_thread = threading.Thread(target=self._send_worker, name="SendThread", daemon=True)
        self.receive_thread = threading.Thread(target=self._receive_worker, name="ReceiveThread", daemon=True)
        self.playback_thread = threading.Thread(target=self._playback_worker, name="PlaybackThread", daemon=True)

        self.send_thread.start()
        self.receive_thread.start()
        self.playback_thread.start()

    def _start_keepalive(self):
        """Запускает таймер для отправки keep-alive пакетов."""
        # Используем QTimer из PyQt для корректной работы с событиями
        self.keepalive_timer = QTimer()
        self.keepalive_timer.timeout.connect(self._send_keepalive)
        self.keepalive_timer.start(int(KEEP_ALIVE_INTERVAL * 1000))  # в миллисекундах

    def _send_keepalive(self):
        """Отправляет короткий keep-alive пакет."""
        if self.is_connected and self.socket:
            try:
                # Отправляем 1 байт, чтобы сервер знал, что клиент активен
                # Сервер уже обрабатывает пакеты <= 1 байта как keep-alive
                self.socket.sendto(b'\x00', (self.server_ip, self.server_port))
                # logger.debug("Keep-alive пакет отправлен")
            except Exception as e:
                logger.error(f"Ошибка отправки keep-alive: {e}")

    def _cleanup_audio_resources(self):
        """Очищает ресурсы PyAudio и Opus."""
        # Остановка и закрытие потоков PyAudio
        streams_to_close = [self.input_stream, self.output_stream]
        for stream in streams_to_close:
            if stream and stream.is_active():
                try:
                    stream.stop_stream()
                except Exception as e:
                    logger.error(f"Ошибка остановки потока PyAudio: {e}")
            if stream:
                try:
                    stream.close()
                except Exception as e:
                    logger.error(f"Ошибка закрытия потока PyAudio: {e}")

        self.input_stream = None
        self.output_stream = None

        # Удаление декодеров Opus
        with self.receivers_lock:
            # Деконструкторы OpusDecoder/__del__ должны освободить ресурсы
            self.opus_decoders.clear()
            self.jitter_buffers.clear()
            self.receiver_last_activity.clear()

        # PyAudio instance не закрываем, так как он может использоваться другими частями

    def _cleanup_on_disconnect(self):
        """Очищает состояние при неудачном подключении или отключении."""
        self.is_connected = False
        self.is_transmitting = False
        self._stop_event.set()
        self._transmit_event.clear()
        if self.socket:
            try:
                self.socket.close()
            except:
                pass
            self.socket = None
        self._cleanup_audio_resources()

    def _send_worker(self):
        """Поток для захвата аудио, кодирования и отправки пакетов."""
        logger.info("Поток отправки запущен")
        try:
            # Инициализация входного потока PyAudio
            self.input_stream = self.pyaudio_instance.open(
                format=pyaudio.paInt16,
                channels=self.channels,
                rate=self.sample_rate,
                input=True,
                frames_per_buffer=self.frame_size,
                # device_index=... # Можно указать конкретное устройство
            )
            logger.debug("Входной поток PyAudio открыт")

            while not self._stop_event.is_set():
                if self.is_transmitting and self._transmit_event.is_set():
                    try:
                        # Захват аудио фрейма
                        raw_audio_data = self.input_stream.read(self.frame_size, exception_on_overflow=False)

                        # Кодирование с помощью Opus
                        encoded_data = self.opus_encoder.encode(raw_audio_data, self.frame_size)

                        # Увеличение номера последовательности
                        self.sequence_number = (self.sequence_number + 1) & 0xFFFFFFFF

                        # Формирование пакета: ClientID + SeqNum + OpusData
                        seq_bytes = struct.pack('>I', self.sequence_number)  # Big-endian
                        packet = self.client_id_bytes + seq_bytes + encoded_data

                        # Отправка пакета
                        if self.socket:
                            self.socket.sendto(packet, (self.server_ip, self.server_port))
                            # logger.debug(f"Отправлен пакет #{self.sequence_number}, размер: {len(packet)} байт")

                    except Exception as e:
                        logger.error(f"Ошибка в потоке отправки (захват/кодирование/отправка): {e}")
                        # traceback.print_exc()
                        time.sleep(0.001)  # Небольшая пауза при ошибке
                else:
                    # Если не передаем, просто ждем
                    time.sleep(0.001)  # 1 мс

        except Exception as e:
            logger.error(f"Критическая ошибка в потоке отправки: {e}")
            # traceback.print_exc()
        finally:
            logger.info("Поток отправки завершен")

    def _receive_worker(self):
        """Поток для получения пакетов, декодирования и помещения в очередь воспроизведения."""
        logger.info("Поток получения запущен")
        try:
            while not self._stop_event.is_set():
                try:
                    if self.socket:
                        # Прием пакета
                        data, addr = self.socket.recvfrom(4096)  # Достаточно большой буфер

                        # Проверка, что пакет не от нас самих
                        if len(data) >= CLIENT_ID_LEN:
                            sender_id_bytes = data[:CLIENT_ID_LEN]

                            # Пропускаем свои же пакеты (если сервер их почему-то вернул)
                            if sender_id_bytes == self.client_id_bytes:
                                continue

                            sender_uuid = uuid.UUID(bytes=sender_id_bytes)

                            # Обновление времени последней активности отправителя
                            current_time = time.time()
                            self.receiver_last_activity[sender_uuid] = current_time

                            # Обработка пакета с данными
                            if len(data) > CLIENT_ID_LEN + 4:  # Должен содержать SeqNum (4 байта) и данные
                                seq_bytes = data[CLIENT_ID_LEN:CLIENT_ID_LEN+4]
                                sequence_number = struct.unpack('>I', seq_bytes)[0]
                                opus_data = data[CLIENT_ID_LEN+4:]

                                # logger.debug(f"Получен пакет от {sender_uuid}, Seq: {sequence_number}, размер Opus: {len(opus_data)} байт")

                                # Получение или создание декодера и буфера для этого отправителя
                                with self.receivers_lock:
                                    if sender_uuid not in self.opus_decoders:
                                        logger.info(f"Создание нового декодера для клиента {sender_uuid}")
                                        decoder = OpusDecoder(self.sample_rate, self.channels)
                                        self.opus_decoders[sender_uuid] = decoder
                                        self.jitter_buffers[sender_uuid] = JitterBuffer(
                                            max_size=JITTER_BUFFER_MAX_SIZE,
                                            min_size=JITTER_BUFFER_MIN_SIZE,
                                            target_size=JITTER_BUFFER_TARGET_SIZE
                                        )

                                    jitter_buffer = self.jitter_buffers[sender_uuid]

                                # Добавление пакета в jitter buffer
                                jitter_buffer.put(sequence_number, opus_data, current_time)

                            # else:
                            #     logger.debug(f"Получен короткий пакет от {sender_uuid} (возможно keep-alive)")

                        # else:
                        #     logger.warning(f"Получен пакет неизвестного формата от {addr}")

                except socket.timeout:
                    # Нормальная ситуация - таймаут приема
                    pass
                except Exception as e:
                    if not self._stop_event.is_set():  # Игнорируем ошибки при завершении
                        logger.error(f"Ошибка в потоке получения: {e}")
                        # traceback.print_exc()

                # --- Проверка таймаутов получателей ---
                current_time = time.time()
                timed_out_senders = []
                with self.receivers_lock:
                    for sender_uuid, last_activity in list(self.receiver_last_activity.items()):
                        if current_time - last_activity > self.receiver_timeout:
                            timed_out_senders.append(sender_uuid)

                if timed_out_senders:
                    logger.info(f"Обнаружены таймауты для клиентов: {[str(u) for u in timed_out_senders]}")
                    with self.receivers_lock:
                        for sender_uuid in timed_out_senders:
                            self.receiver_last_activity.pop(sender_uuid, None)
                            decoder = self.opus_decoders.pop(sender_uuid, None)
                            jitter_buf = self.jitter_buffers.pop(sender_uuid, None)
                            if decoder:
                                del decoder  # Освобождение ресурсов (если необходимо)
                            if jitter_buf:
                                del jitter_buf
                            logger.info(f"Удален клиент {sender_uuid} по таймауту")

        except Exception as e:
            logger.error(f"Критическая ошибка в потоке получения: {e}")
            # traceback.print_exc()
        finally:
            logger.info("Поток получения завершен")

    def _playback_worker(self):
        """Поток для извлечения из jitter buffer'ов, декодирования и воспроизведения."""
        logger.info("Поток воспроизведения запущен")

        # Инициализация выходного потока PyAudio
        try:
            self.output_stream = self.pyaudio_instance.open(
                format=pyaudio.paInt16,
                channels=self.channels,
                rate=self.sample_rate,
                output=True,
                frames_per_buffer=self.frame_size,
                # device_index=... # Можно указать конкретное устройство
            )
            logger.debug("Выходной поток PyAudio открыт")
        except Exception as e:
            logger.error(f"Не удалось открыть выходной поток PyAudio: {e}")
            self.log_message.emit(f"Ошибка аудио воспроизведения: {e}")
            return  # Завершаем поток, если не можем открыть поток

        plc_skip_counter = {}  # {sender_uuid: skip_count}

        try:
            while not self._stop_event.is_set():
                mixed_pcm_frame = None
                current_time = time.time()

                # --- Сбор и декодирование фреймов от всех активных отправителей ---
                decoded_frames = {}  # {sender_uuid: pcm_data or None}

                with self.receivers_lock:
                    active_senders = list(self.jitter_buffers.keys())

                for sender_uuid in active_senders:
                    jitter_buffer = self.jitter_buffers.get(sender_uuid)
                    if not jitter_buffer:
                        continue

                    decoder = self.opus_decoders.get(sender_uuid)
                    if not decoder:
                        continue

                    # Получение пакета из jitter buffer'а
                    packet_data = jitter_buffer.get(current_time)

                    if packet_data is not None:
                        # Декодирование
                        try:
                            opus_packet, packet_ts = packet_data
                            # Передаем bytes напрямую
                            pcm_data = decoder.decode(opus_packet, self.frame_size)
                            decoded_frames[sender_uuid] = pcm_data
                            plc_skip_counter[sender_uuid] = 0  # Сброс счетчика PLC
                            # logger.debug(f"Декодирован фрейм от {sender_uuid}")
                        except Exception as e:  # Включая RuntimeError от OpusDecoder
                            logger.warning(f"Ошибка декодирования Opus от {sender_uuid}: {e}. Используется PLC.")
                            decoded_frames[sender_uuid] = None  # Будет обработано как потеря пакета
                        except Exception as e:
                            logger.error(f"Неизвестная ошибка декодирования от {sender_uuid}: {e}")
                            decoded_frames[sender_uuid] = None
                    else:
                        # Потеря пакета или буфер пуст
                        decoded_frames[sender_uuid] = None

                # --- Применение Packet Loss Concealment (PLC) ---
                for sender_uuid in active_senders:
                    if decoded_frames[sender_uuid] is None:
                        decoder = self.opus_decoders.get(sender_uuid)
                        if decoder:
                            skip_count = plc_skip_counter.get(sender_uuid, 0)
                            if skip_count < PLC_MAX_SKIP_FRAMES:
                                try:
                                    # PLC: декодирование без данных (data=None)
                                    plc_pcm_data = decoder.decode(None, self.frame_size)
                                    decoded_frames[sender_uuid] = plc_pcm_data
                                    plc_skip_counter[sender_uuid] = skip_count + 1
                                    # logger.debug(f"PLC применен для {sender_uuid}, счетчик: {skip_count + 1}")
                                except Exception as e:
                                    logger.error(f"Ошибка PLC для {sender_uuid}: {e}")
                                    # Если PLC не удался, оставляем None
                            else:
                                # Достигнут лимит PLC, сбрасываем счетчик
                                plc_skip_counter[sender_uuid] = 0
                                # logger.debug(f"Лимит PLC достигнут для {sender_uuid}")
                        # Если декодера нет, decoded_frames[sender_uuid] остается None

                # --- Смешивание (Mixing) ---
                active_frames = [pcm for pcm in decoded_frames.values() if pcm is not None]

                if active_frames:
                    mixed_pcm_frame = self._mix_pcm_frames(active_frames)
                else:
                    # Тишина, если нет активных потоков
                    mixed_pcm_frame = b'\x00' * self.opus_frame_bytes

                # --- Воспроизведение ---
                if mixed_pcm_frame and self.output_stream and self.output_stream.is_active():
                    try:
                        self.output_stream.write(mixed_pcm_frame)
                        # logger.debug("Воспроизведен смешанный фрейм")
                    except Exception as e:
                        logger.error(f"Ошибка воспроизведения: {e}")

                # Небольшая пауза для синхронизации (примерно 20мс)
                time.sleep(self.frame_size / self.sample_rate)

        except Exception as e:
            logger.error(f"Критическая ошибка в потоке воспроизведения: {e}")
            # traceback.print_exc()
        finally:
            logger.info("Поток воспроизведения завершен")

    def _mix_pcm_frames(self, pcm_frames_list):
        """
        Простое смешивание PCM фреймов (усреднение с предотвращением clipping'а).
        :param pcm_frames_list: Список байтовых строк PCM данных.
        :return: Байтовая строка смешанного PCM.
        """
        if not pcm_frames_list:
            return b'\x00' * self.opus_frame_bytes

        if len(pcm_frames_list) == 1:
            return pcm_frames_list[0]

        # Преобразование байтов в список 16-битных signed int
        import array
        mixed_samples = array.array('h', b'\x00' * self.opus_frame_bytes)  # Инициализация нулями

        for pcm_frame in pcm_frames_list:
            try:
                samples = array.array('h', pcm_frame)
                for i in range(len(mixed_samples)):
                    # Простое суммирование с нормализацией
                    mixed_samples[i] = int(mixed_samples[i] + samples[i] / len(pcm_frames_list))
                    # Ограничение до 16-битного диапазона (предотвращение clipping'а)
                    if mixed_samples[i] > 32767:
                        mixed_samples[i] = 32767
                    elif mixed_samples[i] < -32768:
                        mixed_samples[i] = -32768
            except Exception as e:
                logger.error(f"Ошибка смешивания PCM фреймов: {e}")
                continue  # Пропускаем проблемный фрейм

        return mixed_samples.tobytes()


class JitterBuffer:
    """
    Простой jitter buffer для упорядочивания пакетов и сглаживания задержек.
    """

    def __init__(self, max_size=50, min_size=5, target_size=20):
        self.max_size = max_size
        self.min_size = min_size
        self.target_size = target_size
        self.buffer = {}  # {seq_num: (opus_data, timestamp)}
        self.last_played_seq = None
        self.playout_delay = 0.0  # секунд

    def put(self, seq_num, opus_data, timestamp):
        """Добавляет пакет в буфер."""
        # opus_data здесь - это bytes
        self.buffer[seq_num] = (bytes(opus_data), timestamp)  # Убедимся, что это bytes

        # Ограничение размера буфера
        if len(self.buffer) > self.max_size:
            # Удаление самых старых пакетов
            sorted_keys = sorted(self.buffer.keys())
            keys_to_remove = sorted_keys[:len(self.buffer) - self.max_size]
            for key in keys_to_remove:
                del self.buffer[key]

    def get(self, current_time):
        """
        Извлекает следующий пакет для воспроизведения.
        :param current_time: Текущее время (timestamp).
        :return: (opus_data_bytes, timestamp) или None, если нет данных.
        """
        if not self.buffer:
            return None

        sorted_seq_nums = sorted(self.buffer.keys())

        # Определение следующего ожидаемого номера
        if self.last_played_seq is None:
            next_seq = sorted_seq_nums[0]
        else:
            next_seq = (self.last_played_seq + 1) & 0xFFFFFFFF

        # Проверка, есть ли пакет с ожидаемым номером
        if next_seq in self.buffer:
            data, ts = self.buffer.pop(next_seq)
            self.last_played_seq = next_seq
            # data уже bytes
            return data, ts

        # Проверка, есть ли более поздние пакеты (опережающие)
        later_packets = [seq for seq in sorted_seq_nums if self._is_seq_later(seq, next_seq)]
        if later_packets:
            # Пакет опережает последовательность - это может быть потеря или reorder
            earliest_later_seq = later_packets[0]

            # Если буфер достаточно большой, можно немного подождать
            if len(self.buffer) >= self.target_size:
                # Проверяем, не слишком ли стар пакет
                _, oldest_ts = self.buffer[sorted_seq_nums[0]]
                if current_time - oldest_ts > self.playout_delay + 0.1:  # 100ms доп. задержка
                    # Буфер переполнен или задержка велика, пропускаем и берем опережающий
                    data, ts = self.buffer.pop(earliest_later_seq)
                    self.last_played_seq = earliest_later_seq
                    logger.debug(f"Пропущен пакет #{next_seq}, воспроизведен опережающий #{earliest_later_seq}")
                    # data уже bytes
                    return data, ts
            # else: Ждем, буфер еще не заполнен

        # Проверка, есть ли более ранние пакеты (запаздывшие)
        earlier_packets = [seq for seq in sorted_seq_nums if self._is_seq_earlier(seq, next_seq)]
        if earlier_packets:
            # Удаляем очень старые пакеты
            for seq in earlier_packets:
                _, ts = self.buffer[seq]
                if current_time - ts > 1.0:  # Удаляем пакеты старше 1 секунды
                    del self.buffer[seq]
                    logger.debug(f"Удален очень старый пакет #{seq}")

        # Если ничего не подошло, возвращаем None (ожидание или PLC)
        return None

    def _is_seq_later(self, seq1, seq2):
        """Проверяет, является ли seq1 более поздним, чем seq2 (с учетом переполнения)."""
        # Простая проверка для 32-битных номеров
        diff = (seq1 - seq2) & 0xFFFFFFFF
        return 0 < diff < 0x80000000

    def _is_seq_earlier(self, seq1, seq2):
        """Проверяет, является ли seq1 более ранним, чем seq2 (с учетом переполнения)."""
        diff = (seq2 - seq1) & 0xFFFFFFFF
        return 0 < diff < 0x80000000

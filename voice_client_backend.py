# voice_client_backend.py
import os
import platform
import traceback
import socket
import threading
import time
import queue
import struct
import ctypes
from ctypes import c_ubyte, c_int32, c_int16, c_int, byref, POINTER, cdll
from collections import deque
from PyQt5.QtCore import QObject, pyqtSignal

# Локальные импорты
from voice_client_constants import *
from voice_client_utils import setup_backend_logging

# Попытка импорта PyAudio
try:
    import pyaudio
    pyaudio_available = True
except ImportError:
    pyaudio = None
    pyaudio_available = False

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
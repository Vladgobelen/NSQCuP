import os
import platform
import traceback
import socket
import threading
import time
import queue
import struct
import ctypes
import math
from ctypes import c_ubyte, c_int32, c_int16, c_int, byref, POINTER, c_void_p, cdll
from collections import deque
from PyQt5.QtCore import QObject, pyqtSignal
from voice_client_constants import *
from voice_client_utils import setup_backend_logging
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
        self.use_dtx = True
        self.aggressive_dtx = False
        self.voice_threshold = DEFAULT_VOICE_THRESHOLD
        self.silence_frames = 0
        self.silence_threshold = 5
        self.silence_start_time = None
        self.dtx_enabled = True
        # Аудио буферы
        self.playback_buffer = deque(maxlen=SAMPLE_RATE * BUFFER_DURATION_MS // 1000)
        self.audio_queue = queue.Queue()
        # Jitter buffer
        self.jitter_buffer = deque(maxlen=JITTER_BUFFER_MAX_SIZE)
        self.jitter_buffer_lock = threading.Lock()
        # Сеть
        self.socket = None
        self.server_address = None
        # Очереди для межпоточного обмена
        self.network_queue = queue.Queue()
        # Аудио потоки
        self.audio_stream_in = None
        self.audio_stream_out = None
        self.pyaudio_instance = None
        # Статистика
        self.packets_sent = 0
        self.packets_received = 0
        self.packets_lost = 0
        self.last_stat_time = time.time()
        self.sequence_number = 0
        self.last_received_seq = 0
        # Потоки
        self.threads = []
        # Opus
        self.opus = None
        self.encoder = None
        self.decoder = None
        # Инициализация Opus
        self._init_opus()

    def _init_opus(self):
        """Инициализация библиотеки Opus с оптимизацией для голоса"""
        try:
            if platform.system() == 'Windows':
                lib_path = 'libopus.dll'
            else:
                lib_path = './libopus.so.0.10.1'
            self.opus = cdll.LoadLibrary(lib_path)
            self.logger.info(f"Opus библиотека загружена: {lib_path}")
            # Определяем прототипы функций
            self.opus.opus_encoder_create.restype = c_void_p
            self.opus.opus_encoder_create.argtypes = [c_int32, c_int, c_int, POINTER(c_int)]
            self.opus.opus_decoder_create.restype = c_void_p
            self.opus.opus_decoder_create.argtypes = [c_int32, c_int, POINTER(c_int)]
            self.opus.opus_encode.restype = c_int
            self.opus.opus_encode.argtypes = [c_void_p, POINTER(c_int16), c_int, POINTER(c_ubyte), c_int32]
            self.opus.opus_decode.restype = c_int
            self.opus.opus_decode.argtypes = [c_void_p, POINTER(c_ubyte), c_int32, POINTER(c_int16), c_int, c_int]
            # Добавляем прототип для управления параметрами кодера
            self.opus.opus_encoder_ctl.restype = c_int
            self.opus.opus_encoder_ctl.argtypes = [c_void_p, c_int, c_int]
            # Создаем кодировщик с оптимизацией для VOIP
            error = c_int(0)
            self.encoder = self.opus.opus_encoder_create(SAMPLE_RATE, CHANNELS, OPUS_APPLICATION_VOIP, byref(error))
            if error.value != 0:
                raise Exception(f"Ошибка создания кодировщика: {error.value}")
            # Устанавливаем битрейт 24 kbps для лучшего качества
            self.opus.opus_encoder_ctl(self.encoder, OPUS_SET_BITRATE_REQUEST, BITRATE)
            # Включаем VBR (переменный битрейт)
            self.opus.opus_encoder_ctl(self.encoder, OPUS_SET_VBR_REQUEST, 1)
            # Устанавливаем сложность кодирования (5 - средняя)
            self.opus.opus_encoder_ctl(self.encoder, OPUS_SET_COMPLEXITY_REQUEST, 5)
            # Указываем, что кодируем голос
            self.opus.opus_encoder_ctl(self.encoder, OPUS_SET_SIGNAL_REQUEST, OPUS_SIGNAL_VOICE)
            # Включаем DTX по умолчанию
            self.set_dtx(self.use_dtx)
            # Создаем декодер
            error = c_int(0)
            self.decoder = self.opus.opus_decoder_create(SAMPLE_RATE, CHANNELS, byref(error))
            if error.value != 0:
                raise Exception(f"Ошибка создания декодера: {error.value}")
            self.logger.info(f"Opus кодек инициализирован для VOIP с битрейтом {BITRATE} bps")
        except Exception as e:
            error_msg = f"Ошибка инициализации Opus: {str(e)}"
            self.logger.error(error_msg)
            self.opus = None

    def set_dtx(self, enabled):
        """Включение/выключение DTX"""
        self.use_dtx = enabled
        if self.encoder:
            result = self.opus.opus_encoder_ctl(self.encoder, OPUS_SET_DTX_REQUEST, 1 if enabled else 0)
            if result == 0:
                self.logger.info(f"DTX {'включен' if enabled else 'выключен'}")
            else:
                self.logger.warning(f"Не удалось изменить состояние DTX: {result}")

    def set_aggressive_dtx(self, enabled):
        """Включение/выключение агрессивного режима DTX"""
        self.aggressive_dtx = enabled
        if enabled:
            self.voice_threshold = AGGRESSIVE_DTX_THRESHOLD
            self.logger.info(f"Агрессивный DTX включен, порог: {self.voice_threshold}")
        else:
            self.voice_threshold = DEFAULT_VOICE_THRESHOLD
            self.logger.info(f"Агрессивный DTX выключен, порог: {self.voice_threshold}")

    def set_voice_threshold(self, threshold):
        """Установка порога активации голоса"""
        self.voice_threshold = threshold
        self.logger.info(f"Установлен порог активации голоса: {threshold}")

    def _calculate_rms(self, data):
        """Вычисление RMS (среднеквадратичное значение) аудиоданных"""
        # Преобразуем байты в массив int16
        format = f"{len(data)//2}h"
        samples = struct.unpack(format, data)
        # Вычисляем сумму квадратов
        sum_squares = 0.0
        for sample in samples:
            sum_squares += sample * sample
        # Вычисляем RMS
        rms = math.sqrt(sum_squares / len(samples)) if samples else 0
        return rms

    def _is_silence(self, audio_data):
        """Определяет, является ли аудио тишиной на основе порога"""
        rms = self._calculate_rms(audio_data)
        return rms < self.voice_threshold

    def connect_to_server(self, server_ip, server_port):
        """Подключение к серверу"""
        try:
            if not self.opus:
                raise Exception("Opus не инициализирован")
            if not pyaudio_available:
                raise Exception("PyAudio не доступен")
            self.server_address = (server_ip, server_port)
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.socket.settimeout(0.001)  # Таймаут 1 мс для неблокирующего режима
            # Явно bind на случайный порт
            self.socket.bind(('0.0.0.0', 0))
            self.logger.info(f"Сокет bound на {self.socket.getsockname()}")
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
        """Отключение от серверу"""
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
                format=pyaudio.paInt16,
                channels=CHANNELS,
                rate=SAMPLE_RATE,
                output=True,
                frames_per_buffer=FRAME_SIZE
            )
            self.logger.info("Аудио потоки успешно инициализированны")
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
                    # Добавляем sequence number
                    self.sequence_number += 1
                    # Формируем пакет с sequence number (4 байта) + данные
                    seq_bytes = struct.pack('>I', self.sequence_number)
                    packet = seq_bytes + bytes(encoded[:result])
                    self.network_queue.put(packet)
                    self.packets_sent += 1
                    # Логируем каждые 50 пакетов
                    if self.packets_sent % 50 == 0:
                        self.logger.info(f"Отправлен аудио пакет #{self.sequence_number}, размер: {result} байт")
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
            threading.Thread(target=self._jitter_thread, name="JitterThread"),
            threading.Thread(target=self._playback_thread, name="PlaybackThread"),
            threading.Thread(target=self._keepalive_thread, name="KeepAliveThread"),
            threading.Thread(target=self._stats_thread, name="StatsThread"),
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
        """Поток приема данных с jitter buffer"""
        self.logger.info("Поток приема данных запущен")
        while self.running:
            try:
                if self.socket:
                    try:
                        data, addr = self.socket.recvfrom(4000)
                        # Игнорируем keep-alive пакеты
                        if len(data) <= 1:
                            continue
                        
                        # Извлекаем sequence number
                        if len(data) >= 4:
                            seq_num = struct.unpack('>I', data[:4])[0]
                            audio_data = data[4:]
                            
                            # Добавляем в jitter buffer
                            with self.jitter_buffer_lock:
                                self.jitter_buffer.append((seq_num, audio_data))
                                
                            self.packets_received += 1
                            
                            # Логируем каждые 50 пакетов
                            if self.packets_received % 50 == 0:
                                self.logger.info(f"Получен аудио пакет #{seq_num}, размер: {len(audio_data)} байт")
                    except socket.timeout:
                        time.sleep(0.001)
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

    def _jitter_thread(self):
        """Поток обработки jitter buffer"""
        self.logger.info("Поток jitter buffer запущен")
        
        target_delay = JITTER_BUFFER_TARGET_SIZE  # целевое количество пакетов в буфере
        min_delay = JITTER_BUFFER_MIN_SIZE
        max_delay = JITTER_BUFFER_MAX_SIZE
        
        while self.running:
            try:
                with self.jitter_buffer_lock:
                    buffer_size = len(self.jitter_buffer)
                    
                    # Если буфер достаточно большой, обрабатываем пакеты
                    if buffer_size >= min_delay:
                        # Сортируем по sequence number
                        sorted_packets = sorted(list(self.jitter_buffer), key=lambda x: x[0])
                        
                        # Извлекаем самый старый пакет
                        if sorted_packets:
                            seq_num, audio_data = sorted_packets[0]
                            # Удаляем из буфера
                            self.jitter_buffer = deque([p for p in self.jitter_buffer if p[0] != seq_num], maxlen=max_delay)
                            
                            # Декодируем
                            pcm_data = (c_int16 * FRAME_SIZE)()
                            c_ubyte_array = (c_ubyte * len(audio_data)).from_buffer_copy(audio_data)
                            result = self.opus.opus_decode(
                                self.decoder, c_ubyte_array, len(audio_data), pcm_data, FRAME_SIZE, 0
                            )
                            if result > 0:
                                # Добавляем в очередь воспроизведения
                                audio_data_decoded = pcm_data[:result]
                                self.audio_queue.put(audio_data_decoded)
                    else:
                        # Если буфер маленький, немного ждем
                        time.sleep(0.005)  # 5ms
                        
            except Exception as e:
                error_msg = f"Ошибка в jitter thread: {str(e)}"
                self.logger.error(error_msg)
                self.logger.error(traceback.format_exc())
                time.sleep(0.01)
                
        self.logger.info("Поток jitter buffer остановлен")

    def _playback_thread(self):
        """Поток воспроизведения аудио с фиксированной скоростью"""
        self.logger.info("Поток воспроизведения аудио запущен")

        frame_duration = FRAME_SIZE / SAMPLE_RATE
        next_play_time = time.time()
        
        # Адаптивные параметры
        target_queue_size = 3
        min_queue_size = 1
        max_queue_size = 8
        
        while self.running:
            try:
                current_queue_size = self.audio_queue.qsize()
                
                # Адаптивное управление буфером
                if current_queue_size < min_queue_size:
                    # Буфер слишком маленький, ждем
                    time.sleep(0.01)
                    continue
                elif current_queue_size > max_queue_size:
                    # Буфер переполнен, пропускаем старые пакеты
                    try:
                        self.audio_queue.get_nowait()
                        self.logger.warning(f"Пропущен пакет из-за переполнения буфера ({current_queue_size} > {max_queue_size})")
                        self.packets_lost += 1
                        continue
                    except queue.Empty:
                        pass
                
                # Получаем данные из очереди
                audio_data = self.audio_queue.get(timeout=0.1)

                # Синхронизируем воспроизведение
                current_time = time.time()
                if current_time < next_play_time:
                    sleep_time = next_play_time - current_time
                    if sleep_time > 0:
                        time.sleep(sleep_time)
                
                # Преобразуем в байты и воспроизводим
                audio_bytes = struct.pack(f'{len(audio_data)}h', *audio_data)
                self.audio_stream_out.write(audio_bytes)
                
                # Обновляем время следующего воспроизведения
                next_play_time += frame_duration
                
            except queue.Empty:
                # Воспроизводим тишину если данных нет
                silence = [0] * FRAME_SIZE
                audio_bytes = struct.pack(f'{FRAME_SIZE}h', *silence)
                try:
                    self.audio_stream_out.write(audio_bytes)
                    next_play_time += frame_duration
                except Exception as e:
                    if self.running:
                        self.logger.error(f"Ошибка воспроизведения тишины: {str(e)}")
            except Exception as e:
                error_msg = f"Ошибка воспроизведения: {str(e)}"
                self.logger.error(error_msg)
                self.logger.error(traceback.format_exc())
        self.logger.info("Поток воспроизведения аудио остановлен")

    def _keepalive_thread(self):
        """Поток отправки keep-alive пакетов"""
        self.logger.info("Поток keep-alive запущен")
        ka_counter = 0
        while self.running:
            if self.is_connected:
                try:
                    if self.socket:
                        self.socket.sendto(b'\x00', self.server_address)
                        ka_counter += 1
                        # Логируем каждые 10 keep-alive пакетов
                        if ka_counter % 10 == 0:
                            self.logger.info(f"Отправлен keep-alive пакет #{ka_counter}")
                except Exception as e:
                    error_msg = f"Ошибка отправки keep-alive: {str(e)}"
                    self.logger.error(error_msg)
            time.sleep(KEEP_ALIVE_INTERVAL)
        self.logger.info("Поток keep-alive остановлен")

    def _stats_thread(self):
        """Поток сбора статистики"""
        self.logger.info("Поток статистики запущен")
        while self.running:
            time.sleep(5.0)
            if self.is_connected:
                # Вычисляем размер буфера в миллисекундах
                buffer_size_ms = (self.audio_queue.qsize() * FRAME_SIZE / SAMPLE_RATE) * 1000
                jitter_buffer_size = 0
                with self.jitter_buffer_lock:
                    jitter_buffer_size = len(self.jitter_buffer)
                
                # Формируем строку статистики
                stats = (f"Статистика: Отправлено: {self.packets_sent}, "
                         f"Получено: {self.packets_received}, "
                         f"Потеряно: {self.packets_lost}, "
                         f"Jitter буфер: {jitter_buffer_size}, "
                         f"Очередь воспроизведения: {self.audio_queue.qsize()} фреймов, "
                         f"Буфер: {buffer_size_ms:.1f}ms")
                self.logger.info(stats)
                self.status_update.emit(stats)
        self.logger.info("Поток статистики остановлен")
from voice_client_utils import setup_backend_logging
from voice_client_constants import *
from PyQt5.QtCore import QObject, pyqtSignal
from ctypes import c_ubyte, c_int32, c_int16, c_int, byref, POINTER, c_void_p, cdll
from collections import deque, defaultdict
import uuid
import math
import ctypes
import struct
import queue
import time
import threading
import socket
import traceback
import platform
import os

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
        self.client_id = uuid.uuid4()  # Генерация уникального ID клиента
        self.client_id_bytes = self.client_id.bytes  # Байтовое представление для отправки
        self.logger = setup_backend_logging()
        self.logger.info(f"[CLIENT] Generated Client ID: {self.client_id}")

        self.is_transmitting = False
        self.is_connected = False
        self.running = False

        # Аудио буферы
        self.playback_buffer = deque(maxlen=SAMPLE_RATE * BUFFER_DURATION_MS // 1000)
        self.audio_queue = queue.Queue()

        self.source_buffers = defaultdict(lambda: deque(maxlen=JITTER_BUFFER_MAX_SIZE))
        self.last_activity = {}  # Время последней активности для каждого источника (UUID)
        self.last_sequence_number = {}  # Последний обработанный sequence number для каждого источника (UUID)
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
        self.packets_skipped = 0
        self.duplicate_packets = 0  # Счетчик дубликатов
        self.last_stat_time = time.time()
        self.sequence_number = 0
        self.last_received_seq = 0

        # Потоки
        self.threads = []

        # Opus
        self.opus = None
        self.encoder = None
        self.decoder = None

        # Для компенсации потерь пакетов
        self.last_audio_frame = None
        self.skip_frames_count = 0

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
            # Включение DTX удалено
            # self.set_dtx(self.use_dtx)

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

            # --- НОВОЕ: Отправка пакета регистрации ---
            register_packet = b'REGISTER:' + self.client_id_bytes
            self.socket.sendto(register_packet, self.server_address)
            self.logger.info(f"[CLIENT] Sent registration packet for ID {self.client_id}")
            # --- КОНЕЦ НОВОГО ---

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
                pcm_data = (c_int16 * FRAME_SIZE).from_buffer_copy(in_data)
                result = self.opus.opus_encode(self.encoder, pcm_data, FRAME_SIZE, encoded, 400)
                if result > 0:
                    # Добавляем sequence number
                    self.sequence_number += 1
                    seq_bytes = struct.pack('>I', self.sequence_number)

                    # Формируем пакет: [CLIENT_ID][SEQUENCE_NUMBER][OPUS_DATA]
                    packet = self.client_id_bytes + seq_bytes + bytes(encoded[:result])

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
        min_packet_size = CLIENT_ID_LEN + 4
        while self.running:
            try:
                if self.socket:
                    try:
                        data, addr = self.socket.recvfrom(4000)

                        # Игнорируем короткие пакеты (keep-alive или служебные)
                        if len(data) < min_packet_size:
                            continue

                        # Извлекаем ID отправителя
                        sender_id_bytes = data[:CLIENT_ID_LEN]
                        try:
                            sender_id = str(uuid.UUID(bytes=sender_id_bytes))
                        except ValueError:
                            self.logger.warning(f"Received packet with invalid UUID from {addr}")
                            continue

                        # Извлекаем sequence number и аудио данные
                        seq_and_audio_data = data[CLIENT_ID_LEN:]

                        if len(seq_and_audio_data) < 4:
                            self.logger.warning(f"Received packet with valid UUID but insufficient data from {addr}")
                            continue

                        seq_num = struct.unpack('>I', seq_and_audio_data[:4])[0]
                        audio_data = seq_and_audio_data[4:]

                        # Используем UUID как идентификатор источника
                        source_id = sender_id

                        # Проверка на дубликаты пакетов
                        with self.jitter_buffer_lock:
                            # Инициализируем последний sequence number для источника, если его нет
                            if source_id not in self.last_sequence_number:
                                self.last_sequence_number[source_id] = -1
                            # Проверяем, не является ли пакет дубликатом
                            if seq_num <= self.last_sequence_number[source_id]:
                                self.duplicate_packets += 1
                                self.logger.debug(f"Дубликат пакета #{seq_num} от {source_id} проигнорирован")
                                continue
                            # Обновляем время активности источника
                            self.last_activity[source_id] = time.time()
                            # Добавляем в jitter buffer
                            self.source_buffers[source_id].append((seq_num, audio_data))
                            # Обновляем последний обработанный sequence number
                            self.last_sequence_number[source_id] = seq_num

                        self.packets_received += 1
                        # Логируем каждые 50 пакетов
                        if self.packets_received % 50 == 0:
                            self.logger.info(f"Получен аудио пакет #{seq_num} от {source_id} (originally from {addr}), размер: {len(audio_data)} байт")

                    except socket.timeout:
                        time.sleep(0.005)  # Увеличенная задержка для снижения нагрузки
                    except BlockingIOError:
                        time.sleep(0.005)  # Увеличенная задержка для снижения нагрузки
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
        """Поток обработки jitter buffer с поддержкой нескольких источников"""
        self.logger.info("Поток jitter buffer запущен")
        target_delay = JITTER_BUFFER_TARGET_SIZE  # целевое количество пакетов в буфере
        min_delay = JITTER_BUFFER_MIN_SIZE
        max_delay = JITTER_BUFFER_MAX_SIZE
        # Период очистки неактивных источников
        last_cleanup = time.time()

        while self.running:
            try:
                # Очищаем буферы неактивных источников
                current_time = time.time()
                if current_time - last_cleanup > 5.0:  # каждые 5 секунд
                    inactive_sources = [
                        source_id for source_id, last_time in self.last_activity.items()
                        if current_time - last_time > 10.0  # неактивен 10 секунд
                    ]
                    for source_id in inactive_sources:
                        if source_id in self.source_buffers:
                            del self.source_buffers[source_id]
                        if source_id in self.last_activity:
                            del self.last_activity[source_id]
                        if source_id in self.last_sequence_number:
                            del self.last_sequence_number[source_id]
                    last_cleanup = current_time

                # Определяем активных спикеров (говорили в последние 2 секунды)
                active_speakers = [
                    source_id for source_id, last_time in self.last_activity.items()
                    if current_time - last_time < 2.0
                ]
                # Сортируем источники: активные спикеры первыми
                with self.jitter_buffer_lock:
                    # Используем list для избежания проблем с изменением словаря во время итерации
                    sources_list = list(self.source_buffers.keys())
                    # Сортируем источники
                    sorted_sources = sorted(
                        sources_list,
                        key=lambda x: (x not in active_speakers, self.last_activity.get(x, 0)),
                        reverse=True
                    )
                    # Извлекаем пакеты для воспроизведения
                    packets_to_play = []
                    for source_id in sorted_sources:
                        if len(packets_to_play) >= target_delay:
                            break
                        # Проверяем, существует ли источник (может быть удален в другом потоке)
                        if source_id not in self.source_buffers:
                            continue
                        buffer = self.source_buffers[source_id]
                        if len(buffer) >= min_delay:
                            # Извлекаем самый старый пакет от этого источника
                            seq_num, audio_data = buffer.popleft()
                            packets_to_play.append((seq_num, audio_data, source_id))
                            # Если буфер опустел, удаляем источник
                            if not buffer and source_id in self.source_buffers:
                                del self.source_buffers[source_id]
                                if source_id in self.last_activity:
                                    del self.last_activity[source_id]

                # Если есть пакеты для воспроизведения
                if packets_to_play:
                    # Сортируем по sequence number для правильной синхронизации
                    packets_to_play.sort(key=lambda x: x[0])
                    for seq_num, audio_data, source_id in packets_to_play:
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

                # Ключевое исправление: добавляем небольшую задержку, чтобы не перегружать процессор
                time.sleep(0.005)
            except Exception as e:
                error_msg = f"Ошибка в jitter thread: {str(e)}"
                self.logger.error(error_msg)
                self.logger.error(traceback.format_exc())
                time.sleep(0.01)
        self.logger.info("Поток jitter buffer остановлен")

    def _playback_thread(self):
        """Поток воспроизведения аудио с адаптивным управлением буфером"""
        self.logger.info("Поток воспроизведения аудио запущен")
        frame_duration = FRAME_SIZE / SAMPLE_RATE
        next_play_time = time.time()
        target_queue_size = 15
        min_queue_size = 5
        max_queue_size = 25
        # Для компенсации потерь пакетов
        last_audio_frame = None
        skip_frames_count = 0

        while self.running:
            try:
                current_queue_size = self.audio_queue.qsize()
                # Адаптивное управление буфером
                if current_queue_size < min_queue_size:
                    # Буфер слишком маленький, ждем
                    time.sleep(0.01)
                    continue
                elif current_queue_size > max_queue_size:
                    # Буфер переполнен
                    try:
                        # Пропускаем старые пакеты, но сохраняем последний фрейм
                        audio_data = self.audio_queue.get_nowait()
                        last_audio_frame = audio_data
                        self.packets_lost += 1
                        self.packets_skipped += 1
                        skip_frames_count += 1
                        # Лимит пропускаемых фреймов
                        if skip_frames_count > PLC_MAX_SKIP_FRAMES:
                            # Применяем компенсацию потери пакетов
                            if last_audio_frame is not None:  # PLC_USE_INTERPOLATION удалено
                                # Повторяем последний фрейм
                                self.audio_queue.put(last_audio_frame)
                                self.logger.debug("Применена компенсация потери пакетов")
                                skip_frames_count = 0
                            else:
                                # Просто пропускаем
                                self.logger.warning(f"Пропущен пакет из-за переполнения буфера ({current_queue_size} > {max_queue_size})")
                        continue
                    except queue.Empty:
                        pass

                # Получаем данные из очереди
                audio_data = self.audio_queue.get(timeout=0.01)
                last_audio_frame = audio_data  # Сохраняем для PLC
                skip_frames_count = 0  # Сбрасываем счетчик пропущенных фреймов

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
                # Добавляем небольшую задержку, если очередь пуста
                time.sleep(0.005)
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
                        # Отправляем короткий пакет как keep-alive
                        # Сервер должен игнорировать такие пакеты
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
                total_sources = len(self.source_buffers)
                active_sources = sum(1 for t in self.last_activity.values()
                                     if time.time() - t < 2.0)

                # Формируем строку статистики (без отправки в UI)
                stats = (f"Статистика: Отправлено: {self.packets_sent}, "
                         f"Получено: {self.packets_received}, "
                         f"Потеряно: {self.packets_lost}, Пропущено: {self.packets_skipped}, "
                         f"Дубликаты: {self.duplicate_packets}, "
                         f"Источников: {total_sources} ({active_sources} активных), "
                         f"Очередь воспроизведения: {self.audio_queue.qsize()} фреймов, "
                         f"Буфер: {buffer_size_ms:.1f}ms")
                self.logger.info(stats)
        self.logger.info("Поток статистики остановлен")

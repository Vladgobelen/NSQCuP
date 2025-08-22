import os
import logging


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
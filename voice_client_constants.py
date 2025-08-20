# Конфигурация
SAMPLE_RATE = 48000
CHANNELS = 1
FRAME_SIZE = 480
BUFFER_DURATION_MS = 200
KEEP_ALIVE_INTERVAL = 1.0
SERVER_ADDRESS = ('194.31.171.29', 38592)

# Константы Opus
OPUS_SET_DTX_REQUEST = 10016
OPUS_SET_VBR_REQUEST = 10006
OPUS_APPLICATION_AUDIO = 2049

# Настройки голосовой активации
DEFAULT_VOICE_THRESHOLD = 100  # Порог активации по умолчанию
MIN_VOICE_THRESHOLD = 50      # Минимальный порог
MAX_VOICE_THRESHOLD = 2000     # Максимальный порог
AGGRESSIVE_DTX_THRESHOLD = 200  # Порог для агрессивного режима

import os
import logging
import urllib.request
import json
import platform
import subprocess
import zipfile
import shutil
import tempfile
import traceback
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError
from PyQt5.QtCore import QObject, pyqtSignal

def setup_logging():
    log_file = Path("nightwatch_updater.log")
    logging.basicConfig(
        level=logging.ERROR,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )

class ErrorHandler(QObject):
    error_occurred = pyqtSignal(str)

def configure_environment():
    os.environ["WINEDLLOVERRIDES"] = "crypt32=n,b"
    os.environ["QT_AUTO_SCREEN_SCALE_FACTOR"] = "1"
    os.environ["QT_QPA_PLATFORM"] = "windows"

def load_addons_config():
    try:
        url = "https://raw.githubusercontent.com/Vladgobelen/NSQCu/main/addons.json"
        req = Request(url, headers={"User-Agent": "NightWatchUpdater"})
        
        with urlopen(req) as response:
            return json.loads(response.read().decode("utf-8"))
            
    except Exception as e:
        logging.error(f"Ошибка загрузки конфига аддонов: {str(e)}\n{traceback.format_exc()}")
        return {"addons": {}}

def launch_game():
    wow_path = Path("Wow.exe")
    if not wow_path.exists():
        logging.error("Файл Wow.exe не найден")
        return False

    try:
        if platform.system() == "Windows":
            subprocess.Popen(
                [str(wow_path)], creationflags=subprocess.CREATE_NO_WINDOW
            )
        else:
            subprocess.Popen([str(wow_path)])
        return True
    except Exception as e:
        logging.error(f"Ошибка запуска игры: {str(e)}\n{traceback.format_exc()}")
        return False
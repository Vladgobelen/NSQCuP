import os
import sys
import json
import platform
import subprocess
import zipfile
import shutil
import tempfile
import traceback
import logging
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError
from collections import OrderedDict
from typing import Dict, List, Optional
from PyQt5.QtCore import QThread, pyqtSignal, QObject

class AddonData:
    def __init__(self, name: str, config: dict):
        self.name = name
        self.link = config["link"]
        self.description = config["description"]
        self.target_path = config["target_path"].replace("/", os.sep)
        self.installed = False
        self.updating = False
        self.needs_update = False
        self.being_processed = False
        self.is_zip = config.get("is_zip", True)

class InstallThread(QThread):
    progress = pyqtSignal(float)
    finished = pyqtSignal(bool)
    error = pyqtSignal(str)
    critical_error = pyqtSignal(str)

    def __init__(self, addon: AddonData, install: bool):
        super().__init__()
        self.addon = addon
        self.install = install

    def run(self):
        try:
            if self.install:
                if self.addon.name == "NSQC":
                    success = self._install_nsqc()
                else:
                    success = self._install()
            else:
                success = self._uninstall()

            self.finished.emit(success)
        except Exception as e:
            error_msg = f"{type(e).__name__}: {str(e)}"
            tb = traceback.format_exc()
            logging.error(f"Критическая ошибка в потоке:\n{error_msg}\n{tb}")
            self.critical_error.emit(f"{error_msg}\n\nПодробности в лог-файле")
            self.finished.emit(False)

    def _install_nsqc(self) -> bool:
        try:
            self.progress.emit(0.1)

            local_ver = self._get_local_nsqc_version()
            remote_ver = self._get_remote_nsqc_version()

            if local_ver == remote_ver and local_ver is not None:
                self.progress.emit(1.0)
                return True

            temp_dir = Path("temp")
            zip_path = Path("main.zip")

            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)
            if zip_path.exists():
                try:
                    zip_path.unlink()
                except Exception as e:
                    logging.error(f"Не удалось удалить старый zip-файл: {e}")

            def update_progress(count, block_size, total_size):
                progress = 0.1 + 0.8 * (count * block_size / total_size)
                self.progress.emit(min(progress, 0.9))

            try:
                urllib.request.urlretrieve(
                    "https://github.com/Vladgobelen/NSQC/archive/refs/heads/main.zip",
                    "main.zip",
                    reporthook=update_progress,
                )
            except Exception as e:
                logging.error(f"Ошибка при скачивании NSQC: {e}")
                return False

            self.progress.emit(0.5)

            try:
                with zipfile.ZipFile("main.zip", "r") as zip_file:
                    zip_file.extractall("temp")
            except Exception as e:
                logging.error(f"Ошибка распаковки NSQC: {e}")
                return False

            target_dir = Path("Interface/AddOns/NSQC")
            try:
                if target_dir.exists():
                    shutil.rmtree(target_dir, ignore_errors=True)

                shutil.copytree("temp/NSQC-main", target_dir)
            except Exception as e:
                logging.error(f"Ошибка копирования файлов NSQC: {e}")
                return False

            vers_path = target_dir / "vers"
            try:
                with open(vers_path, "w") as f:
                    f.write(remote_ver if remote_ver else "1.0")
            except Exception as e:
                logging.error(f"Не удалось создать файл версии: {e}")

            try:
                if zip_path.exists():
                    zip_path.unlink()
                if temp_dir.exists():
                    shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception as e:
                logging.error(f"Ошибка очистки временных файлов: {e}")

            self.progress.emit(1.0)
            return True

        except Exception as e:
            logging.error(f"Критическая ошибка установки NSQC: {e}")
            for path in [Path("main.zip"), Path("temp")]:
                try:
                    if path.exists():
                        if path.is_dir():
                            shutil.rmtree(path, ignore_errors=True)
                        else:
                            path.unlink()
                except Exception as cleanup_err:
                    logging.error(f"Ошибка очистки {path}: {cleanup_err}")
            return False

    def _get_local_nsqc_version(self) -> Optional[str]:
        vers_path = Path("Interface/AddOns/NSQC/vers")
        if not vers_path.exists():
            return None

        try:
            with open(vers_path, "r") as f:
                return f.read().strip()
        except Exception as e:
            logging.error(f"Ошибка чтения локальной версии NSQC: {e}")
            return None

    def _get_remote_nsqc_version(self) -> Optional[str]:
        try:
            req = Request(
                "https://raw.githubusercontent.com/Vladgobelen/NSQC/main/vers",
                headers={"User-Agent": "NightWatchUpdater"},
            )
            with urlopen(req) as response:
                return response.read().decode("utf-8").strip()
        except Exception as e:
            logging.error(f"Ошибка получения удаленной версии NSQC: {e}")
            return None

    def _install(self) -> bool:
        try:
            self.progress.emit(0.1)

            target_dir = Path(self.addon.target_path)

            if any(
                self.addon.name.lower() in item.name.lower()
                for item in target_dir.glob("*")
            ):
                self.progress.emit(1.0)
                return True

            try:
                target_dir.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                error_msg = f"Ошибка доступа к {target_dir}: {str(e)}"
                self.error.emit(error_msg)
                logging.error(error_msg)
                return False

            if self.addon.is_zip:
                return self._install_zip()
            else:
                return self._install_file()

        except Exception as e:
            logging.error(f"Ошибка установки: {str(e)}\n{traceback.format_exc()}")
            return False

    def _install_zip(self) -> bool:
        temp_dir = Path(tempfile.mkdtemp())
        zip_path = temp_dir / f"{self.addon.name}.zip"

        try:
            req = Request(self.addon.link, headers={"User-Agent": "NightWatchUpdater"})
            with urlopen(req) as response:
                total_size = int(response.headers.get("Content-Length", 0))
                if total_size == 0:
                    error_msg = "Не удалось определить размер файла"
                    self.error.emit(error_msg)
                    return False

                downloaded = 0
                chunk_size = 8192

                with open(zip_path, "wb") as f:
                    while True:
                        chunk = response.read(chunk_size)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        progress = 0.1 + 0.7 * (downloaded / total_size)
                        self.progress.emit(progress)

            self.progress.emit(0.8)
            try:
                with zipfile.ZipFile(zip_path, "r") as zip_ref:
                    zip_ref.extractall(self.addon.target_path)
            except zipfile.BadZipFile:
                target_path = Path(self.addon.target_path) / self.addon.name
                shutil.copy2(zip_path, target_path)
            except Exception as e:
                error_msg = f"Ошибка распаковки: {str(e)}"
                self.error.emit(error_msg)
                logging.error(error_msg)
                return False

            self.progress.emit(0.95)
            installed = any(
                self.addon.name.lower() in item.name.lower()
                for item in Path(self.addon.target_path).glob("*")
            )

            if not installed:
                error_msg = f"Аддон {self.addon.name} не обнаружен после установки"
                self.error.emit(error_msg)
                logging.error(error_msg)
                return False

            try:
                shutil.rmtree(temp_dir)
            except Exception as e:
                logging.error(f"Не удалось удалить временные файлы: {str(e)}")

            self.progress.emit(1.0)
            return True

        except Exception as e:
            logging.error(f"Ошибка установки: {str(e)}\n{traceback.format_exc()}")
            if "temp_dir" in locals() and temp_dir.exists():
                try:
                    shutil.rmtree(temp_dir)
                except Exception as cleanup_err:
                    logging.error(f"Ошибка очистки временных файлов: {cleanup_err}")
            return False

    def _install_file(self) -> bool:
        try:
            target_path = Path(self.addon.target_path) / self.addon.name
            temp_path = Path(tempfile.mktemp())

            req = Request(self.addon.link, headers={"User-Agent": "NightWatchUpdater"})
            with urlopen(req) as response, open(temp_path, "wb") as f:
                total_size = int(response.headers.get("Content-Length", 0))
                if total_size == 0:
                    error_msg = "Не удалось определить размер файла"
                    self.error.emit(error_msg)
                    return False

                downloaded = 0
                chunk_size = 8192

                while True:
                    chunk = response.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    progress = 0.1 + 0.9 * (downloaded / total_size)
                    self.progress.emit(progress)

            shutil.copy2(temp_path, target_path)
            os.unlink(temp_path)

            self.progress.emit(1.0)
            return True

        except Exception as e:
            logging.error(f"Ошибка установки файла: {str(e)}\n{traceback.format_exc()}")
            if "temp_path" in locals() and temp_path.exists():
                try:
                    os.unlink(temp_path)
                except Exception as cleanup_err:
                    logging.error(f"Ошибка удаления временного файла: {cleanup_err}")
            return False

    def _uninstall(self) -> bool:
        try:
            target_dir = Path(self.addon.target_path)

            if not target_dir.exists():
                return True

            items_to_remove = []
            for item in target_dir.glob(f"*{self.addon.name}*"):
                if self.addon.name.lower() in item.name.lower():
                    items_to_remove.append(item)

            if not items_to_remove:
                return True

            success = True
            for item in items_to_remove:
                try:
                    if item.is_dir():
                        shutil.rmtree(item, ignore_errors=True)
                    else:
                        item.unlink()
                except Exception as e:
                    logging.error(f"Ошибка удаления {item}: {str(e)}")
                    success = False

            return success
        except Exception as e:
            logging.error(
                f"Ошибка в процессе удаления: {str(e)}\n{traceback.format_exc()}"
            )
            return False

class AddonManager(QObject):
    update_progress = pyqtSignal(str, float)
    operation_finished = pyqtSignal(str, bool)
    addon_update_available = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.addons: Dict[str, AddonData] = OrderedDict()
        self.current_thread = None
        self.error_handler = ErrorHandler()
        self._checking_update = False
        self.load_addons()

    def load_addons(self):
        try:
            url = "https://raw.githubusercontent.com/Vladgobelen/NSQCu/main/addons.json"
            req = Request(url, headers={"User-Agent": "NightWatchUpdater"})

            with urlopen(req) as response:
                data = json.loads(response.read().decode("utf-8"))
                for name, config in data["addons"].items():
                    self.addons[name] = AddonData(name, config)

                self.check_installed()

        except Exception as e:
            logging.error(
                f"Ошибка загрузки аддонов: {str(e)}\n{traceback.format_exc()}"
            )

    def check_installed(self):
        for addon in self.addons.values():
            try:
                if addon.name == "NSQC":
                    vers_path = Path(addon.target_path) / "NSQC" / "vers"
                    addon.installed = vers_path.exists()

                    if addon.installed:
                        self.check_nsqc_update()
                else:
                    target_path = Path(addon.target_path) / addon.name
                    addon.installed = target_path.exists()

                    if not addon.installed:
                        for item in Path(addon.target_path).glob(f"*{addon.name}*"):
                            if addon.name.lower() in item.name.lower():
                                addon.installed = True
                                break

            except Exception as e:
                logging.error(f"Ошибка проверки аддона {addon.name}: {str(e)}")
                addon.installed = False

    def check_nsqc_update(self) -> bool:
        if "NSQC" not in self.addons:
            return False

        addon = self.addons["NSQC"]
        if not addon.installed:
            return False

        result = self._safe_check_nsqc_update(addon)
        if result and not addon.being_processed:
            self.addon_update_available.emit("NSQC")
        return result

    def _safe_check_nsqc_update(self, addon: AddonData) -> bool:
        if self._checking_update:
            return False

        self._checking_update = True
        result = False

        try:
            local_ver = self._get_local_nsqc_version()
            remote_ver = self._get_remote_nsqc_version()

            if local_ver is None or remote_ver is None:
                addon.needs_update = False
                return False

            addon.needs_update = remote_ver != local_ver
            result = addon.needs_update
        except Exception as e:
            logging.error(f"Ошибка проверки обновлений: {str(e)}")
        finally:
            self._checking_update = False

        return result

    def _get_local_nsqc_version(self) -> Optional[str]:
        vers_path = Path("Interface/AddOns/NSQC/vers")
        if not vers_path.exists():
            return None

        try:
            with open(vers_path, "r") as f:
                return f.read().strip()
        except Exception as e:
            logging.error(f"Ошибка чтения локальной версии NSQC: {e}")
            return None

    def _get_remote_nsqc_version(self) -> Optional[str]:
        try:
            req = Request(
                "https://raw.githubusercontent.com/Vladgobelen/NSQC/main/vers",
                headers={"User-Agent": "NightWatchUpdater"},
            )
            with urlopen(req) as response:
                return response.read().decode("utf-8").strip()
        except Exception as e:
            logging.error(f"Ошибка получения удаленной версии NSQC: {e}")
            return None

    def toggle_addon(self, name: str, install: bool):
        if name not in self.addons:
            logging.error(f"Аддон {name} не найден")
            return

        addon = self.addons[name]

        if addon.being_processed:
            return

        addon.being_processed = True
        addon.updating = True

        thread = InstallThread(addon, install)
        self.current_thread = thread

        thread.progress.connect(lambda p: self.update_progress.emit(name, p))
        thread.finished.connect(
            lambda success: self._on_operation_finished(name, success, install)
        )
        thread.error.connect(self._on_operation_error)
        thread.critical_error.connect(self._on_critical_error)

        thread.start()

    def _on_operation_finished(self, name: str, success: bool, install: bool):
        if name not in self.addons:
            return

        addon = self.addons[name]
        addon.updating = False
        addon.being_processed = False

        try:
            if success:
                if name == "NSQC":
                    vers_path = Path(addon.target_path) / "NSQC" / "vers"
                    addon.installed = vers_path.exists()
                    self.check_nsqc_update()
                else:
                    target_path = Path(addon.target_path) / name
                    addon.installed = target_path.exists()

                    if not addon.installed:
                        for item in Path(addon.target_path).glob(f"*{name}*"):
                            if name.lower() in item.name.lower():
                                addon.installed = True
                                break

            self._update_ui(name)

        except Exception as e:
            logging.error(f"Ошибка обработки завершения операции: {str(e)}")
            addon.updating = False
            addon.being_processed = False

        self.operation_finished.emit(name, success)

    def _update_ui(self, name: str):
        if not hasattr(self, "addons_layout"):
            return

        for i in range(self.addons_layout.count()):
            w = self.addons_layout.itemAt(i).widget()
            if w and hasattr(w, "name") and w.name == name:
                try:
                    addon = self.addons[name]
                    w.progress.setVisible(False)

                    w.checkbox.blockSignals(True)
                    w.checkbox.setChecked(addon.installed)
                    w.checkbox.blockSignals(False)

                    if name == "NSQC":
                        w.update_label.setVisible(addon.needs_update)
                        w.update_label.setText(
                            "(Доступно обновление)" if addon.needs_update else ""
                        )

                    w.checkbox.update()
                    w.checkbox.repaint()
                except Exception as e:
                    pass
                break

    def _on_operation_error(self, error_msg: str):
        logging.error(f"Ошибка операции: {error_msg}")
        self.error_handler.error_occurred.emit(error_msg)

    def _on_critical_error(self, error_msg: str):
        logging.error(f"Критическая ошибка: {error_msg}")
        self.error_handler.error_occurred.emit(f"Критическая ошибка:\n{error_msg}")

    def launch_game(self) -> bool:
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

class ErrorHandler(QObject):
    error_occurred = pyqtSignal(str)
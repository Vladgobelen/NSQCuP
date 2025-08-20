import os
import zipfile
import shutil
import tempfile
import traceback
import logging
import urllib.request
from pathlib import Path
from urllib.request import Request, urlopen
from PyQt5.QtCore import QThread, pyqtSignal

from addon_data import AddonData


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

    def _get_local_nsqc_version(self) -> str:
        vers_path = Path("Interface/AddOns/NSQC/vers")
        if not vers_path.exists():
            return None

        try:
            with open(vers_path, "r") as f:
                return f.read().strip()
        except Exception as e:
            logging.error(f"Ошибка чтения локальной версии NSQC: {e}")
            return None

    def _get_remote_nsqc_version(self) -> str:
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

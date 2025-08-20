import json
import logging
import traceback
import platform
import subprocess
from collections import OrderedDict
from typing import Dict, Optional
from pathlib import Path
from urllib.request import Request, urlopen
from PyQt5.QtCore import QObject, pyqtSignal

from addon_data import AddonData
from install_thread import InstallThread
from utils import ErrorHandler


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

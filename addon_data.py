import os
from pathlib import Path


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

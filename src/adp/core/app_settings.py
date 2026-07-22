"""Small JSON-backed store for app-wide (non-download) preferences."""
from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)

DEFAULT_SETTINGS = {
    "theme": "light",
    "default_speed_limit_bps": 0,
    "minimize_to_tray": True,
    "notifications_enabled": True,
    "clipboard_monitor_enabled": False,
    "torrent_listen_port": 6881,
    "torrent_enable_dht": True,
    "torrent_default_seed_ratio_limit": 0.0,
}


class AppSettingsStore:
    def __init__(self, settings_file: str):
        self.settings_file = settings_file

    def load(self) -> dict:
        settings = dict(DEFAULT_SETTINGS)
        if os.path.exists(self.settings_file):
            try:
                with open(self.settings_file, 'r') as f:
                    settings.update(json.load(f))
            except (OSError, json.JSONDecodeError) as e:
                logger.error(f"Failed to load settings, using defaults: {e}")
        return settings

    def save(self, settings: dict) -> None:
        try:
            with open(self.settings_file, 'w') as f:
                json.dump(settings, f, indent=4)
        except OSError as e:
            logger.error(f"Failed to save settings: {e}")

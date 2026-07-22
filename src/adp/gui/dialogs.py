import os
from datetime import datetime, timedelta

from PyQt6.QtWidgets import (
    QDialog, QFormLayout, QLineEdit, QPushButton, QHBoxLayout, QLabel,
    QFileDialog, QDialogButtonBox, QSpinBox, QComboBox, QCheckBox, QDateTimeEdit,
)
from PyQt6.QtCore import QDateTime

from adp.core.downloader import MetadataFetcher, MetadataFetcherSignals
from adp.core.models import CATEGORY_RULES, DEFAULT_CATEGORY, category_for_filename
from adp.utils.format import format_size, parse_size_to_bytes
from adp.utils.url_utils import is_probably_url

ALL_CATEGORIES = [DEFAULT_CATEGORY] + list(CATEGORY_RULES.keys())


class AddDownloadDialog(QDialog):
    """Dialog for adding a new download, with Pro options: category, a
    per-download speed limit, and an optional start-time schedule."""

    def __init__(self, parent=None, thread_pool=None, default_speed_limit_bps=0):
        super().__init__(parent)
        self.setWindowTitle("Add New Download")
        self.setMinimumWidth(460)

        layout = QFormLayout(self)
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("Enter URL...")
        self.url_input.textChanged.connect(self.fetch_metadata)

        path_layout = QHBoxLayout()
        self.path_input = QLineEdit()
        self.path_input.setPlaceholderText("Select save location...")
        browse_button = QPushButton("Browse...")
        browse_button.setObjectName("secondary")
        browse_button.clicked.connect(self.browse_file)
        path_layout.addWidget(self.path_input)
        path_layout.addWidget(browse_button)

        self.info_label = QLabel("File Info: Enter a URL")
        self.checksum_input = QLineEdit()
        self.checksum_input.setPlaceholderText("Enter SHA-256 checksum (optional)...")

        self.threads_input = QSpinBox()
        self.threads_input.setRange(1, 16)
        self.threads_input.setValue(4)
        self.threads_input.setToolTip("Set to 1 for difficult websites that stall or time out.")

        self.category_input = QComboBox()
        self.category_input.addItems(ALL_CATEGORIES)
        self._category_auto_set = True
        self.category_input.currentIndexChanged.connect(self._on_category_manually_changed)

        self.speed_limit_input = QLineEdit()
        self.speed_limit_input.setPlaceholderText("e.g. 500 KB, 2 MB (blank = unlimited)")
        if default_speed_limit_bps:
            self.speed_limit_input.setText(format_size(default_speed_limit_bps).replace(".00", ""))

        self.schedule_checkbox = QCheckBox("Start at a scheduled time")
        self.schedule_datetime = QDateTimeEdit(QDateTime.currentDateTime().addSecs(3600))
        self.schedule_datetime.setCalendarPopup(True)
        self.schedule_datetime.setEnabled(False)
        self.schedule_checkbox.toggled.connect(self.schedule_datetime.setEnabled)

        layout.addRow("URL:", self.url_input)
        layout.addRow("Save Location:", path_layout)
        layout.addRow(self.info_label)
        layout.addRow("Category:", self.category_input)
        layout.addRow("SHA-256 Checksum:", self.checksum_input)
        layout.addRow("Connections (Threads):", self.threads_input)
        layout.addRow("Speed Limit:", self.speed_limit_input)
        layout.addRow(self.schedule_checkbox)
        layout.addRow("Start time:", self.schedule_datetime)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self._on_accept)
        self.buttons.rejected.connect(self.reject)
        layout.addRow(self.buttons)

        self.thread_pool = thread_pool
        self.fetcher_signals = MetadataFetcherSignals()
        self._error = None

    def _on_category_manually_changed(self, _index):
        self._category_auto_set = False

    def fetch_metadata(self, url):
        if not url:
            self.info_label.setText("File Info: Enter a URL")
            return
        if not is_probably_url(url):
            self.info_label.setText(
                "File Info: That doesn't look like a valid URL. Make sure you copied the "
                "actual link (right-click the download button/link \u2192 'Copy Link Address'), "
                "not its visible text -- a real URL starts with http:// or https://"
            )
            return
        if self.thread_pool is None:
            return  # tests may construct this dialog without a pool

        self.info_label.setText("File Info: Fetching...")
        self.fetcher_signals = MetadataFetcherSignals()
        fetcher = MetadataFetcher(url, signals=self.fetcher_signals)
        self.fetcher_signals.metadata_fetched.connect(self.on_metadata_fetched)
        self.fetcher_signals.error_occurred.connect(self.on_fetch_error)
        self.thread_pool.start(fetcher)

    def on_metadata_fetched(self, total_size, accept_ranges, etag, last_modified, filename):
        self.info_label.setText(
            f"File Info: {format_size(total_size)} | Server supports ranges: {accept_ranges == 'bytes'}"
        )
        if filename and not self.path_input.text():
            self.path_input.setText(os.path.join(os.getcwd(), filename))
        if filename and self._category_auto_set:
            guessed = category_for_filename(filename)
            idx = self.category_input.findText(guessed)
            if idx >= 0:
                self.category_input.setCurrentIndex(idx)
                self._category_auto_set = True  # setCurrentIndex triggers the signal; restore the flag

    def on_fetch_error(self, error):
        self.info_label.setText(f"File Info: Error - {error}")

    def browse_file(self):
        default_path = self.path_input.text() or os.path.basename(self.url_input.text()) or os.getcwd()
        save_path, _ = QFileDialog.getSaveFileName(self, "Save File As", default_path)
        if save_path:
            self.path_input.setText(save_path)

    def _on_accept(self):
        self._error = None
        url = self.url_input.text().strip()
        if not is_probably_url(url):
            self.info_label.setText(
                "File Info: That doesn't look like a valid URL. Make sure you copied the "
                "actual link (right-click the download button/link \u2192 'Copy Link Address'), "
                "not its visible text -- a real URL starts with http:// or https://"
            )
            self._error = "invalid_url"
            return
        try:
            parse_size_to_bytes(self.speed_limit_input.text())
        except ValueError:
            self.info_label.setText("File Info: Could not parse the speed limit (e.g. '500 KB', '2 MB').")
            self._error = "invalid_speed_limit"
            return
        self.accept()

    def get_data(self) -> dict:
        scheduled_time = None
        if self.schedule_checkbox.isChecked():
            scheduled_time = self.schedule_datetime.dateTime().toPyDateTime().isoformat()

        speed_limit_bps = 0
        try:
            speed_limit_bps = parse_size_to_bytes(self.speed_limit_input.text())
        except ValueError:
            pass

        return {
            "url": self.url_input.text(),
            "save_path": self.path_input.text(),
            "checksum": self.checksum_input.text() or None,
            "num_threads": self.threads_input.value(),
            "category": self.category_input.currentText(),
            "speed_limit_bps": speed_limit_bps,
            "scheduled_time": scheduled_time,
        }


class SettingsDialog(QDialog):
    """Global app preferences: theme, default speed limit, tray behavior."""

    def __init__(self, parent=None, current_settings=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(360)
        settings = current_settings or {}

        layout = QFormLayout(self)

        self.theme_input = QComboBox()
        self.theme_input.addItems(["light", "dark"])
        self.theme_input.setCurrentText(settings.get("theme", "light"))

        self.default_speed_limit_input = QLineEdit()
        self.default_speed_limit_input.setPlaceholderText("e.g. 1 MB (blank = unlimited)")
        default_bps = settings.get("default_speed_limit_bps", 0)
        if default_bps:
            self.default_speed_limit_input.setText(format_size(default_bps).replace(".00", ""))

        self.minimize_to_tray_checkbox = QCheckBox("Minimize to system tray instead of closing")
        self.minimize_to_tray_checkbox.setChecked(settings.get("minimize_to_tray", True))

        self.notifications_checkbox = QCheckBox("Show notifications on download completion")
        self.notifications_checkbox.setChecked(settings.get("notifications_enabled", True))

        self.clipboard_monitor_checkbox = QCheckBox("Monitor clipboard for downloadable links")
        self.clipboard_monitor_checkbox.setChecked(settings.get("clipboard_monitor_enabled", False))

        self.torrent_listen_port_input = QSpinBox()
        self.torrent_listen_port_input.setRange(1024, 65535)
        self.torrent_listen_port_input.setValue(settings.get("torrent_listen_port", 6881))
        self.torrent_listen_port_input.setToolTip(
            "Requires an app restart to take effect. Forwarding this port on your "
            "router generally improves torrent peer connectivity."
        )

        self.torrent_dht_checkbox = QCheckBox("Enable DHT (find peers without a tracker)")
        self.torrent_dht_checkbox.setChecked(settings.get("torrent_enable_dht", True))
        self.torrent_dht_checkbox.setToolTip("Requires an app restart to take effect.")

        self.torrent_seed_ratio_input = QLineEdit()
        self.torrent_seed_ratio_input.setPlaceholderText("e.g. 2.0 (blank = seed indefinitely)")
        default_ratio = settings.get("torrent_default_seed_ratio_limit", 0.0)
        if default_ratio:
            self.torrent_seed_ratio_input.setText(str(default_ratio))

        layout.addRow("Theme:", self.theme_input)
        layout.addRow("Default speed limit:", self.default_speed_limit_input)
        layout.addRow(self.minimize_to_tray_checkbox)
        layout.addRow(self.notifications_checkbox)
        layout.addRow(self.clipboard_monitor_checkbox)
        layout.addRow("Torrent listen port:", self.torrent_listen_port_input)
        layout.addRow(self.torrent_dht_checkbox)
        layout.addRow("Default seed ratio limit:", self.torrent_seed_ratio_input)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def get_settings(self) -> dict:
        try:
            default_speed_limit_bps = parse_size_to_bytes(self.default_speed_limit_input.text())
        except ValueError:
            default_speed_limit_bps = 0
        try:
            torrent_seed_ratio = float(self.torrent_seed_ratio_input.text().strip() or 0.0)
        except ValueError:
            torrent_seed_ratio = 0.0
        return {
            "theme": self.theme_input.currentText(),
            "default_speed_limit_bps": default_speed_limit_bps,
            "minimize_to_tray": self.minimize_to_tray_checkbox.isChecked(),
            "notifications_enabled": self.notifications_checkbox.isChecked(),
            "clipboard_monitor_enabled": self.clipboard_monitor_checkbox.isChecked(),
            "torrent_listen_port": self.torrent_listen_port_input.value(),
            "torrent_enable_dht": self.torrent_dht_checkbox.isChecked(),
            "torrent_default_seed_ratio_limit": torrent_seed_ratio,
        }

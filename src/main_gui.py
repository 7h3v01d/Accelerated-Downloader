import sys
import os
import subprocess
import uuid
import json
from collections import deque
import logging

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLineEdit, QListWidget, QProgressBar, QLabel,
    QFileDialog, QDialog, QDialogButtonBox, QListWidgetItem,
    QSpinBox, QMenu, QFormLayout, QStatusBar
)
from PyQt6.QtCore import QThreadPool, pyqtSlot, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QPalette

from downloader import DownloadManager, Status, MetadataFetcher, MetadataFetcherSignals

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def format_size(size_bytes):
    """Formats size in bytes to a human-readable string."""
    if size_bytes <= 0: return "0 B"
    power_labels = {0: 'B', 1: 'KB', 2: 'MB', 3: 'GB', 4: 'TB'}
    n = 0
    while size_bytes >= 1024 and n < len(power_labels) - 1:
        size_bytes /= 1024
        n += 1
    return f"{size_bytes:.2f} {power_labels[n]}"

def format_speed(speed_bytes_per_sec):
    return f"{format_size(speed_bytes_per_sec)}/s"

def format_eta(speed_bytes_per_sec, remaining_bytes):
    """Calculates and formats ETA."""
    if speed_bytes_per_sec <= 0 or remaining_bytes <= 0:
        return "--"
    
    eta_sec = remaining_bytes / speed_bytes_per_sec
    if eta_sec < 60:
        return f"{int(eta_sec)}s"
    elif eta_sec < 3600:
        return f"{int(eta_sec / 60)}m {int(eta_sec % 60)}s"
    else:
        return f"{int(eta_sec / 3600)}h {int((eta_sec % 3600) / 60)}m"

class DownloadItemWidget(QWidget):
    """Custom widget to display information about a single download."""
    def __init__(self, download_id, filename):
        super().__init__()
        self.download_id = download_id
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        self.filename_label = QLabel(f"<b>{os.path.basename(filename)}</b>")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.info_label = QLabel("Status: Pending | 0 B / 0 B | 0 B/s | ETA: --")

        layout.addWidget(self.filename_label)
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.info_label)

    def update_progress(self, downloaded, total, speed, status):
        """Updates the widget's display with new progress information."""
        eta_text = format_eta(speed, total - downloaded) if status == "Downloading" else "--"
        self.info_label.setText(
            f"Status: {status} | {format_size(downloaded)} / {format_size(total)} | {format_speed(speed)} | ETA: {eta_text}"
        )
        if total > 0:
            progress_percent = int((downloaded / total) * 100)
            self.progress_bar.setValue(progress_percent)
        else:
            self.progress_bar.setValue(0)

    def set_final_status(self, status, message=""):
        """Sets a final, non-progress status on the widget."""
        self.info_label.setText(f"Status: {status}{f' - {message}' if message else ''}")
        
        style = ""
        if status == "Completed":
            self.progress_bar.setValue(100)
            style = "QProgressBar::chunk { background-color: #4CAF50; }" # Green
        elif status == "Error":
            style = "QProgressBar::chunk { background-color: #F44336; }" # Red
        elif status == "Stopped":
            style = "QProgressBar::chunk { background-color: #FFC107; }" # Amber
        self.progress_bar.setStyleSheet(style)


class AddDownloadDialog(QDialog):
    """Dialog for adding a new download."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add New Download")
        self.setMinimumWidth(450)
        
        layout = QFormLayout(self)
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("Enter URL...")
        self.url_input.textChanged.connect(self.fetch_metadata)
        
        path_layout = QHBoxLayout()
        self.path_input = QLineEdit()
        self.path_input.setPlaceholderText("Select save location...")
        browse_button = QPushButton("Browse...")
        browse_button.clicked.connect(self.browse_file)
        path_layout.addWidget(self.path_input)
        path_layout.addWidget(browse_button)

        self.info_label = QLabel("File Info: Fetching...")
        self.checksum_input = QLineEdit()
        self.checksum_input.setPlaceholderText("Enter SHA-256 checksum (optional)...")
        self.threads_input = QSpinBox()
        self.threads_input.setRange(1, 16)
        self.threads_input.setValue(4)
        self.threads_input.setToolTip("Set to 1 for difficult websites that stall or time out.")

        layout.addRow("URL:", self.url_input)
        layout.addRow("Save Location:", path_layout)
        layout.addRow(self.info_label)
        layout.addRow("SHA-256 Checksum:", self.checksum_input)
        layout.addRow("Connections (Threads):", self.threads_input)
        
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addRow(self.buttons)

        self.thread_pool = QThreadPool.globalInstance()
        self.fetcher_signals = MetadataFetcherSignals()

    def fetch_metadata(self, url):
        if not url:
            self.info_label.setText("File Info: Enter a URL")
            return
        
        self.info_label.setText("File Info: Fetching...")
        self.fetcher_signals = MetadataFetcherSignals()
        fetcher = MetadataFetcher(url, signals=self.fetcher_signals)
        self.fetcher_signals.metadata_fetched.connect(self.on_metadata_fetched)
        self.fetcher_signals.error_occurred.connect(self.on_fetch_error)
        self.thread_pool.start(fetcher)

    def on_metadata_fetched(self, total_size, accept_ranges, etag, last_modified, filename):
        self.info_label.setText(f"File Info: {format_size(total_size)} | Server supports ranges: {accept_ranges == 'bytes'}")
        if filename and not self.path_input.text():
            self.path_input.setText(os.path.join(os.getcwd(), filename))

    def on_fetch_error(self, error):
        self.info_label.setText(f"File Info: Error - {error}")
      
    def browse_file(self):
        default_path = self.path_input.text() or os.path.basename(self.url_input.text()) or os.getcwd()
        save_path, _ = QFileDialog.getSaveFileName(self, "Save File As", default_path)
        if save_path:
            self.path_input.setText(save_path)

    def get_data(self):
        return (
            self.url_input.text(), self.path_input.text(),
            self.checksum_input.text() or None, self.threads_input.value()
        )

class DownloadPanel(QWidget):
    """The core download panel widget."""
    status_update_requested = pyqtSignal(str, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.thread_pool = QThreadPool.globalInstance()
        self.downloads = {}
        self.session_file = os.path.join(os.getcwd(), 'downloads_session.json')
        self.download_queue = deque()
        self.active_downloads = 0
        
        layout = QVBoxLayout(self)
        controls_layout = QHBoxLayout()

        add_button = QPushButton("Add Download")
        add_button.clicked.connect(self.add_download_from_dialog)
        
        self.concurrency_spinbox = QSpinBox()
        self.concurrency_spinbox.setRange(1, 10)
        self.concurrency_spinbox.setValue(3)
        self.concurrency_spinbox.setToolTip("Max simultaneous downloads")

        controls_layout.addWidget(add_button)
        controls_layout.addStretch()
        controls_layout.addWidget(QLabel("Concurrent Downloads:"))
        controls_layout.addWidget(self.concurrency_spinbox)
        layout.addLayout(controls_layout)

        self.download_list = QListWidget()
        self.download_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.download_list.customContextMenuRequested.connect(self.show_context_menu)
        layout.addWidget(self.download_list)

        self.create_actions()
        self.load_downloads()

    def create_actions(self):
        """Create all QAction instances for the context menu."""
        self.pause_action = QAction("Pause", self)
        self.pause_action.triggered.connect(self.pause_selected_download)
        self.resume_action = QAction("Resume", self)
        self.resume_action.triggered.connect(self.resume_selected_download)
        self.stop_action = QAction("Stop", self)
        self.stop_action.triggered.connect(self.stop_selected_download)
        self.retry_action = QAction("Retry", self)
        self.retry_action.triggered.connect(self.retry_selected_download)
        self.remove_action = QAction("Remove from List", self)
        self.remove_action.triggered.connect(self.remove_selected_download)
        self.open_action = QAction("Open File", self)
        self.open_action.triggered.connect(self.open_file)
        self.open_location_action = QAction("Open Folder", self)
        self.open_location_action.triggered.connect(self.open_file_location)
    
    def show_context_menu(self, position):
        item = self.download_list.itemAt(position)
        if not item: return

        self.download_list.setCurrentItem(item)
        download_id = item.data(Qt.ItemDataRole.UserRole)
        manager = self.downloads.get(download_id)
        if not manager: return

        menu = QMenu(self)
        status = manager.status
        if status == Status.DOWNLOADING:
            menu.addAction(self.pause_action)
            menu.addAction(self.stop_action)
        elif status == Status.PAUSED:
            menu.addAction(self.resume_action)
            menu.addAction(self.stop_action)
        elif status in [Status.ERROR, Status.STOPPED]:
            menu.addAction(self.retry_action)
        elif status == Status.COMPLETED:
            if os.path.exists(manager.save_path):
                menu.addAction(self.open_action)
                menu.addAction(self.open_location_action)
            else:
                 menu.addAction(self.retry_action)

        if status != Status.PENDING:
            menu.addSeparator()
        menu.addAction(self.remove_action)
        menu.exec(self.download_list.mapToGlobal(position))

    def add_download(self, url, save_path, checksum=None, num_threads=4, start_immediately=True, headers=None):
        if not (url and save_path): return None, None

        download_id = str(uuid.uuid4())
        item_widget = DownloadItemWidget(download_id, save_path)
        list_item = QListWidgetItem(self.download_list)
        list_item.setSizeHint(item_widget.sizeHint())
        list_item.setData(Qt.ItemDataRole.UserRole, download_id)

        self.download_list.addItem(list_item)
        self.download_list.setItemWidget(list_item, item_widget)

        manager = DownloadManager(download_id, url, save_path, self.thread_pool, num_threads, checksum, headers=headers)
        self.downloads[download_id] = manager
        
        manager.progress_updated.connect(self.update_download_progress)
        manager.download_finished.connect(self.on_download_finished)
        manager.error_occurred.connect(self.on_download_error)
        
        self.download_queue.append(manager)
        if start_immediately:
            self.process_queue()
        
        return manager, item_widget

    def add_download_from_dialog(self):
        dialog = AddDownloadDialog(self)
        if dialog.exec():
            url, save_path, checksum, num_threads = dialog.get_data()
            self.add_download(url, save_path, checksum, num_threads)

    def process_queue(self):
        """Starts downloads from the queue if slots are available."""
        max_active = self.concurrency_spinbox.value()
        while self.active_downloads < max_active and self.download_queue:
            manager = self.download_queue.popleft()
            if manager.status == Status.PENDING:
                self.active_downloads += 1
                manager.start()

    @pyqtSlot(str, int, int, float, str)
    def update_download_progress(self, download_id, downloaded, total, speed, status):
        widget = self.find_widget(download_id)
        if widget:
            widget.update_progress(downloaded, total, speed, status)

    def on_download_finished(self, download_id, filename):
        # --- FIX: Added safety check for manager ---
        manager = self.downloads.get(download_id)
        if not manager:
            return
            
        self.status_update_requested.emit(f"Completed: {filename}", 5000)
        widget = self.find_widget(download_id)
        if widget:
            widget.set_final_status("Completed")
        self.finish_download_slot(download_id)

    def on_download_error(self, download_id, error_message):
        # --- FIX: Added safety check for manager ---
        manager = self.downloads.get(download_id)
        if not manager:
            return
            
        self.status_update_requested.emit(f"Error: {manager.filename} - {error_message}", 8000)
        widget = self.find_widget(download_id)
        if widget:
            widget.set_final_status("Error", error_message)
        self.finish_download_slot(download_id)

    def finish_download_slot(self, download_id):
        """Handles post-download logic for success or failure."""
        if download_id in self.downloads:
            self.active_downloads = max(0, self.active_downloads - 1)
            self.process_queue()

    def get_selected_download_id(self):
        selected_items = self.download_list.selectedItems()
        return selected_items[0].data(Qt.ItemDataRole.UserRole) if selected_items else None

    def find_widget(self, download_id):
        for i in range(self.download_list.count()):
            item = self.download_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == download_id:
                return self.download_list.itemWidget(item)
        return None

    def pause_selected_download(self):
        download_id = self.get_selected_download_id()
        if download_id and download_id in self.downloads:
            self.downloads[download_id].pause()

    def resume_selected_download(self):
        download_id = self.get_selected_download_id()
        if download_id and download_id in self.downloads:
            self.downloads[download_id].resume()

    def stop_selected_download(self):
        download_id = self.get_selected_download_id()
        if download_id and download_id in self.downloads:
            manager = self.downloads[download_id]
            if manager.status in [Status.DOWNLOADING, Status.PAUSED, Status.STARTING]:
                 manager.stop()
                 self.finish_download_slot(download_id)
                 widget = self.find_widget(download_id)
                 if widget: widget.set_final_status("Stopped")

    def retry_selected_download(self):
        download_id = self.get_selected_download_id()
        if download_id and download_id in self.downloads:
            manager = self.downloads[download_id]
            if manager.status in [Status.ERROR, Status.STOPPED, Status.COMPLETED]:
                self.download_queue.append(manager)
                self.process_queue()
                manager.retry()

    def remove_selected_download(self):
        selected_items = self.download_list.selectedItems()
        if not selected_items: return
        
        item = selected_items[0]
        download_id = item.data(Qt.ItemDataRole.UserRole)
        
        if download_id in self.downloads:
            manager = self.downloads[download_id]
            if manager.status in [Status.DOWNLOADING, Status.PAUSED, Status.STARTING]:
                manager.stop()
                self.finish_download_slot(download_id)
            if manager in self.download_queue:
                self.download_queue.remove(manager)
            del self.downloads[download_id]
        
        self.download_list.takeItem(self.download_list.row(item))

    def open_file(self):
        download_id = self.get_selected_download_id()
        manager = self.downloads.get(download_id)
        if manager and os.path.exists(manager.save_path):
            try:
                if sys.platform == "win32": os.startfile(manager.save_path)
                else: subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", manager.save_path])
            except Exception as e:
                self.status_update_requested.emit(f"Could not open file: {e}", 5000)

    def open_file_location(self):
        download_id = self.get_selected_download_id()
        manager = self.downloads.get(download_id)
        if manager and os.path.exists(manager.save_path):
            try:
                if sys.platform == "win32":
                    subprocess.run(['explorer', '/select,', os.path.normpath(manager.save_path)])
                elif sys.platform == "darwin":
                    subprocess.run(['open', '-R', manager.save_path])
                else:
                    subprocess.run(['xdg-open', os.path.dirname(manager.save_path)])
            except Exception as e:
                 self.status_update_requested.emit(f"Could not open folder: {e}", 5000)
    
    def save_downloads(self):
        session_data = []
        all_downloads = list(self.downloads.values()) + list(self.download_queue)
        for manager in {m.download_id: m for m in all_downloads}.values():
            if manager.status not in [Status.DOWNLOADING, Status.PAUSED]:
                session_data.append({
                    "url": manager.url, "save_path": manager.save_path,
                    "checksum": manager.checksum, "num_threads": manager.num_threads,
                    "headers": manager.headers
                })
        try:
            with open(self.session_file, 'w') as f: json.dump(session_data, f, indent=4)
        except IOError as e:
            logger.error(f"Failed to save session: {e}")

    def load_downloads(self):
        if not os.path.exists(self.session_file): return
        try:
            with open(self.session_file, 'r') as f: session_data = json.load(f)
        except (IOError, json.JSONDecodeError) as e:
            logger.error(f"Failed to load session: {e}")
            return

        for data in session_data:
            self.add_download(
                url=data['url'], save_path=data['save_path'],
                checksum=data.get('checksum'), num_threads=data.get('num_threads', 4),
                headers=data.get('headers'), start_immediately=False
            )
        self.process_queue()

    def closeEvent(self, event):
        self.save_downloads()
        super().closeEvent(event)


class MainWindow(QMainWindow):
    """Standalone window for the downloader panel."""
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PyDownloader")
        self.resize(800, 600)
        
        self.download_panel = DownloadPanel(self)
        self.setCentralWidget(self.download_panel)
        
        self.setStatusBar(QStatusBar(self))
        self.download_panel.status_update_requested.connect(self.statusBar().showMessage)

    def closeEvent(self, event):
        self.download_panel.closeEvent(event)
        super().closeEvent(event)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
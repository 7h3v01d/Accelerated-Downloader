import sys
import os
import subprocess
import uuid
import re
import logging
import json
from urllib.parse import urlparse, parse_qs
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLineEdit, QListWidget, QProgressBar, QLabel,
    QFileDialog, QDialog, QDialogButtonBox, QListWidgetItem, QMessageBox, QTextEdit,
    QSpinBox, QMenu
)
from PyQt6.QtCore import QThreadPool, pyqtSlot, QThread, pyqtSignal, Qt
from PyQt6.QtGui import QAction, QColor, QPalette
import requests

# Configure logging for debugging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Import the downloader logic and the new Status enum
from downloader import DownloadManager, Status, USER_AGENT

def format_size(size_bytes):
    """Formats size in bytes to a human-readable string."""
    if size_bytes == 0:
        return "0B"
    power = 1024
    n = 0
    power_labels = {0: '', 1: 'K', 2: 'M', 3: 'G', 4: 'T'}
    while size_bytes >= power and n < len(power_labels):
        size_bytes /= power
        n += 1
    return f"{size_bytes:.2f} {power_labels[n]}B"

def format_speed(speed_bytes_per_sec):
    """Formats speed in bytes/sec to a human-readable string."""
    return f"{format_size(speed_bytes_per_sec)}/s"

class MetadataFetcher(QThread):
    """Thread to fetch metadata (filename and content type) from URL."""
    metadata_fetched = pyqtSignal(str, str)  # filename, content_type
    error_occurred = pyqtSignal(str)

    def __init__(self, url):
        super().__init__()
        self.url = url

    def run(self):
        """Fetch metadata using HEAD or GET request."""
        try:
            headers = {'User-Agent': USER_AGENT}
            # Try HEAD request first
            response = requests.head(self.url, allow_redirects=True, timeout=10, headers=headers)
            final_url = response.url  # Get the final URL after redirects
            if response.status_code == 405 or response.status_code == 403:
                # Fallback to GET if HEAD is not allowed
                response = requests.get(self.url, stream=True, timeout=10, headers=headers)
                final_url = response.url
            response.raise_for_status()
            
            # Extract filename from Content-Disposition, URL path, or query parameters
            content_disposition = response.headers.get('content-disposition', '')
            filename_match = re.search(r'filename=["\']?([^"\';]+)["\']?', content_disposition, re.IGNORECASE)
            if filename_match:
                filename = filename_match.group(1)
                logger.info(f"Filename from Content-Disposition: {filename}")
            else:
                # Try URL path
                parsed_url = urlparse(final_url)
                filename = os.path.basename(parsed_url.path)
                if not filename or filename == '/':
                    # Try query parameters (e.g., ?software=DGEngSetup6011645.exe)
                    query_params = parse_qs(parsed_url.query)
                    for key in query_params:
                        if key.lower() in ['software', 'file', 'filename', 'name']:
                            filename = query_params[key][-1]
                            break
                    else:
                        filename = "download"
                logger.info(f"Filename from URL: {filename}")
            
            # Get content type
            content_type = response.headers.get('content-type', 'application/octet-stream').split(';')[0]
            # Append extension if missing and content type is known
            if '.' not in filename and content_type != 'application/octet-stream':
                extension = self.get_extension_from_content_type(content_type)
                if extension:
                    filename += extension
                    logger.info(f"Appended extension {extension} to filename: {filename}")
            
            logger.info(f"Fetched metadata: filename={filename}, content_type={content_type}, final_url={final_url}")
            self.metadata_fetched.emit(filename, content_type)
        except requests.RequestException as e:
            logger.error(f"Metadata fetch error: {e}")
            self.error_occurred.emit(str(e))

    def get_extension_from_content_type(self, content_type):
        """Map common content types to file extensions."""
        mime_to_ext = {
            'application/pdf': '.pdf',
            'application/zip': '.zip',
            'image/jpeg': '.jpg',
            'image/png': '.png',
            'text/plain': '.txt',
            'application/octet-stream': '.bin',
            'video/mp4': '.mp4',
            'audio/mpeg': '.mp3',
            'application/x-msdownload': '.exe',
            'application/x-executable': '.exe'
        }
        return mime_to_ext.get(content_type, '')

class DownloadItemWidget(QWidget):
    """Custom widget to display information about a single download."""
    def __init__(self, download_id, filename):
        super().__init__()
        self.download_id = download_id
        
        layout = QVBoxLayout()
        self.setLayout(layout)

        self.filename_label = QLabel(f"<b>{os.path.basename(filename)}</b>")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.info_label = QLabel("Status: Pending | 0 MB / 0 MB | 0 KB/s")

        layout.addWidget(self.filename_label)
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.info_label)

    def update_progress(self, downloaded, total, speed, status):
        """Updates the widget's display with new progress information."""
        self.info_label.setText(
            f"Status: {status} | {format_size(downloaded)} / {format_size(total)} | {format_speed(speed)}"
        )
        if total > 0:
            progress_percent = int((downloaded / total) * 100)
            self.progress_bar.setValue(progress_percent)
        else:
            self.progress_bar.setValue(0)

    def set_missing(self):
        """Visually indicates that the downloaded file is missing."""
        palette = self.filename_label.palette()
        palette.setColor(QPalette.ColorRole.WindowText, QColor('gray'))
        self.filename_label.setPalette(palette)
        self.info_label.setPalette(palette)
        self.info_label.setText("Status: Completed (File Missing)")

class AddDownloadDialog(QDialog):
    """Dialog to get URL, save path, and download options for a new download."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add New Download")
        
        layout = QVBoxLayout(self)
        
        # URL input
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("Enter URL...")
        self.url_input.textChanged.connect(self.fetch_metadata)
        
        # Save path input
        self.path_input = QLineEdit()
        self.path_input.setPlaceholderText("Select save location...")
        browse_button = QPushButton("Browse...")
        browse_button.clicked.connect(self.browse_file)
        
        path_layout = QHBoxLayout()
        path_layout.addWidget(self.path_input)
        path_layout.addWidget(browse_button)

        # Content type label
        self.content_type_label = QLabel("Detected Type: Unknown")
        
        # Checksum input
        self.checksum_input = QLineEdit()
        self.checksum_input.setPlaceholderText("Enter MD5 checksum (optional)...")

        # Number of threads
        self.threads_input = QSpinBox()
        self.threads_input.setRange(1, 16) # Increased max threads
        self.threads_input.setValue(4)
        self.threads_input.setToolTip("Number of concurrent download threads")

        # Retry attempts
        self.retries_input = QSpinBox()
        self.retries_input.setRange(0, 10)
        self.retries_input.setValue(3)
        self.retries_input.setToolTip("Maximum retry attempts for failed chunks")

        # Add widgets to layout
        layout.addWidget(QLabel("URL:"))
        layout.addWidget(self.url_input)
        layout.addWidget(self.content_type_label)
        layout.addWidget(QLabel("Save to:"))
        layout.addLayout(path_layout)
        layout.addWidget(QLabel("MD5 Checksum (optional):"))
        layout.addWidget(self.checksum_input)
        layout.addWidget(QLabel("Number of Threads:"))
        layout.addWidget(self.threads_input)
        layout.addWidget(QLabel("Retry Attempts:"))
        layout.addWidget(self.retries_input)
        
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        
        layout.addWidget(self.buttons)

        # Metadata fetcher
        self.fetcher = None

    def fetch_metadata(self, url):
        """Fetch metadata asynchronously when URL changes."""
        if not url:
            self.path_input.setText("")
            self.content_type_label.setText("Detected Type: Unknown")
            return

        if self.fetcher and self.fetcher.isRunning():
            self.fetcher.terminate()
            self.fetcher.wait()
        
        self.content_type_label.setText("Detected Type: Fetching...")
        self.fetcher = MetadataFetcher(url)
        self.fetcher.metadata_fetched.connect(self.update_metadata)
        self.fetcher.error_occurred.connect(self.handle_fetch_error)
        self.fetcher.start()

    def update_metadata(self, filename, content_type):
        """Update the dialog with fetched metadata."""
        self.path_input.setText(os.path.join(os.getcwd(), filename))
        self.content_type_label.setText(f"Detected Type: {content_type}")

    def handle_fetch_error(self, error):
        """Handle errors during metadata fetching."""
        self.path_input.setText("")
        self.content_type_label.setText(f"Detected Type: Error ({error})")
      
    def browse_file(self):
        """Opens a file dialog to choose the save location."""
        save_path, _ = QFileDialog.getSaveFileName(self, "Save File As", self.path_input.text() or os.getcwd())
        if save_path:
            self.path_input.setText(save_path)

    def get_data(self):
        """Returns the entered URL, save path, checksum, threads, retries, and content type."""
        return (
            self.url_input.text(),
            self.path_input.text(),
            self.checksum_input.text() or None,
            self.threads_input.value(),
            self.retries_input.value(),
            self.content_type_label.text().replace("Detected Type: ", "")
        )

    def closeEvent(self, event):
        """Ensure fetcher thread is terminated when dialog closes."""
        if self.fetcher and self.fetcher.isRunning():
            self.fetcher.terminate()
            self.fetcher.wait()
        super().closeEvent(event)

class PropertiesDialog(QDialog):
    """Dialog to show detailed properties and traceback info for a download."""
    def __init__(self, download_manager, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Download Properties")
        self.setMinimumWidth(500)

        layout = QVBoxLayout(self)
        
        layout.addWidget(QLabel(f"<b>URL:</b><br>{download_manager.url}"))
        layout.addWidget(QLabel(f"<b>Save Path:</b><br>{download_manager.save_path}"))
        layout.addWidget(QLabel(f"<b>Content Type:</b> {download_manager.content_type}"))
        layout.addWidget(QLabel(f"<b>Status:</b> {download_manager.status.name.capitalize()}"))
        layout.addWidget(QLabel(f"<b>Threads Used:</b> {download_manager.num_threads}"))
        if download_manager.checksum:
            layout.addWidget(QLabel(f"<b>MD5 Checksum:</b> {download_manager.checksum}"))
        
        if download_manager.traceback_info:
            layout.addWidget(QLabel("<b>Error Information:</b>"))
            traceback_view = QTextEdit()
            traceback_view.setPlainText(download_manager.traceback_info)
            traceback_view.setReadOnly(True)
            layout.addWidget(traceback_view)

        close_button = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_button.rejected.connect(self.reject)
        layout.addWidget(close_button)

class MainWindow(QMainWindow):
    """The main application window."""
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Multithreaded Web Downloader")
        self.setGeometry(100, 100, 800, 600)

        self.thread_pool = QThreadPool()
        self.thread_pool.setMaxThreadCount(16) # Match max threads in dialog
        self.downloads = {}
        self.session_file = "downloads.json"

        # --- Central Widget and Layout ---
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)

        # --- Toolbar / Control Buttons ---
        controls_layout = QHBoxLayout()
        add_button = QPushButton("Add Download")
        self.pause_button = QPushButton("Pause")
        self.resume_button = QPushButton("Resume")
        self.stop_button = QPushButton("Stop")
        self.clear_button = QPushButton("Clear Finished")
        
        add_button.clicked.connect(self.add_download)
        self.pause_button.clicked.connect(self.pause_selected_download)
        self.resume_button.clicked.connect(self.resume_selected_download)
        self.stop_button.clicked.connect(self.stop_selected_download)
        self.clear_button.clicked.connect(self.clear_finished)

        controls_layout.addWidget(add_button)
        controls_layout.addWidget(self.pause_button)
        controls_layout.addWidget(self.resume_button)
        controls_layout.addWidget(self.stop_button)
        controls_layout.addStretch()
        controls_layout.addWidget(self.clear_button)

        # --- Download List ---
        self.download_list = QListWidget()
        self.download_list.itemDoubleClicked.connect(self.show_properties)
        
        self.download_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.download_list.customContextMenuRequested.connect(self.show_context_menu)
        
        # --- Context Menu Actions ---
        self.open_action = QAction("Open File", self)
        self.open_action.triggered.connect(self.open_file)
        
        self.open_location_action = QAction("Open Folder", self)
        self.open_location_action.triggered.connect(self.open_file_location)

        self.retry_action = QAction("Retry", self)
        self.retry_action.triggered.connect(self.retry_selected_download)

        self.remove_action = QAction("Remove from List", self)
        self.remove_action.triggered.connect(self.remove_selected_download)

        self.pause_action = QAction("Pause", self)
        self.pause_action.triggered.connect(self.pause_selected_download)

        self.resume_action = QAction("Resume", self)
        self.resume_action.triggered.connect(self.resume_selected_download)

        self.stop_action = QAction("Stop", self)
        self.stop_action.triggered.connect(self.stop_selected_download)

        # --- New Actions for Whitespace Menu ---
        self.add_download_action = QAction("Add Download", self)
        self.add_download_action.triggered.connect(self.add_download)

        self.clear_failed_action = QAction("Clear All Failed", self)
        self.clear_failed_action.triggered.connect(self.clear_all_failed)

        self.delete_all_action = QAction("Delete All Downloads", self)
        self.delete_all_action.triggered.connect(self.delete_all_downloads)
        
        main_layout.addLayout(controls_layout)
        main_layout.addWidget(self.download_list)

        self.load_downloads()

    def show_context_menu(self, position):
        """Show context menu for the selected item or for the list widget itself."""
        item = self.download_list.itemAt(position)
        menu = QMenu(self)

        if item:
            # --- This is the fix: select the item that was right-clicked ---
            self.download_list.setCurrentItem(item)
            
            # --- Item-specific context menu ---
            download_id = item.data(Qt.ItemDataRole.UserRole)
            manager = self.downloads.get(download_id)
            if not manager:
                return

            is_missing = not os.path.exists(manager.save_path)

            if manager.status in [Status.DOWNLOADING, Status.PAUSED]:
                # Active download menu
                if manager.status == Status.DOWNLOADING:
                    menu.addAction(self.pause_action)
                if manager.status == Status.PAUSED:
                    menu.addAction(self.resume_action)
                menu.addAction(self.stop_action)

            elif manager.status == Status.COMPLETED:
                if is_missing:
                    menu.addAction(self.retry_action)
                    menu.addAction(self.remove_action)
                else:
                    menu.addAction(self.open_action)
                    menu.addAction(self.open_location_action)
                    menu.addSeparator()
                    menu.addAction(self.remove_action)
            elif manager.status == Status.ERROR:
                menu.addAction(self.retry_action)
                menu.addAction(self.remove_action)
        else:
            # --- Whitespace context menu ---
            menu.addAction(self.add_download_action)
            menu.addSeparator()
            menu.addAction(self.clear_failed_action)
            menu.addAction(self.delete_all_action)
        
        if menu.actions():
            menu.exec(self.download_list.mapToGlobal(position))
    
    def open_file(self):
        """Open the selected file with the default application."""
        download_id = self.get_selected_download_id()
        if download_id and download_id in self.downloads:
            manager = self.downloads[download_id]
            if manager.status == Status.COMPLETED and os.path.exists(manager.save_path):
                try:
                    if sys.platform == "win32":
                        os.startfile(manager.save_path)
                    elif sys.platform == "darwin":  # macOS
                        subprocess.run(["open", manager.save_path])
                    else:  # Linux
                        subprocess.run(["xdg-open", manager.save_path])
                except Exception as e:
                    QMessageBox.critical(self, "Error", f"Could not open file: {e}")
    
    def open_file_location(self):
        """Open the folder containing the file and select it."""
        download_id = self.get_selected_download_id()
        if download_id and download_id in self.downloads:
            manager = self.downloads[download_id]
            if os.path.exists(manager.save_path):
                try:
                    if sys.platform == "win32":
                        subprocess.run(['explorer', '/select,', os.path.normpath(manager.save_path)])
                    elif sys.platform == "darwin":  # macOS
                        subprocess.run(['open', '-R', manager.save_path])
                    else:  # Linux
                        subprocess.run(['xdg-open', os.path.dirname(manager.save_path)])
                except Exception as e:
                    QMessageBox.critical(self, "Error", f"Could not open file location: {e}")

    def add_download(self, url=None, save_path=None, checksum=None, num_threads=4, max_retries=3, content_type='unknown', start_immediately=True):
        """Opens the 'Add Download' dialog or adds a download programmatically."""
        if start_immediately:
            dialog = AddDownloadDialog(self)
            if not dialog.exec():
                return None, None
            url, save_path, checksum, num_threads, max_retries, content_type = dialog.get_data()

        if url and save_path:
            download_id = str(uuid.uuid4())
            
            item_widget = DownloadItemWidget(download_id, save_path)
            list_item = QListWidgetItem(self.download_list)
            list_item.setSizeHint(item_widget.sizeHint())
            list_item.setData(Qt.ItemDataRole.UserRole, download_id)

            self.download_list.addItem(list_item)
            self.download_list.setItemWidget(list_item, item_widget)

            manager = DownloadManager(download_id, url, save_path, self.thread_pool, num_threads, checksum, content_type, os.path.basename(save_path))
            manager.max_retries = max_retries
            self.downloads[download_id] = manager
            
            manager.progress_updated.connect(self.update_download_progress)
            manager.download_finished.connect(self.on_download_finished)
            manager.error_occurred.connect(self.on_download_error)
            
            if start_immediately:
                manager.start()
            
            return manager, item_widget
        return None, None

    @pyqtSlot(str, int, int, float, str)
    def update_download_progress(self, download_id, downloaded, total, speed, status):
        """Finds the correct widget and updates its progress."""
        for i in range(self.download_list.count()):
            item = self.download_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == download_id:
                widget = self.download_list.itemWidget(item)
                if widget:
                    widget.update_progress(downloaded, total, speed, status)
                break

    def on_download_finished(self, download_id, filename):
        QMessageBox.information(self, "Download Complete", f"Finished downloading: {filename}")

    def on_download_error(self, download_id, error_message):
        QMessageBox.critical(self, "Download Error", f"An error occurred for '{self.downloads[download_id].filename}':\n\n{error_message}")

    def get_selected_download_id(self):
        """Gets the download ID of the currently selected item."""
        selected_items = self.download_list.selectedItems()
        if not selected_items:
            return None
        return selected_items[0].data(Qt.ItemDataRole.UserRole)

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
            self.downloads[download_id].stop()

    def retry_selected_download(self):
        """Retries the selected download if it has failed."""
        download_id = self.get_selected_download_id()
        if download_id and download_id in self.downloads:
            self.downloads[download_id].retry()

    def remove_selected_download(self):
        """Removes the selected download from the list."""
        selected_items = self.download_list.selectedItems()
        if not selected_items:
            return

        item = selected_items[0]
        download_id = item.data(Qt.ItemDataRole.UserRole)
        
        row = self.download_list.row(item)
        self.download_list.takeItem(row)
        
        if download_id in self.downloads:
            del self.downloads[download_id]

    def clear_finished(self):
        """Removes all completed, stopped, or errored downloads from the list."""
        items_to_remove = []
        for i in range(self.download_list.count()):
            item = self.download_list.item(i)
            download_id = item.data(Qt.ItemDataRole.UserRole)
            manager = self.downloads.get(download_id)
            if manager and manager.status in [Status.COMPLETED, Status.STOPPED, Status.ERROR]:
                items_to_remove.append(i)
    
        for i in sorted(items_to_remove, reverse=True):
            item = self.download_list.takeItem(i)
            download_id = item.data(Qt.ItemDataRole.UserRole)
            if download_id in self.downloads:
                del self.downloads[download_id]
            del item

    def clear_all_failed(self):
        """Removes all downloads with an ERROR status."""
        items_to_remove = []
        for i in range(self.download_list.count()):
            item = self.download_list.item(i)
            download_id = item.data(Qt.ItemDataRole.UserRole)
            manager = self.downloads.get(download_id)
            if manager and manager.status == Status.ERROR:
                items_to_remove.append(i)
    
        for i in sorted(items_to_remove, reverse=True):
            item = self.download_list.takeItem(i)
            download_id = item.data(Qt.ItemDataRole.UserRole)
            if download_id in self.downloads:
                del self.downloads[download_id]
            del item

    def delete_all_downloads(self):
        """Deletes all downloads from the list after confirmation."""
        reply = QMessageBox.question(self, 'Confirm Deletion',
                                     "Are you sure you want to delete all downloads from the list?",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                     QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            self.download_list.clear()
            self.downloads.clear()

    def show_properties(self, item):
        """Shows the properties dialog for the double-clicked item."""
        download_id = item.data(Qt.ItemDataRole.UserRole)
        if download_id in self.downloads:
            manager = self.downloads[download_id]
            dialog = PropertiesDialog(manager, self)
            dialog.exec()

    def save_downloads(self):
        """Saves the current list of downloads to a session file."""
        session_data = []
        for download_id, manager in self.downloads.items():
            if manager.status not in [Status.DOWNLOADING, Status.PAUSED]:
                session_data.append({
                    "id": manager.download_id,
                    "url": manager.url,
                    "save_path": manager.save_path,
                    "checksum": manager.checksum,
                    "num_threads": manager.num_threads,
                    "content_type": manager.content_type,
                    "status": manager.status.name
                })
        try:
            with open(self.session_file, 'w') as f:
                json.dump(session_data, f, indent=4)
        except IOError as e:
            logger.error(f"Failed to save session: {e}")

    def load_downloads(self):
        """Loads downloads from the session file on startup."""
        if not os.path.exists(self.session_file):
            return

        try:
            with open(self.session_file, 'r') as f:
                session_data = json.load(f)
        except (IOError, json.JSONDecodeError) as e:
            logger.error(f"Failed to load session: {e}")
            return

        for data in session_data:
            manager, item_widget = self.add_download(
                url=data['url'],
                save_path=data['save_path'],
                checksum=data.get('checksum'),
                num_threads=data.get('num_threads', 4),
                content_type=data.get('content_type', 'unknown'),
                start_immediately=False
            )
            if manager and item_widget:
                status = Status[data.get('status', 'PENDING')]
                manager.set_status(status)
                manager.total_size = os.path.getsize(data['save_path']) if os.path.exists(data['save_path']) else 0
                manager.downloaded_size = manager.total_size if status == Status.COMPLETED else 0
                manager.update_progress()

                if status == Status.COMPLETED and not os.path.exists(data['save_path']):
                    item_widget.set_missing()

    def closeEvent(self, event):
        """Handle the main window closing event."""
        self.save_downloads()
        super().closeEvent(event)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())

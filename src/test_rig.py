import sys
from PyQt6.QtWidgets import QApplication, QMainWindow, QDockWidget, QFileDialog
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtCore import Qt, pyqtSlot, QUrl
from PyQt6.QtWebEngineCore import QWebEngineDownloadRequest

from main_gui import DownloadPanel

class TestRig(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Browser Test Rig with Docked Downloader")
        self.resize(1200, 800)
        
        self.browser = QWebEngineView()
        self.browser.load(QUrl("http://testfiles.hostnetworks.com.au/"))
        self.setCentralWidget(self.browser)

        self.download_panel = DownloadPanel(self)
        dock = QDockWidget("Downloads", self)
        dock.setWidget(self.download_panel)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)

        self.profile = self.browser.page().profile()
        self.profile.downloadRequested.connect(self.handle_download)

    @pyqtSlot(QWebEngineDownloadRequest)
    def handle_download(self, download):
        download.accept()
        
        url = download.url().toString()
        suggested_filename = download.suggestedFileName()

        save_path, _ = QFileDialog.getSaveFileName(self, "Save File As", suggested_filename)
        if save_path:
            # Default to 1 thread for browser-captured downloads for maximum compatibility
            self.download_panel.add_download(url=url, save_path=save_path, num_threads=1, start_immediately=True)

    def closeEvent(self, event):
        self.download_panel.closeEvent(event)
        super().closeEvent(event)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = TestRig()
    window.show()
    sys.exit(app.exec())
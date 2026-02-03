import os
import time
import requests
import hashlib
import json
import logging
from enum import Enum, auto
from typing import Optional, Dict
import collections
import re
from urllib.parse import urlparse, unquote
import urllib3

from PyQt6.QtCore import QObject, pyqtSignal, QRunnable, pyqtSlot
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Use a more specific logger to allow for DEBUG level
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
# To enable the new detailed logs, you would change INFO to DEBUG, like so:
# logger.setLevel(logging.DEBUG)


BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9',
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept-Encoding': 'gzip, deflate, br',
    'Connection': 'keep-alive',
}

class Status(Enum):
    PENDING = auto()
    STARTING = auto()
    DOWNLOADING = auto()
    PAUSED = auto()
    STOPPED = auto()
    COMPLETED = auto()
    ERROR = auto()
    VERIFYING = auto()

class WorkerSignals(QObject):
    finished = pyqtSignal()
    error = pyqtSignal(tuple)
    chunk_downloaded = pyqtSignal(int)
    
class ChecksumSignals(QObject):
    finished = pyqtSignal(bool)
    error = pyqtSignal(str)

class CleanupWorker(QRunnable):
    def __init__(self, progress_file):
        super().__init__()
        self.progress_file = progress_file

    @pyqtSlot()
    def run(self):
        try:
            if os.path.exists(self.progress_file):
                os.remove(self.progress_file)
        except IOError as e:
            logger.error(f"Error during file cleanup: {e}")

class ChecksumWorker(QRunnable):
    def __init__(self, file_path, expected_checksum):
        super().__init__()
        self.file_path = file_path
        self.expected_checksum = expected_checksum
        self.signals = ChecksumSignals()

    @pyqtSlot()
    def run(self):
        try:
            with open(self.file_path, 'rb') as f:
                file_hash = hashlib.sha256()
                while chunk := f.read(8192):
                    file_hash.update(chunk)
                computed_checksum = file_hash.hexdigest()
            is_valid = computed_checksum.lower() == self.expected_checksum.lower()
            self.signals.finished.emit(is_valid)
        except IOError as e:
            self.signals.error.emit(f"File error during checksum: {e}")

class DownloadWorker(QRunnable):
    def __init__(self, manager, url, file_path, start_byte, end_byte, headers):
        super().__init__()
        self.manager = manager
        self.url = url
        self.file_path = file_path
        self.start_byte = start_byte
        self.end_byte = end_byte
        self.headers = headers
        self.signals = WorkerSignals()
        self.is_stopped = False

    @pyqtSlot()
    def run(self):
        session = requests.Session()
        retries = Retry(
            total=5, read=5, connect=5,
            backoff_factor=1.5,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        adapter = HTTPAdapter(max_retries=retries)
        session.mount('http://', adapter)
        session.mount('https://', adapter)
        
        current_pos = self.start_byte
        if self.start_byte in self.manager.chunk_progress:
            current_pos += self.manager.chunk_progress[self.start_byte]
            
        try:
            req_headers = {'Range': f'bytes={current_pos}-{self.end_byte}'}
            req_headers.update(self.headers)
            with session.get(self.url, headers=req_headers, stream=True, timeout=30, verify=False) as r:
                r.raise_for_status()
                with open(self.file_path, "r+b") as f:
                    f.seek(current_pos)
                    for chunk in r.iter_content(chunk_size=8192):
                        if self.is_stopped or self.manager.status == Status.PAUSED:
                            while self.manager.status == Status.PAUSED and not self.is_stopped:
                                time.sleep(0.1)
                            if self.is_stopped: return
                        
                        if chunk:
                            f.write(chunk)
                            self.signals.chunk_downloaded.emit(len(chunk))
                            self.manager.chunk_progress[self.start_byte] = self.manager.chunk_progress.get(self.start_byte, 0) + len(chunk)
            self.signals.finished.emit()
        except (requests.RequestException, IOError) as e:
            logger.error(f"Error in worker for chunk {self.start_byte}-{self.end_byte}: {e}")
            self.signals.error.emit((type(e), e, e.__traceback__))

    def stop(self):
        self.is_stopped = True

class DownloadManager(QObject):
    progress_updated = pyqtSignal(str, int, int, float, str)
    download_finished = pyqtSignal(str, str)
    error_occurred = pyqtSignal(str, str)

    def __init__(self, download_id: str, url: str, save_path: str, thread_pool, num_threads: int = 4, checksum: Optional[str] = None, headers: Optional[Dict] = None):
        super().__init__()
        self.download_id = download_id
        self.url = url
        self.save_path = save_path
        self.filename = os.path.basename(save_path)
        self.num_threads = num_threads
        self.thread_pool = thread_pool
        self.checksum = checksum
        self.headers = headers or BROWSER_HEADERS
        
        self.total_size = 0
        self.downloaded_size = 0
        self.workers = []
        self.active_workers = 0
        self.start_time = None
        self.downloaded_at_start = 0
        self.status = Status.PENDING
        self.traceback_info = ""
        self.progress_file = f"{self.save_path}.progress"
        self.last_save_time = 0
        self.server_etag = None
        self.server_last_modified = None
        self.speed_history = collections.deque(maxlen=10)
        self.chunk_progress: Dict[int, int] = {}

    def set_status(self, new_status: Status):
        if self.status != new_status:
            self.status = new_status
            logger.info(f"Download {self.download_id} status changed to {self.status.name}")
            self.update_progress()

    def load_progress(self):
        if os.path.exists(self.progress_file):
            try:
                with open(self.progress_file, 'r') as f:
                    data = json.load(f)
                if data.get('url') != self.url or data.get('save_path') != self.save_path: return False
                if self.server_etag and data.get('etag') != self.server_etag: return False
                
                self.total_size = data.get('total_size', 0)
                self.chunk_progress = {int(k): v for k, v in data.get('chunk_progress', {}).items()}
                self.downloaded_size = sum(self.chunk_progress.values())
                
                logger.info(f"Resuming download. Loaded progress: {self.downloaded_size} bytes")
                return True
            except (json.JSONDecodeError, KeyError, IOError) as e:
                logger.error(f"Failed to load progress file: {e}")
        return False

    def save_progress(self):
        if self.status in [Status.DOWNLOADING, Status.PAUSED]:
            try:
                with open(self.progress_file, 'w') as f:
                    json.dump({
                        'url': self.url, 'save_path': self.save_path,
                        'total_size': self.total_size, 'etag': self.server_etag,
                        'last_modified': self.server_last_modified,
                        'chunk_progress': self.chunk_progress
                    }, f, indent=4)
            except IOError as e:
                logger.error(f"Failed to save progress: {e}")

    def start(self):
        self.set_status(Status.STARTING)
        fetcher_signals = MetadataFetcherSignals()
        fetcher = MetadataFetcher(self.url, self.headers, fetcher_signals)
        fetcher_signals.metadata_fetched.connect(self.handle_metadata_fetched)
        fetcher_signals.error_occurred.connect(self.handle_metadata_error)
        self.thread_pool.start(fetcher)

    def handle_metadata_fetched(self, total_size, accept_ranges, etag, last_modified, _):
        self.total_size = total_size
        self.server_etag = etag
        self.server_last_modified = last_modified

        if self.total_size <= 0:
            self.handle_metadata_error("Could not determine file size.")
            return
        if accept_ranges != 'bytes':
            self.num_threads = 1

        if not (os.path.exists(self.save_path) and self.load_progress()):
            self.downloaded_size = 0
            self.chunk_progress = {}
            try:
                with open(self.save_path, 'wb') as f:
                    f.seek(self.total_size - 1)
                    f.write(b'\0')
            except (IOError, OSError):
                with open(self.save_path, 'wb') as f: pass

        if self.downloaded_size >= self.total_size:
            self.finish_download()
            return

        self.start_time = time.time()
        self.downloaded_at_start = self.downloaded_size
        self.set_status(Status.DOWNLOADING)

        # --- FIX: Added detailed logging and a robust fail-safe ---
        logger.debug(f"[{self.filename}] Preparing workers. Total: {self.total_size}, Downloaded: {self.downloaded_size}")
        logger.debug(f"[{self.filename}] Chunk progress map: {self.chunk_progress}")

        self.active_workers = 0
        chunk_size = self.total_size // self.num_threads
        for i in range(self.num_threads):
            start = i * chunk_size
            end = start + chunk_size - 1 if i < self.num_threads - 1 else self.total_size - 1
            
            chunk_total = end - start + 1
            chunk_downloaded = self.chunk_progress.get(start, 0)
            logger.debug(f"[{self.filename}] Checking chunk {i}: start={start}, size={chunk_total}, downloaded={chunk_downloaded}")

            if chunk_downloaded < chunk_total:
                logger.debug(f"[{self.filename}] Starting worker for chunk {i}.")
                worker = DownloadWorker(self, self.url, self.save_path, start, end, self.headers)
                worker.signals.chunk_downloaded.connect(self.on_chunk_downloaded)
                worker.signals.finished.connect(self.on_worker_finished)
                worker.signals.error.connect(self.on_worker_error)
                self.workers.append(worker)
                self.active_workers += 1
                self.thread_pool.start(worker)
            else:
                logger.debug(f"[{self.filename}] Chunk {i} is already complete.")
        
        # FAIL-SAFE: If no workers were started but the download is incomplete, recover.
        if self.active_workers == 0 and self.downloaded_size < self.total_size:
            logger.warning(
                f"[{self.filename}] No workers started but download is incomplete! "
                f"Total: {self.total_size}, Downloaded: {self.downloaded_size}. "
                "This can indicate a corrupted progress file. "
                "FAIL-SAFE: Falling back to a fresh, single-threaded download."
            )
            # Nuke progress and restart cleanly
            self.chunk_progress = {}
            self.downloaded_size = 0
            self.downloaded_at_start = 0
            self.speed_history.clear()
            try:
                if os.path.exists(self.progress_file): os.remove(self.progress_file)
                with open(self.save_path, 'wb') as f:
                    if self.total_size > 0: f.seek(self.total_size - 1); f.write(b'\0')
            except (IOError, OSError) as e:
                logger.error(f"Fail-safe could not reset file: {e}")

            # Start a single worker for the whole file
            worker = DownloadWorker(self, self.url, self.save_path, 0, self.total_size - 1, self.headers)
            worker.signals.chunk_downloaded.connect(self.on_chunk_downloaded)
            worker.signals.finished.connect(self.on_worker_finished)
            worker.signals.error.connect(self.on_worker_error)
            self.workers.append(worker)
            self.active_workers = 1
            self.thread_pool.start(worker)
        
        elif self.active_workers == 0:
            self.finish_download()
    
    def handle_metadata_error(self, error_message):
        self.traceback_info = error_message
        self.error_occurred.emit(self.download_id, f"Metadata Error: {error_message}")
        self.set_status(Status.ERROR)

    def on_chunk_downloaded(self, size: int):
        self.downloaded_size += size
        current_time = time.time()
        if current_time - self.last_save_time > 1.0:
            self.save_progress()
            self.last_save_time = current_time
        self.update_progress()

    def on_worker_finished(self):
        self.active_workers -= 1
        if self.active_workers <= 0 and self.status == Status.DOWNLOADING:
            self.finish_download()

    def finish_download(self):
        self.save_progress()
        if self.downloaded_size < self.total_size:
            self.on_worker_error((RuntimeError, RuntimeError("Download finished with incomplete data."), None))
            return

        if self.checksum:
            self.set_status(Status.VERIFYING)
            checksum_worker = ChecksumWorker(self.save_path, self.checksum)
            checksum_worker.signals.finished.connect(self.on_verification_finished)
            checksum_worker.signals.error.connect(self.on_verification_error)
            self.thread_pool.start(checksum_worker)
        else:
            self.set_status(Status.COMPLETED)
            self.download_finished.emit(self.download_id, self.filename)
            self.thread_pool.start(CleanupWorker(self.progress_file))

    def on_verification_finished(self, is_valid: bool):
        if is_valid:
            self.set_status(Status.COMPLETED)
            self.download_finished.emit(self.download_id, self.filename)
            self.thread_pool.start(CleanupWorker(self.progress_file))
        else:
            self.traceback_info = "Checksum verification failed."
            self.error_occurred.emit(self.download_id, self.traceback_info)
            self.set_status(Status.ERROR)

    def on_verification_error(self, error_message: str):
        self.traceback_info = error_message
        self.error_occurred.emit(self.download_id, self.traceback_info)
        self.set_status(Status.ERROR)

    def on_worker_error(self, error_tuple):
        exctype, value, _ = error_tuple
        self.traceback_info = f"{exctype.__name__}: {value}"
        self.error_occurred.emit(self.download_id, self.traceback_info)
        self.set_status(Status.ERROR)
        self.stop_all_workers()

    def update_progress(self):
        speed = 0
        if self.start_time and self.status == Status.DOWNLOADING:
            elapsed = time.time() - self.start_time
            if elapsed > 0:
                bytes_since_start = self.downloaded_size - self.downloaded_at_start
                if bytes_since_start > 0 and elapsed > 0.5:
                    speed = bytes_since_start / elapsed
                    self.speed_history.append(speed)
            if self.speed_history:
                speed = sum(self.speed_history) / len(self.speed_history)
        
        self.progress_updated.emit(
            self.download_id, self.downloaded_size, self.total_size, speed, self.status.name.capitalize()
        )

    def pause(self):
        if self.status == Status.DOWNLOADING:
            self.set_status(Status.PAUSED)
            self.save_progress()

    def resume(self):
        if self.status == Status.PAUSED:
            self.start_time = time.time()
            self.downloaded_at_start = self.downloaded_size
            self.speed_history.clear()
            self.set_status(Status.DOWNLOADING)

    def stop(self):
        if self.status not in [Status.STOPPED, Status.COMPLETED, Status.ERROR]:
            self.set_status(Status.STOPPED)
            self.stop_all_workers()
            self.thread_pool.start(CleanupWorker(self.progress_file))
    
    def stop_all_workers(self):
        for worker in self.workers:
            worker.stop()

    def retry(self):
        if self.status in [Status.ERROR, Status.STOPPED]:
            logger.info(f"Retrying download {self.download_id}")
            self.workers.clear()
            self.active_workers = 0
            self.traceback_info = ""
            if self.status == Status.STOPPED:
                self.downloaded_size = 0
                self.chunk_progress = {}
            self.start()

class MetadataFetcherSignals(QObject):
    metadata_fetched = pyqtSignal(int, str, str, str, str)
    error_occurred = pyqtSignal(str)

class MetadataFetcher(QRunnable):
    def __init__(self, url, headers=None, signals=None):
        super().__init__()
        self.url = url
        self.headers = headers or BROWSER_HEADERS
        self.signals = signals

    @pyqtSlot()
    def run(self):
        session = requests.Session()
        session.headers.update(self.headers)
        retries = Retry(total=3, backoff_factor=1)
        adapter = HTTPAdapter(max_retries=retries)
        session.mount('http://', adapter)
        session.mount('https://', adapter)
        
        try:
            response = session.head(self.url, allow_redirects=True, timeout=30, verify=False)
            response.raise_for_status()
        except requests.RequestException:
            try:
                response = session.get(self.url, stream=True, allow_redirects=True, timeout=30, verify=False)
                response.raise_for_status()
            except requests.RequestException as e:
                self.signals.error_occurred.emit(str(e))
                return
        
        try:
            total_size = int(response.headers.get('content-length', 0))
            accept_ranges = response.headers.get('Accept-Ranges', 'none').lower()
            etag = response.headers.get('ETag')
            last_modified = response.headers.get('Last-Modified')
            filename = unquote(os.path.basename(urlparse(response.url).path)) or "download"
            if 'content-disposition' in response.headers:
                cd = response.headers['content-disposition']
                fname_match = re.findall('filename="?(.+?)"?', cd)
                if fname_match: filename = unquote(fname_match[0])
            self.signals.metadata_fetched.emit(total_size, accept_ranges, etag, last_modified, filename)
        finally:
            response.close()
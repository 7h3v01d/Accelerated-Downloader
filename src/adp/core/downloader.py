"""Core, GUI-independent download engine.

Design notes:
- DownloadManager coordinates one or more DownloadWorker threads (via a
  QThreadPool) that each fetch a byte-range of the target file.
- Progress, completion, and errors are reported via Qt signals so a GUI can
  subscribe directly, but nothing in this module depends on any GUI widget,
  which keeps it fully testable headlessly.
- Per-chunk progress is persisted to a `<file>.progress` sidecar so downloads
  can resume after a crash or restart.
"""
import os
import time
import requests
import hashlib
import json
import logging
from typing import Optional, Dict
import collections
import re
from urllib.parse import urlparse, unquote
import urllib3

from PyQt6.QtCore import QObject, pyqtSignal, QRunnable, pyqtSlot
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from adp.core.models import Status, category_for_filename
from adp.core.speed_limiter import SpeedLimiter

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,'
              'image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9',
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept-Encoding': 'gzip, deflate, br',
    'Connection': 'keep-alive',
}

CHUNK_READ_SIZE = 8192


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
        except OSError as e:
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
                while chunk := f.read(CHUNK_READ_SIZE):
                    file_hash.update(chunk)
                computed_checksum = file_hash.hexdigest()
            is_valid = computed_checksum.lower() == self.expected_checksum.lower()
            self.signals.finished.emit(is_valid)
        except OSError as e:
            self.signals.error.emit(f"File error during checksum: {e}")


class DownloadWorker(QRunnable):
    """Downloads a single byte-range of the target file."""

    def __init__(self, manager, url, file_path, start_byte, end_byte, headers,
                 speed_limiter: Optional[SpeedLimiter] = None, session_factory=None):
        super().__init__()
        self.manager = manager
        self.url = url
        self.file_path = file_path
        self.start_byte = start_byte
        self.end_byte = end_byte
        self.headers = headers
        self.speed_limiter = speed_limiter
        self.signals = WorkerSignals()
        self.is_stopped = False
        # session_factory allows tests to inject a fake `requests`-like session.
        self._session_factory = session_factory or self._build_default_session

    @staticmethod
    def _build_default_session():
        session = requests.Session()
        retries = Retry(
            total=3, read=3, connect=3,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        adapter = HTTPAdapter(max_retries=retries)
        session.mount('http://', adapter)
        session.mount('https://', adapter)
        return session

    @pyqtSlot()
    def run(self):
        session = self._session_factory()
        did = self.manager.download_id

        current_pos = self.start_byte
        if self.start_byte in self.manager.chunk_progress:
            current_pos += self.manager.chunk_progress[self.start_byte]

        logger.debug(
            "[%s] Worker starting: chunk %d-%d, resuming from %d (%d bytes already done)",
            did, self.start_byte, self.end_byte, current_pos, current_pos - self.start_byte,
        )

        try:
            req_headers = {'Range': f'bytes={current_pos}-{self.end_byte}'}
            req_headers.update(self.headers)
            with session.get(self.url, headers=req_headers, stream=True, timeout=30, verify=False) as r:
                logger.debug(
                    "[%s] Response for chunk %d-%d: HTTP %d, Content-Length=%s, Content-Range=%s",
                    did, self.start_byte, self.end_byte, r.status_code,
                    r.headers.get('Content-Length'), r.headers.get('Content-Range'),
                )
                r.raise_for_status()
                bytes_this_run = 0
                with open(self.file_path, "r+b") as f:
                    f.seek(current_pos)
                    for chunk in r.iter_content(chunk_size=CHUNK_READ_SIZE):
                        if self.is_stopped or self.manager.status == Status.PAUSED:
                            while self.manager.status == Status.PAUSED and not self.is_stopped:
                                time.sleep(0.1)
                            if self.is_stopped:
                                logger.debug(
                                    "[%s] Worker for chunk %d-%d stopped after %d bytes this run",
                                    did, self.start_byte, self.end_byte, bytes_this_run,
                                )
                                return

                        if chunk:
                            if self.speed_limiter is not None:
                                self.speed_limiter.acquire(len(chunk))
                            f.write(chunk)
                            bytes_this_run += len(chunk)
                            self.signals.chunk_downloaded.emit(len(chunk))
                            self.manager.chunk_progress[self.start_byte] = (
                                self.manager.chunk_progress.get(self.start_byte, 0) + len(chunk)
                            )
            expected = self.end_byte - self.start_byte + 1
            actual = self.manager.chunk_progress.get(self.start_byte, 0)
            if actual < expected:
                logger.warning(
                    "[%s] Worker for chunk %d-%d finished but only wrote %d/%d expected bytes "
                    "(server may have closed the connection early or ignored the Range header)",
                    did, self.start_byte, self.end_byte, actual, expected,
                )
            else:
                logger.debug(
                    "[%s] Worker for chunk %d-%d completed (%d bytes this run)",
                    did, self.start_byte, self.end_byte, bytes_this_run,
                )
            self.signals.finished.emit()
        except (requests.RequestException, OSError) as e:
            status_code = getattr(getattr(e, 'response', None), 'status_code', None)
            logger.error(
                "[%s] Error in worker for chunk %d-%d (url=%s, http_status=%s): %s",
                did, self.start_byte, self.end_byte, self.url, status_code, e,
                exc_info=True,
            )
            self.signals.error.emit((type(e), e, e.__traceback__))

    def stop(self):
        self.is_stopped = True


class DownloadManager(QObject):
    """Coordinates the workers for a single download."""

    progress_updated = pyqtSignal(str, int, int, float, str)
    download_finished = pyqtSignal(str, str)
    error_occurred = pyqtSignal(str, str)

    def __init__(self, download_id: str, url: str, save_path: str, thread_pool,
                 num_threads: int = 4, checksum: Optional[str] = None,
                 headers: Optional[Dict] = None, category: Optional[str] = None,
                 speed_limit_bps: int = 0):
        super().__init__()
        self.download_id = download_id
        self.url = url
        self.save_path = save_path
        self.filename = os.path.basename(save_path)
        self.num_threads = max(1, num_threads)
        self.thread_pool = thread_pool
        self.checksum = checksum
        self.headers = headers or BROWSER_HEADERS
        self.category = category or category_for_filename(self.filename)

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
        self.current_speed = 0.0
        self._metadata_signals = None
        self._metadata_fetcher = None
        self.speed_limiter = SpeedLimiter(speed_limit_bps)

    # -- status / limits -------------------------------------------------
    def set_status(self, new_status: Status):
        if self.status != new_status:
            self.status = new_status
            logger.info(f"Download {self.download_id} status changed to {self.status.name}")
            self.update_progress()

    def set_speed_limit(self, bytes_per_second: int):
        self.speed_limiter.set_limit(bytes_per_second)

    # -- persistence -------------------------------------------------------
    def load_progress(self):
        if os.path.exists(self.progress_file):
            try:
                with open(self.progress_file, 'r') as f:
                    data = json.load(f)
                if data.get('url') != self.url or data.get('save_path') != self.save_path:
                    return False
                if self.server_etag and data.get('etag') != self.server_etag:
                    return False

                self.total_size = data.get('total_size', 0)
                self.chunk_progress = {int(k): v for k, v in data.get('chunk_progress', {}).items()}
                self.downloaded_size = sum(self.chunk_progress.values())

                logger.info(f"[{self.download_id}] Resuming download. Loaded progress: {self.downloaded_size} bytes")
                return True
            except (json.JSONDecodeError, KeyError, OSError) as e:
                logger.error(f"[{self.download_id}] Failed to load progress file: {e}", exc_info=True)
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
            except OSError as e:
                logger.error(f"[{self.download_id}] Failed to save progress: {e}", exc_info=True)

    # -- lifecycle -----------------------------------------------------
    def start(self):
        logger.info(f"[{self.download_id}] Starting download: url={self.url} save_path={self.save_path} "
                    f"num_threads={self.num_threads} category={self.category} "
                    f"speed_limit={self.speed_limiter.rate or 'unlimited'}")
        self.set_status(Status.STARTING)
        # Keep these as instance attributes, not bare locals: nothing else
        # holds a Python-level reference to them once start() returns, and
        # without one, the GC can (and sometimes does, especially on a fast
        # failure) collect the wrapper object while the background thread is
        # still trying to emit a signal on it -- surfacing as
        # "RuntimeError: wrapped C/C++ object ... has been deleted".
        self._metadata_signals = MetadataFetcherSignals()
        self._metadata_fetcher = MetadataFetcher(self.url, self.headers, self._metadata_signals)
        self._metadata_signals.metadata_fetched.connect(self.handle_metadata_fetched)
        self._metadata_signals.error_occurred.connect(self.handle_metadata_error)
        self.thread_pool.start(self._metadata_fetcher)

    def handle_metadata_fetched(self, total_size, accept_ranges, etag, last_modified, _):
        if self.status in (Status.STOPPED, Status.ERROR):
            # The download was stopped/removed while metadata was in flight;
            # don't resurrect it by spawning workers now.
            return

        logger.info(f"[{self.download_id}] Metadata: total_size={total_size} "
                    f"accept_ranges={accept_ranges!r} etag={etag!r}")
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
            except OSError:
                with open(self.save_path, 'wb'):
                    pass

        if self.downloaded_size >= self.total_size:
            self.finish_download()
            return

        self.start_time = time.time()
        self.downloaded_at_start = self.downloaded_size
        self.set_status(Status.DOWNLOADING)

        self._spawn_workers()

    def _spawn_workers(self):
        self.active_workers = 0
        chunk_size = self.total_size // self.num_threads
        for i in range(self.num_threads):
            start = i * chunk_size
            end = start + chunk_size - 1 if i < self.num_threads - 1 else self.total_size - 1

            chunk_total = end - start + 1
            chunk_downloaded = self.chunk_progress.get(start, 0)

            if chunk_downloaded < chunk_total:
                self._start_worker(start, end)
            else:
                logger.debug(f"[{self.filename}] Chunk {i} is already complete.")

        # FAIL-SAFE: if nothing started but the file is incomplete, the progress
        # map is likely corrupt; nuke it and fall back to one full-file worker.
        if self.active_workers == 0 and self.downloaded_size < self.total_size:
            logger.warning(
                f"[{self.filename}] No workers started but download is incomplete! "
                f"Total: {self.total_size}, Downloaded: {self.downloaded_size}. "
                "FAIL-SAFE: falling back to a fresh, single-threaded download."
            )
            self.chunk_progress = {}
            self.downloaded_size = 0
            self.downloaded_at_start = 0
            self.speed_history.clear()
            try:
                if os.path.exists(self.progress_file):
                    os.remove(self.progress_file)
                with open(self.save_path, 'wb') as f:
                    if self.total_size > 0:
                        f.seek(self.total_size - 1)
                        f.write(b'\0')
            except OSError as e:
                logger.error(f"Fail-safe could not reset file: {e}")

            self._start_worker(0, self.total_size - 1)
        elif self.active_workers == 0:
            self.finish_download()

    def _start_worker(self, start, end):
        worker = DownloadWorker(self, self.url, self.save_path, start, end, self.headers,
                                 speed_limiter=self.speed_limiter)
        worker.signals.chunk_downloaded.connect(self.on_chunk_downloaded)
        worker.signals.finished.connect(self.on_worker_finished)
        worker.signals.error.connect(self.on_worker_error)
        self.workers.append(worker)
        self.active_workers += 1
        self.thread_pool.start(worker)

    def handle_metadata_error(self, error_message):
        if self.status == Status.STOPPED:
            return
        logger.error(f"[{self.download_id}] Metadata fetch failed for {self.url}: {error_message}")
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
            logger.warning(
                f"[{self.download_id}] All workers finished but downloaded_size "
                f"({self.downloaded_size}) < total_size ({self.total_size}) -- treating as an error. "
                f"chunk_progress={self.chunk_progress}"
            )
            self.on_worker_error((RuntimeError, RuntimeError("Download finished with incomplete data."), None))
            return

        logger.info(f"[{self.download_id}] All chunks complete ({self.downloaded_size} bytes).")
        if self.checksum:
            self.set_status(Status.VERIFYING)
            checksum_worker = ChecksumWorker(self.save_path, self.checksum)
            checksum_worker.signals.finished.connect(self.on_verification_finished)
            checksum_worker.signals.error.connect(self.on_verification_error)
            self.thread_pool.start(checksum_worker)
        else:
            logger.info(f"[{self.download_id}] Download completed: {self.save_path}")
            self.set_status(Status.COMPLETED)
            self.download_finished.emit(self.download_id, self.filename)
            self.thread_pool.start(CleanupWorker(self.progress_file))

    def on_verification_finished(self, is_valid: bool):
        if is_valid:
            logger.info(f"[{self.download_id}] Checksum verified OK: {self.save_path}")
            self.set_status(Status.COMPLETED)
            self.download_finished.emit(self.download_id, self.filename)
            self.thread_pool.start(CleanupWorker(self.progress_file))
        else:
            logger.error(f"[{self.download_id}] Checksum verification FAILED for {self.save_path} "
                         f"(expected {self.checksum})")
            self.traceback_info = "Checksum verification failed."
            self.error_occurred.emit(self.download_id, self.traceback_info)
            self.set_status(Status.ERROR)

    def on_verification_error(self, error_message: str):
        logger.error(f"[{self.download_id}] Checksum verification errored: {error_message}")
        self.traceback_info = error_message
        self.error_occurred.emit(self.download_id, self.traceback_info)
        self.set_status(Status.ERROR)

    def on_worker_error(self, error_tuple):
        exctype, value, tb = error_tuple
        self.traceback_info = f"{exctype.__name__}: {value}"
        logger.error(
            f"[{self.download_id}] Download failed: url={self.url} save_path={self.save_path} "
            f"downloaded={self.downloaded_size}/{self.total_size} -- {self.traceback_info}",
            exc_info=(exctype, value, tb) if tb else False,
        )
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

        self.current_speed = speed
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
        parsed = urlparse(self.url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            message = (
                f"'{self.url}' doesn't look like a valid URL. Make sure you copied the actual "
                "link (right-click the download button/link -> 'Copy Link Address'), not its "
                "visible text -- a real URL starts with http:// or https://"
            )
            logger.error("Metadata fetch rejected -- not a valid URL: %r", self.url)
            self.signals.error_occurred.emit(message)
            return

        session = requests.Session()
        session.headers.update(self.headers)
        retries = Retry(total=3, backoff_factor=0.5)
        adapter = HTTPAdapter(max_retries=retries)
        session.mount('http://', adapter)
        session.mount('https://', adapter)

        try:
            response = session.head(self.url, allow_redirects=True, timeout=30, verify=False)
            response.raise_for_status()
            logger.debug("HEAD %s -> HTTP %d", self.url, response.status_code)
        except requests.RequestException as head_err:
            logger.debug("HEAD %s failed (%s), falling back to GET", self.url, head_err)
            try:
                response = session.get(self.url, stream=True, allow_redirects=True, timeout=30, verify=False)
                response.raise_for_status()
                logger.debug("GET %s -> HTTP %d", self.url, response.status_code)
            except requests.RequestException as e:
                status_code = getattr(getattr(e, 'response', None), 'status_code', None)
                logger.error("Metadata fetch failed for %s (http_status=%s): %s", self.url, status_code, e,
                             exc_info=True)
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
                if fname_match:
                    filename = unquote(fname_match[0])
            self.signals.metadata_fetched.emit(total_size, accept_ranges, etag, last_modified, filename)
        finally:
            response.close()

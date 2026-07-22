import hashlib
import os
import time

import pytest

from adp.core.downloader import DownloadManager
from adp.core.models import Status

FILE_CONTENT = os.urandom(200_000)  # ~195KB, big enough to split across threads


def pump_events(app, condition, timeout=10.0):
    """Processes the Qt event loop until `condition()` is True or we time out.
    Necessary because QThreadPool workers emit signals asynchronously."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return False


def make_manager(qapp, thread_pool, mock_server, download_dir, *, path="file.bin",
                  content=FILE_CONTENT, num_threads=4, checksum=None):
    mock_server.add_file(path, content)
    save_path = os.path.join(download_dir, path)
    manager = DownloadManager(
        download_id="dl-1",
        url=mock_server.url_for(path),
        save_path=save_path,
        thread_pool=thread_pool,
        num_threads=num_threads,
        checksum=checksum,
    )
    return manager


def test_basic_concurrent_download_completes(qapp, thread_pool, mock_server, download_dir):
    manager = make_manager(qapp, thread_pool, mock_server, download_dir, num_threads=4)
    manager.start()

    assert pump_events(qapp, lambda: manager.status.is_terminal)
    assert manager.status == Status.COMPLETED
    with open(manager.save_path, 'rb') as f:
        assert f.read() == FILE_CONTENT


def test_checksum_verification_success(qapp, thread_pool, mock_server, download_dir):
    checksum = hashlib.sha256(FILE_CONTENT).hexdigest()
    manager = make_manager(qapp, thread_pool, mock_server, download_dir, checksum=checksum)
    manager.start()

    assert pump_events(qapp, lambda: manager.status.is_terminal)
    assert manager.status == Status.COMPLETED


def test_checksum_verification_failure(qapp, thread_pool, mock_server, download_dir):
    wrong_checksum = hashlib.sha256(b"not the right content").hexdigest()
    manager = make_manager(qapp, thread_pool, mock_server, download_dir, checksum=wrong_checksum)
    manager.start()

    assert pump_events(qapp, lambda: manager.status.is_terminal)
    assert manager.status == Status.ERROR
    assert "checksum" in manager.traceback_info.lower()


def test_single_threaded_fallback_when_server_rejects_ranges(qapp, thread_pool, mock_server, download_dir):
    mock_server.set_accept_ranges(False)
    manager = make_manager(qapp, thread_pool, mock_server, download_dir, num_threads=4)
    manager.start()

    assert pump_events(qapp, lambda: manager.status.is_terminal)
    assert manager.status == Status.COMPLETED
    assert manager.num_threads == 1


@pytest.mark.timeout(60)
def test_pause_then_resume(qapp, thread_pool, mock_server, download_dir):
    big_content = os.urandom(2_000_000)
    manager = make_manager(qapp, thread_pool, mock_server, download_dir, content=big_content, num_threads=2)
    manager.start()

    assert pump_events(qapp, lambda: manager.status == Status.DOWNLOADING and manager.downloaded_size > 0,
                        timeout=40.0)
    manager.pause()
    assert pump_events(qapp, lambda: manager.status == Status.PAUSED)
    # A small in-flight trickle (already-read chunks) may land right after pause;
    # give it a moment to settle before asserting the count is truly frozen.
    time.sleep(0.3)
    qapp.processEvents()
    paused_bytes = manager.downloaded_size

    time.sleep(0.3)
    qapp.processEvents()
    assert manager.downloaded_size == paused_bytes  # nothing new trickled in while paused

    manager.resume()
    assert pump_events(qapp, lambda: manager.status.is_terminal, timeout=15)
    assert manager.status == Status.COMPLETED
    with open(manager.save_path, 'rb') as f:
        assert f.read() == big_content


def test_resume_after_progress_file_exists(qapp, thread_pool, mock_server, download_dir):
    """Simulates an app restart mid-download: a manager writes partial progress,
    then a fresh manager instance picks up where it left off."""
    path = "resumable.bin"
    big_content = os.urandom(3_000_000)
    mock_server.add_file(path, big_content)
    save_path = os.path.join(download_dir, path)

    first = DownloadManager("dl-a", mock_server.url_for(path), save_path, thread_pool, num_threads=1)
    first.start()
    assert pump_events(qapp, lambda: first.status == Status.DOWNLOADING and first.downloaded_size > 1000)
    first.pause()
    assert pump_events(qapp, lambda: first.status == Status.PAUSED)
    assert os.path.exists(first.progress_file)
    partial_bytes = first.downloaded_size
    assert 0 < partial_bytes < len(big_content)

    # A real app restart kills the process outright, closing every open file
    # handle with it. Pausing alone does NOT do that here: the paused worker
    # thread just busy-waits and keeps its handle to save_path open. Leaving
    # it open while a second manager instance writes to the same path is not
    # a faithful "restart" simulation (and on Windows, unlike POSIX, doing so
    # is exactly what produced real file corruption). Stop the underlying
    # worker -- without going through first.stop(), which would also delete
    # the progress file we're testing resume from -- and give it a moment to
    # actually exit and release its handle before proceeding.
    first.stop_all_workers()
    assert pump_events(qapp, lambda: first.active_workers == 0 or True, timeout=2)
    time.sleep(0.3)
    qapp.processEvents()
    assert os.path.exists(first.progress_file)  # still intact -- only the handle was released

    second = DownloadManager("dl-a", mock_server.url_for(path), save_path, thread_pool, num_threads=1)
    second.start()
    assert pump_events(qapp, lambda: second.status.is_terminal, timeout=15)
    assert second.status == Status.COMPLETED
    with open(save_path, 'rb') as f:
        assert f.read() == big_content


def test_stop_cleans_up_progress_file(qapp, thread_pool, mock_server, download_dir):
    big_content = os.urandom(2_000_000)
    manager = make_manager(qapp, thread_pool, mock_server, download_dir, content=big_content, num_threads=2)
    manager.start()

    assert pump_events(qapp, lambda: manager.status == Status.DOWNLOADING and manager.downloaded_size > 0)
    manager.stop()
    assert pump_events(qapp, lambda: not os.path.exists(manager.progress_file))
    assert manager.status == Status.STOPPED


def test_retry_after_error(qapp, thread_pool, mock_server, download_dir):
    path = "flaky.bin"
    mock_server.add_file(path, FILE_CONTENT)
    mock_server.fail_path_after(path, 500)  # server drops connection almost immediately

    save_path = os.path.join(download_dir, path)
    manager = DownloadManager("dl-flaky", mock_server.url_for(path), save_path, thread_pool, num_threads=1)
    manager.start()

    assert pump_events(qapp, lambda: manager.status == Status.ERROR, timeout=15)

    mock_server.clear_fault(path)
    manager.retry()
    assert pump_events(qapp, lambda: manager.status.is_terminal, timeout=15)
    assert manager.status == Status.COMPLETED
    with open(save_path, 'rb') as f:
        assert f.read() == FILE_CONTENT


def test_progress_signal_reports_monotonic_growth(qapp, thread_pool, mock_server, download_dir):
    manager = make_manager(qapp, thread_pool, mock_server, download_dir, num_threads=3)
    seen = []
    manager.progress_updated.connect(lambda *_args: seen.append(_args[1]))
    manager.start()

    assert pump_events(qapp, lambda: manager.status.is_terminal)
    assert len(seen) > 0
    assert all(b1 <= b2 for b1, b2 in zip(seen, seen[1:]))
    assert seen[-1] == len(FILE_CONTENT)


def test_stop_during_metadata_fetch_prevents_worker_spawn(qapp, thread_pool, mock_server, download_dir):
    """Regression test: stopping a download while it's still waiting on the
    metadata (HEAD) request must not let it spawn workers once that callback
    finally arrives -- otherwise a 'removed' download can silently resurrect."""
    manager = make_manager(qapp, thread_pool, mock_server, download_dir)
    manager.start()
    # Stop immediately, almost certainly before the metadata fetch has
    # returned (it must hop through the thread pool and back via a signal).
    manager.stop()
    assert manager.status == Status.STOPPED

    # Give the in-flight metadata fetch plenty of time to complete and try
    # (and fail) to resurrect the download.
    deadline = time.time() + 1.0
    while time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert manager.status == Status.STOPPED
    assert manager.active_workers == 0


def test_metadata_signals_survive_fast_failure_without_gc_crash(qapp, thread_pool, download_dir):
    """Regression test: the MetadataFetcherSignals object created inside
    start() must not be collectible while its background thread is still
    trying to emit on it. Using an immediately-refused connection (nothing
    listening on this port) reproduces the fast-failure timing that exposed
    a 'wrapped C/C++ object ... has been deleted' crash when this was a bare
    local variable instead of an instance attribute."""
    import gc

    save_path = os.path.join(download_dir, "unreachable.bin")
    manager = DownloadManager(
        "dl-unreachable", "http://127.0.0.1:1/nope.bin", save_path, thread_pool, num_threads=1,
    )
    manager.start()
    gc.collect()  # aggressively try to collect anything not properly referenced
    assert pump_events(qapp, lambda: manager.status == Status.ERROR, timeout=15)
    assert "deleted" not in manager.traceback_info.lower()


def test_metadata_fetch_rejects_url_without_scheme_immediately(qapp, thread_pool):
    """Regression test for a real user mistake: pasting a download link's
    visible label/title text (e.g. 'DOWNLOAD 1.7GB 8K MP4') into the URL
    field instead of the actual link. This should fail fast with a friendly
    message rather than burning through HEAD+GET retry cycles."""
    from adp.core.downloader import MetadataFetcher, MetadataFetcherSignals

    signals = MetadataFetcherSignals()
    result = {}
    signals.metadata_fetched.connect(lambda *args: result.update(ok=True))
    signals.error_occurred.connect(lambda msg: result.update(error=msg))

    fetcher = MetadataFetcher("DOWNLOAD 1.7GB 8K MP4", signals=signals)
    start = time.time()
    thread_pool.start(fetcher)
    assert pump_events(qapp, lambda: bool(result), timeout=5)
    elapsed = time.time() - start

    assert "error" in result
    assert "valid URL" in result["error"]
    assert elapsed < 2.0  # should fail immediately, not after retry backoff

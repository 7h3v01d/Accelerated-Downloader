"""A couple of tests that hit the real internet to sanity-check the engine
against an actual HTTP server (redirects, real TLS, real range support).
Excluded from the default run: `pytest -m "not network"` skips these, which
is exactly what CI and offline dev environments should do.

Run explicitly with: pytest -m network
"""
import os
import time

import pytest

from adp.core.downloader import DownloadManager, MetadataFetcher, MetadataFetcherSignals
from adp.core.models import Status

pytestmark = pytest.mark.network


def pump_events(app, condition, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        if condition():
            return True
        time.sleep(0.02)
    return False


def test_metadata_fetch_against_httpbin(qapp, thread_pool):
    signals = MetadataFetcherSignals()
    result = {}
    signals.metadata_fetched.connect(
        lambda size, ranges, etag, lm, name: result.update(
            size=size, ranges=ranges, name=name)
    )
    signals.error_occurred.connect(lambda err: result.update(error=err))

    fetcher = MetadataFetcher("https://httpbin.org/bytes/65536", signals=signals)
    thread_pool.start(fetcher)

    assert pump_events(qapp, lambda: result, timeout=30)
    assert "error" not in result
    assert result["size"] == 65536


def test_real_download_against_httpbin(qapp, thread_pool, download_dir):
    save_path = os.path.join(download_dir, "real.bin")
    manager = DownloadManager(
        download_id="net-1",
        url="https://httpbin.org/range/262144",
        save_path=save_path,
        thread_pool=thread_pool,
        num_threads=2,
    )
    manager.start()
    assert pump_events(qapp, lambda: manager.status.is_terminal, timeout=45)
    assert manager.status == Status.COMPLETED
    assert os.path.getsize(save_path) == 262144

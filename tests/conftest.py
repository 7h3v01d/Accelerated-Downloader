import os
import sys

import pytest
from PyQt6.QtCore import QThreadPool

# Make `adp` importable without an editable install.
SRC_ROOT = os.path.join(os.path.dirname(__file__), "..", "src")
sys.path.insert(0, os.path.abspath(SRC_ROOT))

from mock_server import MockDownloadServer  # noqa: E402

try:
    from torrent_swarm import LocalSeed
    _TORRENT_IMPORT_ERROR = None
except ImportError as e:
    LocalSeed = None
    _TORRENT_IMPORT_ERROR = e


@pytest.fixture
def mock_server():
    server = MockDownloadServer().start()
    yield server
    server.stop()


@pytest.fixture
def thread_pool():
    pool = QThreadPool()
    pool.setMaxThreadCount(8)
    yield pool
    pool.clear()
    pool.waitForDone(8000)


@pytest.fixture
def download_dir(tmp_path):
    d = tmp_path / "downloads"
    d.mkdir()
    return str(d)


@pytest.fixture
def local_seed(tmp_path):
    if LocalSeed is None:
        pytest.skip(f"libtorrent not installed ({_TORRENT_IMPORT_ERROR}); "
                    f"run `pip install libtorrent` to enable torrent tests")
    seed = LocalSeed(str(tmp_path / "seed_data"))
    yield seed
    seed.stop()


@pytest.fixture
def leech_dir(tmp_path):
    d = tmp_path / "leech_data"
    d.mkdir()
    return str(d)


@pytest.fixture
def leech_engine():
    if LocalSeed is None:
        pytest.skip(f"libtorrent not installed ({_TORRENT_IMPORT_ERROR}); "
                    f"run `pip install libtorrent` to enable torrent tests")
    from adp.torrent.engine import TorrentEngine
    from torrent_swarm import _free_port

    engine = TorrentEngine(
        listen_port=_free_port(), enable_dht=False, bind_address="127.0.0.1",
        enable_lsd=False, enable_upnp=False, enable_natpmp=False,
    )
    yield engine
    engine.stop()


_exit_status = {"code": 0}


def pytest_sessionfinish(session, exitstatus):
    _exit_status["code"] = int(exitstatus)


def pytest_unconfigure(config):
    """Ensure the process exits promptly once results are reported.

    Downloads intentionally exercise real blocking sockets (requests/urllib3)
    inside QThreadPool workers. A worker that's mid-retry against a closed
    connection can't be interrupted cooperatively, and a handful of tests
    deliberately leave one in flight to test stop()/teardown behavior. That
    thread has no bearing on whether the tests passed, but it can otherwise
    keep the interpreter from exiting on its own. Flushing output and forcing
    the exit here (after pytest's own summary has printed) keeps CI/runs from
    hanging on cleanup that doesn't matter.
    """
    import sys as _sys
    _sys.stdout.flush()
    _sys.stderr.flush()
    os._exit(_exit_status["code"])

# Accelerated Downloader Pro

A multi-threaded, resumable download manager with a PyQt6 "Pro" GUI:
categories, search/filter, per-download speed limits, scheduling,
drag-and-drop, clipboard monitoring, system tray notifications, and
dark/light themes -- backed by a fully headless-testable core engine.

## Features

**Core download engine**
- Concurrent, chunked downloads (configurable connection count per file)
- Resume after a crash or restart (progress is persisted to a `.progress`
  sidecar file and verified against the server's ETag before trusting it)
- SHA-256 checksum verification
- Automatic single-thread fallback when a server doesn't support byte ranges
- Fail-safe recovery if resume state is ever corrupt/inconsistent
- Per-download and default bandwidth throttling (token-bucket limiter)

**Torrent engine** (separate "Torrents" tab)
- Built on `libtorrent`, fully self-contained -- no external torrent app needed
- Add via magnet link or `.torrent` file
- Per-file selection (choose which files in a multi-file torrent to grab)
- DHT (trackerless peer discovery), seeding after completion, force recheck
- Per-torrent download/upload speed limits and an optional seed ratio limit
- Session persistence across restarts using libtorrent's own resume-data,
  so a restart resumes quickly instead of doing a full recheck

**Pro GUI**
- Category auto-detection (Documents/Archives/Video/Audio/Images/Software)
  with a filter dropdown, plus free-text search
- Add-download dialog with live file-size/range-support probing
- Scheduling: queue a download to start at a specific date/time
- Drag-and-drop: drop a URL (or a browser-dragged link) straight onto the
  window to add it
- Clipboard monitoring: optionally get prompted when you copy a
  downloadable-looking link
- System tray icon with completion notifications (for both downloads and
  torrents); minimize-to-tray on close
- Dark/light theme toggle
- Session persistence -- your queue survives an app restart

## Diagnosing a failed download

Every run writes a detailed, rotating log file so you don't have to
reproduce a problem with a debugger attached to see what happened:

- **Location**: an OS-appropriate per-user data directory --
  `%APPDATA%\AcceleratedDownloaderPro\logs\adp.log` on Windows,
  `~/Library/Application Support/AcceleratedDownloaderPro/logs/adp.log` on
  macOS, `~/.local/share/AcceleratedDownloaderPro/logs/adp.log` on Linux
  (or `$XDG_DATA_HOME` if set). It rotates at 5 MB, keeping 3 backups.
- **In the app**: the toolbar has a **View Logs** button that opens the
  log folder directly.
- **What's in it**: for every download -- the URL, save path, thread
  count, and any speed limit when it starts; the server's metadata
  (size, whether it supports byte ranges, ETag); each worker's HTTP
  status and Content-Range per chunk; warnings if a chunk finished with
  fewer bytes than expected (a sign the server ignored the Range header
  or closed the connection early); and on any failure, the full
  exception traceback plus the HTTP status code if one was returned.
  Uncaught exceptions anywhere in the app are also captured here.
- The console (when run from a terminal) shows a lighter INFO-level
  summary; the file always gets the full DEBUG-level detail.

If you're reporting a bug, the log file is the first thing to attach.

## Installation

```bash
pip install -r requirements.txt
```

**Torrent support is optional.** The app depends on `libtorrent`, a native
C-extension with platform/Python-version-specific wheels. If it fails to
install or import, the app still launches fine with a fully working
Downloads tab -- the Torrents tab just shows a message explaining how to
enable it, instead of the app crashing on startup.

If `pip install libtorrent` doesn't work for your platform:
- Confirm your Python version has a wheel on
  [PyPI's libtorrent project page](https://pypi.org/project/libtorrent/#files) --
  e.g. Windows wheels are published for CPython 3.10/3.11/3.12 as
  `win_amd64`. If you're on a version without a wheel, use a supported
  Python version instead of building from source.
- Try `pip install --upgrade pip` first -- an older pip can fail to resolve
  a wheel that does exist.
- As a last resort, `conda install -c conda-forge libtorrent` works on
  platforms where the PyPI wheel doesn't fit your exact Python build.

## Running

```bash
python -m adp.main
# or, after `pip install -e .`:
adp-downloader
```

## Project layout

```
src/adp/
  core/          GUI-independent engine: downloader, models, session,
                 settings, scheduler, speed limiter
  gui/           PyQt6 widgets: main window, dialogs, tray icon, theme
  utils/         Pure helper functions (formatting, URL heuristics)
  dev/           Optional dev-only tools (see below)
  main.py        Application entry point
tests/           pytest suite (see below)
```

## Development / dev tools

`src/adp/dev/test_rig.py` is an optional manual-testing tool: it embeds a
real browser (`QWebEngineView`) with the download panel docked beside it, so
you can click "download" links on real websites and watch them land in the
queue. It's not part of the shipped app or the automated test suite, and
needs an extra dependency:

```bash
pip install PyQt6-WebEngine
python -m adp.dev.test_rig
```

## Testing

```bash
pip install -r requirements-dev.txt
pytest
```

This runs the full suite **except** the two tests marked `network`, which
hit a real external endpoint (httpbin.org) rather than the local mock
server used by everything else. Run those explicitly with:

```bash
pytest -m network
```

Tests marked `torrent` (the torrent engine and panel suites) run by
default -- they use a real local BitTorrent swarm on loopback, not the
internet, so they don't need the same opt-in treatment.

The suite is organized as:
- `test_downloader_engine.py` -- the core HTTP engine, against a real local
  HTTP server (`tests/mock_server.py`) that supports range requests and can
  simulate dropped connections, so resume/retry logic is exercised for real
  rather than mocked away.
- `test_torrent_engine.py` -- the torrent engine, against a real local
  BitTorrent swarm (`tests/torrent_swarm.py`): a seed session with real data
  and a leeching `TorrentEngine`, connected directly (no tracker/DHT needed)
  the same way the HTTP tests use a real local server instead of a mock.
  Covers adding via `.torrent` file and magnet link, metadata resolution,
  pause/resume, per-file selection, seeding after completion, and removal.
- `test_torrent_panel.py` -- pytest-qt tests driving the real `TorrentPanel`
  widget (add/category-filter/remove/session round-trip), plus the add-torrent
  dialog's validation.
- `test_speed_limiter.py`, `test_scheduler.py`, `test_session.py`,
  `test_format_utils.py`, `test_url_utils.py` -- fast, fully offline unit
  tests for the supporting modules.
- `test_gui_smoke.py` -- pytest-qt tests that drive the real `DownloadPanel`
  widget (add/search/filter/pause/stop/schedule/settings/session round-trip).
- `test_network_smoke.py` -- opt-in tests against httpbin.org.

### Running headlessly

GUI tests need a Qt platform plugin. `offscreen` is the simplest and fastest
option and is what this suite is written for:

```bash
QT_QPA_PLATFORM=offscreen pytest
```

(`xvfb-run pytest` also works if you'd rather render to a virtual display,
but if your Qt install is missing `libxcb-cursor0` you'll need
`offscreen` instead.)

### A note on full-suite runtime

Because the engine tests intentionally exercise real blocking sockets
(`requests`/`urllib3`) inside `QThreadPool` workers, a worker that's
mid-retry against a connection the test has already torn down can't be
interrupted cooperatively -- it just has to finish its (now short, ~3s
worst case) retry backoff. A couple of tests deliberately leave one of
these in flight to test `stop()`/teardown behavior. This doesn't affect
whether tests pass, but left alone it can make the interpreter slow to
exit after the run; `tests/conftest.py` handles this by forcing a clean
exit once pytest has finished reporting results.

If you're running on a heavily loaded/CPU-constrained CI host and see an
occasional timing-related flake on `test_pause_then_resume`, that's a
resource-contention artifact of running many concurrent local HTTP servers
in one process, not a logic bug -- re-running it (or running
`tests/test_downloader_engine.py` on its own) will confirm.

## License

See `LICENSE`.

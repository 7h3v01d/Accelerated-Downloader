"""A minimal, dependency-free HTTP server that supports Range requests,
used so the pytest suite can exercise real socket I/O without touching
the network. Runs in a background thread per test.
"""
from __future__ import annotations

import hashlib
import http.server
import re
import socketserver
import threading
from typing import Dict, Optional


class RangeRequestHandler(http.server.BaseHTTPRequestHandler):
    # Populated by MockDownloadServer before serving.
    files: Dict[str, bytes] = {}
    # download_id -> number of GET/HEAD requests seen, for fault injection
    fail_after_bytes: Dict[str, int] = {}
    unreliable_paths: set = set()
    request_counts: Dict[str, int] = {}
    accept_ranges = True

    def log_message(self, fmt, *args):
        pass  # silence default stderr logging during tests

    def _get_body(self):
        path = self.path.lstrip('/')
        return self.files.get(path)

    def do_HEAD(self):
        body = self._get_body()
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        if self.accept_ranges:
            self.send_header('Accept-Ranges', 'bytes')
        self.send_header('ETag', f'"{hashlib.md5(body).hexdigest()}"')
        self.end_headers()

    def do_GET(self):
        body = self._get_body()
        if body is None:
            self.send_response(404)
            self.end_headers()
            return

        path = self.path.lstrip('/')
        self.request_counts[path] = self.request_counts.get(path, 0) + 1

        total = len(body)
        start, end = 0, total - 1
        range_header = self.headers.get('Range')
        status = 200
        if range_header and self.accept_ranges:
            m = re.match(r'bytes=(\d+)-(\d*)', range_header)
            if m:
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else total - 1
                status = 206

        chunk = body[start:end + 1]

        # Fault injection: simulate a connection that dies partway through.
        limit = self.fail_after_bytes.get(path)
        truncate_at = len(chunk)
        if limit is not None and start < limit:
            truncate_at = min(len(chunk), max(0, limit - start))

        self.send_response(status)
        self.send_header('Content-Length', str(len(chunk)))
        if self.accept_ranges:
            self.send_header('Accept-Ranges', 'bytes')
        self.send_header('ETag', f'"{hashlib.md5(body).hexdigest()}"')
        self.end_headers()
        try:
            self.wfile.write(chunk[:truncate_at])
            if truncate_at < len(chunk):
                self.close_connection = True
        except (BrokenPipeError, ConnectionResetError):
            pass


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class MockDownloadServer:
    """Context-manager-friendly wrapper that starts a RangeRequestHandler
    server on a background thread on an OS-assigned free port."""

    def __init__(self):
        handler_cls = type('BoundHandler', (RangeRequestHandler,), {
            'files': {}, 'fail_after_bytes': {}, 'request_counts': {}, 'accept_ranges': True,
        })
        self.handler_cls = handler_cls
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), handler_cls)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def url_for(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}/{path}"

    def add_file(self, path: str, content: bytes):
        self.handler_cls.files[path] = content

    def set_accept_ranges(self, value: bool):
        self.handler_cls.accept_ranges = value

    def fail_path_after(self, path: str, byte_offset: int):
        """Causes the server to drop the connection once `byte_offset` bytes
        into the file have been sent for the given path."""
        self.handler_cls.fail_after_bytes[path] = byte_offset

    def clear_fault(self, path: str):
        self.handler_cls.fail_after_bytes.pop(path, None)

    def request_count(self, path: str) -> int:
        return self.handler_cls.request_counts.get(path, 0)

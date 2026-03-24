from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

from app.http_server import get_local_ip
from app.screen_capture import ScreenCaptureConfig, ScreenCapturer

BOUNDARY = "frame"


class FrameStore:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._frame: bytes = b""
        self._updated_at = 0.0

    def set(self, frame: bytes) -> None:
        with self._condition:
            self._frame = frame
            self._updated_at = time.time()
            self._condition.notify_all()

    def get(self) -> tuple[bytes, float]:
        with self._condition:
            return self._frame, self._updated_at

    def wait_for_update(self, last_updated_at: float, timeout: float) -> tuple[bytes, float]:
        with self._condition:
            if self._updated_at <= last_updated_at:
                self._condition.wait(timeout=timeout)
            return self._frame, self._updated_at


class MirrorCaptureLoop:
    def __init__(self, capture_func: Callable[[], bytes], fps: int):
        self.capture_func = capture_func
        self.fps = max(1, fps)
        self.frames = FrameStore()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        interval = 1.0 / self.fps
        while not self._stop_event.is_set():
            started = time.perf_counter()
            try:
                frame = self.capture_func()
                self.frames.set(frame)
            except Exception:
                pass
            elapsed = time.perf_counter() - started
            time.sleep(max(0.01, interval - elapsed))

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=1.5)
        self._thread = None


class MirrorRequestHandler(BaseHTTPRequestHandler):
    server_version = "ScreenCastingMirror/0.2"

    def do_GET(self) -> None:
        if self.path == "/" or self.path.startswith("/?"):
            self._handle_index()
            return
        if self.path.startswith("/snapshot.jpg"):
            self._handle_snapshot()
            return
        if self.path.startswith("/stream.mjpg"):
            self._handle_stream()
            return
        if self.path.startswith("/health"):
            self._handle_health()
            return
        self.send_error(404, "Not found")

    def log_message(self, format: str, *args) -> None:
        return

    def _handle_index(self) -> None:
        html = """<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>Screen Casting Mirror</title>
  <style>
    :root { color-scheme: dark; }
    body {
      margin: 0;
      background: radial-gradient(circle at top, #1b2735, #090a0f 70%);
      color: #f5f7fa;
      font-family: "Segoe UI", sans-serif;
      min-height: 100vh;
      display: grid;
      grid-template-rows: auto 1fr auto;
    }
    header, footer {
      padding: 14px 18px;
      background: rgba(255,255,255,0.06);
      backdrop-filter: blur(8px);
    }
    header strong { font-size: 18px; }
    main {
      display: grid;
      place-items: center;
      padding: 16px;
    }
    .frame {
      width: min(96vw, 1800px);
      height: min(82vh, 1000px);
      object-fit: contain;
      border-radius: 14px;
      box-shadow: 0 18px 50px rgba(0,0,0,0.35);
      background: #000;
    }
    .hint { opacity: 0.82; font-size: 13px; }
  </style>
</head>
<body>
  <header>
    <strong>Screen Casting Desktop Mirror</strong>
  </header>
  <main>
    <img class=\"frame\" src=\"/stream.mjpg\" alt=\"Desktop stream\">
  </main>
  <footer>
    <div class=\"hint\">If your TV browser does not support MJPEG, try opening /snapshot.jpg manually.</div>
  </footer>
</body>
</html>"""
        payload = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _handle_snapshot(self) -> None:
        frame, _ = self.server.capture_loop.frames.get()  # type: ignore[attr-defined]
        if not frame:
            self.send_error(503, "Frame unavailable")
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(frame)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(frame)

    def _handle_stream(self) -> None:
        self.send_response(200)
        self.send_header("Age", "0")
        self.send_header("Cache-Control", "no-store, private")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY}")
        self.end_headers()

        last_updated_at = 0.0
        while True:
            frame, updated_at = self.server.capture_loop.frames.wait_for_update(last_updated_at, timeout=2.0)  # type: ignore[attr-defined]
            if not frame:
                continue
            last_updated_at = updated_at
            try:
                self.wfile.write(
                    (
                        f"--{BOUNDARY}\r\n"
                        "Content-Type: image/jpeg\r\n"
                        f"Content-Length: {len(frame)}\r\n\r\n"
                    ).encode("ascii")
                )
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
            except (BrokenPipeError, ConnectionResetError):
                break

    def _handle_health(self) -> None:
        _, updated_at = self.server.capture_loop.frames.get()  # type: ignore[attr-defined]
        payload = json.dumps({"ready": bool(updated_at), "updated_at": updated_at}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class ScreenMirrorServer:
    def __init__(self, capture_func: Optional[Callable[[], bytes]] = None, fps: int = 8):
        self.capture_func = capture_func
        self.fps = fps
        self._capture_loop: Optional[MirrorCaptureLoop] = None
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self, config: Optional[ScreenCaptureConfig] = None) -> str:
        self.stop()
        config = config or ScreenCaptureConfig.from_preset("Standard")
        capture_func = self.capture_func or ScreenCapturer(config).capture_jpeg
        capture_loop = MirrorCaptureLoop(capture_func=capture_func, fps=config.fps)
        capture_loop.start()

        deadline = time.monotonic() + 2.0
        ready = False
        while time.monotonic() < deadline:
            frame, _ = capture_loop.frames.get()
            if frame:
                ready = True
                break
            time.sleep(0.05)
        if not ready:
            capture_loop.stop()
            raise RuntimeError("Failed to capture the desktop. Start the app in an interactive Windows desktop session.")

        server = ThreadingHTTPServer(("0.0.0.0", 0), MirrorRequestHandler)
        server.capture_loop = capture_loop  # type: ignore[attr-defined]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        self._capture_loop = capture_loop
        self._server = server
        self._thread = thread
        return f"http://{get_local_ip()}:{server.server_address[1]}/"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=1)
        if self._capture_loop is not None:
            self._capture_loop.stop()
        self._server = None
        self._thread = None
        self._capture_loop = None

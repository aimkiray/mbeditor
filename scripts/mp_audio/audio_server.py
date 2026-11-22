"""把本地音频「喂」给页面里的 <input type=file>。

为什么需要它：Chrome 的 file input 不能用 JS 赋值，而 bsk 的 upload 动作在这个
编辑器里不稳定（页面持续重绘导致 observation 过期）。绕法是把文件用本机
CORS 服务暴露成 http://127.0.0.1:<port>/<name>，再在页面里 fetch 成 blob、
构造 File、塞进 input.files 并派发 change —— 页面组件照常收到 change。

安全性：只监听 127.0.0.1，随机端口，只服务一个文件，任务结束即关。
"""

from __future__ import annotations

import http.server
import os
import urllib.parse
import socketserver
import threading
from contextlib import contextmanager
from typing import Iterator


class _Handler(http.server.BaseHTTPRequestHandler):
    file_path: str = ""
    file_name: str = ""

    def do_GET(self) -> None:  # noqa: N802 - stdlib 命名
        if urllib.parse.unquote((self.path or "/").split("?")[0]) != f"/{self.file_name}":
            self.send_error(404)
            return
        try:
            size = os.path.getsize(self.file_path)
            handle = open(self.file_path, "rb")
        except OSError:
            self.send_error(404)
            return
        with handle:
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            # 分块搬运：1GB 的音频不能整个读进内存（页面那边还有 Blob 拷贝）
            while True:
                chunk = handle.read(1 << 16)
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.end_headers()

    def log_message(self, fmt: str, *args) -> None:  # 静音
        return


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


@contextmanager
def serve_audio(file_path: str) -> Iterator[str]:
    """起一个只服务 ``file_path`` 的本地服务，yield 出可被页面 fetch 的 URL。"""
    abs_path = os.path.abspath(file_path)
    if not os.path.isfile(abs_path):
        raise FileNotFoundError(abs_path)
    name = os.path.basename(abs_path)

    handler = type("Handler", (_Handler,), {"file_path": abs_path, "file_name": name})
    server = _Server(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/{urllib.parse.quote(name)}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


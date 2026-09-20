#!/usr/bin/env python3
"""
Local Fetcher Server - shares a folder over your home network for the Roku channel.

  python serve.py                 share the "files" folder next to this script
  python serve.py D:\\Movies      share any folder
  python serve.py . -p 9000       pick a different port

Uses only the Python standard library (3.7+). Supports HTTP Range requests, which
the Roku video player needs for seeking and for many mp4/mkv files.
"""
import argparse
import hashlib
import html
import mimetypes
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit

CHUNK = 256 * 1024
HIDDEN_NAMES = {"thumbs.db", "desktop.ini"}
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
_convert_locks = {}

EXTRA_TYPES = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
    ".mkv": "video/x-matroska", ".m3u8": "application/vnd.apple.mpegurl",
    ".ts": "video/mp2t", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
    ".aac": "audio/aac", ".flac": "audio/flac", ".wav": "audio/wav", ".ogg": "audio/ogg",
    ".oga": "audio/ogg", ".opus": "audio/opus", ".aiff": "audio/aiff", ".aif": "audio/aiff",
    ".wma": "audio/x-ms-wma", ".webm": "video/webm", ".mpd": "application/dash+xml",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".bmp": "image/bmp",
    ".json": "application/json",
    ".txt": "text/plain", ".md": "text/plain", ".log": "text/plain", ".csv": "text/plain",
    ".srt": "text/plain", ".nfo": "text/plain", ".ini": "text/plain", ".conf": "text/plain",
    ".yml": "text/plain", ".yaml": "text/plain", ".xml": "text/plain",
}


def lan_ip():
    """Best guess at this PC's address on the local network."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packets are actually sent
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def content_type(path):
    ext = os.path.splitext(path)[1].lower()
    ctype = EXTRA_TYPES.get(ext) or mimetypes.guess_type(path)[0] or "application/octet-stream"
    if ctype.startswith("text/") or ctype in ("application/json", "application/xml"):
        ctype += "; charset=utf-8"
    return ctype


def parse_range(header, size):
    """Returns (start, end) inclusive, None if the header should be ignored, or "invalid"."""
    if not header.lower().startswith("bytes="):
        return None
    spec = header[6:].split(",")[0].strip()
    if "-" not in spec:
        return None
    a, b = spec.split("-", 1)
    try:
        if a == "":                      # suffix range: last N bytes
            n = int(b)
            if n <= 0:
                return "invalid"
            start, end = max(size - n, 0), size - 1
        else:
            start = int(a)
            end = int(b) if b else size - 1
    except ValueError:
        return None
    end = min(end, size - 1)
    if start >= size or start > end:
        return "invalid"
    return start, end


def transcode_to_mp3(src):
    """Convert an audio file to MP3 with ffmpeg (cached). Returns the MP3 path, or None."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    st = os.stat(src)
    key = hashlib.sha1(("%s|%d|%d" % (src, st.st_mtime_ns, st.st_size)).encode("utf-8")).hexdigest()
    out = os.path.join(CACHE_DIR, key + ".mp3")
    if os.path.exists(out):
        return out
    lock = _convert_locks.setdefault(key, threading.Lock())
    with lock:                                   # the Roku may ask twice; convert only once
        if os.path.exists(out):
            return out
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = out + ".part"
        print("          converting to MP3: %s" % os.path.basename(src))
        cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", src, "-vn", "-map_metadata", "0",
               "-c:a", "libmp3lame", "-q:a", "2", "-f", "mp3", tmp]
        result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if result.returncode != 0 or not os.path.exists(tmp):
            print("          ffmpeg failed: %s" % result.stderr.decode("utf-8", "replace").strip()[-300:])
            if os.path.exists(tmp):
                os.remove(tmp)
            return None
        os.replace(tmp, out)
        return out


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "LocalFetcherServer/1.0"
    root = "."

    def do_GET(self):
        self.handle_request(True)

    def do_HEAD(self):
        self.handle_request(False)

    def log_message(self, fmt, *args):
        sys.stdout.write("%s  %-15s %s\n" % (time.strftime("%H:%M:%S"), self.address_string(), fmt % args))
        sys.stdout.flush()

    def log_transfer(self, full, start, end, sent, length, seconds, note):
        seconds = max(seconds, 0.001)
        sys.stdout.write("%s  %-15s   sent %s of %s in %.1fs (%s/s)  [%s, bytes %d-%d]  %s\n" % (
            time.strftime("%H:%M:%S"), self.address_string(), human_size(sent), human_size(length),
            seconds, human_size(sent / seconds), os.path.basename(full), start, end, note))
        sys.stdout.flush()

    # ---- path handling (stays inside the shared folder) ----
    def resolve(self):
        path = unquote(urlsplit(self.path).path)
        parts = [p for p in path.split("/") if p not in ("", ".")]
        for p in parts:
            if p.startswith(".") or "\\" in p or "\x00" in p:
                return None
        full = os.path.realpath(os.path.join(self.root, *parts))
        try:
            common = os.path.commonpath([self.root, full])
        except ValueError:
            return None
        if os.path.normcase(common) != os.path.normcase(self.root):
            return None
        return full

    def handle_request(self, send_body):
        full = self.resolve()
        if full is None:
            return self.send_text(403, "Forbidden", send_body)
        if os.path.isdir(full):
            url_path = urlsplit(self.path).path
            if not url_path.endswith("/"):
                self.send_response(301)
                self.send_header("Location", url_path + "/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            return self.send_listing(full, url_path, send_body)
        if os.path.isfile(full):
            query = parse_qs(urlsplit(self.path).query)
            if query.get("as", [""])[0].lower() == "mp3":      # ?as=mp3 -> convert audio for the Roku
                converted = transcode_to_mp3(full)
                if converted is None:
                    return self.send_text(501, "Can't convert: install ffmpeg on the PC.", send_body)
                return self.send_file(converted, send_body, "audio/mpeg")
            return self.send_file(full, send_body)
        return self.send_text(404, "Not found", send_body)

    # ---- responses ----
    def send_text(self, status, message, send_body=True):
        body = message.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def send_listing(self, full, url_path, send_body):
        try:
            names = os.listdir(full)
        except OSError:
            return self.send_text(403, "Can't read this folder", send_body)

        entries = []
        for n in names:
            if n.startswith(".") or n.lower() in HIDDEN_NAMES:
                continue
            p = os.path.join(full, n)
            entries.append((not os.path.isdir(p), n.lower(), n, p))
        entries.sort()

        rows = []
        if url_path != "/":
            rows.append('<tr><td><a href="../">../</a></td><td></td></tr>')
        for is_file, _, n, p in entries:
            href = quote(n) + ("" if is_file else "/")
            label = html.escape(n + ("" if is_file else "/"))
            size = ""
            if is_file:
                try:
                    size = human_size(os.path.getsize(p))
                except OSError:
                    pass
            rows.append('<tr><td><a href="%s">%s</a></td><td>%s</td></tr>' % (href, label, size))

        title = html.escape(unquote(url_path))
        page = (
            '<!DOCTYPE html><html><head><meta charset="utf-8"><title>Index of %s</title>'
            '<style>body{font-family:sans-serif;margin:2em}td{padding:2px 24px 2px 0}</style></head>'
            '<body><h2>Index of %s</h2><table>%s</table></body></html>'
        ) % (title, title, "\n".join(rows))
        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def send_file(self, full, send_body, ctype=None):
        try:
            f = open(full, "rb")
        except OSError:
            return self.send_text(403, "Can't read this file", send_body)
        with f:
            size = os.fstat(f.fileno()).st_size
            start, end, status = 0, size - 1, 200

            rng = self.headers.get("Range")
            if rng:
                parsed = parse_range(rng, size)
                if parsed == "invalid":
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % size)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if parsed:
                    start, end = parsed
                    status = 206

            length = end - start + 1
            self.send_response(status)
            self.send_header("Content-Type", ctype or content_type(full))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if status == 206:
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
            self.send_header("Last-Modified", self.date_time_string(os.stat(full).st_mtime))
            self.end_headers()
            if not send_body:
                return

            f.seek(start)
            remaining = length
            sent = 0
            note = ""
            began = last_report = time.time()
            try:
                while remaining > 0:
                    chunk = f.read(min(CHUNK, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    sent += len(chunk)
                    remaining -= len(chunk)
                    if time.time() - last_report >= 5 and remaining > 0:
                        last_report = time.time()
                        self.log_transfer(full, start, end, sent, length, time.time() - began, "still sending...")
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                self.close_connection = True   # player hung up (normal when seeking/stopping)
                note = "<- the Roku closed the connection"
            except OSError as exc:
                self.close_connection = True
                note = "<- network error: %s" % exc
            self.log_transfer(full, start, end, sent, length, time.time() - began, note)


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="Share a folder with the Local Fetcher Roku channel.")
    ap.add_argument("folder", nargs="?", default=None,
                    help='folder to share (default: a "files" folder next to this script)')
    ap.add_argument("-p", "--port", type=int, default=8000, help="port (default 8000)")
    ap.add_argument("-b", "--bind", default="0.0.0.0", help="address to listen on (default: all)")
    args = ap.parse_args()

    folder = args.folder or os.path.join(here, "files")
    if args.folder is None:
        os.makedirs(folder, exist_ok=True)
    root = os.path.realpath(folder)
    if not os.path.isdir(root):
        sys.exit("Not a folder: %s" % root)
    Handler.root = root

    try:
        server = ThreadingHTTPServer((args.bind, args.port), Handler)
    except OSError as e:
        sys.exit("Couldn't start on port %d: %s\n(is another copy already running? try -p 9000)" % (args.port, e))
    server.daemon_threads = True

    ip = lan_ip()
    print("=" * 60)
    print(" Local Fetcher Server")
    print(" Sharing : %s" % root)
    print(" On your Roku, enter this address:  %s:%d" % (ip, args.port))
    print(" (or open http://%s:%d/ in a browser to check it)" % (ip, args.port))
    if shutil.which("ffmpeg"):
        print(" ffmpeg found: audio the Roku can't play natively is converted to MP3 automatically.")
    else:
        print(" ffmpeg not found (optional): install it if WAV/OGG/etc. won't play on the Roku.")
    print(" Press Ctrl+C to stop.")
    print("=" * 60)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()

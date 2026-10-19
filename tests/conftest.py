from __future__ import annotations
import os
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit
import pytest
from videograb.config import Settings


@pytest.fixture
def fixture_site(tmp_path):
    data = tmp_path / "site"
    data.mkdir()
    video = data / "media.mp4"
    if shutil.which("ffmpeg"):
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=10",
                        "-t", "2", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(video)], check=True, capture_output=True)
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(video), "-c", "copy", "-f", "hls", "-hls_time", "1", "-hls_list_size", "0", "-hls_segment_filename", str(data / "chunk%02d.ts"), str(data / "stream.m3u8")], check=True, capture_output=True)
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(video), "-c", "copy", "-f", "dash", "-seg_duration", "1", str(data / "manifest.mpd")], check=True, capture_output=True)
    else:
        video.write_bytes(b"demo" * 4096)
        (data / "stream.m3u8").write_text("#EXTM3U\n#EXTINF:2,\nmedia.mp4\n")
        (data / "manifest.mpd").write_text('<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"><Period><AdaptationSet><Representation id="v1" width="320" height="180" bandwidth="90000"/></AdaptationSet></Period></MPD>')
    (data / "master.m3u8").write_text('#EXTM3U\n#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",LANGUAGE="en",URI="captions.vtt"\n#EXT-X-STREAM-INF:BANDWIDTH=500000,RESOLUTION=160x90\nstream.m3u8\n')
    (data / "captions.vtt").write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHello")
    (data / "page.html").write_text('''<html><head><title>Fixture player</title><meta property="og:video" content="/media.mp4"></head>
    <body><video controls><source src="/media.mp4" type="video/mp4"></video>
    <video src="/other.mp4"></video>
    <script>const player = {file:'/master.m3u8'}</script>
    <script src="/player.js"></script><iframe src="/frame.html"></iframe></body></html>''')
    (data / "frame.html").write_text('<html><video src="/iframe.mp4"></video></html>')
    (data / "player.js").write_text('const cfg={source:"/manifest.mpd"};')
    (data / "other.mp4").write_bytes(video.read_bytes())
    (data / "large.mp4").write_bytes(video.read_bytes() + b"\0" * (3 * 1024 * 1024))
    (data / "iframe.mp4").write_bytes(video.read_bytes())
    (data / "dynamic.html").write_text('<html><title>Dynamic</title><video id="player"></video><script>setTimeout(()=>document.getElementById("player").src="/media.mp4",250)</script></html>')

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlsplit(self.path)
            if parsed.path == '/redirect':
                self.send_response(302);self.send_header('Location', '/media.mp4');self.end_headers();return
            path = (data / unquote(parsed.path).lstrip('/')).resolve()
            if not path.is_relative_to(data) or not path.is_file():
                self.send_error(404);return
            raw = path.read_bytes()
            extension = path.suffix
            mime = {'.html':'text/html','.js':'application/javascript','.mp4':'video/mp4','.m3u8':'application/vnd.apple.mpegurl','.mpd':'application/dash+xml','.ts':'video/mp2t','.vtt':'text/vtt','.m4s':'video/iso.segment'}.get(extension,'application/octet-stream')
            range_header = self.headers.get('Range')
            if range_header and range_header.startswith('bytes='):
                offset=int(range_header.removeprefix('bytes=').split('-')[0]);self.send_response(206)
                self.send_header('Content-Range',f'bytes {offset}-{len(raw)-1}/{len(raw)}');raw=raw[offset:]
            else:self.send_response(200)
            self.send_header('Content-Type',mime)
            self.send_header('Accept-Ranges','bytes')
            self.send_header('Content-Length',str(len(raw)))
            self.end_headers()
            for i in range(0,len(raw),8192):
                try:self.wfile.write(raw[i:i+8192])
                except (BrokenPipeError,ConnectionResetError):return
        def log_message(self,*args):pass

    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown();server.server_close();thread.join(timeout=2)


@pytest.fixture
def settings(tmp_path):
    s=Settings(data_dir=tmp_path/'state',output_dir=tmp_path/'out',allow_private_networks=True)
    s.prepare()
    return s

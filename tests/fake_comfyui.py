"""테스트용 가짜 ComfyUI 서버 + 가짜 상용 API(Runway) HTTP.

실제 GPU·모델·API 키 없이 어댑터의 업로드·제출·대기·다운로드 흐름과 워크플로 값 주입을 검증한다.
- FakeComfyUI: /system_stats, /upload/image, /prompt, /history/<id>, /view, /free
- FakeRunwayHTTP: requests 대용 객체 (post/get) — 실제 외부 호출은 하지 않는다
"""
import base64
import json
import subprocess
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


def make_sample_mp4(ffmpeg, dest, size="128x96", seconds=1, with_audio=False):
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi", "-i",
           f"testsrc=size={size}:rate=12", "-t", str(seconds)]
    if with_audio:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=440", "-shortest", "-c:a", "aac"]
    cmd += ["-pix_fmt", "yuv420p", str(dest)]
    subprocess.run(cmd, check=True)
    return Path(dest)


# 1x1 투명 PNG (이미지 먼저 모드의 첫 프레임 대용)
_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


class FakeComfyUI:
    def __init__(self, ffmpeg, result_delay=0.3):
        self.result_delay = result_delay
        self.submitted = []          # 제출된 워크플로(dict) 목록
        self.uploads = 0
        self.frees = 0
        self.fail_markers = set()    # 프롬프트에 이 문자열이 있으면 실행 오류로 응답
        self.drop_markers = set()    # 프롬프트에 이 문자열이 있으면 큐·history 어디에도 남지 않음(큐 비움 모사)
        self._jobs = {}              # prompt_id -> (제출 시각, 실패 여부)
        self._dir = Path(tempfile.mkdtemp(prefix="fake_comfy_"))
        self._sample = make_sample_mp4(ffmpeg, self._dir / "sample.mp4")
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, payload, status=200):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = urlparse(self.path).path
                if path == "/system_stats":
                    return self._json({"system": {"comfyui_version": "fake"}})
                if path.startswith("/history/"):
                    pid = path.rsplit("/", 1)[1]
                    job = outer._jobs.get(pid)
                    if job is None or job[3] or time.time() - job[0] < outer.result_delay:
                        return self._json({})
                    if job[1]:
                        return self._json({pid: {"status": {"status_str": "error", "messages": [
                            ["execution_error", {"exception_message": "fake failure"}]]}}})
                    if job[2]:  # 이미지 워크플로(SaveImage) → png 출력
                        return self._json({pid: {"status": {"status_str": "success"}, "outputs": {
                            "10": {"images": [{"filename": f"{pid}.png", "subfolder": "", "type": "output"}]}}}})
                    return self._json({pid: {"status": {"status_str": "success"}, "outputs": {
                        "9": {"videos": [{"filename": f"{pid}.mp4", "subfolder": "", "type": "output"}]}}}})
                if path == "/queue":
                    pending = [[0, pid] for pid, j in outer._jobs.items()
                               if not j[3] and time.time() - j[0] < outer.result_delay]
                    return self._json({"queue_running": [], "queue_pending": pending})
                if path == "/view":
                    is_png = urlparse(self.path).query.count(".png") > 0
                    data = _PNG_1X1 if is_png else outer._sample.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png" if is_png else "video/mp4")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                self._json({"error": "not found"}, 404)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b""
                path = urlparse(self.path).path
                if path == "/upload/image":
                    outer.uploads += 1
                    return self._json({"name": f"upload_{outer.uploads}.png"})
                if path == "/prompt":
                    workflow = json.loads(raw)["prompt"]
                    outer.submitted.append(workflow)
                    text = json.dumps(workflow, ensure_ascii=False)
                    fail = any(m in text for m in outer.fail_markers)
                    pid = uuid.uuid4().hex
                    is_image = any(n.get("class_type") == "SaveImage" for n in workflow.values())
                    dropped = any(m in text for m in outer.drop_markers)
                    outer._jobs[pid] = (time.time(), fail, is_image, dropped)
                    return self._json({"prompt_id": pid})
                if path == "/free":
                    outer.frees += 1
                    return self._json({})
                self._json({"error": "not found"}, 404)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._server.shutdown()
        self._server.server_close()


class _Resp:
    def __init__(self, payload=None, content=b"", status=200):
        self._payload, self.content, self.status_code = payload, content, status
        self.text = json.dumps(payload) if payload is not None else ""

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeRunwayHTTP:
    """상용 API 테스트 더블. 호출 내역(requests)을 기록한다."""

    def __init__(self, sample_mp4):
        self._video = Path(sample_mp4).read_bytes()
        self.requests = []
        self._tasks = 0

    def post(self, url, json=None, headers=None, timeout=None):
        self.requests.append({"url": url, "body": json, "headers": headers})
        self._tasks += 1
        return _Resp({"id": f"task-{self._tasks}"})

    def get(self, url, headers=None, timeout=None):
        if "/tasks/" in url:
            return _Resp({"status": "SUCCEEDED", "output": ["http://fake.invalid/out.mp4"]})
        return _Resp(content=self._video)

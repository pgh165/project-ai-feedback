"""ComfyUI HTTP API 클라이언트 — 업로드·제출·대기·다운로드·free.

[오픈소스] ComfyUI 서버 API (/upload/image, /prompt, /history, /view, /free)
[팀 직접 구현] promo_draft/src/comfy_client.py를 이식하고 tqdm 대신 진행 콜백으로 바꿈.
websocket-client가 있으면 스텝 단위 진행률을 받고, 없으면 /history 폴링만 사용한다.
"""
import json
import time
import uuid
from pathlib import Path

import requests


class ComfyError(RuntimeError):
    pass


class ComfyClient:
    # 제출한 프롬프트가 /history에도 /queue에도 이 시간 동안 안 보이면 사라진 것으로 보고 바로 실패시킨다.
    # (ComfyUI에서 큐를 비우거나 취소하면 history에 남지 않아, 감지하지 않으면 타임아웃까지 워커가 묶인다)
    missing_grace_sec = 10.0

    def __init__(self, url, timeout_sec=900, poll_interval_sec=2.0):
        self.url = url.rstrip("/")
        self.timeout = timeout_sec
        self.poll_interval = poll_interval_sec
        self.client_id = uuid.uuid4().hex

    def system_stats(self, timeout=10):
        r = requests.get(f"{self.url}/system_stats", timeout=timeout)
        r.raise_for_status()
        return r.json()

    def is_alive(self, timeout=3):
        try:
            self.system_stats(timeout=timeout)
            return True
        except (requests.RequestException, ValueError):
            return False

    # 컷이 끝난 직후 ComfyUI가 큰 모델을 정리하느라 몇 분씩 응답하지 않을 때가 있다
    # (2026-10-23: 750KB /view 3분 23초, 다음 장면 /upload/image 300초 초과로 실패) → 시간 초과·연결 오류만 다시 시도
    transfer_attempts = 3

    def _transfer(self, send):
        for attempt in range(1, self.transfer_attempts + 1):
            try:
                return send()
            except (requests.Timeout, requests.ConnectionError):
                if attempt == self.transfer_attempts:
                    raise
                time.sleep(5)

    def upload(self, path):
        """이미지/영상을 ComfyUI input 폴더로 올리고 서버상의 파일명을 반환."""
        path = Path(path)

        def send():
            with open(path, "rb") as f:
                return requests.post(
                    f"{self.url}/upload/image",
                    files={"image": (path.name, f)},
                    data={"type": "input", "overwrite": "true"},
                    timeout=300,
                )
        r = self._transfer(send)
        r.raise_for_status()
        return r.json()["name"]

    def interrupt(self, timeout=5):
        """지금 실행 중인 작업을 중단한다 (대기열은 그대로)."""
        requests.post(f"{self.url}/interrupt", timeout=timeout).raise_for_status()

    # client_id를 붙이면 ComfyUI가 스텝 진행률을 이 클라이언트에만 보내 ComfyUI 창에는 진행이 안 보인다.
    # 빼고 보내면 모든 창에 진행률을 뿌린다 — 대신 완료 알림(executing None)이 오지 않으므로 websocket 대기는 쓰지 않고 /history 폴링만 한다.
    show_in_ui = True

    def submit(self, workflow):
        body = {"prompt": workflow} if self.show_in_ui else {"prompt": workflow, "client_id": self.client_id}
        r = requests.post(f"{self.url}/prompt", json=body, timeout=30)
        if r.status_code != 200:
            raise ComfyError(f"/prompt 거부({r.status_code}): {r.text[:800]}")
        return r.json()["prompt_id"]

    def wait(self, prompt_id, progress_cb=None):
        """완료까지 대기하고 history 항목을 반환. progress_cb(fraction|None, message)."""
        report = progress_cb or (lambda fraction, message: None)
        started = time.time()
        if not self.show_in_ui:
            try:
                self._wait_ws(prompt_id, report)
            except ComfyError:
                raise
            except Exception:  # websocket-client 없음 / 연결 실패 → 폴링으로 대체
                pass

        deadline = started + self.timeout
        missing_since = None
        while time.time() < deadline:
            history = requests.get(f"{self.url}/history/{prompt_id}", timeout=10).json()
            if prompt_id in history:
                entry = history[prompt_id]
                status = entry.get("status", {})
                if status.get("status_str") == "error":
                    msgs = [m for m in status.get("messages", []) if m[0] == "execution_error"]
                    raise ComfyError(
                        "ComfyUI 실행 오류: " + json.dumps(msgs[-1][1] if msgs else status)[:800])
                return entry
            if self._in_queue(prompt_id):
                missing_since = None
            elif missing_since is None:
                missing_since = time.time()
            elif time.time() - missing_since >= self.missing_grace_sec:
                raise ComfyError(
                    "ComfyUI 큐에서 작업이 사라졌습니다 (ComfyUI에서 큐를 비우거나 작업을 취소했을 수 있습니다). 재시도하세요.")
            report(None, f"ComfyUI 생성 중 ({int(time.time() - started)}초 경과)")
            time.sleep(self.poll_interval)
        raise ComfyError(f"시간 초과({self.timeout}s)")

    def _in_queue(self, prompt_id):
        """실행 중이거나 대기 중인가. 조회가 안 되면(서버 일시 오류·미지원) 있는 것으로 본다."""
        try:
            r = requests.get(f"{self.url}/queue", timeout=5)
            if r.status_code != 200:
                return True
            data = r.json()
        except (requests.RequestException, ValueError):
            return True
        for key in ("queue_running", "queue_pending"):
            for item in data.get(key, []):
                if isinstance(item, (list, tuple)) and len(item) > 1 and item[1] == prompt_id:
                    return True
        return False

    def _wait_ws(self, prompt_id, report):
        import websocket  # [오픈소스] websocket-client — 없으면 ImportError → 폴링

        ws = websocket.create_connection(
            self.url.replace("http", "ws", 1) + f"/ws?clientId={self.client_id}",
            timeout=self.timeout)
        try:
            while True:
                msg = ws.recv()
                if isinstance(msg, bytes):
                    continue
                m = json.loads(msg)
                kind, data = m.get("type"), m.get("data", {})
                if data.get("prompt_id") not in (None, prompt_id):
                    continue
                if kind == "progress" and data.get("max"):
                    report(data["value"] / data["max"], f"{data['value']}/{data['max']} 스텝")
                elif kind == "execution_error":
                    raise ComfyError(f"ComfyUI 실행 오류: {json.dumps(data)[:800]}")
                elif kind == "executing" and data.get("node") is None:
                    return
        finally:
            ws.close()

    @staticmethod
    def output_files(entry):
        """history 출력에서 파일 정보 목록 추출. 영상 우선."""
        files = []
        for out in entry.get("outputs", {}).values():
            for key in ("videos", "gifs", "images"):
                for f in out.get(key, []):
                    if isinstance(f, dict) and "filename" in f:
                        files.append(f)
        video = [f for f in files
                 if Path(f["filename"]).suffix.lower() in (".mp4", ".webm", ".mov", ".mkv", ".gif")]
        return video or files

    def download(self, file_info, dest):
        r = self._transfer(lambda: requests.get(
            f"{self.url}/view",
            params={"filename": file_info["filename"], "subfolder": file_info.get("subfolder", ""),
                    "type": file_info.get("type", "output")},
            timeout=300))
        r.raise_for_status()
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(r.content)
        return dest

    def free(self):
        try:
            requests.post(f"{self.url}/free",
                          json={"unload_models": True, "free_memory": True}, timeout=30)
        except requests.RequestException:
            pass

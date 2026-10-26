"""ComfyUI HTTP/WebSocket API 클라이언트: 업로드, 제출, 대기, 다운로드, free."""
import json
import time
import uuid
from pathlib import Path

import requests
from tqdm import tqdm


class ComfyError(RuntimeError):
    pass


class ComfyClient:
    def __init__(self, url: str, timeout_sec: int = 1800):
        self.url = url.rstrip("/")
        self.timeout = timeout_sec
        self.client_id = uuid.uuid4().hex

    def system_stats(self) -> dict:
        r = requests.get(f"{self.url}/system_stats", timeout=10)
        r.raise_for_status()
        return r.json()

    def upload(self, path: Path) -> str:
        """이미지/영상을 ComfyUI input 폴더로 올리고 서버상의 파일명을 반환."""
        with open(path, "rb") as f:
            r = requests.post(
                f"{self.url}/upload/image",
                files={"image": (path.name, f)},
                data={"type": "input", "overwrite": "true"},
                timeout=300,
            )
        r.raise_for_status()
        return r.json()["name"]

    def submit(self, workflow: dict) -> str:
        r = requests.post(
            f"{self.url}/prompt",
            json={"prompt": workflow, "client_id": self.client_id},
            timeout=30,
        )
        if r.status_code != 200:
            raise ComfyError(f"/prompt 거부({r.status_code}): {r.text[:800]}")
        return r.json()["prompt_id"]

    def wait(self, prompt_id: str, label: str = "") -> dict:
        """완료까지 대기하며 진행률 표시. history 항목을 반환."""
        try:
            self._wait_ws(prompt_id, label)
        except ComfyError:
            raise
        except Exception:  # 웹소켓 실패 시 폴링으로 대체
            pass
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            h = requests.get(f"{self.url}/history/{prompt_id}", timeout=10).json()
            if prompt_id in h:
                entry = h[prompt_id]
                st = entry.get("status", {})
                if st.get("status_str") == "error":
                    msgs = [m for m in st.get("messages", []) if m[0] == "execution_error"]
                    raise ComfyError(f"ComfyUI 실행 오류: {json.dumps(msgs[-1][1] if msgs else st)[:800]}")
                return entry
            time.sleep(2)
        raise ComfyError(f"시간 초과({self.timeout}s)")

    def _wait_ws(self, prompt_id: str, label: str):
        import websocket

        ws = websocket.create_connection(
            self.url.replace("http", "ws", 1) + f"/ws?clientId={self.client_id}", timeout=self.timeout
        )
        bar = None
        try:
            while True:
                msg = ws.recv()
                if isinstance(msg, bytes):
                    continue
                m = json.loads(msg)
                t, d = m.get("type"), m.get("data", {})
                if d.get("prompt_id") not in (None, prompt_id):
                    continue
                if t == "progress":
                    if bar is None or bar.total != d["max"]:
                        if bar:
                            bar.close()
                        bar = tqdm(total=d["max"], desc=label or "생성", leave=False)
                    bar.n = d["value"]
                    bar.refresh()
                elif t == "execution_error":
                    raise ComfyError(f"ComfyUI 실행 오류: {json.dumps(d)[:800]}")
                elif t == "executing" and d.get("node") is None:
                    return
        finally:
            if bar:
                bar.close()
            ws.close()

    def output_files(self, entry: dict) -> list[dict]:
        """history 출력에서 파일 정보(filename/subfolder/type) 목록 추출. 영상 우선."""
        files = []
        for out in entry.get("outputs", {}).values():
            for key in ("videos", "gifs", "images"):
                for f in out.get(key, []):
                    if isinstance(f, dict) and "filename" in f:
                        files.append(f)
        vid = [f for f in files if Path(f["filename"]).suffix.lower() in (".mp4", ".webm", ".mov", ".mkv", ".gif")]
        return vid or files

    def download(self, f: dict, dest: Path) -> Path:
        r = requests.get(
            f"{self.url}/view",
            params={"filename": f["filename"], "subfolder": f.get("subfolder", ""), "type": f.get("type", "output")},
            timeout=300,
        )
        r.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(r.content)
        return dest

    def free(self):
        try:
            requests.post(f"{self.url}/free", json={"unload_models": True, "free_memory": True}, timeout=30)
        except requests.RequestException:
            pass

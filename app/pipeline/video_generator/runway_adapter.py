"""[팀 직접 구현] 상용 영상 API 어댑터 (Runway) — 품질 부족 장면 재생성 전용.

[오픈소스] requests. API 형식: POST {base}/text_to_video | /image_to_video → task id →
GET {base}/tasks/{id} 폴링 (status SUCCEEDED/FAILED/CANCELLED) → output[0] URL 다운로드.
엔드포인트·모델·버전 헤더는 전부 설정(RUNWAY_*)이라 공급자 변경에 코드를 고치지 않는다.
비용이 드는 호출이므로 orchestrator가 사용자 확인·예산 검사를 통과한 장면만 보낸다.
"""
import base64
import mimetypes
import time
from pathlib import Path

import requests

from .base import BaseVideoGeneratorAdapter, ShotResult
from ..storyboard_model import final_prompt


class RunwayError(RuntimeError):
    pass


class RunwayAdapter(BaseVideoGeneratorAdapter):
    name = "runway"
    is_slow = True
    supports_shots = True

    def __init__(self, config, http=None):
        self.config = config
        self.http = http or requests  # 테스트에서 가짜 HTTP 객체를 주입한다

    def is_available(self):
        return bool(self.config.get("RUNWAY_API_KEY"))

    def unavailable_reason(self):
        return "" if self.is_available() else "RUNWAY_API_KEY가 설정되지 않았습니다."

    def estimate_cost(self, duration_sec):
        return round(float(duration_sec) * self.config["COMMERCIAL_COST_PER_SEC_USD"], 4)

    def generate(self, prompt_text, output_path, progress=None):
        raise NotImplementedError("runway 어댑터는 장면 단위(generate_shot)로만 동작합니다.")

    # ── 내부 ──
    def _headers(self):
        return {
            "Authorization": f"Bearer {self.config['RUNWAY_API_KEY']}",
            "X-Runway-Version": self.config["RUNWAY_API_VERSION"],
            "Content-Type": "application/json",
        }

    def _ratio(self, storyboard):
        """스토리보드 화면 비율 → API ratio. 세로(기본)·가로·정사각을 설정으로 지정한다."""
        d = storyboard["defaults"]
        if d["width"] == d["height"]:
            return self.config["RUNWAY_RATIO_SQUARE"]
        if d["width"] > d["height"]:
            return self.config["RUNWAY_RATIO_LANDSCAPE"]
        return self.config["RUNWAY_RATIO"]

    @staticmethod
    def _data_uri(path):
        mime = mimetypes.guess_type(str(path))[0] or "image/png"
        return f"data:{mime};base64," + base64.b64encode(Path(path).read_bytes()).decode()

    def generate_shot(self, storyboard, shot, out_path, progress=None):
        if not self.is_available():
            raise RunwayError(self.unavailable_reason())
        report = progress or (lambda fraction, message: None)
        base = self.config["RUNWAY_API_BASE"].rstrip("/")
        duration = float(shot["duration"])
        body = {
            "promptText": final_prompt(storyboard, shot, self.config.get("STORYBOARD_APPEND_NOTES", True)),
            "ratio": self._ratio(storyboard),
            "duration": int(round(duration)),
        }
        if shot.get("image"):
            endpoint = "image_to_video"
            body["model"] = self.config["RUNWAY_MODEL"]
            body["promptImage"] = self._data_uri(shot["image"])
        else:
            endpoint = "text_to_video"
            body["model"] = self.config["RUNWAY_T2V_MODEL"]

        started = time.time()
        report(None, "상용 API에 작업 제출")
        resp = self.http.post(f"{base}/{endpoint}", json=body, headers=self._headers(), timeout=60)
        if resp.status_code >= 400:
            raise RunwayError(f"상용 API 제출 거부({resp.status_code}): {resp.text[:400]}")
        task_id = resp.json()["id"]

        deadline = started + self.config["RUNWAY_TIMEOUT_SEC"]
        while True:
            task = self.http.get(f"{base}/tasks/{task_id}", headers=self._headers(), timeout=30).json()
            status = task.get("status")
            if status == "SUCCEEDED":
                break
            if status in ("FAILED", "CANCELLED"):
                raise RunwayError(f"상용 API 작업 실패({status}): {task.get('failure', '')}")
            if time.time() > deadline:
                raise RunwayError("상용 API 시간 초과")
            report(task.get("progress"), f"상용 API 생성 중 ({status})")
            time.sleep(self.config["RUNWAY_POLL_INTERVAL_SEC"])

        output = (task.get("output") or [None])[0]
        if not output:
            raise RunwayError("상용 API 출력 URL 없음")
        data = self.http.get(output, timeout=300)
        data.raise_for_status() if hasattr(data, "raise_for_status") else None
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(data.content)
        return ShotResult(path=str(out_path), provider="runway",
                          elapsed_sec=round(time.time() - started, 1),
                          cost_usd=self.estimate_cost(duration))

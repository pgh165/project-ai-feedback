"""[팀 직접 구현] 로컬 ComfyUI(LTX-2.5) 장면 단위 영상 생성 어댑터.

[오픈소스] ComfyUI 서버 + LTX-2.5 워크플로. 워크플로 JSON/mapping.yaml은
`COMFYUI_WORKFLOWS_DIR`(promo_draft 공유)에서 읽기만 하고 수정하지 않는다.
"""
import subprocess
import threading
import time
from pathlib import Path

from ..storyboard_model import resolve_params
from .base import BaseVideoGeneratorAdapter, ShotResult
from .comfyui import workflow
from .comfyui.comfy_client import ComfyClient, ComfyError
from .mock_adapter import build_storyboard

_gpu_lock = threading.Lock()  # GPU 작업은 동시에 하나만 (16GB VRAM)


class _VramSampler:
    """nvidia-smi를 1초마다 샘플링해 최대 VRAM 사용량(MiB)을 기록 (없으면 0)."""

    def __init__(self):
        self.peak = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self):
        while not self._stop.is_set():
            try:
                out = subprocess.run(
                    ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=5).stdout.strip().splitlines()
                self.peak = max(self.peak, int(out[0]))
            except Exception:
                pass
            self._stop.wait(1.0)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=3)


class ComfyUIAdapter(BaseVideoGeneratorAdapter):
    name = "comfyui"
    is_slow = True
    supports_shots = True

    def __init__(self, config):
        self.config = config
        self.client = ComfyClient(
            config["COMFYUI_URL"], timeout_sec=config["COMFYUI_SHOT_TIMEOUT_SEC"],
            poll_interval_sec=config["COMFYUI_POLL_INTERVAL_SEC"])
        self.provider = f"comfyui-{config['LTX_MODEL_VARIANT']}"
        self._reason = ""

    def is_available(self):
        if not self.client.is_alive(timeout=self.config["COMFYUI_AVAILABLE_TIMEOUT_SEC"]):
            self._reason = f"ComfyUI 서버 응답 없음 ({self.config['COMFYUI_URL']})"
            return False
        ok, reason = workflow.workflow_ready(self.config)
        if not ok:
            self._reason = reason
            return False
        return True

    def unavailable_reason(self):
        return self._reason

    def generate(self, prompt_text, output_path, progress=None):
        raise NotImplementedError("comfyui 어댑터는 장면 단위(generate_shot)로만 동작합니다.")

    def _first_frame(self, params, out_path, report, free_after=True):
        """'이미지 먼저': 장면 프롬프트로 Z-Image 이미지를 만들어 영상 옆(v<k>.png)에 저장한다."""
        template, mapping = workflow.load_template(self.config, self.config["COMFYUI_WORKFLOW_T2I"])
        # 첫 프레임은 동작이 시작되기 전 모습 (의견으로 고쳐 쓸 때 따로 만든 묘사가 있으면)
        wf = workflow.fill_image(template, mapping, {**params, "prompt": params.get("image_prompt") or params["prompt"]})
        prompt_id = self.client.submit(wf)
        entry = self.client.wait(prompt_id, progress_cb=report)
        files = self.client.output_files(entry)
        if not files:
            raise ComfyError("첫 프레임 이미지 출력 없음")
        image_path = Path(out_path).with_suffix(".png")
        self.client.download(files[0], image_path)
        if free_after and self.config.get("COMFYUI_FREE_BETWEEN_SHOTS"):
            self.client.free()  # 이미지 모델을 내려야 영상 모델이 VRAM에 올라간다
        return image_path

    def generate_image(self, storyboard, shot, out_path, progress=None):
        """단계별 확인용: 장면의 첫 프레임 이미지만 만든다 (영상은 만들지 않는다).
        모델은 이미지 단계가 끝날 때 after_shot()으로 한 번만 내린다 — 장면마다 내리면 다시 올리느라 느려진다."""
        report = progress or (lambda fraction, message: None)
        params = resolve_params(storyboard, shot, self.config)
        started = time.time()
        with _gpu_lock, _VramSampler() as vram:
            image_path = self._first_frame(params, out_path, report, free_after=False)
        return ShotResult(path="", provider=self.provider, image_path=str(image_path),
                          elapsed_sec=round(time.time() - started, 1), peak_vram_mb=float(vram.peak))

    def generate_shot(self, storyboard, shot, out_path, progress=None):
        report = progress or (lambda fraction, message: None)
        params = resolve_params(storyboard, shot, self.config)
        template, mapping = workflow.load_template(self.config, params["workflow"])
        enhance = bool(self.config.get("COMFYUI_PROMPT_ENHANCE"))

        started = time.time()
        image_path = ""
        video_report = report
        with _gpu_lock, _VramSampler() as vram:
            if params["image_first"]:  # 이미지 단계는 전체 진행률의 앞 25%
                image_path = str(self._first_frame(
                    params, out_path,
                    lambda fraction, message: report(
                        None if fraction is None else fraction * 0.25, f"첫 프레임 이미지 · {message}")))
                params = {**params, "image": image_path}

                def video_report(fraction, message):
                    report(None if fraction is None else 0.25 + fraction * 0.75, message)

            uploaded = uploaded_end = None
            if params["image"]:
                image = Path(params["image"])
                if not image.exists():
                    raise FileNotFoundError(f"입력 이미지 없음: {image}")
                uploaded = self.client.upload(image)
            if params.get("end_image"):  # 처음·끝 프레임: 끝 화면도 올린다 (이 워크플로엔 프롬프트 강화 노드가 없다)
                end = Path(params["end_image"])
                if not end.exists():
                    raise FileNotFoundError(f"끝 프레임 이미지 없음: {end}")
                uploaded_end, enhance = self.client.upload(end), False
            wf = workflow.fill(template, mapping, params, uploaded, enhance=enhance, uploaded_end_image=uploaded_end)

            prompt_id = self.client.submit(wf)
            entry = self.client.wait(prompt_id, progress_cb=video_report)
            files = self.client.output_files(entry)
            if not files:
                raise ComfyError("출력 파일 없음")
            self.client.download(files[0], out_path)
        return ShotResult(path=str(out_path), provider=self.provider,
                          elapsed_sec=round(time.time() - started, 1),
                          peak_vram_mb=float(vram.peak),
                          enhanced_prompt=workflow.enhanced_prompt(wf, entry) if enhance else "",
                          image_path=image_path)

    def after_shot(self):
        if self.config.get("COMFYUI_FREE_BETWEEN_SHOTS"):
            self.client.free()


__all__ = ["ComfyUIAdapter", "build_storyboard"]

"""[팀 직접 구현] 로컬 ComfyUI(LTX-2.5) 장면 단위 영상 생성 어댑터.

[오픈소스] ComfyUI 서버 + LTX-2.5 워크플로. 워크플로 JSON/mapping.yaml은
`COMFYUI_WORKFLOWS_DIR`(promo_draft 공유)에서 읽기만 하고 수정하지 않는다.
"""
import json
import subprocess
import threading
import time
from pathlib import Path

import requests

from ..storyboard_model import resolve_params
from .base import BaseVideoGeneratorAdapter, ShotResult
from .comfyui import workflow
from .comfyui.comfy_client import ComfyClient, ComfyError
from .mock_adapter import build_storyboard

_gpu_lock = threading.Lock()  # GPU 작업은 동시에 하나만 (16GB VRAM)
_MOTION_NODES =("LTXICLoRALoaderModelOnly", "LTXVDrawTracks", "LTXAddVideoICLoRAGuide")  # ComfyUI-LTXVideo가 주는 노드


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

    def motion_problem(self, workflow_name=None):
        """IC-LoRA 워크플로(움직임 경로·참조 영상)를 쓸 수 없는 이유 (ComfyUI-LTXVideo 노드·LoRA·워크플로). 쓸 수 있으면 ''."""
        try:
            template, _mapping = workflow.load_template(
                self.config, workflow_name or self.config["COMFYUI_WORKFLOW_MOTION"])
            for node in template.values():
                if node["class_type"] not in _MOTION_NODES:
                    continue
                info = self.client.node_info(node["class_type"])
                if not info:
                    return f"ComfyUI에 {node['class_type']} 노드가 없습니다 (ComfyUI-LTXVideo 설치 필요)."
                lora = node["inputs"].get("lora_name")
                if lora and lora not in json.dumps(info.get("input", {})):
                    return f"ComfyUI models/loras에 {lora}가 없습니다."
        except (FileNotFoundError, KeyError) as exc:
            return str(exc)
        except requests.RequestException as exc:
            return f"ComfyUI에 물어보지 못했습니다: {exc}"
        return ""

    def generate(self, prompt_text, output_path, progress=None):
        raise NotImplementedError("comfyui 어댑터는 장면 단위(generate_shot)로만 동작합니다.")

    def face_problem(self):
        """얼굴 사진으로 인물 그리기(Qwen-Image-Edit)를 쓸 수 없는 이유. 쓸 수 있으면 ''. 한 번 확인하면 기억한다."""
        if getattr(self, "_face_problem", None) is None:
            name = self.config.get("COMFYUI_WORKFLOW_FACE", "qwen_face_t2i")
            try:
                template, _mapping = workflow.load_template(self.config, name)
                unet = next(n["inputs"]["unet_name"] for n in template.values() if n["class_type"] == "UNETLoader")
                info = self.client.node_info("UNETLoader")
                self._face_problem = "" if unet in json.dumps(info or {}) else f"ComfyUI에 {unet}가 없습니다."
            except (FileNotFoundError, KeyError, StopIteration) as exc:
                self._face_problem = f"얼굴 워크플로를 읽지 못했습니다: {exc}"
            except requests.RequestException as exc:
                return f"ComfyUI에 물어보지 못했습니다: {exc}"
        return self._face_problem

    def _first_frame(self, params, out_path, report, free_after=True):
        """'이미지 먼저': 장면 프롬프트로 Z-Image 이미지를 만들어 영상 옆(v<k>.png)에 저장한다.
        params["face"](얼굴 사진)가 있으면 Qwen-Image-Edit으로 사진 속 사람이 나오는 첫 프레임을 그린다."""
        prompt = params.get("image_prompt") or params["prompt"]  # 첫 프레임은 동작이 시작되기 전 모습
        refs = params.get("refs") or ([{"path": params["face"], "label": "person"}] if params.get("face") else [])
        if refs:  # 참조 사진(얼굴·물건·장소, 최대 3장)을 보고 그린다 — Qwen-Image-Edit-2511
            from ..ref_photos import instruction
            template, mapping = workflow.load_template(self.config, self.config.get("COMFYUI_WORKFLOW_FACE", "qwen_face_t2i"))
            wf = workflow.fill_image(template, mapping, {**params, "prompt": f"{prompt}. {instruction(refs[:3])}"})
            wf[str(mapping["image"]["node"])]["inputs"][mapping["image"]["input"]] = self.client.upload(refs[0]["path"])
            encoders = workflow._find_nodes(wf, "TextEncodeQwenImageEditPlus")
            first_scale = wf[encoders[0]]["inputs"]["image1"]  # 이미지 1과 같은 크기 맞춤 노드를 따라 만든다
            for k, ref in enumerate(refs[1:3], start=2):
                load_id, scale_id = f"ref{k}_load", f"ref{k}_scale"
                wf[load_id] = {"class_type": "LoadImage", "inputs": {"image": self.client.upload(ref["path"])}}
                wf[scale_id] = {"class_type": wf[first_scale[0]]["class_type"], "inputs": {"image": [load_id, 0]}}
                for node_id in encoders:  # 긍정·부정 인코딩 모두 (공식 템플릿과 같게)
                    wf[node_id]["inputs"][f"image{k}"] = [scale_id, 0]
        else:
            template, mapping = workflow.load_template(self.config, self.config["COMFYUI_WORKFLOW_T2I"])
            wf = workflow.fill_image(template, mapping, {**params, "prompt": prompt})
        prompt_id = self.client.submit(wf)
        entry = self.client.wait(prompt_id, progress_cb=report)
        files = self.client.output_files(entry)
        if not files:
            raise ComfyError("첫 프레임 이미지 출력 없음")
        image_path = Path(out_path).with_suffix(".png")
        self.client.download(files[0], image_path)
        if refs:  # Qwen 그림은 색이 진해 채도·대비를 낮춘다 (pipeline/tone.py, 사용자가 고른 보정)
            from ..tone import soften
            try:
                soften(image_path, self.config)
            except OSError as exc:  # 보정에 실패해도 그림은 그대로 쓴다
                import logging
                logging.getLogger(__name__).warning("첫 프레임 색 보정 실패: %s", exc)
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
            uploaded_ref = None
            if params.get("reference"):  # 유니언 컨트롤: 참조 영상을 장면 fps·프레임 수로 맞춰 올린다
                from ..reference_video import prepare
                from .comfyui.rough_cut import resolve_ffmpeg
                ffmpeg = resolve_ffmpeg(self.config)
                if not ffmpeg:
                    raise ComfyError("참조 영상을 맞출 ffmpeg가 없습니다")
                fitted = prepare(ffmpeg, params["reference"], params["fps"], params["frames"],
                                 Path(out_path).with_name(Path(out_path).stem + "_ref.mp4"))
                uploaded_ref, enhance = self.client.upload(fitted), False
            wf = workflow.fill(template, mapping, params, uploaded, enhance=enhance, uploaded_end_image=uploaded_end,
                               uploaded_reference=uploaded_ref)

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

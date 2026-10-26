"""로컬 GPU 영상 생성 — [오픈소스] diffusers text-to-video 파이프라인 (기본: Wan 2.1 T2V 1.3B).

[샷 목록]의 각 샷을 클립으로 생성한 뒤 하나의 mp4로 이어 붙인다.
"""
import threading

import numpy as np

from ..prompt_builder import extract_shots, extract_style
from .base import BaseVideoGeneratorAdapter, VideoGenerationResult
from .mock_adapter import build_storyboard

_pipeline_cache = {}
_gpu_lock = threading.Lock()  # GPU는 한 번에 한 작업만


def _load_pipeline(model_id):
    if model_id not in _pipeline_cache:
        import torch
        from diffusers import DiffusionPipeline

        if not torch.cuda.is_available():
            raise RuntimeError(
                "CUDA GPU를 찾을 수 없습니다. requirements-video.txt로 GPU용 torch를 설치하세요.")
        pipe = DiffusionPipeline.from_pretrained(model_id, torch_dtype=torch.bfloat16)
        if getattr(pipe, "vae", None) is not None:
            pipe.vae.to(dtype=torch.float32)  # VAE는 float32가 화질에 안전
        pipe.enable_model_cpu_offload()  # 16GB VRAM에 맞추기 위해 모듈 단위로 GPU에 올림
        _pipeline_cache.clear()
        _pipeline_cache[model_id] = pipe
    return _pipeline_cache[model_id]


class LocalVideoAdapter(BaseVideoGeneratorAdapter):
    name = "local"
    is_slow = True

    def __init__(self, config):
        self.model_id = config["LOCAL_VIDEO_MODEL"]
        self.width = config["LOCAL_VIDEO_WIDTH"]
        self.height = config["LOCAL_VIDEO_HEIGHT"]
        self.frames = config["LOCAL_VIDEO_FRAMES"]
        self.fps = config["LOCAL_VIDEO_FPS"]
        self.steps = config["LOCAL_VIDEO_STEPS"]
        self.guidance = config["LOCAL_VIDEO_GUIDANCE"]
        self.max_shots = config["LOCAL_VIDEO_MAX_SHOTS"]
        self.negative_prompt = config["LOCAL_VIDEO_NEGATIVE_PROMPT"]

    def generate(self, prompt_text, output_path, progress=None):
        import imageio

        report = progress or (lambda msg: None)
        shots = extract_shots(prompt_text)[: self.max_shots]
        if not shots:
            raise ValueError("프롬프트의 [샷 목록]에 '1. ...' 형식의 샷이 없습니다.")
        style = extract_style(prompt_text)

        with _gpu_lock:
            report("영상 모델 로딩 중 (최초 1회는 수 분 소요)")
            pipe = _load_pipeline(self.model_id)
            writer = imageio.get_writer(
                output_path, fps=self.fps, codec="libx264", quality=8,
                pixelformat="yuv420p", macro_block_size=None)
            try:
                for i, shot in enumerate(shots, start=1):
                    def on_step(_pipe, step, _t, kwargs, i=i):
                        report(f"샷 {i}/{len(shots)} 생성 중 — {step + 1}/{self.steps} 스텝")
                        return kwargs

                    report(f"샷 {i}/{len(shots)} 생성 중")
                    frames = pipe(
                        prompt=f"{shot}. {style}",
                        negative_prompt=self.negative_prompt,
                        width=self.width, height=self.height, num_frames=self.frames,
                        num_inference_steps=self.steps, guidance_scale=self.guidance,
                        output_type="np", callback_on_step_end=on_step,
                    ).frames[0]
                    for frame in frames:
                        writer.append_data((np.clip(frame, 0, 1) * 255).astype(np.uint8))
            finally:
                writer.close()
        return VideoGenerationResult(storyboard=build_storyboard(prompt_text), video_path=output_path)

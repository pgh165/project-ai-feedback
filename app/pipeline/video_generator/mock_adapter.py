"""시뮬레이션용 mock — 영상 파일 없이 스토리보드 텍스트만 반환."""
import time

from ..prompt_builder import extract_shots, extract_style
from .base import BaseVideoGeneratorAdapter, ShotResult, VideoGenerationResult


def build_storyboard(prompt_text):
    shots = extract_shots(prompt_text)
    lines = [f"샷 {i}: {shot}" for i, shot in enumerate(shots, start=1)]
    lines.append(f"스타일: {extract_style(prompt_text)}")
    return "\n".join(lines)


class MockVideoAdapter(BaseVideoGeneratorAdapter):
    name = "mock"
    supports_shots = True

    def __init__(self, config=None):
        # MOCK_DELAY_SEC: 스테퍼 진행 표시를 눈으로 확인하기 위한 인위적 지연
        self.delay = float((config or {}).get("MOCK_DELAY_SEC", 0) or 0)

    def generate(self, prompt_text, output_path, progress=None):
        return VideoGenerationResult(storyboard=build_storyboard(prompt_text))

    def generate_shot(self, storyboard, shot, out_path, progress=None):
        if self.delay > 0:
            time.sleep(self.delay)
        return ShotResult(path="", provider="mock", elapsed_sec=self.delay)

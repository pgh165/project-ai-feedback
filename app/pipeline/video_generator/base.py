from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class VideoGenerationResult:
    storyboard: str          # 샷별 설명 텍스트 (항상 채움)
    video_path: str = ""     # 생성된 mp4 절대 경로 (mock은 빈 문자열)


@dataclass
class ShotResult:
    """장면 1개 생성 결과 (supports_shots 어댑터의 generate_shot 반환값)."""
    path: str = ""           # 생성된 클립 경로 (mock은 빈 문자열)
    provider: str = ""       # comfyui-ltx25-gguf-q4km / runway / mock ...
    elapsed_sec: float = 0.0
    peak_vram_mb: float = 0.0
    cost_usd: float = 0.0
    enhanced_prompt: str = ""  # Gemma 프롬프트 강화가 켜졌을 때 실제로 영상 모델에 들어간 문장
    image_path: str = ""     # '이미지 먼저'로 만든 첫 프레임 (없으면 빈 문자열)


class BaseVideoGeneratorAdapter(ABC):
    name = "base"
    # True면 생성이 오래 걸려 백그라운드 스레드에서 실행해야 함
    is_slow = False
    # True면 장면 단위 generate_shot()을 지원 → 부분 재생성·상용 API 혼합 가능
    supports_shots = False

    def is_available(self):
        return True

    def unavailable_reason(self):
        return ""

    @abstractmethod
    def generate(self, prompt_text, output_path, progress=None):
        """프롬프트 문서 → VideoGenerationResult. progress(msg)로 진행 상황 보고."""

    def generate_shot(self, storyboard, shot, out_path, progress=None):
        """장면 1개 생성 → ShotResult. progress(fraction|None, message)."""
        raise NotImplementedError(f"{self.name} 어댑터는 장면 단위 생성을 지원하지 않습니다.")

    def estimate_cost(self, duration_sec):
        return 0.0

    def after_shot(self):
        """장면 하나가 끝날 때마다 호출 (VRAM 정리 등)."""

"""어댑터 선택 + 폴백 체인.

영상 생성(장면별):
  generator=local → comfyui(로컬 ComfyUI 응답 시) → mock
  generator=api   → 상용 API(COMMERCIAL_VIDEO_ADAPTER, 키 있을 때) → 없음 (mock으로 몰래 대체하지 않음)
"""
import logging

from .mock_adapter import MockVideoAdapter

log = logging.getLogger(__name__)


def configured_name(config):
    return (config.get("VIDEO_GENERATOR_ADAPTER") or "mock").lower()


def create_adapter(config):
    """VIDEO_GENERATOR_ADAPTER: comfyui / local(diffusers) / mock(기본). 어댑터 객체만 만든다."""
    name = configured_name(config)
    if name == "comfyui":
        from .comfyui_adapter import ComfyUIAdapter

        return ComfyUIAdapter(config)
    if name == "local":
        from .local_adapter import LocalVideoAdapter

        return LocalVideoAdapter(config)
    return MockVideoAdapter(config)


def resolve_local_adapter(config):
    """로컬 장면 생성용 어댑터. 사용 불가면 mock으로 폴백. (adapter, fallback_reason) 반환."""
    adapter = create_adapter(config)
    if adapter.is_available():
        return adapter, ""
    reason = adapter.unavailable_reason() or f"{adapter.name} 어댑터 사용 불가"
    log.warning("영상 어댑터 %s 사용 불가 → mock으로 폴백: %s", adapter.name, reason)
    return MockVideoAdapter(config), reason


def create_commercial_adapter(config, http=None):
    """상용 API 어댑터. 키가 없거나 미지원이면 None (자동 폴백 없음)."""
    name = (config.get("COMMERCIAL_VIDEO_ADAPTER") or "").lower()
    if name == "runway":
        from .runway_adapter import RunwayAdapter

        adapter = RunwayAdapter(config, http=http)
        return adapter if adapter.is_available() else None
    return None  # kling·veo 등은 같은 인터페이스로 추후 추가


def commercial_available(config):
    return create_commercial_adapter(config) is not None


def create_adapter_for_shot(shot, config, http=None):
    """장면의 generator 값으로 로컬/상용 어댑터를 고른다. (adapter|None, fallback_reason)."""
    if shot.get("generator") == "api":
        return create_commercial_adapter(config, http=http), ""
    return resolve_local_adapter(config)

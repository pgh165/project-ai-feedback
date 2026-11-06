"""어댑터 선택 + 폴백 체인.

영상 생성(장면별):
  comfyui(로컬 ComfyUI 응답 시) → mock
  상용 API(Runway) 재생성은 2026-11-05 사용자 결정으로 뺐다 (코드는 _archive/unused_code/).
  예전 스토리보드의 generator=api 장면도 로컬로 만든다.
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


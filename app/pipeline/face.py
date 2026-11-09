"""[팀 직접 구현] 인물 사진 — 기본(REF_PHOTO_MODE=reference, 2026-11-07~)은 '참고 사진': 26B가 머리·안경·옷을 읽어
(describe) 사람이 나오는 장면 줄마다 넣고(ref_photos.describe_line) Z-Image로 그린다. 사진을 그리거나 합성에 쓰지 않는다.
draw 모드는 예전 방식 — Qwen-Image-Edit가 사진 속 사람으로 첫 프레임을 그린다(orchestrator._ref_draw). Qwen을 못 쓰면 그냥 그린다.
inswapper 얼굴 합성(비상업 연구용 라이선스)은 2026-11-09에 뺐다 (코드는 _archive/unused_code/face_inswapper.py).
표정이 커질수록 다른 사람처럼 보이므로 사람 장면의 동작은 작게 두는 편이 낫다(장면 다듬기 지시문은 그대로 둔다).
"""
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

# 사람이 나오는 장면인지 (영어 장면 묘사 기준). 손만 나오는 장면은 얼굴이 없어 빼 둔다
_PERSON = re.compile(r"\b(man|woman|men|women|person|people|student|students|boy|girl|engineer|presenter|host|"
                     r"he|she|guy|lady|developer|researcher|teacher|professor|portrait|face|selfie)\b", re.I)
_DESCRIBE_SYSTEM = (
    "Describe the person in the photo for an image-generation prompt in ONE English line: "
    "apparent age range, nationality look if obvious, gender, hairstyle (length, straight or curly, bangs shape) and "
    "hair color, glasses (frame shape, thickness and color) or no glasses, clothing (type and color). "
    "Start with 'a young Korean man' or 'a young Korean woman' if the person looks Korean. "
    "Example: a young Korean man with straight black bowl-cut bangs covering his forehead and thick black "
    "rectangular-frame glasses, wearing a black zip-up hoodie over a white T-shirt. "
    "Only visible things, no names, no opinions, no background."
)


def wanted(shot_prompt):
    return bool(_PERSON.search(shot_prompt or ""))


def describe(config, face_path):
    """얼굴 사진 → 영어 생김새 한 줄 (장면 묘사에 같은 사람이 나오게). 못 하면 "".
    큰 모델(26B 비전)을 먼저 쓴다 — 생김새를 장면마다 그대로 쓰므로 판단이 필요한 일 (e2b는 단순 작업만, 2026-11-06 사용자).
    AI 자동 기획(작업 큐 안)에서 불리므로 in_job=True."""
    from . import comfy_text, llm

    if not face_path or not Path(face_path).is_file():
        return ""
    text = ""
    if llm.vision_ready(config):
        text, reason = llm.generate_strong_only(_DESCRIBE_SYSTEM, "Describe this person.", config, max_tokens=120,
                                                in_job=True, image_path=str(face_path), temperature=0.0)
        if not text:
            log.info("얼굴 사진 묘사: 큰 모델을 쓰지 못해 작은 모델로 (%s)", reason)
    if not text and config.get("LLM_COMFY_ENABLED", True):
        c = comfy_text.client(config)
        if c is None:
            return ""
        try:
            text = comfy_text.generate(c, config, _DESCRIBE_SYSTEM, "Describe this person.",
                                       image_path=str(face_path), max_length=80)
        except Exception as exc:
            log.warning("얼굴 사진 묘사 실패: %s", exc)
            return ""
    from . import ref_photos

    return ref_photos.tidy((text or "").strip().splitlines()[0] if (text or "").strip() else "")

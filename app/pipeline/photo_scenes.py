"""[팀 직접 구현] 사진으로 영상 만들기 — 받은 사진을 장면의 첫 프레임으로 그대로 쓰고 LTX I2V로 움직인다.

사진 하나 = 장면 하나 (올린 순서대로). 첫 프레임을 새로 그리지 않으므로 회로 기판·모터처럼 AI가 잘 못 그리는 것도
실물 그대로 나온다 (2026-10 원칙: '기판·모터는 사진 I2V'). 장면 묘사(영어)는 Gemma(ComfyUI TextGenerate, 이미지 입력)가
사진을 보고 '보이는 것 + 사진 안에서 가능한 작은 동작 하나 + 카메라 움직임'으로 쓴다 (완성 영상 불러오기와 같은 방식).
사용자가 사진마다 '이 장면에서 일어날 일'을 적으면 그 동작을 따른다.
장면 데이터: shot["image"] = 사진, shot["photo"] = True → run_staged가 첫 프레임으로 복사하고, AI 점검은 첫 프레임을 건너뛴다.
"""
import json
import logging
import os
import re

log = logging.getLogger(__name__)

MAX_PHOTOS = 8

# 카메라는 거의 고정 — 2026-11-04 실제 시험(프로젝트 37): 'slowly zooms in'을 LTX가 3~4배 확대로 해석해
# 사진에 없던 칩 무늬를 지어내고 기판 인쇄 글자가 뭉개졌다. 예전 원칙(사진 I2V는 카메라 고정, 움직임은 LED·모터 정도)과 같다
_SYSTEM = (
    "You write one shot description for an image-to-video model that will animate this exact photo as the first frame. "
    "Write ONE English line (25-50 words): first what is visible (main subject, setting, lighting), then ONE small "
    "natural motion that can happen inside this photo (for example an LED blinking, a motor shaft spinning, light "
    "shimmering). End with exactly: 'locked-off static camera, the whole object stays fully in frame'. "
    "Never zoom, push in or move the camera. Keep every object that is in the photo and add nothing new. "
    "Only visible things, no negatives, no text or captions. Output only the line."
)
CAMERA = "locked-off static camera, the whole object stays fully in frame"
FALLBACK = f"The scene in the photo comes gently to life with subtle natural motion and soft light, {CAMERA}"
_CAMERA_MOVES = re.compile(r",?\s*(?:and\s+|while\s+|as\s+)?(?:the\s+)?camera\s+(?:slowly\s+|gently\s+)?"
                           r"(?:zooms?|pushe?s?|dollies|pans?|tilts?|moves?|tracks?|pulls?)[^,.;]*|"
                           r",?\s*(?:slow|gentle)\s+(?:push-in|zoom-in|zoom|pan|dolly)[^,.;]*", re.I)


def steady_camera(line):
    """작은 모델이 지시를 어기고 카메라를 움직여도 문장에서 빼고, 끝에 고정 카메라를 붙인다."""
    text = _CAMERA_MOVES.sub("", line or "").strip(" ,.;")
    return text if CAMERA in text else f"{text}, {CAMERA}"


def load(session):
    try:
        items = json.loads(session.photos_json) if session.photos_json else []
    except ValueError:
        return []
    return [p for p in items if p.get("path") and os.path.exists(p["path"])]


def save(session, items):
    session.photos_json = json.dumps(items, ensure_ascii=False) if items else ""


def ask(photo_path, caption, config):
    """사진(+사용자가 적은 동작) → Gemma 답. 쓸 수 없으면 None. 시험에서 바꿔 끼울 수 있게 모듈 함수로 둔다."""
    from . import comfy_text

    if not config.get("LLM_COMFY_ENABLED", True):
        return None
    c = comfy_text.client(config)
    if c is None:
        return None
    user = "Describe this photo as an image-to-video shot."
    if caption:
        user += f" The motion that should happen (written in Korean, follow it in English): {caption}"
    try:
        return comfy_text.generate(c, config, _SYSTEM, user, image_path=str(photo_path), max_length=160) or None
    except Exception as exc:
        log.warning("사진 장면 묘사 실패: %s", exc)
        return None


def describe(photo_path, caption, config):
    """사진 → 영어 장면 묘사 한 줄. Gemma를 못 쓰면 적어 둔 동작을 번역하고, 그것도 없으면 기본 문장."""
    from . import ko_en

    text = " ".join((ask(photo_path, caption, config) or "").split()).strip(" \"'")
    if text and not re.search(r"[가-힣]", text) and len(text.split()) >= 6:
        return steady_camera(text)
    if caption:
        try:
            english = ko_en.translate_lines([caption], config)[0][0]
            if english and not re.search(r"[가-힣]", english):
                return f"{english.rstrip('. ')}, natural light, {CAMERA}"
        except ko_en.KoEnError:
            pass
    return FALLBACK


def orientation_aspect(paths):
    """사진 대부분이 가로면 16:9, 세로면 9:16, 비슷하면 None (화면에서 고른 비율을 그대로)."""
    from PIL import Image

    wide = tall = 0
    for p in paths:
        try:
            with Image.open(p) as im:
                w, h = im.size
        except OSError:
            continue
        if w > h * 1.1:
            wide += 1
        elif h > w * 1.1:
            tall += 1
    if wide > tall:
        return "16:9"
    if tall > wide:
        return "9:16"
    return None


def attach(storyboard, photos):
    """스토리보드 장면에 사진을 순서대로 붙인다 (사진 수만큼). 바뀌었으면 True."""
    changed = False
    for shot, photo in zip(storyboard["shots"], photos):
        if shot.get("image") != photo["path"] or not shot.get("photo"):
            shot["image"], shot["photo"] = photo["path"], True
            changed = True
    return changed

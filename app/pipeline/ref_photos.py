"""[팀 직접 구현] 참조 사진 여러 장 — 얼굴 사진 + 실제 물건·장소 사진으로 첫 프레임을 그린다 (Qwen-Image-Edit-2511).

Qwen-Image-Edit-2511은 한 번에 참조 이미지 3장까지 받는다. 사진은 여러 장 받아 두고 장면마다 필요한 것만 고른다:
  1) 사람이 나오는 장면이면 얼굴 사진이 이미지 1
  2) 장면 묘사에 사진의 영어 이름(en)이 나오면 그 사진을 이어서 (최대 3장)
영어 이름은 AI 자동 기획 때 정하고(번역), 장면 다듬기에 "이 이름을 그대로 쓰라"고 알려 장면 묘사와 맞춘다.
2026-11-04 시험: 얼굴 + 라즈베리파이 5·STM32 사진 → 기판은 포트·칩 배치까지 사진대로, 얼굴은 4장 중 3장 유지.
"""
import json
import os
import re

KINDS = {"object": "물건", "place": "장소", "person": "사람 얼굴"}
# 사람 얼굴은 이름을 맞추지 않는다 — 사람이 나오는 장면마다 얼굴 사진 중에서 무작위로 고른다 (2026-11-06 사용자:
# 팀원 세 명 얼굴로 랜덤하게). 두 사람이 나오는 장면은 서로 다른 두 얼굴.
_TWO_PEOPLE = re.compile(r"\b(two|both|pair of|couple of)\b[^,.]*\b(students?|men|people|engineers?|persons?|friends?)\b|"
                         r"\b(?:man|student|engineer)\b[^,.]{0,80}\band\s+(?:a|an|another)\s+(?:\w+\s+){0,4}"
                         r"(?:man|student|engineer|person)\b", re.I)
MAX_PHOTOS = 6
MAX_PER_SHOT = 3  # Qwen-Image-Edit 참조 이미지 칸 수


def load(session):
    try:
        items = json.loads(session.refs_json) if session.refs_json else []
    except ValueError:
        return []
    return [r for r in items if r.get("path") and os.path.exists(r["path"])]


def save(session, items):
    session.refs_json = json.dumps(items, ensure_ascii=False) if items else ""


def _norm(text):
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def matches(shot_prompt, ref):
    """장면 묘사에 사진의 영어 이름이 그대로 나오는가 (대소문자·기호 무시)."""
    en = _norm(ref.get("en"))
    return bool(en) and f" {en} " in f" {_norm(shot_prompt)} "


def people_count(shot_prompt):
    """장면에 나오는 사람 수 (얼굴 사진을 몇 장 넣을지): 두 사람이면 2, 아니면 1."""
    return 2 if _TWO_PEOPLE.search(shot_prompt or "") else 1


def pick(shot_prompt, refs, face_path=None, person=False, seed=None):
    """장면에 넣을 참조 사진 [{"path", "label"}] (최대 3장). 넣을 것이 없으면 [].
    사람 장면이면 얼굴 사진(주인공 face_path + 참조 사진 중 '사람 얼굴')에서 seed로 무작위로 고른다 — 같은 장면·시드는 같은 얼굴."""
    import random

    picked = []
    faces = list(dict.fromkeys(([face_path] if face_path and os.path.exists(face_path) else [])
                               + [r["path"] for r in refs if r.get("kind") == "person"]))
    if person and faces:
        n = min(people_count(shot_prompt), len(faces), MAX_PER_SHOT)
        chosen = faces if len(faces) == 1 else random.Random(str(seed)).sample(faces, n)
        for k, path in enumerate(chosen[:n], start=1):
            picked.append({"path": path, "label": "person", "n": k, "of": n})
    objects = 0
    for ref in refs:
        if len(picked) >= MAX_PER_SHOT:
            break
        if ref.get("kind") == "person" or not matches(shot_prompt, ref):
            continue
        if ref.get("kind", "object") == "object":
            # 물건은 장면당 하나 — 2026-11-04 시험: 라즈베리파이 + STM32 두 장을 넣으면 하나만 그렸다
            if objects:
                continue
            objects += 1
        picked.append({"path": ref["path"], "label": ref["en"], "kind": ref.get("kind", "object")})
    return picked


def instruction(picked):
    """그림 모델에 붙일 지시: 이미지 번호마다 무엇을 그대로 둘지."""
    parts = []
    for i, p in enumerate(picked, start=1):
        if p["label"] == "person" and p.get("of", 1) > 1:
            which = "first" if p.get("n") == 1 else "second"
            parts.append(f"The {which} person is the person from image {i}: keep the same face, the same hairstyle and "
                         f"the same glasses as image {i}")
        elif p["label"] == "person":
            parts.append(f"The main person is the person from image {i}: keep the same face, the same hairstyle and "
                         f"the same glasses as image {i}")
        elif p.get("kind") == "place":
            parts.append(f"The place looks exactly like image {i} ({p['label']})")
        else:
            parts.append(f"The {p['label']} is exactly the object in image {i}: keep the same shape, color, "
                         f"components and connectors")
    # 참조 사진으로 그리면 모델이 묘사에 없는 모니터를 스스로 넣고 화면에 코드를 그렸다(2026-11-04). 2026-11-06 사용자: 모니터 속
    # 작은 코드는 괜찮고 화면 위에 자막처럼 뜨는 큰 글자가 문제 → 화면은 흐릿한 코드·그래프, 큰 글자·제목은 넣지 않게
    return ". ".join(parts) + (". Any screen in the scene shows small out-of-focus code and colorful data graphs, "
                               "and no big titles or captions appear anywhere. "
                               "Full scene photo, natural realistic lighting" if parts else "")


_NAME_SYSTEM = ("Translate the name of a real object or place into a short English name, at most 5 words. "
                "Output only the name: no sentence, no location, no description. "
                "Examples: 라즈베리파이 5 -> Raspberry Pi 5 / STM32 보드 -> STM32 board / 우리 실습실 -> our lab room")
_CUT = re.compile(r"\s+(?:on|in|at|with|next to|beside|sitting|placed|lying)\b.*$|[,.:;!].*$", re.I)


def english_name(name, config):
    """참조 사진 이름 → 짧은 영어 이름. 한글이 없으면 그대로. 못 하면 ''.
    장면 번역기(ko_en)를 쓰면 'Raspberry Pi 5 on a wooden desk'처럼 없는 배경까지 붙여서(2026-11-04) 따로 묻는다."""
    from . import ko_en, llm

    name = (name or "").strip()
    if not re.search(r"[가-힣]", name):
        return name
    try:
        text, _src = llm.generate(_NAME_SYSTEM, name, ko_en.engine_config(config), max_length=24)
    except Exception:
        text = None
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    line = _CUT.sub("", line.split("->")[-1]).strip(" \"'")
    if not line or re.search(r"[가-힣]", line) or len(line.split()) > 6:
        return ""
    return line


def planning_note(refs):
    """기획 지시(한국어 설명에 덧붙임): 실제 사진이 있는 대상."""
    things = [r for r in refs if r.get("kind") != "person"]
    people = [r for r in refs if r.get("kind") == "person"]
    lines = []
    if things:
        lines.append("- 실제 사진이 있는 대상 (장면에 넣으면 사진대로 그린다): " + ", ".join(
            f"{r['name']}({KINDS.get(r.get('kind'), '물건')})" for r in things))
    if people:
        lines.append(f"- 얼굴 사진 {len(people)}명 — 사람이 나오는 장면은 이 사람들 중에서 나온다")
    return "\n".join(lines)


def refine_note(refs):
    """장면 다듬기 지시: 영어 이름을 그대로 쓰게."""
    named = [r for r in refs if r.get("en") and r.get("kind") != "person"]
    if not named:
        return ""
    return ("Real things with reference photos — when a scene shows one, write its English name exactly as given, "
            "and show at most one of these objects per scene (spread them over different scenes): "
            + "; ".join(f'"{r["en"]}" ({r["name"]})' for r in named))

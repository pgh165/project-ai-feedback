"""[팀 직접 구현] AI로 채우기 — 세부사항 화면에서 비어 있는 칸만 한국어 초안으로 채운다.

입력  첫 화면 설명(필수) + 사용자가 이미 적은 칸(제목·종류·대상·장면·내레이션·스타일).
출력  비어 있던 칸의 한국어 초안. 이미 적은 칸은 절대 덮어쓰지 않는다 (LLM에는 '정해진 값'으로만 준다).
엔진  llm.generate_strong (큰 모델 Gemma4 26B → 못 쓰면 Claude → Ollama → ComfyUI e2b, ko_en.engine_config).
장면은 한국어로 받고, '영상 만들기' 때 ko_en이 영어로 바꿔 영상 모델에 넣는다.
작은 모델은 '(장면 설명)'처럼 괄호로 감싸거나 '(활기찬 음악과 함께) 문장' 같은 지시문을 붙여서 정리한다.
"""
import re

_NUM = re.compile(r"^\s*(\d{1,2})\s*[.)]\s*(.+?)\s*$")
HANGUL = re.compile(r"[가-힣]")
LATIN_WORD = re.compile(r"[A-Za-z]{3,}")

_SYSTEM = (
    "너는 짧은 영상 기획자다. 사용자가 만들고 싶은 영상을 설명하면 아래 형식 그대로, 한국어로만 답한다.\n"
    "제목: (20자 이내)\n"
    "영상 종류: ({genres} 중 하나)\n"
    "등장 요소: (여러 장면에 반복해서 나오는 인물·제품·장소의 생김새 한 줄)\n"
    "[장면]\n1. 한 장면의 화면 묘사 (누가·무엇을·어디서·카메라)\n2. ...\n(장면은 정확히 {n}개, 괄호 없이)\n"
    "[내레이션]\n1. 그 장면에 깔리는 문장 ({chars}자 이내)\n2. ...\n(장면과 같은 개수, 괄호·지시문 없이)\n"
    "스타일: (화면 분위기·조명·색감 한 줄)\n"
    "화면에 글자·로고·자막이 나오는 장면은 만들지 않는다. 다른 말은 쓰지 않는다."
)

FIELDS = ("title", "genre", "characters", "shots", "narration", "style")


class DraftFillError(RuntimeError):
    pass


def _strip_wrap(text):
    """'(…)' 전체 감싸기, 앞의 '(지시문)', 끝에 남은 ')' 를 정리한다."""
    text = text.strip()
    m = re.match(r"^\(([^()]*)\)\s*(.*)$", text)
    if m:  # '(지시문) 문장' → 문장, '(문장 전체)' → 문장
        text = m.group(2) or m.group(1)
    if text.count(")") > text.count("("):  # 짝 없는 닫는 괄호
        text = re.sub(r"\)+$", "", text)
    return text.strip(" -·\"'“”")


def _section(text, start, stops):
    m = re.search(start, text)
    if not m:
        return ""
    rest = text[m.end():]
    ends = [mm.start() for mm in (re.search(s, rest) for s in stops) if mm]
    return rest[:min(ends)] if ends else rest


def _numbered(block):
    out = []
    for line in block.splitlines():
        m = _NUM.match(line)
        if m:
            line = _strip_wrap(m.group(2))
            if line:
                out.append(line)
    return out


def _field(text, label):
    m = re.search(rf"{label}\s*[:：]\s*(.+)", text)
    return _strip_wrap(m.group(1)) if m else ""


def parse(text, n, chars, genres):
    """LLM 답 → {칸: 값}. 형식이 어긋난 칸은 빼고, 장면·내레이션 개수가 다르면 짧은 쪽에 맞춘다."""
    text = text or ""
    shots = _numbered(_section(text, r"\[장면\]", [r"\[내레이션\]", r"스타일\s*[:：]"]))[:n]
    narration = _numbered(_section(text, r"\[내레이션\]", [r"스타일\s*[:：]"]))[:n]
    out = {"title": _field(text, "제목")[:40], "genre": _field(text, r"영상\s*종류"),
           "characters": _field(text, "등장 요소")[:200], "style": _field(text, "스타일")[:200]}
    if out["genre"] not in genres:  # 목록 밖이면 비슷한 것으로, 없으면 버린다
        out["genre"] = next((g for g in genres if g.split("·")[0] in out["genre"]), "")
    good = lambda s: HANGUL.search(s) and not LATIN_WORD.search(re.sub(r"\b(?:AI|LED|PC|IT|MCU|USB)\b", "", s))
    shots = [s for s in shots if good(s)]
    narration = [s for s in narration if good(s) and len(s) <= chars + 10]
    if len(shots) >= 2:
        out["shots"] = "\n".join(shots)
    if narration:  # 장면보다 적으면 뒤쪽 장면은 해설 없이 (번호로 장면에 맞춘다)
        out["narration"] = "\n".join(f"{i}. {t}" for i, t in enumerate(narration[:n], start=1))
    return {k: v for k, v in out.items() if v}


def shot_count(idea, config, shot_seconds=None):
    """설명에 'N초'가 있으면 그 길이에 맞는 장면 수, 없으면 6개 (설정 최대 장면 수 안에서)."""
    sec = float(shot_seconds or config.get("STORYBOARD_SHOT_SECONDS", 5) or 5)
    m = re.search(r"(\d{1,3})\s*초", idea or "")
    n = round(int(m.group(1)) / sec) if m else 6
    return max(3, min(n, int(config.get("LOCAL_VIDEO_MAX_SHOTS", 8))))


def fill(form, config, genres):
    """form: 세부사항 화면 값 dict → ({채운 칸: 값}, source). 비어 있던 칸만 돌려준다."""
    from . import ko_en, llm

    idea = (form.get("synopsis") or "").strip()
    if not idea:
        raise DraftFillError("만들고 싶은 영상 설명을 먼저 적어 주세요.")
    empty = [f for f in FIELDS if not (form.get(f) or "").strip()]
    if not empty:
        return {}, ""
    try:
        seconds = float(form.get("shot_seconds") or 0) or float(config.get("STORYBOARD_SHOT_SECONDS", 5))
    except ValueError:
        seconds = float(config.get("STORYBOARD_SHOT_SECONDS", 5))
    n = shot_count(idea, config, seconds)
    if (form.get("shots") or "").strip():  # 장면을 이미 적었으면 그 개수에 맞춘다
        n = len([l for l in form["shots"].splitlines() if l.strip()])
    chars = max(10, int(seconds * 5))  # 한국어 내레이션 ≈ 초당 5자
    known = [f"{label}: {form[f].strip()}" for f, label in
             (("title", "제목"), ("genre", "영상 종류"), ("characters", "등장 요소"), ("style", "스타일"))
             if (form.get(f) or "").strip()]
    if (form.get("shots") or "").strip():
        known.append("[장면]\n" + "\n".join(f"{i}. {l.strip()}" for i, l in
                                           enumerate([l for l in form["shots"].splitlines() if l.strip()], 1)))
    user = idea + ("\n\n이미 정해진 값 (그대로 따른다):\n" + "\n".join(known) if known else "")
    system = _SYSTEM.format(genres=" / ".join(genres), n=n, chars=chars)
    text, source, _reason = llm.generate_strong(system, user, config, max_tokens=900,
                                                fallback_config=ko_en.engine_config(config))
    if source == "template" or not text:
        raise DraftFillError("AI를 쓸 수 없습니다. ComfyUI를 켜거나 ANTHROPIC_API_KEY/OLLAMA_MODEL을 설정하세요.")
    parsed = parse(text, n, chars, genres)
    filled = {f: parsed[f] for f in empty if f in parsed}
    if not filled:
        raise DraftFillError("AI 답을 알아보지 못했습니다. 설명을 조금 더 구체적으로 적고 다시 눌러 보세요.")
    return filled, source

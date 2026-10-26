"""영상 프롬프트 문서 구성. LLM이 없으면 템플릿으로 폴백한다.

사용자가 원하는 영상(어떤 종류든)을 설명하면 장면(샷) 목록으로 풀어 쓴다.
프롬프트 문서는 `[섹션명]` 헤더로 구분된 텍스트이며, 버전 관리·diff의 대상이다.
영상 모델에는 [샷 목록]의 각 줄 + [영상 스타일]만 전달된다.

섹션 이름은 예전(드라마용) 이름도 읽을 수 있다 — _ALIASES 참고.
"""
import re
from collections import OrderedDict

from . import llm

SECTION_ORDER = [
    "프로젝트", "영상 종류", "버전", "등장 요소", "이전 버전 요약", "영상 설명",
    "샷 목록", "내레이션", "자막", "영상 스타일", "시청자 반영 사항", "연출 지침",
]
REQUIRED_SECTIONS = ["영상 설명", "샷 목록"]
DEFAULT_STYLE = "cinematic, realistic, natural lighting, high detail, smooth camera motion"

# 예전 문서(숏폼 드라마용)의 섹션 이름 → 현재 이름. 읽을 때 정규화하고, 쓸 때는 현재 이름만 쓴다.
_ALIASES = {
    "시리즈": "프로젝트", "장르": "영상 종류", "회차": "버전", "등장인물": "등장 요소",
    "지금까지의 줄거리": "이전 버전 요약", "이번 화 줄거리": "영상 설명",
}

_HEADER = re.compile(r"^\[([^\[\]]+)\]\s*$")
_SHOT_LINE = re.compile(r"^\s*\d+\s*[.)]\s*(.+)$")
# 샷 줄 앞 표시: "[6초]", "[6초 시드 202]", "[시드 202]" — 길이·시드 중 하나 이상
_SHOT_MARK = re.compile(
    r"^\[\s*(?:(\d+(?:\.\d+)?)\s*(?:초|s|sec))?[\s,/]*(?:(?:시드|seed)\s*[=:]?\s*(\d+))?\s*\]\s*", re.IGNORECASE)
_NARRATION_LINE = re.compile(r"^\s*(\d{1,2})\s*[.)]\s+(.+)$")

_SHOT_SYSTEM = """You write shot lists for an AI text-to-video model.
Given a Korean description of the video the user wants, output up to {n} numbered shots in English,
one per line ("1. ..."). Each shot must be a self-contained visual description of about {seconds}
seconds: the subject (repeat each recurring subject's appearance in every shot so it stays
consistent), the action, the place, camera framing and lighting. Keep the shots in an order that
tells the requested video from start to finish. No dialogue, no on-screen text, no explanations."""


def parse_sections(text):
    sections = OrderedDict()
    current = None
    for line in (text or "").splitlines():
        match = _HEADER.match(line.strip())
        if match:
            name = match.group(1).strip()
            current = _ALIASES.get(name, name)
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    return OrderedDict((k, "\n".join(v).strip()) for k, v in sections.items())


def render_sections(sections):
    names = [n for n in SECTION_ORDER if n in sections] + \
            [n for n in sections if n not in SECTION_ORDER]
    return "\n\n".join(f"[{n}]\n{sections[n]}".rstrip() for n in names) + "\n"


def _shot_entries(text):
    """[(길이 또는 None, 시드 또는 None, 샷 묘사)] — 샷 줄 앞의 "[6초 시드 202]" 표시는 읽고 묘사에서는 뺀다."""
    body = parse_sections(text).get("샷 목록", "")
    entries = []
    for match in filter(None, map(_SHOT_LINE.match, body.splitlines())):
        line = match.group(1).strip()
        mark = _SHOT_MARK.match(line)
        if mark and (mark.group(1) or mark.group(2)):
            sec = float(mark.group(1)) if mark.group(1) else None
            seed = int(mark.group(2)) if mark.group(2) else None
            entries.append((sec, seed, line[mark.end():].strip()))
        else:
            entries.append((None, None, line))
    return entries


def set_shot_seconds(text, index, seconds):
    """[샷 목록]의 index번째(1부터) 줄 앞 표시를 [N초]로 바꾼다 (시드 표시는 그대로). 바뀐 문서를 돌려준다."""
    lines = (text or "").split("\n")
    section, count = None, 0
    for i, line in enumerate(lines):
        m = _HEADER.match(line.strip())
        if m:
            section = _ALIASES.get(m.group(1).strip(), m.group(1).strip())
            continue
        shot = _SHOT_LINE.match(line) if section == "샷 목록" else None
        if not shot:
            continue
        count += 1
        if count != index:
            continue
        body = shot.group(1).strip()
        mark = _SHOT_MARK.match(body)
        seed = mark.group(2) if mark and mark.end() else None
        rest = body[mark.end():].strip() if mark and mark.end() else body
        new_mark = f"[{seconds:g}초" + (f" 시드 {seed}" if seed else "") + "]"
        lines[i] = line[:line.index(shot.group(1))] + f"{new_mark} {rest}"
        break
    return "\n".join(lines)


def extract_shots(text):
    return [shot for _sec, _seed, shot in _shot_entries(text)]


def extract_shot_durations(text):
    """샷마다 지정된 길이(초). 지정이 없으면 None — 호출측이 기본 길이를 쓴다."""
    return [sec for sec, _seed, _shot in _shot_entries(text)]


def extract_shot_seeds(text):
    """샷마다 지정된 시드. 지정이 없으면 None — 호출측이 기본 시드를 쓴다."""
    return [seed for _sec, seed, _shot in _shot_entries(text)]


def extract_narration(text):
    """[내레이션] 섹션 → {샷 번호(1부터): 문장}. 번호 없는 줄은 순서대로 샷에 대응시킨다.
    섹션이 없거나 비어 있으면 빈 dict (내레이션 없는 영상)."""
    return _numbered_section(text, "내레이션")


def extract_subtitles(text):
    """[자막] 섹션 → {샷 번호: 문장}. 소리가 있는 완성 영상을 불러올 때 .srt 문장을 넣는 곳 (화면 자막만, TTS로 읽지 않음).
    프롬프트 문서에 있으므로 다음 버전에도 이어진다."""
    return _numbered_section(text, "자막")


def _numbered_section(text, name):
    body = parse_sections(text).get(name, "")
    result, next_no = {}, 1
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line == "(없음)":
            continue
        match = _NARRATION_LINE.match(line)  # "12.5kg" 같은 숫자 시작 문장을 번호로 오인하지 않도록 공백 필수
        no, sentence = (int(match.group(1)), match.group(2).strip()) if match else (next_no, line)
        if sentence:
            result[no] = sentence
        next_no = no + 1
    return result


def extract_style(text):
    return parse_sections(text).get("영상 스타일", "").strip() or DEFAULT_STYLE


def _numbered_narration(text, max_shots):
    """입력한 번호를 그대로 살린다 (해설이 없는 샷은 번호를 건너뛰어 쓸 수 있다). 번호 없는 줄은 이어서 매긴다."""
    lines, next_no = [], 1
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        match = _NARRATION_LINE.match(line)
        no, sentence = (int(match.group(1)), match.group(2).strip()) if match else (next_no, line)
        if sentence and no <= max_shots:
            lines.append(f"{no}. {sentence}")
        next_no = no + 1
    return "\n".join(lines)


def number_lines(lines):
    return "\n".join(f"{i}. {line}" for i, line in enumerate(lines, start=1))


def generate_shots(description, subjects, config, shot_seconds=None):
    """영상 설명 → (영어 샷 목록, source). LLM이 없으면 빈 목록."""
    n = config["LOCAL_VIDEO_MAX_SHOTS"]
    seconds = shot_seconds or config["STORYBOARD_SHOT_SECONDS"]
    text, source = llm.generate(
        _SHOT_SYSTEM.format(n=n, seconds=seconds),
        f"등장 요소:\n{subjects}\n\n만들고 싶은 영상:\n{description}",
        config,
    )
    if not text:
        return [], "template"
    shots = [m.group(1).strip() for m in map(_SHOT_LINE.match, text.splitlines()) if m][:n]
    return shots, source if shots else "template"


def build_initial_prompt(title, genre, characters, synopsis, shots_text, style,
                         global_settings, config, shot_seconds=None, narration_text=""):
    """첫 버전 프롬프트 문서 생성. (text, source) 반환 — source: claude/ollama/template.

    인자 이름(genre/characters/synopsis)은 DB 컬럼과 맞추기 위해 유지한다:
    genre=영상 종류, characters=등장 요소, synopsis=만들고 싶은 영상 설명.
    """
    shots = [m.group(1).strip() if (m := _SHOT_LINE.match(s)) else s.strip()
             for s in (shots_text or "").splitlines() if s.strip()]
    source = "template"
    if not shots:
        shots, source = generate_shots(synopsis, characters, config, shot_seconds)
    if not shots:
        # LLM도 수동 입력도 없으면 설명 문장을 그대로 샷으로 사용 (영어 입력 권장)
        shots = [s.strip() for s in re.split(r"(?<=[.!?。])\s+|\n+", synopsis) if s.strip()]

    sections = OrderedDict([
        ("프로젝트", title),
        ("영상 종류", genre),
        ("버전", "1차"),
        ("등장 요소", characters),
        ("이전 버전 요약", "(첫 버전)"),
        ("영상 설명", synopsis),
        ("샷 목록", number_lines(shots[:config["LOCAL_VIDEO_MAX_SHOTS"]])),
    ])
    narration = _numbered_narration(narration_text, config["LOCAL_VIDEO_MAX_SHOTS"])
    if narration:  # 내레이션은 선택 — 없으면 섹션 자체를 만들지 않는다
        sections["내레이션"] = narration
    sections["영상 스타일"] = style or DEFAULT_STYLE
    sections["시청자 반영 사항"] = "(아직 없음)"
    sections["연출 지침"] = "\n".join(f"- [전역] {s}" for s in global_settings) or "(아직 없음)"
    return render_sections(sections), source

"""[팀 직접 구현] 한국어 장면 묘사·스타일 → 영어 (영상 모델 입력용).

사용자는 [샷 목록]·[영상 스타일]을 한국어로 적어도 된다. 영상 모델에는 영어만 들어가야 하므로
(한국어를 넣으면 이미지 모델이 화면에 글자로 그려 버린다) 프롬프트를 만들 때 한글이 든 줄만 영어로 바꾼다.

  번역 엔진  llm.generate (Claude → Ollama → ComfyUI TextGenerate). ComfyUI 모델은 KO_EN_COMFY_MODEL.
  한 번에    장면 줄을 번호 붙여 한 번에 번역하고(모델 로드 1회), 줄 수·한글 없음을 검사한다.
             묶음 번역이 어긋나면 한 줄씩 다시 한다. 그래도 실패하면 KoEnError — 한국어를 몰래 넣지 않는다.
  표시용     번역 캐시(TextTranslation)에 '영어 → 사용자가 쓴 한국어 원문'을 넣어 두므로,
             화면의 '한글 먼저 + 영어 아래' 표시에 기계 번역이 아닌 사용자 문장이 나온다.
  줄 앞 표시 `[6초]`·`[6초 시드 202]`는 번역하지 않고 그대로 붙인다.
"""
import re

HANGUL = re.compile(r"[가-힣]")
_MARK = re.compile(
    r"^\[\s*(?:\d+(?:\.\d+)?\s*(?:초|s|sec))?[\s,/]*(?:(?:시드|seed)\s*[=:]?\s*\d+)?\s*\]\s*", re.IGNORECASE)
_NUM = re.compile(r"^\s*(\d{1,2})\s*[.)]\s*(.+?)\s*$")

_LINES_SYSTEM = (
    "You translate Korean scene descriptions into English prompts for a text-to-video model. "
    "Translate every numbered line faithfully into one concise, concrete English visual description "
    "(subject, action, setting, camera). Keep proper nouns. Never add text, captions, logos or letters to the scene. "
    "Keep the same numbers and line count. Output only the numbered English lines, nothing else.")
_ONE_SYSTEM = (
    "Translate this Korean scene description into one concise, concrete English visual description for a "
    "text-to-video model (subject, action, setting, camera). Output only the English sentence.")
_STYLE_SYSTEM = ("Translate this Korean video style description into a short English style prompt "
                 "(comma-separated keywords). Output only the English text.")


class KoEnError(RuntimeError):
    pass


def has_korean(text):
    return bool(HANGUL.search(text or ""))


_QUOTED = re.compile(r'"[^"]*"|“[^”]*”|「[^」]*」|『[^』]*』')


def needs_translation(line):
    """번호·장면 길이 표시([4초])와 따옴표 안(화면에 보일 글자를 인용한 것)을 뺀 본문에 한글이 있으면 번역할 줄.
    영어 묘사가 'text reads "임베디드소프트웨어학과"'처럼 글자를 인용했거나 [4.04초] 표시만 있어도
    저장할 때마다 다시 번역하려 들다가 실패했다."""
    body = re.sub(r"^\s*\d{1,3}\s*[.)]\s*", "", line or "")
    return has_korean(_QUOTED.sub("", _split_mark(body)[1]))


def engine_config(config):
    """번역·AI 채우기용 config — ComfyUI 모델과 그 모델의 CLIPLoader type을 바꿔 끼운다."""
    model = config.get("KO_EN_COMFY_MODEL") or config.get("LLM_COMFY_MODEL") or config.get("TRANSLATE_COMFY_MODEL")
    clip_type = config.get("KO_EN_COMFY_CLIP_TYPE") or config.get("TRANSLATE_COMFY_CLIP_TYPE", "ltxv")
    return dict(config, LLM_COMFY_MODEL=model, TRANSLATE_COMFY_CLIP_TYPE=clip_type)


def _clean(text):
    return " ".join((text or "").split()).strip().strip("\"'“”")


def _ok(english):
    return bool(english) and not has_korean(english) and len(english.split()) >= 3


def _split_mark(line):
    m = _MARK.match(line)
    return (line[:m.end()], line[m.end():].strip()) if m and m.end() > 0 else ("", line.strip())


def translate_lines(lines, config):
    """[줄] → ([영어 줄], source, [(영어 본문, 한국어 본문)]). 한글이 없는 줄은 그대로 둔다. 실패하면 KoEnError.
    본문 = 줄 앞 표시를 뺀 부분 (화면 표시·번역 캐시가 쓰는 것과 같다)."""
    from . import llm

    cfg = engine_config(config)
    parts = [_split_mark(l) for l in lines]
    todo = [i for i, (_m, body) in enumerate(parts) if needs_translation(body)]
    if not todo:
        return list(lines), "", []
    result = {}
    source = ""
    user = "\n".join(f"{k}. {parts[i][1]}" for k, i in enumerate(todo, start=1))
    text, source = llm.generate(_LINES_SYSTEM, user, cfg, max_length=120 * len(todo) + 100)
    got = {}
    for line in (text or "").splitlines():
        m = _NUM.match(line)
        if m:
            got[int(m.group(1))] = _clean(m.group(2))
    for k, i in enumerate(todo, start=1):
        if _ok(got.get(k, "")):
            result[i] = got[k]
    for i in [i for i in todo if i not in result]:  # 묶음에서 어긋난 줄만 한 줄씩
        text, src = llm.generate(_ONE_SYSTEM, parts[i][1], cfg, max_length=160)
        english = _clean((text or "").splitlines()[0] if text else "")
        if not _ok(english):
            raise KoEnError(f"장면 {i + 1}을(를) 영어로 바꾸지 못했습니다: {parts[i][1][:40]}")
        result[i], source = english, source or src
    if source == "template":
        raise KoEnError("번역할 LLM이 없습니다 (ComfyUI를 켜거나 ANTHROPIC_API_KEY/OLLAMA_MODEL을 설정하세요).")
    out = [mark + result[i] if i in result else lines[i] for i, (mark, _body) in enumerate(parts)]
    return out, source, [(result[i], parts[i][1]) for i in todo]


def translate_style(style, config):
    """한국어 스타일 → 영어 키워드. 한글이 없으면 그대로."""
    from . import llm

    if not has_korean(style):
        return style, ""
    text, source = llm.generate(_STYLE_SYSTEM, style, engine_config(config), max_length=120)
    english = _clean((text or "").splitlines()[0] if text else "")
    if not english or has_korean(english):
        raise KoEnError("영상 스타일을 영어로 바꾸지 못했습니다. 영어로 적거나 비워 두세요.")
    return english, source


def remember_originals(pairs, engine="user"):
    """[(영어, 한국어 원문)] → 번역 캐시. 화면에 사용자가 쓴 한국어가 그대로 보이게."""
    from ..models import TextTranslation, db
    from .translate import key

    for english, korean in pairs:
        if not english or not korean or english == korean:
            continue
        row = TextTranslation.query.filter_by(source_hash=key(english)).first()
        if row:
            row.ko_text, row.engine = korean, engine
        else:
            db.session.add(TextTranslation(source_hash=key(english), source_text=english.strip(),
                                           ko_text=korean.strip(), engine=engine))


def translate_prompt_doc(text, config):
    """프롬프트 문서의 [샷 목록]·[영상 스타일]에서 한글이 든 줄만 영어로 바꾼다 (다른 섹션·줄은 그대로).
    (새 문서, source, [(영어 본문, 한국어 원문)]) — 바꿀 것이 없으면 source는 "". 실패하면 KoEnError."""
    from .prompt_builder import _HEADER, _SHOT_LINE, _ALIASES

    lines = (text or "").split("\n")
    section, shot_at, style_at = None, [], []
    for i, line in enumerate(lines):
        m = _HEADER.match(line.strip())
        if m:
            name = m.group(1).strip()
            section = _ALIASES.get(name, name)
            continue
        if not needs_translation(line):
            continue
        if section == "샷 목록" and _SHOT_LINE.match(line):
            shot_at.append(i)
        elif section == "영상 스타일" and line.strip():
            style_at.append(i)
    if not shot_at and not style_at:
        return text, "", []
    source, pairs = "", []
    if shot_at:
        bodies = [_SHOT_LINE.match(lines[i]).group(1).strip() for i in shot_at]
        english, source, pairs = translate_lines(bodies, config)
        for i, body, new in zip(shot_at, bodies, english):
            prefix = lines[i][:lines[i].index(body)]  # "1. " 같은 번호
            lines[i] = prefix + new
    for i in style_at:
        english, src = translate_style(lines[i].strip(), config)
        pairs.append((english, lines[i].strip()))
        lines[i], source = english, source or src
    return "\n".join(lines), source, pairs

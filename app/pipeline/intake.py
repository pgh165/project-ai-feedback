"""[팀 직접 구현] AI에게 맡기기 — 한두 문장 설명에서 부족한 정보만 되묻고, 나머지는 기본값으로 가정한다.

정해진 칸(영상 종류·길이·화면 비율·내레이션·배경음악)은 규칙으로 읽고, 읽히지 않으면 기본값을 '이렇게 가정했어요'로 보여 준다.
영상 종류를 알 수 없을 때만 그 칸을 질문으로 올린다 (내용 전체가 달라지므로).
내용 질문(무엇을 보여 줄지 등)은 작은 LLM(Gemma e2b)에게 '설명에 빠진 꼭 필요한 정보'만 최대 2개 묻게 한다.
제작 원칙(등장인물 생김새, 꼭 실제와 같아야 할 물건 등)도 정해 둔 규칙 대신 같은 모델이 설명을 보고 질문을 최대 2개 만든다
(2026-11-05 사용자 결정). 답한 것만 그 프로젝트의 원칙(plan_rules)이 된다. 화면 글자 금지는 묻지 않고 항상 적용한다.
LLM을 쓸 수 없거나 답을 알아보지 못하면 질문 없이 진행한다 — 묻는 것보다 멈추지 않는 것이 우선.
질문 수를 2개로 둔 이유: 사람 확인을 줄이는 방향(2026-11-03)이고, 작은 모델이 지시를 어기고 기본값으로 정할 것을
묻는 일이 많아 질문이 늘수록 쓸모없는 질문도 늘었다.
"""
import re

DEFAULT_SECONDS = 30
SECONDS_OPTIONS = (15, 30, 45, 60)
ASPECT_LABELS = {"9:16": "세로 (쇼츠·릴스)", "16:9": "가로 (유튜브)", "1:1": "정사각"}
MAX_QUESTIONS = 2
MAX_RULE_QUESTIONS = 2

GENRE_HINTS = [  # 설명 → 영상 종류(routes/session.GENRES). 앞에서부터 맞춘다. 기획 화면 추천도 이 규칙을 쓴다
    (r"뮤직\s*비디오|MV|노래", "뮤직비디오"), (r"단편|드라마|영화|이야기", "단편 영화"),
    (r"브이로그|일상|여행", "브이로그"), (r"설명|사용법|조립|강의|튜토리얼|교육", "교육·설명"),
    (r"제품|신제품|출시|광고", "홍보·광고"), (r"학과|학교|대학|입학|캠퍼스|축제|행사|박람회|소개|안내", "소개·안내"),
    (r"홍보", "홍보·광고"),
]

_Q_SYSTEM = (
    "너는 짧은 영상 기획자다. 사용자의 영상 설명을 읽고, 영상 장면을 짜는 데 꼭 필요한데 설명에 빠진 정보만 질문한다.\n"
    "길이·화면 비율·내레이션·배경음악·분위기·색감·영상 종류는 묻지 않는다 (기본값으로 정한다).\n"
    "설명만으로 장면을 짤 수 있으면 '없음' 한 단어만 쓴다. 질문은 최대 2개, 각 질문마다 고를 보기 3개를 준다.\n"
    "형식 (다른 말 없이):\n질문: (한 문장)\n보기: (보기1) | (보기2) | (보기3)"
)

_RULE_SYSTEM = (
    "너는 짧은 영상 기획자다. 사용자의 영상 설명을 읽고, AI가 장면을 그릴 때 꼭 지켜야 할 조건(제작 원칙)을 정하기 위한 질문을 만든다.\n"
    "예: 등장인물의 나이·국적·옷차림, 실제 물건과 똑같아야 하는 것(보드 색·모양·장비 종류), 꼭 나오거나 빠져야 할 소재, 배경 장소.\n"
    "길이·화면 비율·내레이션·배경음악·분위기·색감·영상 종류·화면 글자는 묻지 않는다.\n"
    "설명에 이미 정해져 있거나 조건이 필요 없으면 '없음' 한 단어만 쓴다. 질문은 최대 2개, 각 질문마다 고를 보기 3개를 준다.\n"
    "형식 (다른 말 없이):\n질문: (한 문장)\n보기: (보기1) | (보기2) | (보기3)"
)


def guess_genre(idea):
    return next((g for pattern, g in GENRE_HINTS if re.search(pattern, idea or "", re.I)), "")


def guess_seconds(idea):
    """'30초'·'1분'·'1분 30초' → 초. 없으면 None."""
    text = idea or ""
    minutes = re.search(r"(\d{1,2})\s*분", text)
    seconds = re.search(r"(\d{1,3})\s*초", text)
    if not minutes and not seconds:
        return None
    total = (int(minutes.group(1)) * 60 if minutes else 0) + (int(seconds.group(1)) if seconds else 0)
    return max(5, min(total, 120))


def guess_aspect(idea, chosen=""):
    text = idea or ""
    if re.search(r"쇼츠|숏츠|릴스|틱톡|세로", text):
        return "9:16"
    if re.search(r"가로|유튜브\s*(본|영상)|16\s*:\s*9|와이드", text):
        return "16:9"
    if re.search(r"정사각|1\s*:\s*1|인스타\s*피드", text):
        return "1:1"
    return chosen if chosen in ASPECT_LABELS else "9:16"


def guess_narration(idea):
    return not re.search(r"내레이션\s*(없이|빼|없는)|나레이션\s*(없이|빼|없는)|음악만|말\s*없이", idea or "")


def guess_music(idea):
    return not re.search(r"(배경\s*)?음악\s*(없이|빼|없는)|무음", idea or "")


# 기본값으로 정하는 것들 — 작은 모델이 지시를 어기고 물어도 버린다
# (2026-11-03 실제 시험: '분위기는 어떤 느낌?'·'장면 전환 속도는?'을 물었다)
_SKIP_TOPICS = re.compile(r"분위기|느낌|톤|색감|색상|조명|전환|속도|템포|길이|분량|몇\s*초|비율|세로|가로|"
                          r"내레이션|나레이션|목소리|음악|배경음|BGM|효과음|자막|스타일|종류|장르")


# 원칙 질문은 '어떤 종류의 보드'·'옷 색상'처럼 물건·사람 생김새를 물어야 하므로 종류·색상·스타일은 허용한다
_RULE_SKIP = re.compile(r"분위기|느낌|톤|색감|조명|전환|속도|템포|길이|분량|몇\s*초|비율|세로|가로|"
                        r"내레이션|나레이션|목소리|음악|배경음|BGM|효과음|자막|글자|텍스트|장르")


def parse_questions(text, limit=MAX_QUESTIONS, skip=_SKIP_TOPICS):
    """LLM 답 → [{"question", "options"}]. '없음'이거나 형식이 어긋나거나 기본값으로 정할 것을 물으면 뺀다."""
    text = (text or "").strip()
    if not text or text.startswith("없음"):
        return []
    out = []
    for m in re.finditer(r"질문\s*[:：]\s*(.+?)\s*\n\s*보기\s*[:：]\s*(.+)", text):
        question = m.group(1).strip().strip("\"'")
        options = [o.strip(" ()[]\"'-·") for o in re.split(r"\s*[|/]\s*", m.group(2)) if o.strip(" ()[]\"'-·")]
        if skip.search(question):
            continue
        if 4 <= len(question) <= 80 and len(options) >= 2 and re.search(r"[가-힣]", question):
            out.append({"question": question, "options": options[:4]})
    return out[:limit]


def content_questions(idea, config):
    """설명에 빠진 내용 질문 (최대 2개). LLM이 없거나 답을 못 읽으면 [].
    큰 모델(26B)을 먼저 쓴다 — e2b는 판단이 필요한 일에 약하다(2026-11-06 사용자 결정). 못 쓰면 e2b로."""
    from . import ko_en, llm

    try:
        text, _source, _reason = llm.generate_strong(_Q_SYSTEM, idea, config, max_tokens=220,
                                                     fallback_config=ko_en.engine_config(config))
    except Exception:
        return []
    return parse_questions(text)


def rule_questions(idea, config):
    """제작 원칙 질문 (최대 2개). LLM이 없거나 답을 못 읽으면 [] — 원칙 없이 진행.
    큰 모델(26B)을 먼저 쓴다 — 2026-11-06 e2b는 원칙 대신 '강조할 기술 분야' 같은 내용 질문을 냈다."""
    from . import ko_en, llm

    try:
        text, _source, _reason = llm.generate_strong(_RULE_SYSTEM, idea, config, max_tokens=220,
                                                     fallback_config=ko_en.engine_config(config))
    except Exception:
        return []
    return parse_questions(text, MAX_RULE_QUESTIONS, _RULE_SKIP)


def analyze(idea, config, aspect="", genres=(), ask_llm=True):
    """설명 → {"questions": [{key, question, options}], "assumptions": [{key, label, value, options, shown}]}.
    assumptions의 value는 폼 값(문자열), shown은 화면에 보이는 말."""
    idea = (idea or "").strip()
    genre = guess_genre(idea)
    seconds = guess_seconds(idea)
    questions = []
    if not genre:
        questions.append({"key": "genre", "question": "어떤 종류의 영상인가요?", "options": list(genres)})
    if ask_llm:
        from . import llm
        with llm.strong_server(config):  # 내용·원칙 질문 두 번을 큰 모델 한 번 켜서 묻는다 (켜는 데 약 1분)
            content = content_questions(idea, config)
            rules = rule_questions(idea, config)
        for i, q in enumerate(content, start=1):
            questions.append({"key": f"q{i}", **q})
        asked = {q["question"] for q in questions}
        for i, q in enumerate(rules, start=1):
            if q["question"] not in asked:  # 원칙 질문: 답이 그 프로젝트의 제작 원칙이 된다
                questions.append({"key": f"r{i}", "rule": True, **q})
    ratio = guess_aspect(idea, aspect)
    narration, music = guess_narration(idea), guess_music(idea)
    assumptions = [
        {"key": "seconds", "label": "길이", "value": str(seconds or DEFAULT_SECONDS),
         "shown": f"{seconds or DEFAULT_SECONDS}초", "options": [str(s) for s in SECONDS_OPTIONS],
         "from_text": seconds is not None},
        {"key": "aspect", "label": "화면 비율", "value": ratio, "shown": ASPECT_LABELS[ratio],
         "options": list(ASPECT_LABELS)},
        {"key": "narration", "label": "내레이션", "value": "on" if narration else "off",
         "shown": "한국어 내레이션 넣기 (기본 목소리)" if narration else "내레이션 없이", "options": ["on", "off"]},
        {"key": "music", "label": "배경음악", "value": "on" if music else "off",
         "shown": "분위기에 맞는 연주곡 한 곡" if music else "배경음악 없이", "options": ["on", "off"]},
    ]
    if genre:
        assumptions.insert(0, {"key": "genre", "label": "영상 종류", "value": genre, "shown": genre,
                               "options": list(genres)})
    return {"questions": questions, "assumptions": assumptions}


def compose(idea, qa, seconds):
    """기획 LLM에 줄 설명: 원래 설명 + 질문의 답 + 길이."""
    lines = [(idea or "").strip()]
    for question, answer in qa:
        if (answer or "").strip():
            lines.append(f"- {question} → {answer.strip()}")
    if seconds and not re.search(r"\d+\s*(초|분)", idea or ""):
        lines.append(f"- 영상 길이: {int(seconds)}초")
    return "\n".join(l for l in lines if l)


def shot_plan(seconds, config):
    """영상 길이 → (장면 수, 장면 길이). 장면 길이는 설정 기본값(5초)에서 시작해 최대 장면 수를 넘지 않게 늘린다."""
    base = float(config.get("STORYBOARD_SHOT_SECONDS", 5) or 5)
    limit = int(config.get("LOCAL_VIDEO_MAX_SHOTS", 8))
    top = float(config.get("SHOT_MAX_SECONDS", 8))
    seconds = float(seconds or DEFAULT_SECONDS)
    n = max(3, round(seconds / base))
    if n > limit:
        n = limit
    length = min(max(seconds / n, 2.0), top)
    return n, round(length, 1)

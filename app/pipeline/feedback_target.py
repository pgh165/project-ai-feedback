"""[팀 직접 구현] 승인된 의견의 '반영 위치' — 장면을 다시 만들어야 하는 의견인가?

화면과 무관한 의견(음악·음성, 전환·속도, 설명 부족)까지 장면을 다시 그리면
1차와 전혀 다른 화면이 나오고, 한국어 의견 원문이 이미지 프롬프트에 섞여 화면에 글자로 찍힌다.
그래서 의견마다 반영 위치를 정하고, 화면·움직임 의견만 장면을 다시 만든다.

  scene   화면       그 장면의 첫 프레임 이미지부터 다시 (시드 변경)
  motion  움직임     이전 버전의 첫 프레임을 그대로 쓰고 컷 영상만 다시
  edit    편집       장면은 그대로 — 전환·속도·순서·자막은 [연출 지침]으로
  audio   소리       장면은 그대로 — 음악·음성·내레이션은 [연출 지침]으로
  content 내용       장면은 그대로 — 설명 부족·장면 추가는 [시청자 반영 사항]으로

자동 제안은 키워드 + 분류 카테고리로 하고, 제작자가 검토 화면에서 바꿀 수 있다 (ClusterResult.apply_target).
"""
import re

TARGETS = {
    "scene": ("화면", "그 장면의 첫 프레임부터 다시 만듭니다"),
    "motion": ("움직임", "첫 프레임은 그대로 두고 컷 영상만 다시 만듭니다"),
    "edit": ("편집", "장면은 그대로 — 전환·속도·순서·자막은 연출 지침에 반영"),
    "audio": ("소리", "장면은 그대로 — 음악·음성·내레이션은 연출 지침에 반영"),
    "content": ("내용", "장면은 그대로 — 설명·장면 추가는 시청자 반영 사항에 반영"),
}
REGEN_TARGETS = {"scene", "motion"}

_AUDIO = re.compile(r"음악|배경음|bgm|소리|사운드|음성|목소리|내레이션|나레이션|해설|더빙|음량|볼륨|효과음|"
                    r"music|sound|voice|narrat|audio", re.I)
_EDIT = re.compile(r"전환|트랜지션|넘어가|넘어갈|페이드|디졸브|순서|편집|템포|리듬|영상 길이|너무 길|너무 짧|자막|"
                   r"transition|pacing|subtitle|caption", re.I)
_SCENE_CHANGE = re.compile(r"색|모양|배경|물체|사물|바꿔|바뀌|만들어|그려|등장|넣어|빼|크기|밝|어둡|조명|화질|흐릿|"
                           r"선명|깨져|왜곡|얼굴|글자|텍스트|color|shape|background|object|bright|dark|blur", re.I)
_MOTION = re.compile(r"움직|동작|카메라|천천히|느리게|빠르게|흔들|회전|줌|패닝|멈춰|멈추|앞으로|뒤로|방향|달려|뛰어|걸어|"
                     r"(?:가|와|돌아)야|motion|camera|slow|fast|shak|zoom|pan|forward|backward|direction", re.I)
_CONTENT = re.compile(r"설명|내용|정보|소개|부족|추가|더 보여|더 넣|장면을 더|explain|more info|add", re.I)

_BY_CATEGORY = {"audio": "audio", "subtitle": "edit", "pacing": "edit", "story": "content",
                "content": "content", "visual": "scene", "technical": "scene"}


def suggest(category, text):
    """의견 문장·카테고리 → 반영 위치 제안."""
    text = text or ""
    if _AUDIO.search(text):
        return "audio"
    if _EDIT.search(text):
        return "edit"
    if _SCENE_CHANGE.search(text):
        return "scene"
    if _MOTION.search(text):
        return "motion"
    if _CONTENT.search(text):
        return "content"
    return _BY_CATEGORY.get(category or "", "scene")


def target_of(cluster):
    """저장된 반영 위치(제작자가 고른 값) 또는 자동 제안."""
    chosen = getattr(cluster, "apply_target", None)
    return chosen if chosen in TARGETS else suggest(cluster.topic_category, cluster.representative_text)


def regenerates(cluster):
    return target_of(cluster) in REGEN_TARGETS


# ── 장면을 다시 만들 때 붙이는 의견: 한국어 원문 대신 영어 영상 지시문으로 ──
_NOTE_SYSTEM = (
    "You convert a viewer's feedback about one shot of a video into a short English instruction for an "
    "AI image/video generation model. The feedback usually points out what is WRONG; describe the CORRECTED "
    "state that should be visible in the frame or how things should move, never the mistake itself "
    "(e.g. '컵이 비어 있어' -> 'cup filled with hot coffee'), in under 25 words, as a phrase "
    "(no full sentences addressed to anyone). "
    "Use the shot description for context (name the actual subjects in it) and never mention timestamps. "
    "If the feedback is not about visuals or motion (for example music, voice, narration, editing, "
    "transitions, or general information), output exactly NONE."
)


# ── 장면 묘사 고쳐 쓰기: 의견을 꼬리에 붙이지 않고 장면 묘사 자체를 '고친 모습'으로 다시 쓴다 ──
# 꼬리에 붙이면 (1) 작은 모델이 지적된 문제를 그대로 묘사하고("사진이 없어" → "empty frame")
# (2) 원래 묘사가 앞에 그대로 남아 이미지 모델(Z-Image Turbo, 부정 프롬프트 없음)이 거의 따르지 않았다.
_REVISE_SYSTEM = (
    "You revise one shot description for an AI video pipeline using a viewer's feedback. An image model first draws "
    "the FIRST FRAME, then a video model animates that frame using the video description.\n"
    "The feedback (often Korean) usually points out what is WRONG in the generated shot, or describes what the "
    "shot should show instead. Rewrite the shot in English so it shows the CORRECTED version.\n"
    "Rules:\n"
    "- Start from the original description and change only the words the feedback is about. Copy every other phrase "
    "word for word — people, clothing, hair, objects, setting, lighting and camera — in BOTH lines.\n"
    "- State the fix positively and concretely, as visible details (what is there, how it is held, where it faces).\n"
    "- Never describe the mistake itself and never use words that describe the problem (empty, missing, wrong, "
    "upside down, backwards) unless they are the goal.\n"
    "- When the feedback replaces something (X -> Y), drop X and every word that described X (material, color, shape). "
    "Never write 'instead of', 'not', 'no', 'without' or 'replaced' — the image model would draw X again.\n"
    "- When the feedback asks for more or fewer people or objects, write the exact total number.\n"
    "- If something happens during the shot (picking up, placing, opening, walking in), the video line describes it "
    "as visible steps in order, and the first-frame line freezes the moment BEFORE it starts: every moving subject at "
    "its starting position, objects not yet picked up, moved or opened.\n"
    "- When something is grabbed or picked up (gripper, claw, hand), give the object a familiar small size the image "
    "model can draw (e.g. 'a tiny dice-sized cube', not 'half the gripper width'), say where it rests (on the desk next "
    "to the arm's base, away from people's hands) in BOTH lines, and in the video line the open fingers come down on "
    "either side of the object and close against its outer faces before lifting — otherwise the video model pushes "
    "the fingers through the object.\n"
    "- In the video line, people and things not part of that action keep doing what they did at the start "
    "(e.g. 'she keeps typing'), and the shot ends right after the requested action — nothing else happens.\n"
    "- Same subjects, setting, lighting and camera in both lines. Each line under 60 words. No text, captions or logos.\n"
    "Output exactly two lines:\nVideo: <revised shot description>\nFirst frame: <the very first moment of that shot>\n\n"
    "Example\nShot: Two students smile behind a wooden board holding sensors.\nFeedback: 나무 보드를 브레드 보드로 바꿔줘\n"
    "Video: Two students smile behind a white plastic breadboard holding sensors.\n"
    "First frame: Two students smile behind a white plastic breadboard holding sensors.\n\n"
    "Example\nShot: A man and his friend look at a monitor in a lab.\nFeedback: 학생이 한 명 더 있었으면 좋겠어\n"
    "Video: Three young students look at a monitor together in a lab.\n"
    "First frame: Three young students look at a monitor together in a lab.\n\n"
    "Example\nShot: A white robotic arm stands on a desk beside a woman at a keyboard.\n"
    "Feedback: 로봇팔이 아래에 있는 상자를 집어서 들어 올리게 해줘\n"
    "Video: A white robotic arm lowers its open gripper over a tiny dice-sized red cube resting on the desk next to "
    "the arm's base, the two fingers slide down on either side of the cube and close against its outer faces, then "
    "the arm lifts it upward and holds it still, while the woman beside it keeps typing at her keyboard.\n"
    "First frame: A tiny dice-sized red cube rests on the desk next to the base of a white robotic arm, far from the "
    "woman's hands, and the arm's open gripper hovers just above it, beside a woman typing at a keyboard.\n\n"
    "Example\nShot: A man holds a cup at a cafe table, soft light.\nFeedback: 컵이 비어 있어\n"
    "Video: A man holds a cup filled with steaming black coffee at a cafe table, soft light.\n"
    "First frame: A man holds a cup filled with steaming black coffee at a cafe table, soft light."
)


# ── 장면 길이 요청: "3초로", "2초 더", "두 배로", "늘려줘", "짧게" → 장면 길이를 바꿔 다시 만든다 ──
# (예전에는 이런 의견도 장면 묘사에 붙기만 하고 길이는 그대로였다)
_LEN_NARRATION = re.compile(r"(?:내레이션|나레이션|해설|목소리|음성|말이|말을).{0,12}(?:맞[춰추게아는]|빨[라리]|빠르|들어가|안\s*들어)|"
                            r"맞[춰추게].{0,6}(?:내레이션|나레이션|해설|목소리|음성)")
_LEN_CONTEXT = re.compile(r"길이|초|길게|짧게|늘[려리릴린여어]|길어|짧아|줄[여이일인]|두\s*배|2\s*배|오래|" + _LEN_NARRATION.pattern)
_LEN_EXACT = re.compile(r"(\d+(?:\.\d+)?)\s*초(?!\s*더)")
_LEN_ADD = re.compile(r"(\d+(?:\.\d+)?)\s*초\s*(?:더|추가|만큼\s*더)")
_LEN_DOUBLE = re.compile(r"두\s*배|2\s*배")
_LEN_LONGER = re.compile(r"길게|늘[려리릴린여]|늘어나|길어졌|더\s*길|오래")
_LEN_SHORTER = re.compile(r"짧게|짧아|줄[여이일인]|빨리\s*끝")
_LEN_FILLER = re.compile(
    r"\d+(?:\.\d+)?\s*초|영상|장면|컷|클립|길이|정도|좀|조금|더|두\s*배|2\s*배|길게|짧게|늘[려리릴린여]|늘어나|길어|짧아|줄[여이일인]|"
    r"내레이션|나레이션|해설|목소리|음성|말이|말을|맞[춰추게아는]|빨[라리]|빠르[다게네]?|들어가[게도]?|안|너무|아직|여전히|네|"
    r"오래|만들어|해\s*줘|해\s*주세요|주세요|줘|수\s*있나|있을까|있나요|있어|가능|할까|로|으로|까지|에|와|과|랑|을|를|이|가|은|는|요|[?!.,~\s]")


def needs_narration_fit(note):
    """길이를 '얼마나'는 말하지 않은 길이 요청인지 ('늘려줘', '내레이션에 맞춰', '나레이션이 빨라').
    이럴 때는 내레이션이 1배속으로 들어가는 길이를 재서 쓴다 (호출측이 fit_sec로 넘긴다)."""
    text = (note or "").strip()
    if not text or _LEN_ADD.search(text) or _LEN_EXACT.search(text) or _LEN_DOUBLE.search(text):
        return False
    return bool(_LEN_NARRATION.search(text) or _LEN_LONGER.search(text))


def length_request(note, current, min_sec=1.5, max_sec=8.0, fit_sec=None):
    """의견 문장 → 새 장면 길이(초), 길이 이야기가 아니면 None.
    fit_sec: 이 장면의 내레이션이 1배속으로 들어가는 길이. '늘려줘'처럼 얼마나를 말하지 않았으면
    이 길이로 맞춘다 (이미 들어가면 예전처럼 1.5배). '내레이션에 맞춰'면 짧아질 수도 있다."""
    text = (note or "").strip()
    if not text or not _LEN_CONTEXT.search(text):
        return None
    if (m := _LEN_ADD.search(text)):
        target = current + float(m.group(1))
    elif (m := _LEN_EXACT.search(text)):
        target = float(m.group(1))
    elif _LEN_DOUBLE.search(text):
        target = current * 2
    elif _LEN_NARRATION.search(text):
        target = fit_sec if fit_sec else max(current * 1.5, current + 1.0)
    elif _LEN_LONGER.search(text):
        target = fit_sec if fit_sec and fit_sec > current + 0.05 else max(current * 1.5, current + 1.0)
    elif _LEN_SHORTER.search(text):
        target = current * 0.7
    else:
        return None
    return round(min(max(target, min_sec), max_sec), 2)


def is_length_only(note):
    """길이만 바꿔 달라는 의견인지 — 그렇다면 장면 묘사에는 붙이지 않는다."""
    rest = _LEN_FILLER.sub("", note or "")
    return len(re.findall(r"[가-힣A-Za-z]", rest)) < 2


def visual_notes(notes):
    """화면·움직임에 관한 의견만 (소리·편집·내용 의견은 장면 묘사에 넣지 않는다)."""
    return [n.strip() for n in notes or [] if n and n.strip()
            and (not re.search(r"[가-힣]", n) or suggest(None, n) in REGEN_TARGETS)]


def _clean_line(text):
    text = " ".join((text or "").split()).strip(" \"'")
    text = re.sub(r"^(?:Revised|Shot|Video|First\s*frame)\s*:\s*", "", text, flags=re.I)
    return "" if not text or re.search(r"[가-힣]", text) or not 4 <= len(text.split()) <= 90 else text


def split_revision(text):
    """'Video: … / First frame: …' 답 → (영상 묘사, 첫 프레임 묘사). 형식을 안 지켰으면 전체를 영상 묘사로, 첫 프레임은 ''."""
    text = text or ""
    first = re.search(r"First\s*frame\s*:\s*(.+)", text, flags=re.I | re.S)
    video = re.search(r"Video\s*:\s*(.+?)(?=First\s*frame\s*:|$)", text, flags=re.I | re.S)
    head = text[:first.start()] if first else text
    return _clean_line(video.group(1) if video else head), _clean_line(first.group(1)) if first else ""


def revise_shot(shot_prompt, notes, config, client=None):
    """원래 영어 장면 묘사 + 의견들 → (고친 영상 묘사, 첫 프레임 묘사). 화면 의견이 없으면 ("", "").
    첫 프레임 묘사는 동작이 시작되기 전 모습 — 하나로 쓰면 이미지 모델이 동작 중간(이미 물건을 든 모습)을 그려서
    '로봇 팔이 물체를 집게 해 줘'가 '든 채로 위로 올리기'가 됐다(2026-10-23). 형식을 못 지키면 첫 프레임은 ''(영상 묘사를 같이 씀).
    변환할 수단이 없거나 답이 이상하면 None (호출측이 예전 방식으로 대신한다)."""
    from . import comfy_text, llm

    notes = visual_notes(notes)
    if not notes:
        return "", ""
    if not any(re.search(r"[가-힣]", n) for n in notes):  # 영어로 적은 지시문은 그대로 꼬리에 (notes_en)
        return None
    user = f"Shot: {shot_prompt.strip()}\nFeedback: " + " / ".join(notes)

    def ask(prompt):
        # 큰 모델 먼저 (2026-10-23 비교: e2b는 '나무 보드 → 브레드 보드'를 'wooden breadboard'로, '코드 말고 그래프'를
        # 'graphs instead of code'로 써서 고칠 것을 다시 그리게 했다). 작업 안에서만 불리므로 in_job.
        text, _reason = llm.generate_strong_only(_REVISE_SYSTEM, prompt, config, max_tokens=320, in_job=True)
        c = None if text else (client or comfy_text.client(config))
        if c is not None:
            try:
                text = comfy_text.generate(c, config, _REVISE_SYSTEM, prompt, max_length=260)
            except Exception:
                text = None
        if not text and (config.get("ANTHROPIC_API_KEY") or config.get("OLLAMA_MODEL")):
            text, _source = llm.generate(_REVISE_SYSTEM, prompt, config, comfy=False)
        return split_revision(text)

    video, first = ask(user)
    if not video:
        return None
    # 원래 묘사의 인물 생김새(옷·머리·안경 …)가 빠졌는지 — 의견이 그것을 바꾸라는 게 아니면 지켜야 한다.
    # 2026-10-23: '그리퍼 모양을 바꿔'로 고쳐 쓰다 'cream silk blouse'가 빠져 여성이 흰 티셔츠로 바뀌었다.
    keep = [p for p in kept_phrases(shot_prompt) if not _changes_looks(notes, p)]
    missing = [p for p in keep if not _has_phrase(video, p) or (first and not _has_phrase(first, p))]
    if missing:  # 한 번 더, 지켜야 할 말을 짚어 주고
        v2, f2 = ask(user + "\nKeep these phrases exactly as written: " + "; ".join(missing))
        if v2:
            video, first = v2, f2
        video, first = _restore(video, keep), (_restore(first, keep) if first else first)
    return video, first


# ── 원래 묘사의 인물 생김새 지키기 ──
_LOOKS = re.compile(
    r"\b(?:in|wearing) (?:a |an )?[a-z\-]+(?: [a-z\-]+){0,3} "
    r"(?:blouse|shirt|t-shirt|hoodie|jacket|coat|dress|sweater|cardigan|suit|uniform|vest|apron|lab coat)\b"
    r"|\bwith (?:long |short |curly |straight |wavy )?(?:[a-z\-]+ )?(?:black|brown|blonde|gray|grey|red|white) hair\b"
    r"|\b(?:wearing|with) (?:round |black |thin )?glasses\b", re.I)
_LOOKS_NOTE = re.compile(r"옷|셔츠|블라우스|후드|재킷|자켓|코트|원피스|드레스|스웨터|가디건|정장|유니폼|조끼|앞치마|가운|"
                         r"머리|헤어|안경|입[혀히고은]|outfit|cloth|shirt|hair|glasses", re.I)


def kept_phrases(prompt):
    """원래 묘사에서 지켜야 할 인물 생김새 표현 ('in a cream silk blouse', 'with long black hair' …)."""
    return list(dict.fromkeys(m.group(0).strip() for m in _LOOKS.finditer(prompt or "")))


def _head(phrase):
    return phrase.lower().split()[-1]


def _has_phrase(text, phrase):
    """표현의 핵심 낱말(옷 종류·머리색 …)이 남아 있는지 — 'in a cream silk blouse'면 'blouse'와 'cream'."""
    low = (text or "").lower()
    words = [w for w in re.findall(r"[a-z\-]+", phrase.lower()) if w not in ("in", "a", "an", "with", "wearing")]
    return all(w in low for w in words[-2:])


def _changes_looks(notes, phrase):
    """의견이 생김새(옷·머리·안경)를 바꾸라는 것이면 그 표현은 지키지 않아도 된다."""
    return any(_LOOKS_NOTE.search(n or "") for n in notes)


_PERSON = re.compile(r"\b(?:woman|man|girl|boy|student|person|engineer)\b", re.I)


def _restore(text, keep):
    """그래도 빠진 생김새 표현은 사람을 가리키는 첫 낱말 바로 뒤에 되살린다 ('woman' → 'woman with long black hair
    in a cream silk blouse'). 끝에 붙이면 엉뚱한 물건에 붙는다('cube with long black hair'). 사람 낱말이 없으면 끝에."""
    lost = [p for p in keep if not _has_phrase(text, p)]
    if not lost:
        return text
    m = _PERSON.search(text)
    if m:
        return text[:m.end()] + " " + " ".join(lost) + text[m.end():]
    return text.rstrip(" .") + ", " + ", ".join(re.sub(r"^in ", "wearing ", p, flags=re.I) for p in lost)


# ── 모호한 의견 안내: 무엇으로 바꿀지, 어떻게 보이면 좋을지가 없으면 모델이 임의로 정한다 ──
_VAGUE_CHANGE = re.compile(r"바꿔|바꾸|바꿨|변경|다르게")
# '작게 바꿔', '밝게 바꿔'처럼 '~게'도 방향이다 (2026-10-23 '큐브를 … 작게 바꿔줘'가 모호하다고 걸렸다)
_CHANGE_TARGET = re.compile(r"(?:으로|로|게)\s*(?:바꿔|바꾸|바꿨|변경|해|만들)|처럼|같이|같은|대신")
_VAGUE_BAD = re.compile(r"이상해|이상하|별로|어색|마음에 안|맘에 안|구려|이상함")


def vague_reason(note):
    """의견이 모호하면 고쳐 쓸 방향을 묻는 안내 문장, 아니면 ''.
    2026-10-23: '로봇의 그리퍼 모양을 바꿔' → 모델이 '두 손가락 집게'를 임의로 정했다."""
    note = (note or "").strip()
    if not note or not re.search(r"[가-힣]", note) or is_length_only(note):
        return ""
    if _VAGUE_CHANGE.search(note) and not _CHANGE_TARGET.search(note):
        return "무엇으로 바꿀지 적으면 더 정확합니다. 예: '그리퍼를 두 손가락 집게로 바꿔', '배경을 파란 벽으로 바꿔'"
    if _VAGUE_BAD.search(note) and len(re.sub(r"\s", "", note)) < 15:
        return "어떻게 보이면 좋을지 적으면 더 정확합니다. 예: '얼굴을 자연스럽게, 정면을 보게', '팔이 천천히 내려오게'"
    return ""


def revise_shot_prompt(shot_prompt, notes, config, client=None):
    """revise_shot의 영상 묘사만 (화면 의견이 없으면 '', 실패하면 None)."""
    out = revise_shot(shot_prompt, notes, config, client=client)
    return out if out is None else out[0]


def to_visual_instruction(note, config, client=None, shot_prompt=""):
    """한국어 의견 → 영어 영상 지시문. 화면과 무관하면 ''. 변환할 수단이 없으면 None (원문을 넣지 않는다)."""
    from . import comfy_text, llm

    note = (note or "").strip()
    if not note:
        return ""
    if not re.search(r"[가-힣]", note):  # 이미 영어
        return note
    if suggest(None, note) not in REGEN_TARGETS:  # 소리·편집·내용 의견은 작은 모델에 맡기지 않고 바로 뺀다
        return ""
    user =f"Shot: {shot_prompt}\nFeedback: {note}" if shot_prompt else f"Feedback: {note}"
    text = None
    c = client or comfy_text.client(config)
    if c is not None:
        try:
            text = comfy_text.generate(c, config, _NOTE_SYSTEM, user, max_length=80)
        except Exception:
            text = None
    if text is None and (config.get("ANTHROPIC_API_KEY") or config.get("OLLAMA_MODEL")):
        text, _source = llm.generate(_NOTE_SYSTEM, user, config, comfy=False)
    if text is None:
        return None
    text = " ".join(text.split()).strip(" .\"'")
    if not text or text.upper().startswith("NONE") or re.search(r"[가-힣]", text):
        return ""
    return text

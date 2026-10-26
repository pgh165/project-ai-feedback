"""[팀 직접 구현] 편집 설정 — 장면을 다시 만들지 않고 올릴 영상(러프컷)만 바꾸는 피드백의 반영.

반영 위치가 편집(edit)·소리(audio)인 의견은 장면 영상은 그대로 두고, 러프컷을 합성할 때의 설정으로 반영한다.

  transition / transition_sec   장면 사이 전환: cut(바로) · fade(검은 화면을 거쳐) · dissolve(겹치며)
  sound_gain                    영상에 들어 있는 소리(효과음·배경음) 크기 배율
  narration_gain / tempo        내레이션 크기 배율 / 말 빠르기 배율
  subtitles / size / position   내레이션 문장을 자막으로 입히기, 크기(small·medium·large), 위치(bottom·top)
  order                         장면 순서 (비어 있으면 스토리보드 순서)
  shots[sid].keep / speed       장면별 남길 길이 비율(끝을 잘라 냄) / 재생 속도 배율

흐름: N차 검토 화면에서 승인된 편집·소리 의견 → 키워드 규칙으로 설정 변경을 제안(suggest_changes)
      → 제작자가 검토 화면에서 고칠 수 있음(VideoRound.next_edit_json) → 반영하면 N+1차의 edit_json
      → 스토리보드 스냅샷 storyboard["edit"] → rough_cut.build(edit=...).
장면 길이·순서가 바뀌므로 타임스탬프 → 장면 매핑도 timeline()으로 계산한다.
"""
import copy
import json
import re

TRANSITIONS = {"cut": "바로 넘김", "fade": "검은 화면으로 페이드", "dissolve": "겹치며 넘김(디졸브)"}
SUBTITLE_SIZES = {"small": "작게", "medium": "보통", "large": "크게"}
SUBTITLE_POSITIONS = {"bottom": "아래", "top": "위"}

DEFAULTS = {
    # 기본은 디졸브 — 장면마다 따로 만든 컷을 바로 붙이면 구도·색이 툭 바뀌어 어색했다
    "transition": "dissolve", "transition_sec": 0.4,
    "sound_gain": 1.0, "narration_gain": 1.0, "narration_tempo": 1.0,
    "scene_sound": True,  # 영상 모델(LTX)이 컷과 함께 만든 장면 소리·효과음을 쓸지. 끄면 컷 파일에서도 소리를 뺀다
    "subtitles": False, "subtitle_size": "medium", "subtitle_position": "bottom",
    "order": [], "shots": {},
    "subtitle_times": {},  # 타임라인에서 정한 장면별 자막 구간 {sid: [시작, 끝]} 초(올릴 영상 기준). 없으면 말하는 구간에 맞춘다
    "music": "",  # 다음 버전 배경음악 바꾸기: "" / on(넣기) / reroll(다른 곡으로) / off(빼기) — 적용 후 지운다 (bgm.py)
}
MUSIC_CHANGES = {"on": "배경음악 넣기", "reroll": "배경음악을 다른 곡으로", "off": "배경음악 빼기"}

_LIMITS = {
    "transition_sec": (0.2, 1.5), "sound_gain": (0.0, 2.5), "narration_gain": (0.3, 2.5),
    "narration_tempo": (0.7, 1.4), "keep": (0.3, 1.0), "speed": (0.5, 2.0),
}


def _clamp(key, value):
    low, high = _LIMITS[key]
    return round(min(max(float(value), low), high), 3)


def normalize(plan, shot_ids=None):
    """빠진 값은 기본값으로, 범위를 벗어난 값은 잘라서 돌려준다. shot_ids가 있으면 없는 장면을 걸러 낸다."""
    src = plan or {}
    out = copy.deepcopy(DEFAULTS)
    if src.get("transition") in TRANSITIONS:
        out["transition"] = src["transition"]
    for key in ("transition_sec", "sound_gain", "narration_gain", "narration_tempo"):
        try:
            out[key] = _clamp(key, src.get(key, out[key]))
        except (TypeError, ValueError):
            pass
    out["subtitles"] = bool(src.get("subtitles"))
    out["scene_sound"] = bool(src.get("scene_sound", True))
    if src.get("subtitle_size") in SUBTITLE_SIZES:
        out["subtitle_size"] = src["subtitle_size"]
    if src.get("subtitle_position") in SUBTITLE_POSITIONS:
        out["subtitle_position"] = src["subtitle_position"]
    if src.get("music") in MUSIC_CHANGES:
        out["music"] = src["music"]
    known = set(shot_ids) if shot_ids is not None else None
    for sid, span in (src.get("subtitle_times") or {}).items():
        if known is not None and sid not in known:
            continue
        try:
            a, b = round(max(float(span[0]), 0.0), 2), round(float(span[1]), 2)
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        if b - a >= 0.3:
            out["subtitle_times"][sid] = [a, b]
    order = [s for s in (src.get("order") or []) if isinstance(s, str) and (known is None or s in known)]
    out["order"] = list(dict.fromkeys(order))
    for sid, values in (src.get("shots") or {}).items():
        if known is not None and sid not in known or not isinstance(values, dict):
            continue
        entry = {}
        for key in ("keep", "speed"):
            try:
                v = _clamp(key, values.get(key, 1.0))
            except (TypeError, ValueError):
                continue
            if abs(v - 1.0) > 1e-3:
                entry[key] = v
        if entry:
            out["shots"][sid] = entry
    return out


def loads(text, shot_ids=None):
    try:
        return normalize(json.loads(text) if text else {}, shot_ids)
    except ValueError:
        return normalize({}, shot_ids)


def dumps(plan):
    return json.dumps(plan, ensure_ascii=False, sort_keys=True)


def is_default(plan):
    return normalize(plan) == normalize({})


# ── 의견 → 설정 변경 제안 (키워드 규칙, 오프라인) ─────────────────────────
_MUSIC = re.compile(r"음악|배경음|bgm|효과음|소리|사운드|music|sound|sfx", re.I)
_VOICE = re.compile(r"내레이션|나레이션|해설|목소리|음성|더빙|성우|voice|narrat", re.I)
# 크기·빠르기는 '해 달라는 말'(더 크게)과 '상태를 말한 것'(너무 커요)의 방향이 반대라 요청을 먼저 본다
_WANT_UP = re.compile(r"크게 해|더 크게|키워|올려|높여|louder|turn (it )?up", re.I)
_WANT_DOWN = re.compile(r"작게 해|더 작게|줄여|낮춰|quieter|turn (it )?down", re.I)
_IS_LOUD = re.compile(r"너무 크|커서|커요|크다|크네|시끄|요란|귀가 아|빵빵|too loud|loud", re.I)
_IS_QUIET = re.compile(r"너무 작|작아|작다|작네|안 ?들|들리지|약해|묻[혀힌히]|too quiet|quiet|hard to hear|can't hear", re.I)
_DROWN = re.compile(r"묻[혀힌히]|덮[어여]|drown", re.I)
_WANT_SLOWER = re.compile(r"천천히|느리게 해|더 느리게|slower|slow down", re.I)
_WANT_FASTER = re.compile(r"빠르게 해|더 빠르게|빨리 해|속도를 (올|높)|faster|speed up", re.I)
_IS_FAST = re.compile(r"빠르|빨라|급[하해]|too fast|rush", re.I)
_IS_SLOW = re.compile(r"느리|느려|답답|처[져지]|too slow", re.I)
_TRANSITION = re.compile(r"전환|트랜지션|장면이 바뀔|화면이 바뀔|컷이|transition|\bcuts?\b", re.I)
_ABRUPT = re.compile(r"갑작|뚝|끊[겨기어]|툭|부자연|어색|튀[는어]|abrupt|jarring|choppy", re.I)
_FADE = re.compile(r"페이드|암전|fade", re.I)
_DISSOLVE = re.compile(r"디졸브|겹치|부드럽|자연스럽|dissolve|smooth", re.I)
_TOO_LONG = re.compile(r"너무 길|길어|지루|늘어[지져]|루즈|질질|too long|drag|boring", re.I)
_TOO_SHORT = re.compile(r"너무 짧|짧아|빨리 지나|빨리 넘어|휙|금방 지나|금방 넘어|순식간|too short|too fast|blink", re.I)
_SUBTITLE = re.compile(r"자막|캡션|글씨|subtitle|caption", re.I)
_ORDER = re.compile(r"순서|먼저 나|뒤로 빼|앞으로 (옮|빼)|order", re.I)
_BGM = re.compile(r"음악|배경음|bgm|music", re.I)
_BGM_MISMATCH = re.compile(r"안 ?맞|어울리지|어색|별로|다른 (음악|곡|노래)|바꿔|분위기|촌스|doesn't fit|different", re.I)
_BGM_WANT = re.compile(r"없어서|없네|없어요|넣어|있으면|심심|허전|add music|no music", re.I)
_BGM_REMOVE = re.compile(r"음악(을|은)? ?빼|음악 없이|without music|remove (the )?music", re.I)


def _direction(text, want_up, want_down, is_up, is_down):
    """+1(올리기) / -1(내리기) / 0. is_up = '너무 크다·너무 빠르다'처럼 이미 높아서 내려야 하는 상태."""
    if want_up.search(text):
        return 1
    if want_down.search(text):
        return -1
    if is_up.search(text):
        return -1
    if is_down.search(text):
        return 1
    return 0


def _change(key, op, value, label, shots=None):
    return {"key": key, "op": op, "value": value, "label": label, "shots": shots or []}


def suggest_changes(cluster_text, target, shot_ids, all_shot_ids):
    """의견 문장 1개 → 편집 설정 변경 목록. 편집·소리 의견이 아니면 [].
    shot_ids: 의견이 가리키는 장면 (장면별 길이·속도 변경에 쓴다)."""
    if target not in ("edit", "audio"):
        return []
    text = cluster_text or ""
    shots = [s for s in shot_ids if s in all_shot_ids] or list(all_shot_ids)
    where = "모든 장면" if len(shots) == len(all_shot_ids) else ", ".join(str(int(s[1:])) + "번" for s in shots)
    changes = []

    voice, music, subtitle = _VOICE.search(text), _MUSIC.search(text), _SUBTITLE.search(text)
    volume = _direction(text, _WANT_UP, _WANT_DOWN, _IS_LOUD, _IS_QUIET)
    if _BGM.search(text) and not voice:  # 배경음악 자체에 대한 의견: 넣기·빼기·다른 곡으로
        if _BGM_REMOVE.search(text):
            changes.append(_change("music", "set", "off", MUSIC_CHANGES["off"]))
        elif _BGM_WANT.search(text):
            changes.append(_change("music", "set", "on", MUSIC_CHANGES["on"]))
        elif _BGM_MISMATCH.search(text) and volume == 0:
            changes.append(_change("music", "set", "reroll", MUSIC_CHANGES["reroll"]))
    if voice and music and not _WANT_UP.search(text):  # "음악에 목소리가 묻혀요", "음악이 커서 해설이 안 들려요"
        changes.append(_change("narration_gain", "mul", 1.3, "내레이션 크게 (+30%)"))
        changes.append(_change("sound_gain", "mul", 0.7, "배경 소리 작게 (−30%)"))
    elif voice:
        if volume > 0:
            changes.append(_change("narration_gain", "mul", 1.3, "내레이션 크게 (+30%)"))
        elif volume < 0:
            changes.append(_change("narration_gain", "mul", 0.8, "내레이션 작게 (−20%)"))
        tempo = _direction(text, _WANT_FASTER, _WANT_SLOWER, _IS_FAST, _IS_SLOW)
        if tempo < 0:
            changes.append(_change("narration_tempo", "mul", 0.88, "내레이션 천천히 (−12%)"))
        elif tempo > 0:
            changes.append(_change("narration_tempo", "mul", 1.12, "내레이션 빠르게 (+12%)"))
    elif music and not subtitle:
        if volume < 0:
            changes.append(_change("sound_gain", "mul", 0.7, "배경 소리 작게 (−30%)"))
        elif volume > 0:
            changes.append(_change("sound_gain", "mul", 1.3, "배경 소리 크게 (+30%)"))

    if subtitle:  # 자막은 YouTube 자막 트랙 — 크기·위치는 보는 사람이 YouTube에서 정하므로 제안하지 않는다
        changes.append(_change("subtitles", "set", True, "내레이션 문장을 YouTube 자막으로 올리기"))

    transition = None
    if _FADE.search(text):
        transition = ("fade", "장면 전환을 페이드로 (검은 화면을 거쳐)")
    elif _TRANSITION.search(text) or _DISSOLVE.search(text):
        if _ABRUPT.search(text) or _DISSOLVE.search(text):
            transition = ("dissolve", "장면 전환을 디졸브로 (겹치며 넘김)")
        elif _IS_SLOW.search(text) or _TOO_LONG.search(text):
            transition = ("cut", "장면 전환을 바로 넘기기로")
    if transition:
        changes.append(_change("transition", "set", transition[0], transition[1]))
    elif not voice and not music and not subtitle:
        if _TOO_SHORT.search(text) or _WANT_SLOWER.search(text):
            changes.append(_change("speed", "mul", 0.8, f"{where} 천천히 재생 (0.8배, 길이 늘어남)", shots))
        elif _TOO_LONG.search(text) or _IS_SLOW.search(text):
            changes.append(_change("keep", "mul", 0.8, f"{where} 길이 20% 줄이기 (끝을 잘라 냄)", shots))

    if _ORDER.search(text):
        changes.append(_change("order", "manual", None, "장면 순서 — 아래 편집 설정에서 직접 정하세요"))
    return changes


def apply_changes(plan, changes, shot_ids=None):
    """설정에 변경 목록을 적용한다 (곱하기 변경은 겹쳐서 누적된다)."""
    out = normalize(plan, shot_ids)
    for c in changes:
        key, op, value = c["key"], c["op"], c["value"]
        if op == "manual":
            continue
        if key in ("keep", "speed"):
            for sid in c.get("shots") or []:
                entry = out["shots"].setdefault(sid, {})
                entry[key] = _clamp(key, entry.get(key, 1.0) * value)
        elif op == "mul":
            out[key] = _clamp(key, out[key] * value)
        else:
            out[key] = value
    return normalize(out, shot_ids)


# ── 검토 화면 / 다음 버전 ──────────────────────────────────────────────────
def round_plan(round_obj):
    """이 버전의 러프컷에 쓰는 편집 설정."""
    sb = round_obj.storyboard_data
    ids = [s["id"] for s in sb["shots"]] if sb else None
    return loads(round_obj.edit_json or "", ids)


def cluster_changes(round_obj):
    """{cluster_id: [변경]} — 이 버전 검토 화면의 편집·소리 의견별 제안."""
    sb = round_obj.storyboard_data
    all_ids = [s["id"] for s in sb["shots"]] if sb else []
    return {c.id: suggest_changes(c.representative_text, c.target, c.affected_shots, all_ids)
            for c in round_obj.clusters}


def suggested_next_plan(round_obj):
    """이 버전 설정 + 승인된 편집·소리 의견의 제안 → 다음 버전 설정 (제작자가 고치기 전)."""
    sb = round_obj.storyboard_data
    ids = [s["id"] for s in sb["shots"]] if sb else None
    changes = cluster_changes(round_obj)
    approved = [ch for c in round_obj.clusters if c.status == "approved" for ch in changes.get(c.id, [])]
    return apply_changes(round_plan(round_obj), approved, ids)


def next_plan(round_obj):
    """다음 버전에 쓸 설정 — 제작자가 검토 화면에서 직접 고쳤으면 그 값, 아니면 자동 제안. (plan, manual)"""
    if round_obj.next_edit_json:
        sb = round_obj.storyboard_data
        return loads(round_obj.next_edit_json, [s["id"] for s in sb["shots"]] if sb else None), True
    return suggested_next_plan(round_obj), False


def from_form(form, shot_ids):
    """검토 화면 폼 → 설정. 퍼센트 입력은 배율로 바꾼다."""
    def pct(name, default=100.0):
        try:
            return float(form.get(name, default)) / 100.0
        except (TypeError, ValueError):
            return default / 100.0

    order_text = form.get("order", "")
    order = []
    for token in re.split(r"[\s,]+", order_text.strip()):
        if token.isdigit() and 1 <= int(token) <= len(shot_ids):
            order.append(shot_ids[int(token) - 1])
    if sorted(order) != sorted(shot_ids):  # 일부만 적었으면 적은 순서 뒤에 나머지를 원래 순서대로
        order = list(dict.fromkeys(order + list(shot_ids)))
    shots = {sid: {"keep": pct(f"keep_{sid}"), "speed": pct(f"speed_{sid}")} for sid in shot_ids}
    return normalize({
        "transition": form.get("transition"), "transition_sec": form.get("transition_sec", 0.5),
        "sound_gain": pct("sound_gain"), "narration_gain": pct("narration_gain"),
        "narration_tempo": pct("narration_tempo"),
        "subtitles": form.get("subtitles") == "on", "subtitle_size": form.get("subtitle_size"),
        "subtitle_position": form.get("subtitle_position"),
        "order": [] if order == list(shot_ids) else order, "shots": shots, "music": form.get("music", ""),
    }, shot_ids)


def describe(plan, storyboard=None):
    """기본값과 다른 설정을 사람이 읽는 문장 목록으로."""
    p = normalize(plan)
    lines = []
    if (p["transition"], p["transition_sec"]) != (DEFAULTS["transition"], DEFAULTS["transition_sec"]):
        lines.append(f"장면 전환: {TRANSITIONS[p['transition']]}"
                     + (f" {p['transition_sec']:g}초" if p["transition"] != "cut" else ""))
    for key, label in (("sound_gain", "배경 소리"), ("narration_gain", "내레이션 크기"),
                       ("narration_tempo", "내레이션 빠르기")):
        if abs(p[key] - 1.0) > 1e-3:
            lines.append(f"{label} {round(p[key] * 100)}%")
    if not p["scene_sound"]:
        lines.append("장면 소리(효과음) 끔")
    if p["subtitles"]:
        lines.append("자막: YouTube 자막으로 올림" + (" (시간 직접 지정)" if p["subtitle_times"] else ""))
    if p["music"]:
        lines.append(MUSIC_CHANGES[p["music"]])
    if p["order"]:
        ids = [s["id"] for s in storyboard["shots"]] if storyboard else p["order"]
        lines.append("장면 순서: " + " → ".join(str(ids.index(s) + 1) if s in ids else s for s in ordered_ids(ids, p)))
    for sid, v in sorted(p["shots"].items()):
        parts = []
        if "keep" in v:
            parts.append(f"길이 {round(v['keep'] * 100)}%")
        if "speed" in v:
            parts.append(f"재생 속도 {v['speed']:g}배")
        lines.append(f"{int(sid[1:])}번 장면: " + ", ".join(parts))
    return lines


# ── 시간 계산 ────────────────────────────────────────────────────────────
def ordered_ids(shot_ids, plan):
    order = [s for s in plan.get("order") or [] if s in shot_ids]
    return order + [s for s in shot_ids if s not in order]


def shot_length(duration, plan, shot_id):
    v = plan["shots"].get(shot_id, {})
    return float(duration) * v.get("keep", 1.0) / v.get("speed", 1.0)


def overlap_sec(plan, lengths):
    """디졸브는 장면이 겹치므로 경계마다 이만큼 짧아진다. 가장 짧은 장면의 절반을 넘지 않는다."""
    if plan["transition"] != "dissolve" or len(lengths) < 2:
        return 0.0
    return round(min(plan["transition_sec"], min(lengths) / 2), 3)


def timeline(storyboard):
    """[(shot_id, start, end)] — 편집 설정(순서·길이·속도·디졸브 겹침)을 반영한 올릴 영상 기준 시각."""
    plan = normalize(storyboard.get("edit"))
    by_id = {s["id"]: s for s in storyboard["shots"]}
    ids = ordered_ids(list(by_id), plan)
    lengths = [shot_length(by_id[i]["duration"], plan, i) for i in ids]
    overlap = overlap_sec(plan, lengths)
    ranges, cursor = [], 0.0
    for n, (sid, length) in enumerate(zip(ids, lengths)):
        # 겹치는 구간은 다음 장면 몫으로 친다 (그때부터 다음 장면이 보이기 시작한다)
        end = cursor + length - (overlap if n < len(ids) - 1 else 0.0)
        ranges.append((sid, cursor, end))
        cursor = end
    return ranges

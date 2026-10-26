"""[팀 직접 구현] 스토리보드 스키마 · 시간 계산 · 타임스탬프→장면 매핑.

프롬프트 문서(텍스트)가 원본이고, 영상 생성 시점에 장면 단위 스토리보드로 변환해
`VideoRound.storyboard_json`에 스냅샷으로 저장한다. 스냅샷이 있어야 라운드 사이에
"어느 장면이 바뀌었는지"를 비교하고, 피드백 타임스탬프를 장면에 연결할 수 있다.

    {"style": "...", "negative": "...",
     "defaults": {"workflow", "width", "height", "fps", "seed", "generator"},
     "shots": [{"id": "s01", "prompt": "...", "duration": 5.0, "generator": "local",
                "seed": 42, "notes": [], "image": null}]}
"""
import json
import math

from .prompt_builder import (extract_narration, extract_shot_durations, extract_shot_seeds, extract_shots, extract_style,
                             extract_subtitles)

GENERATORS = ("local", "api")

# 화면 비율. 실제 크기는 설정(STORYBOARD_WIDTH/HEIGHT)의 짧은 변·긴 변에서 만든다.
ASPECTS = ("9:16", "16:9", "1:1")


def _round_mult(value, mult):
    return max(mult, int(round(value / mult)) * mult)


def frames_for(duration_sec, fps):
    """duration × fps 를 8n+1 프레임으로 올림한다 (LTX 요구 조건)."""
    raw = max(1, math.ceil(duration_sec * fps))
    n = max(1, math.ceil((raw - 1) / 8))
    return n * 8 + 1


def dimensions(aspect, config):
    """화면 비율 → (너비, 높이). 설정의 짧은 변·긴 변을 비율에 맞게 배치한다."""
    w, h = int(config["STORYBOARD_WIDTH"]), int(config["STORYBOARD_HEIGHT"])
    short, long_ = min(w, h), max(w, h)
    if aspect == "16:9":
        return long_, short
    if aspect == "1:1":
        return short, short
    if aspect == "9:16":
        return short, long_
    return w, h


def build_storyboard(prompt_text, config, aspect=None, shot_seconds=None):
    """프롬프트 문서 → 스토리보드 dict. aspect/shot_seconds가 없으면 설정 기본값."""
    width, height = dimensions(aspect, config)
    seconds = float(shot_seconds or config["STORYBOARD_SHOT_SECONDS"])
    limit = config["LOCAL_VIDEO_MAX_SHOTS"]
    shots_text = extract_shots(prompt_text)[:limit]
    durations = extract_shot_durations(prompt_text)[:limit]  # 샷 줄의 "[2초]" 지정, 없으면 기본 길이
    seeds = extract_shot_seeds(prompt_text)[:limit]  # "[6초 시드 202]" 지정, 없으면 기본 시드
    default_generator = "local"
    narration = extract_narration(prompt_text)
    subtitles = extract_subtitles(prompt_text)  # 불러온 영상의 .srt (소리가 있는 영상일 때)
    shots = [
        {"id": f"s{i:02d}", "prompt": text, "duration": float(durations[i - 1] or seconds),
         "generator": default_generator,
         "seed": int(seeds[i - 1] if seeds[i - 1] is not None else config["STORYBOARD_SEED"]),
         "notes": [], "image": None, "narration": narration.get(i, ""),
         **({"subtitle": subtitles[i]} if subtitles.get(i) else {})}
        for i, text in enumerate(shots_text, start=1)
    ]
    return {
        "style": extract_style(prompt_text),
        "negative": config["LOCAL_VIDEO_NEGATIVE_PROMPT"],
        "defaults": {
            "width": width, "height": height,
            "fps": int(config["STORYBOARD_FPS"]), "seed": int(config["STORYBOARD_SEED"]),
            "generator": default_generator,
        },
        "shots": shots,
    }


def dumps(storyboard):
    return json.dumps(storyboard, ensure_ascii=False, indent=2, sort_keys=True)


def loads(text):
    return json.loads(text) if text else None


def shot_ranges(storyboard):
    """[(shot_id, start_sec, end_sec)] — duration 누적으로 계산(저장하지 않음).
    편집 설정(storyboard["edit"]: 순서·길이·디졸브)이 있으면 올릴 영상 기준 시각으로 계산한다."""
    if storyboard.get("edit"):
        from .edit_plan import timeline
        return timeline(storyboard)
    ranges, cursor = [], 0.0
    for shot in storyboard["shots"]:
        end = cursor + float(shot["duration"])
        ranges.append((shot["id"], cursor, end))
        cursor = end
    return ranges


def total_duration(storyboard):
    if storyboard.get("edit"):
        ranges = shot_ranges(storyboard)
        return ranges[-1][2] if ranges else 0.0
    return sum(float(s["duration"]) for s in storyboard["shots"])


def shot_at(storyboard, seconds):
    """타임스탬프(초) → 장면 ID. 영상 길이를 벗어나면 None."""
    if seconds is None or seconds < 0:
        return None
    for shot_id, start, end in shot_ranges(storyboard):
        if start <= seconds < end:
            return shot_id
    return None


def get_shot(storyboard, shot_id):
    return next((s for s in storyboard["shots"] if s["id"] == shot_id), None)


def final_prompt(storyboard, shot, append_notes=True, first_frame=False):
    """영상 모델에 보내는 최종 프롬프트 = 장면 + 스타일. 의견이 있으면 장면 대신 의견대로 고쳐 쓴 prompt_fixed,
    고쳐 쓰기에 실패했으면 장면 + 영어 지시문(notes_en).
    first_frame=True(이미지 모델)면 동작이 시작되기 전 모습 image_prompt_fixed가 있을 때 그것을 쓴다.
    한국어 의견 원문(notes)은 넣지 않는다 — 이미지 모델이 화면에 글자로 그려 버린다.
    notes_en은 orchestrator.ensure_english_notes가 채우고, 화면과 무관한 의견은 빈 문자열이라 빠진다."""
    if append_notes and first_frame and shot.get("prompt_fixed") and shot.get("image_prompt_fixed"):
        parts = [shot["image_prompt_fixed"].rstrip(" .")]
    elif append_notes and shot.get("prompt_fixed"):  # 의견대로 고쳐 쓴 장면 묘사 (notes_en은 붙이지 않는다)
        parts = [shot["prompt_fixed"].rstrip(" .")]
    else:
        parts = [shot["prompt"].rstrip(" .")]
        if append_notes:
            parts.extend(n for n in (shot.get("notes_en") or []) if n)
    parts.append(storyboard["style"])
    return ", ".join(p for p in parts if p)


def resolve_params(storyboard, shot, config):
    """장면 1개의 실제 생성 파라미터 (해상도·프레임 보정, 시험 모드 적용)."""
    defaults = storyboard["defaults"]
    width, height = int(defaults["width"]), int(defaults["height"])
    fps = int(defaults["fps"])
    duration = float(shot["duration"])
    frames = frames_for(duration, fps)
    if config.get("COMFYUI_TEST_MODE"):
        width, height = config["COMFYUI_TEST_WIDTH"], config["COMFYUI_TEST_HEIGHT"]
        frames = config["COMFYUI_TEST_FRAMES"]
    mult = int(config["COMFYUI_SIZE_MULTIPLE"])
    width, height = _round_mult(width, mult), _round_mult(height, mult)
    frames = max(9, int(round((frames - 1) / 8)) * 8 + 1)
    # 이미지가 없는 장면도 '이미지 먼저'면 어댑터가 첫 프레임을 만들어 I2V로 보낸다 → 기록되는 워크플로는 I2V
    image_first = bool(config.get("COMFYUI_IMAGE_FIRST")) and not shot.get("image")
    workflow = (config["COMFYUI_WORKFLOW_I2V"] if shot.get("image") or image_first
                else config["COMFYUI_WORKFLOW_T2V"])
    if shot.get("image") and shot.get("end_image"):  # 처음·끝 프레임 고정 ('이 순간을 끝으로 다시 만들기')
        workflow = config.get("COMFYUI_WORKFLOW_FLF", "ltx25_flf2v")
    return {
        "prompt": final_prompt(storyboard, shot, config.get("STORYBOARD_APPEND_NOTES", True)),
        "image_prompt": final_prompt(storyboard, shot, config.get("STORYBOARD_APPEND_NOTES", True), first_frame=True),
        "negative": storyboard["negative"],
        "width": width, "height": height, "frames": frames, "fps": fps,
        "seed": int(shot.get("seed", defaults["seed"])),
        "workflow": workflow, "duration": duration, "image": shot.get("image"),
        "end_image": shot.get("end_image") if shot.get("image") else None,
        "image_first": image_first,
    }


def shot_signature(storyboard, shot):
    """라운드 간 '같은 장면인가' 비교용 — 프롬프트·스타일·해상도·길이가 같으면 같은 장면."""
    d = storyboard["defaults"]
    return (shot["prompt"].strip(), storyboard["style"].strip(), float(shot["duration"]),
            int(d["width"]), int(d["height"]), int(d["fps"]), shot.get("image") or "")

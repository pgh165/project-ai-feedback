"""[팀 직접 구현] 배경음악 — 영상 전체 분위기에 맞춘 연주곡 한 곡 (MiniMax Music 3, pipeline/music.py).

장면별 효과음은 어색해서 쓰지 않는다. 소리는 장면 소리(LTX) + 내레이션(TTS) + 배경음악 한 곡이다.

  곡 설명(caption)  [영상 종류]·[영상 설명]·[영상 스타일]로 LLM(Claude → Ollama → ComfyUI Gemma)이 제안, 없으면 종류별 템플릿.
                    제작자가 고칠 수 있다. 항상 연주곡(보컬 없음)으로 만든다.
  곡 파일           (설명·길이·시드)가 같으면 다시 만들지 않는다. 다음 버전에도 그대로 이어지고,
                    '다른 곡으로'(피드백 또는 버튼)일 때만 시드를 바꿔 새로 만든다.
  섞기              rough_cut.build — 내레이션이 나오면 음악을 낮추고(sidechaincompress), 처음·끝은 페이드.
설정은 VideoRound.music_json에 둔다. 라이선스: MiniMax-Music3 Community License — 상업 이용 시 'MiniMax-Music3' 표기.
"""
import copy
import hashlib
import json
import random
import re
import subprocess
from pathlib import Path

DEFAULTS = {"enabled": False, "caption": "", "seed": 0, "volume": 0.5, "file": "", "key": "", "seconds": 0.0,
            "offset": 0.0}  # offset: 곡에서 쓰기 시작하는 지점(초) — 소리 크기가 고른 구간
CREDIT = "배경음악: MiniMax-Music3로 생성"

_SYSTEM = (
    "You write prompts for an instrumental background-music generator. Given a short video's type, description and "
    "visual style, write ONE English music description of 20-40 words: genre, mood, tempo (BPM), main instruments, "
    "and how it builds. It must be background music under a voice-over: steady, not busy, no vocals, no lyrics, "
    "no sound effects. Output only the description."
)
_TEMPLATES = [  # LLM이 없을 때: 영상 종류 키워드 → 곡 설명
    (r"홍보|소개|학과|학교|대학|기업|회사|제품|기술|tech|promo",
     "modern uplifting corporate tech background music, 110 BPM, soft electronic pulses, warm synth pads, light "
     "piano, clean steady beat that builds gently, optimistic and confident, instrumental, no vocals"),
    (r"뮤직비디오|music", "cinematic modern pop instrumental, 100 BPM, punchy drums, bright synths, emotional build, no vocals"),
    (r"단편|드라마|영화|이야기|short film",
     "cinematic emotional underscore, 80 BPM, soft strings, ambient piano, slow swells, gentle and reflective, no vocals"),
    (r"요리|여행|일상|브이로그|vlog",
     "light acoustic background music, 105 BPM, ukulele, soft claps, warm and cheerful, no vocals"),
]
_DEFAULT_CAPTION = ("calm modern background music, 100 BPM, soft synth pads, light piano, gentle steady rhythm, "
                    "warm and positive, instrumental, no vocals")


def normalize(data):
    out = copy.deepcopy(DEFAULTS)
    src = data or {}
    out["enabled"] = bool(src.get("enabled"))
    out["caption"] = " ".join(str(src.get("caption") or "").split())[:500]
    out["file"] = str(src.get("file") or "")
    out["key"] = str(src.get("key") or "")
    for key in ("seconds", "offset"):
        try:
            out[key] = round(max(float(src.get(key) or 0.0), 0.0), 2)
        except (TypeError, ValueError):
            out[key] = 0.0
    try:
        out["seed"] = int(src.get("seed") or 0)
    except (TypeError, ValueError):
        out["seed"] = 0
    try:
        out["volume"] = round(min(max(float(src.get("volume", out["volume"])), 0.05), 1.5), 3)
    except (TypeError, ValueError):
        pass
    return out


def load(round_obj, config=None):
    """이 버전의 배경음악 설정. 한 번도 정하지 않은 버전이면 BGM_AUTO(기본 켬)에 따라 켠 상태로 본다 —
    확인 단계에서 러프컷을 만들 때 곡 설명을 제안받아 곡을 만든다. 제작자가 끄면 그 값이 저장되어 유지된다."""
    try:
        data = json.loads(round_obj.music_json) if round_obj.music_json else {}
    except ValueError:
        data = {}
    if not round_obj.music_json and config is not None and config.get("BGM_AUTO", True):
        data = {"enabled": True}
    return normalize(data)


def save(round_obj, settings):
    round_obj.music_json = json.dumps(normalize(settings), ensure_ascii=False)


def template_caption(genre, description=""):
    text = f"{genre} {description}"
    for pattern, caption in _TEMPLATES:
        if re.search(pattern, text, re.I):
            return caption
    return _DEFAULT_CAPTION


def suggest_caption(prompt_text, config, seconds=None, allow_llm=True):
    """프롬프트 문서 → (곡 설명, source). LLM 결과가 이상하면 템플릿."""
    from . import llm
    from .prompt_builder import parse_sections

    sections = parse_sections(prompt_text or "")
    genre = sections.get("영상 종류", "").strip()
    description = sections.get("영상 설명", "").strip()
    style = sections.get("영상 스타일", "").strip()
    fallback = template_caption(genre, description)
    if not allow_llm:
        return fallback, "template"
    user = (f"Video type: {genre or 'promo'}\nDescription (Korean): {description}\nVisual style: {style}\n"
            f"Length: about {int(seconds or 30)} seconds")
    text, source = llm.generate(_SYSTEM, user, config, max_length=120)
    text = " ".join((text or "").split()).strip("\"' ").rstrip(".")
    # 영어 문장이고 너무 짧거나 길지 않을 때만 쓴다 (한글이 섞이면 음악 모델이 알아듣지 못한다)
    if not text or re.search(r"[가-힣]", text) or not 8 <= len(text.split()) <= 70:
        return fallback, "template"
    if not re.search(r"no vocals|instrumental", text, re.I):
        text += ", instrumental, no vocals"
    return text, source


def track_key(caption, seed):
    raw = f"{caption}|{int(seed)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]


def ensure_track(config, settings, seconds, out_dir, progress=None):
    """설정에 맞는 곡 파일 경로. 같은 (설명·시드)로 만든 곡이 영상보다 길면 그대로 쓰고(잘라 씀), 아니면 새로 만든다.
    settings를 고쳐서 돌려준다 (seed·file·key). ComfyUI가 꺼져 있거나 실패하면 RuntimeError."""
    from . import music

    settings = normalize(settings)
    if not settings["caption"]:
        raise RuntimeError("배경음악 설명이 비어 있습니다.")
    if not settings["seed"]:
        settings["seed"] = random.randint(1, 2**31 - 1)
    seconds = max(8.0, float(seconds) + 1.5)  # 끝 페이드 여유
    key = track_key(settings["caption"], settings["seed"])
    if (settings["file"] and settings["key"] == key and Path(settings["file"]).exists()
            and settings["seconds"] >= seconds - 1.0):  # 버전이 조금 길어져도 끝 페이드 여유 안이면 같은 곡
        out = Path(settings["file"])
    else:
        length = max(seconds, float(config.get("MUSIC_MIN_SECONDS", 60)))  # 길게 만들어 고른 구간을 골라 쓴다
        out = Path(out_dir) / f"bgm_{key}_{int(round(length))}s.flac"
        if not out.exists():
            music.generate(config, settings["caption"], length, out, seed=settings["seed"], progress=progress)
        settings["file"], settings["key"], settings["seconds"] = str(out), key, round(length, 2)
    settings["offset"] = steady_offset(config, out, seconds)
    return out, settings


def _levels(ffmpeg, path, step):
    """step초마다 소리 크기(dB RMS). 무음은 -90."""
    proc = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path), "-af",
                           f"aresample=48000,asetnsamples={int(48000 * step)},astats=metadata=1:reset=1,"
                           "ametadata=print:key=lavfi.astats.Overall.RMS_level", "-f", "null", "-"],
                          capture_output=True, text=True, errors="replace")
    out = []
    for m in re.finditer(r"RMS_level=(-?[\d.]+|-?inf)", proc.stderr):
        out.append(-90.0 if "inf" in m.group(1) else max(float(m.group(1)), -90.0))
    return out


def steady_offset(config, path, need, step=0.5):
    """곡에서 need초 동안 소리 크기가 가장 고른 구간의 시작(초).
    도입부(앞 2초)·끝맺음(뒤 2초)은 될 수 있으면 피하고, 크기가 들쭉날쭉하거나 거의 무음인 곳이 있는 구간은 감점한다.
    곡이 영상보다 짧거나 길이를 잴 수 없으면 0."""
    from .video_generator.comfyui import rough_cut

    ffmpeg = rough_cut.resolve_ffmpeg(config)
    levels = _levels(ffmpeg, path, step) if ffmpeg else []
    win = int(round(need / step))
    if len(levels) <= win:
        return 0.0
    loud = sorted(levels)[len(levels) // 2:]  # 곡 본체(큰 쪽 절반)의 크기를 기준으로
    body = sum(loud) / len(loud)
    edge = int(2.0 / step)
    first, last = (edge, len(levels) - win - edge) if len(levels) - win - 2 * edge >= 0 else (0, len(levels) - win)
    best, best_score = first, None
    for start in range(first, last + 1):
        part = levels[start:start + win]
        mean = sum(part) / win
        spread = (sum((v - mean) ** 2 for v in part) / win) ** 0.5
        dips = sum(1 for v in part if v < body - 12)  # 본체보다 12dB 넘게 작은 곳 = 끊긴 듯 조용한 곳
        score = spread + 30.0 * dips / win + 0.3 * max(body - mean, 0)  # 들쭉날쭉 + 끊김 비율 + 본체보다 작음
        if best_score is None or score < best_score:
            best, best_score = start, score
    return round(best * step, 2)


def describe(settings):
    s = normalize(settings)
    if not s["enabled"]:
        return ""
    return f"배경음악 {round(s['volume'] * 100)}%" + ("" if s["file"] else " (만들 예정)")

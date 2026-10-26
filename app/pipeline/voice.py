"""[팀 직접 구현] 내레이션 목소리 설정 — 프로젝트 공통(PipelineSession.voice_json), 버전이 바뀌어도 같은 목소리.

  enabled    내레이션(TTS)을 올릴 영상에 넣을지. 끄면 [내레이션] 문장은 자막으로만 쓸 수 있다.
  speaker    "<엔진>:<목소리>" — 엔진과 목소리를 한 목록에서 고른다. 비우면 설정(VOICE_ADAPTER + 엔진별 기본 목소리).
               edge        Microsoft 신경망 음성 (계정 불필요, 인터넷 필요) — 한국어가 가장 자연스러워 기본
               supertonic  Supertonic-3 (로컬 CPU, 오프라인)
               qwen        Qwen3-TTS (로컬 GPU) — 한국어 억양이 어색해 기본에서 뺐다
  tone / instruct  말투 지시문 — Qwen3-TTS만 따른다. 영어로 줄 때 가장 잘 따른다.
크기·빠르기는 버전별 편집 설정(edit_plan narration_gain / narration_tempo)이다.
목소리를 바꾸면 narration._cache_key가 달라져 음성만 다시 합성된다 (장면은 그대로).
"""
import copy
import hashlib
import json

# "<엔진>:<목소리>" → (화면 이름, 설명). 위에서부터 추천 순서.
SPEAKERS = {
    "edge:ko-KR-SunHiNeural": ("선희", "여성 · 또렷하고 밝음 · Edge(인터넷)"),
    "edge:ko-KR-InJoonNeural": ("인준", "남성 · 차분함 · Edge(인터넷)"),
    "edge:ko-KR-HyunsuMultilingualNeural": ("현수", "남성 · 자연스러운 대화체 · Edge(인터넷)"),
    "supertonic:F1": ("Supertonic F1", "여성 · 로컬(오프라인)"),
    "supertonic:F2": ("Supertonic F2", "여성 · 로컬(오프라인)"),
    "supertonic:F3": ("Supertonic F3", "여성 · 로컬(오프라인)"),
    "supertonic:M1": ("Supertonic M1", "남성 · 로컬(오프라인)"),
    "supertonic:M2": ("Supertonic M2", "남성 · 로컬(오프라인)"),
    "supertonic:M3": ("Supertonic M3", "남성 · 로컬(오프라인)"),
    "qwen:sohee": ("Qwen 소희", "여성 · 로컬 GPU · 억양이 어색할 수 있음"),
}
TONES = {  # 키 → (화면 이름, 모델에 줄 지시문). Qwen3-TTS만 따른다.
    "": ("기본", ""),
    "anchor": ("차분한 아나운서", "Speak in a calm, clear news-anchor tone with steady pacing and precise pronunciation."),
    "promo": ("밝은 홍보", "Speak in a bright, energetic and friendly promotional tone, smiling and upbeat."),
    "warm": ("따뜻한 이야기", "Speak warmly and softly, like telling a heartfelt story to a friend."),
    "confident": ("자신감 있는 발표", "Speak with a confident, powerful and inspiring presentation tone."),
    "custom": ("직접 적기", ""),
}
DEFAULTS = {"enabled": True, "speaker": "", "tone": "", "instruct": ""}
SAMPLE_TEXT = "안녕하세요. 이 목소리로 영상의 내레이션을 읽어 드립니다."
_ENGINE_VOICE_KEY = {"edge": "EDGE_TTS_VOICE", "supertonic": "SUPERTONIC_VOICE", "qwen": "QWEN_TTS_SPEAKER"}


def normalize(data):
    out = copy.deepcopy(DEFAULTS)
    src = data or {}
    out["enabled"] = bool(src.get("enabled", True))
    speaker = str(src.get("speaker") or "").strip()
    if speaker and ":" not in speaker:  # 예전 저장값(Qwen 화자 이름만)
        speaker = f"qwen:{speaker.lower()}"
    out["speaker"] = speaker if speaker in SPEAKERS else ""
    tone = str(src.get("tone") or "")
    out["tone"] = tone if tone in TONES else ""
    if out["tone"] == "custom":
        out["instruct"] = " ".join(str(src.get("instruct") or "").split())[:300]
        if not out["instruct"]:
            out["tone"] = ""
    return out


def load(session):
    try:
        return normalize(json.loads(session.voice_json) if session.voice_json else {})
    except ValueError:
        return normalize({})


def save(session, settings):
    settings = normalize(settings)
    session.voice_json = "" if settings == DEFAULTS else json.dumps(settings, ensure_ascii=False)


def instruct_text(settings):
    s = normalize(settings)
    return s["instruct"] if s["tone"] == "custom" else TONES[s["tone"]][1]


def effective_speaker(config, settings):
    """지금 쓰일 "<엔진>:<목소리>" (프로젝트에서 고른 것, 없으면 설정 기본값)."""
    s = normalize(settings)
    if s["speaker"]:
        return s["speaker"]
    engine = str(config.get("VOICE_ADAPTER") or "edge").lower()
    defaults = {"edge": "ko-KR-SunHiNeural", "supertonic": "F1", "qwen": "sohee"}
    voice = str(config.get(_ENGINE_VOICE_KEY.get(engine, ""), "") or defaults.get(engine, ""))
    return f"{engine}:{voice.lower() if engine == 'qwen' else voice}"


def apply(config, settings):
    """설정을 얹은 config 사본 — narration.prepare_clips에 그대로 넘긴다."""
    s = normalize(settings)
    cfg = dict(config)
    if s["speaker"]:
        engine, voice = s["speaker"].split(":", 1)
        cfg["VOICE_ADAPTER"] = engine
        cfg[_ENGINE_VOICE_KEY[engine]] = voice
    if s["tone"]:
        cfg["QWEN_TTS_INSTRUCT"] = instruct_text(s)
    return cfg


def engine_of(config, settings):
    return effective_speaker(config, settings).split(":", 1)[0]


def preview_key(config, settings, text):
    cfg = apply(config, settings)
    raw = "|".join([effective_speaker(config, settings), str(cfg.get("QWEN_TTS_INSTRUCT", "")), text])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def describe(config, settings):
    s = normalize(settings)
    if not s["enabled"]:
        return "내레이션 끔"
    name = SPEAKERS.get(effective_speaker(config, s), (effective_speaker(config, s),))[0]
    tone = s["instruct"] if s["tone"] == "custom" else TONES[s["tone"]][0]
    return f"목소리 {name}" + (f" · {tone}" if s["tone"] and engine_of(config, s) == "qwen" else "")

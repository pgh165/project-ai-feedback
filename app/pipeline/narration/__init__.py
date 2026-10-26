"""[팀 직접 구현] 내레이션(해설 음성) — 스토리보드의 장면별 문장을 TTS로 합성한다.

프롬프트 문서의 [내레이션] 섹션 → 스토리보드 shot["narration"] → 장면별 wav → 러프컷 위에 믹스.
영상 장면은 그대로 두고 음성만 바꿀 수 있으므로, 문장을 고쳐도 영상을 다시 만들지 않는다.

어댑터: `edge`(Microsoft 신경망 음성, 계정 불필요·인터넷 필요, 기본) / `supertonic`(Supertonic-3, 로컬 CPU) /
`qwen`(Qwen3-TTS, 로컬 GPU — 한국어가 어색해 기본에서 뺐다) / `windows`(내장 SAPI, 오프라인) / `none`.
edge·supertonic은 venv_tts2의 tts2_worker.py로 합성한다. edge가 실패하면(오프라인 등) VOICE_FALLBACK(기본 supertonic)으로 다시 합성한다.
합성 결과는 문장·음성 설정의 해시로 캐시한다.
"""
import hashlib
import logging
import re
import subprocess
import sys
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

_SCRIPT = Path(__file__).with_name("sapi_speak.ps1")
_voice_cache = {}


class NarrationError(RuntimeError):
    pass


def adapter_name(config):
    return (config.get("VOICE_ADAPTER") or "none").strip().lower()


def _powershell():
    return "powershell.exe" if sys.platform == "win32" else None


def _run_sapi(args, timeout=60):
    exe = _powershell()
    if not exe:
        raise NarrationError("Windows 내장 음성은 Windows에서만 사용할 수 있습니다.")
    cmd = [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(_SCRIPT), *args]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout)


def installed_voices():
    """[(이름, 문화권)] — 설치·활성화된 Windows 음성. 한 번만 조회한다."""
    if "voices" not in _voice_cache:
        try:
            proc = _run_sapi(["-ListOnly"], timeout=30)
            lines = [l.strip() for l in proc.stdout.splitlines() if "|" in l]
            _voice_cache["voices"] = [tuple(l.split("|", 1)) for l in lines]
        except (NarrationError, subprocess.SubprocessError, OSError):
            _voice_cache["voices"] = []
    return _voice_cache["voices"]


_QWEN_WORKER = Path(__file__).with_name("qwen_tts_worker.py")


def _qwen_python(config):
    """Qwen3-TTS가 설치된 별도 가상환경의 python (기존 venv와 패키지가 충돌하지 않게 분리)."""
    configured = config.get("QWEN_TTS_PYTHON")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[3] / "venv_tts" / "Scripts" / "python.exe"


_TTS2_WORKER = Path(__file__).with_name("tts2_worker.py")
TTS2_ENGINES = ("edge", "supertonic")


def _tts2_python(config):
    """edge-tts·Supertonic이 설치된 가상환경(venv_tts2)의 python."""
    configured = config.get("TTS2_PYTHON")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[3] / "venv_tts2" / "Scripts" / "python.exe"


def _tts2_voice(config):
    if adapter_name(config) == "edge":
        return config.get("EDGE_TTS_VOICE") or "ko-KR-SunHiNeural"
    return config.get("SUPERTONIC_VOICE") or "F1"


def is_available(config):
    """(사용 가능 여부, 사유)."""
    name = adapter_name(config)
    if name == "none":
        return False, "VOICE_ADAPTER=none"
    if name in TTS2_ENGINES:
        py = _tts2_python(config)
        if not py.exists():
            return False, f"edge-tts·Supertonic 가상환경이 없습니다: {py}"
        return True, f"{name} {_tts2_voice(config)}"
    if name == "qwen":
        py = _qwen_python(config)
        if not py.exists():
            return False, f"Qwen3-TTS 가상환경이 없습니다: {py}"
        return True, f"Qwen3-TTS {config.get('QWEN_TTS_SPEAKER', 'Sohee')}"
    if name != "windows":
        return False, f"알 수 없는 VOICE_ADAPTER={name}"
    if _powershell() is None:
        return False, "Windows가 아니라 내장 음성을 쓸 수 없습니다"
    wanted = config.get("VOICE_NAME")
    voices = installed_voices()
    if wanted:
        if not any(n == wanted for n, _c in voices):
            return False, f"음성 '{wanted}'이(가) 설치되어 있지 않습니다"
        return True, wanted
    culture = config.get("VOICE_CULTURE", "ko-KR")
    match = next((n for n, c in voices if c == culture), None)
    if not match:
        return False, f"{culture} 음성이 설치되어 있지 않습니다 (Windows 설정 → 음성에서 추가)"
    return True, match


def _cache_key(text, config):
    parts = [adapter_name(config), str(config.get("VOICE_NAME", "")),
             str(config.get("VOICE_CULTURE", "")), str(config.get("VOICE_RATE", 0)), text]
    if adapter_name(config) == "qwen":
        parts += [str(config.get("QWEN_TTS_MODEL", "")), str(config.get("QWEN_TTS_SPEAKER", "")),
                  str(config.get("QWEN_TTS_INSTRUCT", ""))]
    if adapter_name(config) in TTS2_ENGINES:
        parts += [_tts2_voice(config)]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:10]


def _synthesize_tts2(items, config):
    """[(text, out_wav)] 를 edge-tts 또는 Supertonic으로 한 번에 합성한다. edge는 mp3로 받아 wav로 바꾼다."""
    import json
    from ..video_generator.comfyui.rough_cut import resolve_ffmpeg

    engine = adapter_name(config)
    staged = [(t, Path(p), Path(p).with_suffix(".mp3") if engine == "edge" else Path(p)) for t, p in items]
    job = {"engine": engine, "voice": _tts2_voice(config), "rate": int(config.get("VOICE_RATE", 0)) * 5,
           "items": [{"text": t, "out": str(raw)} for t, _p, raw in staged]}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
        json.dump(job, fh, ensure_ascii=False)
        job_file = fh.name
    try:
        try:
            proc = subprocess.run([str(_tts2_python(config)), str(_TTS2_WORKER), job_file],
                                  capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=int(config.get("TTS2_TIMEOUT_SEC", 300)))
        except subprocess.TimeoutExpired as exc:
            raise NarrationError(f"{engine} 합성 시간 초과") from exc
    finally:
        Path(job_file).unlink(missing_ok=True)
    ffmpeg = resolve_ffmpeg(config) if engine == "edge" else None
    for _t, out, raw in staged:
        if not raw.exists() or raw.stat().st_size < 100:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-2:]
            hint = " (인터넷 연결 확인)" if engine == "edge" else ""
            raise NarrationError(f"{engine} 합성 실패{hint}: " + " / ".join(tail)[:300])
        if raw != out:
            subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(raw), "-ar", "48000", str(out)], check=True)
            raw.unlink(missing_ok=True)


def _synthesize_qwen(items, config):
    """[(text, out_path)] 를 모델 1회 로드로 합성한다. 끝나면 프로세스가 종료돼 VRAM이 반환된다."""
    import json
    job = {"model": config.get("QWEN_TTS_MODEL", "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"),
           "speaker": config.get("QWEN_TTS_SPEAKER", "Sohee"),
           "language": config.get("QWEN_TTS_LANGUAGE", "Korean"),
           "instruct": config.get("QWEN_TTS_INSTRUCT", ""),
           "items": [{"text": t, "out": str(p)} for t, p in items]}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
        json.dump(job, fh, ensure_ascii=False)
        job_file = fh.name
    try:
        timeout = int(config.get("QWEN_TTS_TIMEOUT_SEC", 900))
        try:
            proc = subprocess.run([str(_qwen_python(config)), str(_QWEN_WORKER), job_file],
                                  capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise NarrationError("Qwen3-TTS 합성 시간 초과") from exc
    finally:
        Path(job_file).unlink(missing_ok=True)
    for _t, p in items:
        if not Path(p).exists() or Path(p).stat().st_size < 100:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
            raise NarrationError("Qwen3-TTS 합성 실패: " + " / ".join(tail)[:300])


def synthesize(text, out_path, config):
    """text → wav 파일. 실패하면 NarrationError."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if adapter_name(config) == "qwen":
        _synthesize_qwen([(text, out_path)], config)
        return out_path
    if adapter_name(config) in TTS2_ENGINES:
        _synthesize_tts2([(text, out_path)], config)
        return out_path
    if adapter_name(config) != "windows":
        raise NarrationError(f"지원하지 않는 VOICE_ADAPTER={adapter_name(config)}")
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
        fh.write(text)
        text_file = fh.name
    try:
        args = ["-TextFile", text_file, "-OutFile", str(out_path), "-Rate", str(int(config.get("VOICE_RATE", 0))),
                "-Culture", config.get("VOICE_CULTURE", "ko-KR")]
        if config.get("VOICE_NAME"):
            args += ["-VoiceName", config["VOICE_NAME"]]
        try:
            proc = _run_sapi(args)
        except subprocess.TimeoutExpired as exc:
            raise NarrationError("음성 합성 시간 초과") from exc
        if proc.returncode != 0 or not out_path.exists() or out_path.stat().st_size < 100:
            raise NarrationError((proc.stderr or proc.stdout or "음성 합성 실패").strip()[:300])
    finally:
        Path(text_file).unlink(missing_ok=True)
    return out_path


def audio_duration(ffmpeg, path):
    """오디오 파일 길이(초). ffprobe 없이 `ffmpeg -i` 출력에서 읽는다."""
    proc = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)],
                          capture_output=True, text=True, errors="replace")
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", proc.stderr)
    if not m:
        raise NarrationError(f"오디오 길이를 읽을 수 없습니다: {path}")
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


_SILENCE = "silenceremove=start_periods=1:start_threshold=-45dB"


def speech_duration(ffmpeg, path):
    """앞뒤 무음을 걷어낸 말 길이(초) — 러프컷이 장면에 맞출 때 재는 기준과 같다."""
    with tempfile.TemporaryDirectory() as tmp:
        trimmed = Path(tmp) / "trim.wav"
        subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(path), "-af",
                        f"{_SILENCE},areverse,{_SILENCE},areverse", "-c:a", "pcm_s16le", str(trimmed)],
                       capture_output=True)
        if trimmed.exists() and trimmed.stat().st_size > 100:
            return audio_duration(ffmpeg, trimmed)
    return audio_duration(ffmpeg, path)


def fit_plan(speech_sec, slot_sec, lead_sec, max_speedup):
    """말(speech_sec)을 장면 길이(slot_sec) 안에 넣는 계획.
    반환: {"speedup": 배속(1.0이면 그대로), "truncated": 그래도 넘치면 True, "start": 시작 지연(초)}.
    장면이 말보다 많이 짧으면 lead를 줄여서라도 먼저 들어맞는지 본다."""
    lead = min(max(lead_sec, 0.0), max(slot_sec - 0.3, 0.0))
    room = max(slot_sec - lead, 0.1)
    speedup = 1.0
    if speech_sec > room:
        speedup = min(speech_sec / room, max(max_speedup, 1.0))
    truncated = speech_sec / speedup > room + 0.02
    return {"speedup": round(speedup, 3), "truncated": truncated, "start": round(lead, 3)}


def prepare_clips(config, storyboard, out_dir):
    """장면별 문장을 합성(캐시 사용)한다. {shot_id: {"text", "path"}} 와 경고 목록 반환.
    음성이 없는 환경이면 NarrationError — 호출측이 내레이션 없이 계속 진행한다."""
    out_dir = Path(out_dir)
    wanted = [(s["id"], s.get("narration", "").strip()) for s in storyboard["shots"]]
    wanted = [(sid, t) for sid, t in wanted if t]
    if not wanted:
        return {}
    ok, reason = is_available(config)
    if not ok:
        raise NarrationError(reason)
    try:
        return _prepare(config, wanted, out_dir)
    except NarrationError as exc:  # edge가 오프라인 등으로 실패하면 로컬 엔진으로
        fallback = (config.get("VOICE_FALLBACK") or "").strip().lower()
        if not fallback or fallback == adapter_name(config):
            raise
        log.warning("내레이션 %s 실패 → %s로 다시 합성: %s", adapter_name(config), fallback, exc)
        return _prepare(dict(config, VOICE_ADAPTER=fallback, VOICE_FALLBACK=""), wanted, out_dir)


def _prepare(config, wanted, out_dir):
    clips = {}
    todo = []
    for shot_id, text in wanted:
        path = out_dir / f"{shot_id}_{_cache_key(text, config)}.wav"
        clips[shot_id] = {"text": text, "path": path}
        if not path.exists():
            todo.append((text, path))
    if adapter_name(config) in ("qwen",) + TTS2_ENGINES:
        if todo:  # 모델 로드·연결이 오래 걸려 한 번에 합성한다
            out_dir.mkdir(parents=True, exist_ok=True)
            (_synthesize_qwen if adapter_name(config) == "qwen" else _synthesize_tts2)(todo, config)
    else:
        for text, path in todo:
            synthesize(text, path, config)
    return clips

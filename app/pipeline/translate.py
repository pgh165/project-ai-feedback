"""[팀 직접 구현] 영어 장면 묘사 → 한국어 번역 (화면 표시용).

영상 모델에는 영어 원문이 그대로 들어가고, 화면에서만 한글 번역을 먼저 보여 준다.
번역은 ComfyUI의 TextGenerate 노드로 이미 받아 둔 Gemma 모델을 돌린다 (외부 계정 불필요).
ComfyUI가 꺼져 있으면 llm.py 폴백(Claude API → Ollama), 그래도 안 되면 영어만 보인다.

번역은 요청 스레드를 막지 않도록 백그라운드 스레드에서 하고, 결과는 TextTranslation 테이블에 캐시한다.
영상 생성 중이면 ComfyUI 큐에서 장면 사이에 끼어 실행된다.
"""
import hashlib
import logging
import threading
import time

from ..models import TextTranslation, db

log = logging.getLogger(__name__)

_SYSTEM = (
    "You translate English video-generation shot descriptions into natural Korean for a Korean video producer. "
    "Keep the meaning exact and complete, including camera, lighting and motion terms. "
    "Output only the Korean translation, nothing else."
)
_RETRY_AFTER_SEC = 60  # 번역 엔진이 없을 때 다시 시도하기까지 (페이지를 열 때마다 두드리지 않게)

_lock = threading.Lock()
_pending = set()       # 번역 중인 원문 해시
_last_failure = [0.0]


def key(text):
    return hashlib.sha1((text or "").strip().encode("utf-8")).hexdigest()[:16]


def _has_hangul_majority(text):
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and sum("가" <= c <= "힣" for c in letters) / len(letters) > 0.5


def lookup(texts):
    """{해시: 한국어} — 캐시에 있는 것만. 이미 한국어인 문장은 그대로 돌려준다."""
    found = {}
    wanted = {}
    for t in texts:
        t = (t or "").strip()
        if not t:
            continue
        if _has_hangul_majority(t):
            found[key(t)] = t
        else:
            wanted[key(t)] = t
    if wanted:
        rows = TextTranslation.query.filter(TextTranslation.source_hash.in_(list(wanted))).all()
        found.update({r.source_hash: r.ko_text for r in rows if r.ko_text})
    return found


def is_pending(texts):
    with _lock:
        return any(key(t) in _pending for t in texts if t)


def request(app, texts):
    """캐시에 없는 문장을 백그라운드에서 번역한다. 이미 번역 중이거나 꺼져 있으면 아무것도 하지 않는다."""
    if not app.config.get("TRANSLATE_ENABLED", True):
        return False
    texts = [t.strip() for t in texts if t and t.strip()]
    missing = {key(t): t for t in texts if key(t) not in lookup([t])}
    with _lock:
        missing = {k: t for k, t in missing.items() if k not in _pending}
        if not missing or time.time() - _last_failure[0] < _RETRY_AFTER_SEC:
            return False
        _pending.update(missing)
    threading.Thread(target=_run, args=(app, missing), daemon=True, name="translate").start()
    return True


def _run(app, items):
    try:
        with app.app_context():
            engine = _comfy_engine(app.config) or _llm_engine(app.config)
            if engine is None:
                _last_failure[0] = time.time()
                log.info("번역 엔진 없음 — 영어만 표시합니다")
                return
            name, translate_one = engine
            for k, text in items.items():
                try:
                    ko = (translate_one(text) or "").strip().strip('"“”')
                except Exception:
                    log.warning("번역 실패: %s", text[:60], exc_info=True)
                    _last_failure[0] = time.time()
                    break
                if ko and not TextTranslation.query.filter_by(source_hash=k).first():
                    db.session.add(TextTranslation(source_hash=k, source_text=text, ko_text=ko, engine=name))
                    db.session.commit()
                with _lock:
                    _pending.discard(k)
    finally:
        with _lock:
            _pending.difference_update(items)


def _comfy_engine(config):
    from . import comfy_text

    c = comfy_text.client(config)
    if c is None:
        return None
    return (f"comfyui:{config['TRANSLATE_COMFY_MODEL']}",
            lambda text: comfy_text.generate(c, config, _SYSTEM, text))


def _llm_engine(config):
    from . import llm

    if not (config.get("ANTHROPIC_API_KEY") or config.get("OLLAMA_MODEL")):
        return None

    def translate_one(text):
        out, _source = llm.generate(_SYSTEM, text, config, comfy=False)  # ComfyUI는 위 엔진이 이미 시도했다
        return out

    return "llm", translate_one

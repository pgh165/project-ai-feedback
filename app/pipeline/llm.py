"""텍스트 생성 LLM 폴백 체인: Claude API → Ollama(로컬) → ComfyUI Gemma(로컬) → None(호출측이 템플릿 사용).
generate_strong: 장면 다듬기·AI로 채우기처럼 규칙을 잘 따라야 하는 글은 큰 로컬 모델(llama-server)을 먼저 쓴다."""
import logging
import os
import shlex
import subprocess
import threading
import time
from contextlib import contextmanager
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)
# 큰 모델이 GPU에 떠 있는 동안 잡는다 — 영상 작업(jobs.execute)도 이 잠금을 기다려 둘이 VRAM을 다투지 않게 한다.
# 작업 안에서 큰 모델을 부르기도 하므로(의견으로 장면 묘사 고쳐 쓰기) 같은 스레드가 다시 잡을 수 있게 RLock
strong_model_lock = threading.RLock()
_local = threading.local()  # strong_server 안에서 켜기에 실패했으면 다시 켜려고 기다리지 않는다


def _claude(system, user, config):
    import anthropic

    client = anthropic.Anthropic(api_key=config["ANTHROPIC_API_KEY"])
    try:
        response = client.messages.create(
            model=config["ANTHROPIC_MODEL"],
            max_tokens=16000,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except anthropic.AuthenticationError:
        log.warning("Claude API 인증 실패 — ANTHROPIC_API_KEY를 확인하세요")
        return None
    except anthropic.RateLimitError:
        log.warning("Claude API 요청 한도 초과")
        return None
    except anthropic.APIStatusError as exc:
        log.warning("Claude API 오류 (%s): %s", exc.status_code, exc.message)
        return None
    except anthropic.APIConnectionError:
        log.warning("Claude API 연결 실패")
        return None
    if response.stop_reason == "refusal":
        log.warning("Claude가 요청을 거절함")
        return None
    return "".join(b.text for b in response.content if b.type == "text").strip() or None


def _ollama(system, user, config):
    try:
        resp = requests.post(
            config["OLLAMA_HOST"].rstrip("/") + "/api/chat",
            json={
                "model": config["OLLAMA_MODEL"],
                "stream": False,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
            timeout=300,
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"].strip() or None
    except (requests.RequestException, KeyError, ValueError) as exc:
        log.warning("Ollama 호출 실패: %s", exc)
        return None


def _comfy(system, user, config, max_length):
    """ComfyUI의 Gemma(TextGenerate) — 영상 워크플로가 이미 쓰는 모델이라 따로 받을 것이 없다.
    ComfyUI가 꺼져 있거나 실패하면 None. 영상 생성 중이면 ComfyUI 큐에서 장면 사이에 끼어 실행된다."""
    from . import comfy_text
    from .video_generator.comfyui.comfy_client import ComfyError

    c = comfy_text.client(config, timeout_sec=config.get("LLM_COMFY_TIMEOUT_SEC", 900))
    if c is None:
        return None
    model = config.get("LLM_COMFY_MODEL") or config["TRANSLATE_COMFY_MODEL"]
    try:
        return comfy_text.generate(c, dict(config, TRANSLATE_COMFY_MODEL=model), system, user,
                                   max_length=max_length) or None
    except (ComfyError, requests.RequestException, OSError) as exc:
        log.warning("ComfyUI Gemma 호출 실패: %s", exc)
        return None


def _strong_alive(url):
    try:
        return requests.get(url.rstrip("/") + "/health", timeout=2).json().get("status") == "ok"
    except (requests.RequestException, ValueError):
        return False


def _gpu_busy():
    """영상 생성 작업이 돌거나 기다리는 중인가 — 그때 큰 모델을 GPU에 올리면 둘 다 느려지거나 멈춘다."""
    from ..models import GenerationJob

    try:
        return GenerationJob.query.filter(GenerationJob.status.in_(("queued", "running"))).count() > 0
    except RuntimeError:  # 앱 컨텍스트 밖
        return False


def _start_strong(config, url):
    """llama-server를 켜고 준비될 때까지 기다린다. 켜지 못하면 None. 켜기 전 ComfyUI 모델을 내려 VRAM을 비운다."""
    from .video_generator.comfyui.comfy_client import ComfyClient

    exe, model = config.get("REFINE_LLAMA_SERVER", ""), config.get("REFINE_LLAMA_MODEL", "")
    if not (exe and model and os.path.isfile(exe) and os.path.isfile(model)):
        return None
    ComfyClient(config["COMFYUI_URL"]).free()
    parsed = urlparse(url)
    cmd = [exe, "--model", model, "--host", parsed.hostname or "127.0.0.1", "--port", str(parsed.port or 8099),
           *shlex.split(config.get("REFINE_LLAMA_ARGS", ""))]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    deadline = time.time() + int(config.get("REFINE_LLM_START_TIMEOUT_SEC", 180))
    while time.time() < deadline:
        if proc.poll() is not None:
            log.warning("llama-server가 켜지자마자 끝났습니다 (종료 코드 %s): %s", proc.returncode, cmd)
            return None
        if _strong_alive(url):
            return proc
        time.sleep(1)
    log.warning("llama-server가 %s초 안에 준비되지 않았습니다", config.get("REFINE_LLM_START_TIMEOUT_SEC"))
    proc.kill()
    return None


def _strong_chat(system, user, config, url, max_tokens):
    try:
        resp = requests.post(
            url.rstrip("/") + "/v1/chat/completions",
            json={"messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                  "temperature": 0.4, "max_tokens": max_tokens,
                  "chat_template_kwargs": {"enable_thinking": False}},
            timeout=int(config.get("REFINE_LLM_TIMEOUT_SEC", 300)))
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip() or None
    except (requests.RequestException, KeyError, IndexError, ValueError) as exc:
        log.warning("큰 모델(llama-server) 호출 실패: %s", exc)
        return None


@contextmanager
def strong_server(config, in_job=False):
    """여러 번 부를 때 큰 모델을 한 번만 켜 두고 끝나면 끈다 (장면마다 켜고 끄면 장면당 15~20초가 더 든다).
    켜지 못해도 그냥 넘어간다 — 안에서 부르는 generate_strong_only가 작은 모델로 대신할 수 있게 None을 돌려준다."""
    url = config.get("REFINE_LLM_URL", "")
    if not url or (not in_job and _gpu_busy()):
        yield
        return
    with strong_model_lock:
        proc = None if _strong_alive(url) else _start_strong(config, url)
        _local.no_start = proc is None and not _strong_alive(url)
        try:
            yield
        finally:
            _local.no_start = False
            if proc is not None:
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()


def generate_strong_only(system, user, config, max_tokens=1200, in_job=False):
    """(text|None, reason) — 큰 모델만. 서버를 여기서 켰으면 다 쓰고 끈다.
    in_job=True: GPU 작업 안에서(장면 사이, ComfyUI가 쉬는 때) 부른다 — 작업이 도는 중이어도 켠다.
    작업 큐는 워커 1개라 그 사이 다른 영상 작업이 끼어들지 않는다."""
    url = config.get("REFINE_LLM_URL", "")
    if not url:
        return None, "큰 모델이 설정되지 않아"
    if not in_job and _gpu_busy():
        return None, "영상을 만드는 중이라"
    with strong_model_lock:
        if getattr(_local, "no_start", False):
            return None, "큰 모델을 켜지 못해"
        proc = None if _strong_alive(url) else _start_strong(config, url)
        try:
            text = _strong_chat(system, user, config, url, max_tokens) if proc or _strong_alive(url) else None
        finally:
            if proc is not None:
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
    return (text, "") if text else (None, "큰 모델을 켜지 못해")


def generate_strong(system, user, config, max_tokens=1200, fallback_config=None):
    """(text, source, reason). 큰 모델(REFINE_LLM_URL)을 먼저 쓰고, 못 쓰면 generate()로 (보통 e2b).
    reason은 큰 모델을 못 쓴 이유 (화면에 '작은 모델로 했다'고 알리는 데 쓴다)."""
    text, reason = generate_strong_only(system, user, config, max_tokens)
    if text:
        return text, config.get("REFINE_LLM_LABEL", "large"), ""
    text, source = generate(system, user, fallback_config or config, max_length=max_tokens)
    return text, source, reason


def generate(system, user, config, comfy=True, max_length=None):
    """(text, source) 반환. 사용 가능한 LLM이 없으면 (None, 'template').
    순서: Claude API → Ollama → ComfyUI Gemma(LLM_COMFY_ENABLED). comfy=False면 Gemma를 건너뛴다
    (이미 ComfyUI를 먼저 시도한 호출측이 같은 실패를 되풀이하지 않게)."""
    if config.get("ANTHROPIC_API_KEY"):
        text = _claude(system, user, config)
        if text:
            return text, "claude"
    if config.get("OLLAMA_MODEL"):
        text = _ollama(system, user, config)
        if text:
            return text, "ollama"
    if comfy and config.get("LLM_COMFY_ENABLED", True):
        text = _comfy(system, user, config, max_length or int(config.get("LLM_COMFY_MAX_TOKENS", 1500)))
        if text:
            return text, "gemma"
    return None, "template"

"""[팀 직접 구현] AI 점검 — 사람 대신 장면의 첫 프레임 이미지와 컷 영상이 기준을 넘는지 판단한다.

이미지  Gemma(ComfyUI TextGenerate, 이미지 입력)가 장면 묘사와 비교해 일치 점수(0~10)·글자·형태 깨짐을 답한다.
컷      ffmpeg로 프레임을 뽑아 '멈춘 화면'(프레임 차이)·'어두운 화면'(평균 밝기)을 재고,
        뒤쪽 프레임 한 장을 Gemma로 글자·형태 깨짐만 본다 (묘사 일치는 첫 프레임에서 이미 봤다).
결과    {"checked", "passed", "score", "problems": [한국어], "fix": 영어 한 문장, "kind": still|look|""}
        fix는 미달 장면을 다시 만들 때 장면 의견(notes)으로 넣어 feedback_target이 묘사를 고쳐 쓰게 한다.
AI가 답하지 못하면(ComfyUI 꺼짐 등) checked=False·passed=True로 두고 사람이 마지막에 보게 한다 — 판단 없이 다시 만들지 않는다.
작은 모델이라 틀릴 수 있어 점수는 '다시 만들지'의 근거로만 쓰고, 가장 나은 버전을 고를 때도 같은 점수를 쓴다.
"""
import logging
import re
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

_VISION_SYSTEM = (
    "You are a strict quality inspector for AI-generated video frames. "
    "Compare the image with the scene description and answer in exactly this format. "
    "Read the image as a 3D scene: when an object near the camera overlaps a person or object further back "
    "(for example a robot arm in front of a man's hands, or a car in front of a traffic cone), that is normal depth, "
    "not a wrong position, not fused objects and not a malformed shape.\n"
    "SCORE: <0-10, how well the image shows the described scene>\n"
    "TEXT: <yes only if large readable words, titles, captions, logos or watermarks appear over the image or on signs "
    "and objects; small blurry code or charts on monitors do not count, else no>\n"
    "BROKEN: <yes if anything is malformed: extra or melted fingers, twisted limbs, distorted faces, "
    "objects fused together or impossible shapes; overlapping at different depths does not count, else no>\n"
    "PROBLEM: <one short sentence about the biggest problem, or none>\n"
    "FIX: <one short sentence describing how the scene should look instead, only visible things, no negatives, or none>"
)
# 컷 뒤쪽 프레임에 붙이는 설명 — 묘사는 동작 전체(달린다·집어 들어 올린다)라서, 끝 무렵 모습(다 달려 멈춤·들어 올린 뒤)을
# '묘사와 다르다'고 미달시켰다 (2026-11-07 데모 컷 재점검: s01 차·s05 로봇팔 오판, 사용자가 눈으로 확인)
_LATE_FRAME_NOTE = (" (This frame is from late in the video clip: the described action may be in progress or already "
                    "finished, e.g. a vehicle that has driven further, an object that has been lifted. "
                    "Judge whether it is a plausible moment of the described scene.)")
_FIELD = re.compile(r"^\s*\**\s*(SCORE|TEXT|BROKEN|PROBLEM|FIX)\s*\**\s*[:：]\s*(.*)$", re.I | re.M)

# 화면에 쓰는 문제 이름
P_UNCHECKED = "AI가 점검하지 못했어요"
P_MISMATCH = "묘사와 달라요"
P_TEXT = "화면에 글자가 보여요"
P_BROKEN = "형태가 깨졌어요"
P_STILL = "화면이 거의 움직이지 않아요"
P_DARK = "화면이 너무 어두워요"
P_CAMERA = "카메라가 다가가거나 멀어졌어요"
P_MISSING = "파일이 없어요"

_FIX_TEXT = "plain clean surfaces"
_TEXT_WORDS = re.compile(r"\b(text|letters?|words?|writing|written|logo|caption|sign|label|characters?|code|"
                         r"typography|watermark|numbers?|digits?)\b", re.I)
_FIX_BROKEN = "clean, natural proportions with every object clearly separated"
_SCREEN_WORDS = re.compile(r"\b(monitors?|screens?|displays?|laptops?|computers?|tablets?)\b", re.I)
_BROKEN_WORDS = re.compile(r"\b(melt\w*|fus(?:ed|ing)|merg\w*|distort\w*|deform\w*|malform\w*|twist\w*|warp\w*|mangl\w*|"
                           r"artifacts?|smudg\w*|glitch\w*|duplicat\w*|extra\s+(?:fingers?|limbs?|arms?|hands?|legs?|heads?)|"
                           r"(?:fingers?|hands?|limbs?|faces?)\s+(?:are|is)\s+(?:wrong|odd|strange|missing)|"
                           r"impossible|stretch\w*|bent\s+unnaturally|blob\w*)\b", re.I)


def _yes(value):
    return (value or "").strip().lower().startswith(("y", "예", "네"))


def parse(text):
    """Gemma 답 → {score, text, broken, problem, fix}. 알아보지 못하면 None."""
    fields = {k.upper(): v.strip() for k, v in _FIELD.findall(text or "")}
    if "SCORE" not in fields:
        return None
    m = re.search(r"\d+(?:\.\d+)?", fields["SCORE"])
    if not m:
        return None
    clean = lambda v: "" if (v or "").strip().lower().strip(".") in ("", "none", "n/a", "없음") else v.strip()
    return {"score": max(0.0, min(float(m.group(0)), 10.0)), "text": _yes(fields.get("TEXT")),
            "broken": _yes(fields.get("BROKEN")), "problem": clean(fields.get("PROBLEM")),
            "fix": clean(fields.get("FIX"))}


def ask_vision(image_path, description, config):
    """이미지 + 장면 묘사 → Gemma 답(문자열). 쓸 수 없으면 None. 시험에서 바꿔 끼울 수 있게 모듈 함수로 둔다.
    큰 모델(26B + projector)을 먼저 쓴다 — e2b는 문제를 적어 놓고도 높은 점수를 줬다(2026-11-06 '실제 크기 차 같다'에 8점).
    점검은 언제나 작업 큐 안(AI 자동 진행)에서 불리므로 in_job=True. 큰 모델을 못 쓸 때만 e2b로."""
    from . import comfy_text, llm

    if llm.vision_ready(config):
        text, reason = llm.generate_strong_only(_VISION_SYSTEM, f"Scene description: {description}", config,
                                                max_tokens=300, in_job=True, image_path=str(image_path),
                                                temperature=float(config.get("AUTO_REVIEW_TEMPERATURE", 0.0)))
        if text:
            return text
        log.info("AI 점검: 큰 모델을 쓰지 못해 작은 모델로 (%s)", reason)
    if not config.get("LLM_COMFY_ENABLED", True):
        return None
    c = comfy_text.client(config)
    if c is None:
        return None
    model = config.get("AUTO_REVIEW_MODEL") or config["TRANSLATE_COMFY_MODEL"]
    try:
        return comfy_text.generate(c, dict(config, TRANSLATE_COMFY_MODEL=model), _VISION_SYSTEM,
                                   f"Scene description: {description}", image_path=str(image_path),
                                   max_length=160) or None
    except Exception as exc:  # 점검 실패(ComfyUI 오류·시간 초과)가 생성 전체를 멈추면 안 된다
        log.warning("AI 점검(이미지) 실패: %s", exc)
        return None


def _verdict(answer, min_score, check_score=True):
    """parse 결과 → (passed, score, problems, fix, kind)."""
    problems, fixes = [], []
    if check_score and answer["score"] < min_score:
        problems.append(f"{P_MISMATCH} ({answer['score']:g}/10)")
        if answer["fix"]:
            fixes.append(answer["fix"])
    # 작은 모델은 글자가 없어도 'TEXT: yes'라고 자주 답한다(2026-11-03 시험: 4장 중 3장 오판) — 글자는 프롬프트에서
    # 막고(text_free), 여기서는 PROBLEM 문장도 글자를 말할 때만 미달로 친다
    # 모니터 속 작은 코드·그래프는 괜찮다(2026-11-06 사용자) — 문제 문장이 화면 이야기면 글자 미달로 치지 않는다
    if answer["text"] and _TEXT_WORDS.search(answer["problem"] or "") and not _SCREEN_WORDS.search(answer["problem"] or ""):
        problems.append(P_TEXT)
        fixes.append(_FIX_TEXT)
    # 26B는 '큐브를 안 잡았다'·'실험복을 입었다'처럼 묘사와 다른 것도 BROKEN: yes라고 답한다(2026-11-06 컷 25개 재점검) —
    # 묘사 차이는 점수로 보고, 형태 깨짐은 PROBLEM 문장이 실제 변형(녹음·붙음·일그러짐·손가락 등)을 말할 때만 친다
    if answer["broken"] and _BROKEN_WORDS.search(answer["problem"] or ""):
        problems.append(P_BROKEN)
        fixes.append(answer["fix"] if answer["fix"] and not fixes else _FIX_BROKEN)
    fix = ", ".join(dict.fromkeys(f.rstrip(". ") for f in fixes if f))
    return not problems, answer["score"], problems, fix


def ask_scored(image_path, description, config, min_score):
    """한 장을 묻고 parse한 답. 점수가 기준 ±1 안이면 한 번 더 물어 평균한다 — 26B 판정이 돌릴 때마다 조금씩
    달라 기준 근처 장면의 통과·미달이 뒤집혔다(2026-11-06). 문제 문장·고쳐 쓰기는 낮은 점수 쪽을 쓴다."""
    first = parse(ask_vision(image_path, description, config) or "")
    margin = float(config.get("AUTO_REVIEW_RECHECK_MARGIN", 1))
    if first is None or abs(first["score"] - min_score) > margin:
        return first
    second = parse(ask_vision(image_path, description, config) or "")
    if second is None:
        return first
    low = min((first, second), key=lambda a: a["score"])
    return {**low, "score": round((first["score"] + second["score"]) / 2, 1),
            "text": first["text"] and second["text"], "broken": first["broken"] and second["broken"]}


def check_image(image_path, description, config):
    """첫 프레임 이미지 점검."""
    if not image_path or not Path(image_path).exists():
        return {"checked": True, "passed": False, "score": 0.0, "problems": [P_MISSING], "fix": "", "kind": "look"}
    min_score = float(config.get("AUTO_REVIEW_MIN_SCORE", 6))
    answer = ask_scored(image_path, description, config, min_score)
    if answer is None:
        return {"checked": False, "passed": True, "score": None, "problems": [P_UNCHECKED], "fix": "", "kind": ""}
    passed, score, problems, fix = _verdict(answer, min_score)
    return {"checked": True, "passed": passed, "score": score, "problems": problems, "fix": fix,
            "kind": "" if passed else "look", "note": answer["problem"]}


def _gray_samples(ffmpeg, path, n=8, size=(64, 36)):
    """영상 전체에서 고르게 n장을 회색 작은 그림으로 → numpy (n, h*w), 0~255."""
    import subprocess

    import numpy as np

    from .video_generator.comfyui import rough_cut

    info = rough_cut.probe_media(ffmpeg, path) or {}
    duration = float(info.get("duration") or 0)
    if duration <= 0:
        return None
    w, h = size
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(path),
           "-vf", f"fps={n / duration:.4f},scale={w}:{h}:flags=area,format=gray",
           "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    frames = np.frombuffer(raw, dtype=np.uint8)
    count = len(frames) // (w * h)
    if count < 2:
        return None
    return frames[: count * w * h].reshape(count, h * w).astype(np.float32)


def motion_stats(ffmpeg, path):
    """(이웃 프레임 평균 차이, 평균 밝기). 잴 수 없으면 None."""
    frames = _gray_samples(ffmpeg, path)
    if frames is None:
        return None
    diff = float(abs(frames[1:] - frames[:-1]).mean())
    return diff, float(frames.mean())


def zoom_factor(first, last):
    """첫·끝 프레임(회색 2차원 배열) → (배율, 일치도). 배율 > 1 = 카메라가 다가감, < 1 = 멀어짐.
    첫 프레임 가운데를 s배 키운 그림과 끝 프레임이 가장 비슷한 s를 찾는다. 일치도가 낮으면(화면이 크게 바뀜) 믿지 않는다."""
    import numpy as np
    from PIL import Image

    h, w = first.shape
    # 가장자리 띠만 비교한다 — 카메라가 다가가면 가장자리(배경)까지 커지지만, 물체가 다가오면 가운데만 커진다.
    # 2026-11-06: 화면 가운데의 모형차가 다가오는 컷을 1.16배 줌으로 오판했다
    band = np.ones((h, w), dtype=bool)
    band[int(h * 0.25):int(h * 0.75), int(w * 0.25):int(w * 0.75)] = False

    def corr(x, y):
        x, y = x[band], y[band]
        x, y = x - x.mean(), y - y.mean()
        return float((x * y).sum() / (np.sqrt((x * x).sum() * (y * y).sum()) + 1e-9))

    def grown(img, s):  # 가운데 1/s 영역을 원래 크기로 키운다
        ch, cw = max(2, int(h / s)), max(2, int(w / s))
        top, left = (h - ch) // 2, (w - cw) // 2
        crop = Image.fromarray(img[top:top + ch, left:left + cw].astype(np.uint8))
        return np.asarray(crop.resize((w, h), Image.BILINEAR), dtype=np.float32)

    best = (1.0, corr(first, last))
    for s in np.arange(1.04, 1.81, 0.04):
        for scale, c in ((float(s), corr(grown(first, s), last)), (1 / float(s), corr(first, grown(last, s)))):
            if c > best[1]:
                best = (scale, c)
    return round(best[0], 2), round(best[1], 3)


def camera_zoom(ffmpeg, path):
    """컷의 카메라 줌 (배율, 일치도). 잴 수 없으면 None."""
    frames = _gray_samples(ffmpeg, path, n=8, size=(128, 72))
    if frames is None:
        return None
    return zoom_factor(frames[0].reshape(72, 128), frames[-1].reshape(72, 128))


_FADE = re.compile(r"\b(fades?|fading|fade-?out|fade to (?:black|dark)|darkness|dims|dimming|goes dark|turns dark|grows dark|darkens?)\b", re.I)


def wants_fade(description):
    """장면이 어두워지며 끝나기를 원하는가 (페이드아웃·불이 꺼짐)."""
    return bool(_FADE.search(description or ""))


def check_clip(clip_path, description, config):
    """컷 영상 점검: 멈춤·어두움(ffmpeg) + 뒤쪽 프레임의 글자·형태 깨짐(Gemma)."""
    from . import video_import
    from .video_generator.comfyui import rough_cut

    if not clip_path or not Path(clip_path).exists():
        return {"checked": True, "passed": False, "score": 0.0, "problems": [P_MISSING], "fix": "", "kind": "still"}
    ffmpeg = rough_cut.resolve_ffmpeg(config)
    problems, fix, kind, score, checked = [], "", "", 10.0, False
    fades = wants_fade(description)
    if ffmpeg:
        try:
            stats = motion_stats(ffmpeg, clip_path)
        except Exception as exc:
            log.warning("컷 움직임을 재지 못함: %s", exc)
            stats = None
        if stats is not None:
            checked = True
            diff, level = stats
            if diff < float(config.get("AUTO_REVIEW_STILL_DIFF", 1.5)):
                problems.append(P_STILL)
                kind, score = "still", min(score, 4.0)
            if level < float(config.get("AUTO_REVIEW_DARK_LEVEL", 18)) and not fades:
                problems.append(P_DARK)
                fix, kind, score = "bright, evenly lit scene", "look", min(score, 3.0)
        from . import camera
        if not camera.has_move(description):  # 장면이 카메라 움직임을 원하지 않았는데 다가가거나 멀어졌다
            try:
                zoom = camera_zoom(ffmpeg, clip_path)
            except Exception as exc:
                log.warning("컷 줌을 재지 못함: %s", exc)
                zoom = None
            if zoom is not None:
                checked = True
                factor, match = zoom
                if match >= float(config.get("AUTO_REVIEW_ZOOM_MIN_MATCH", 0.5)) and not (
                        float(config.get("AUTO_REVIEW_ZOOM_OUT", 0.85)) <= factor
                        <= float(config.get("AUTO_REVIEW_ZOOM_IN", 1.18))):
                    problems.append(f"{P_CAMERA} ({factor:g}배)")
                    kind, score = kind or "still", min(score, 4.0)  # 같은 첫 프레임, 시드만 바꿔 다시
        # 뒤쪽(75%)·끝(95%) 두 장을 장면 묘사와 비교한다 — 2026-11-06: 카메라가 돌아가 빈 벽으로 끝나는 컷이
        # 글자·깨짐만 보던 점검을 통과해 '가장 나은 버전'으로 뽑혔다 (26B는 그 끝 프레임에 1점)
        info = rough_cut.probe_media(ffmpeg, clip_path) or {}
        duration = float(info.get("duration") or 1)
        min_score = float(config.get("AUTO_REVIEW_CLIP_MIN_SCORE", config.get("AUTO_REVIEW_MIN_SCORE", 6)))
        notes = []
        with tempfile.TemporaryDirectory() as tmp:
            # 장면이 어두워지며 끝나기를 원하면(페이드아웃) 끝 프레임은 어두운 게 맞다 — 앞쪽 두 장으로 본다
            # (2026-11-06 프로젝트 51 s06: 끝 프레임이 어둡다고 세 번 미달, 고쳐 쓰기가 '노출을 높여라'로 의도와 반대)
            for at in ((0.25, 0.5) if fades else (0.75, 0.95)):
                frame = Path(tmp) / f"at{int(at * 100)}.png"
                try:
                    video_import.grab_frame(ffmpeg, clip_path, duration * at, frame, width=640)
                    answer = ask_scored(frame, description + ("" if fades else _LATE_FRAME_NOTE), config, min_score)
                except Exception as exc:
                    log.warning("컷 프레임 점검 실패: %s", exc)
                    answer = None
                if answer is None:
                    continue
                checked = True
                _ok, frame_score, look, look_fix = _verdict(answer, min_score)
                new = [p for p in look if p.split(" (")[0] not in {q.split(" (")[0] for q in problems}]
                if new:
                    problems += new
                    fix = ", ".join(dict.fromkeys(f for f in (fix, look_fix) if f))
                    kind = "look"
                score = min(score, frame_score)  # 통과한 컷끼리도 점수로 더 나은 버전을 고른다
                if P_BROKEN in look:
                    score = min(score, 3.0)
                elif look == [P_TEXT]:
                    score = min(score, 5.0)
                if answer["problem"]:
                    notes.append(answer["problem"])
    if not checked:
        return {"checked": False, "passed": True, "score": None, "problems": [P_UNCHECKED], "fix": "", "kind": ""}
    result = {"checked": True, "passed": not problems, "score": score, "problems": problems, "fix": fix, "kind": kind}
    if ffmpeg and notes:
        result["note"] = notes[-1]
    return result


def rank(review):
    """가장 나은 버전 고르기용 정렬 키 — 통과한 것 먼저, 그다음 점수. 점검하지 못한 것은 중간."""
    if not review:
        return (0, 0.0)
    score = review.get("score")
    return (1 if review.get("passed") else 0, 5.0 if score is None else float(score))

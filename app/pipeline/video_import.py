"""[팀 직접 구현] 완성 영상 불러오기 — 컷(장면) 경계를 찾아 장면별 클립으로 나눈다.

ffmpeg의 scene 점수는 바로 이어지는 두 프레임만 비교해서 디졸브·페이드 전환을 놓친다.
여기서는 시각 t 앞 0.5초의 평균 화면과 뒤 0.5초의 평균 화면을 비교한다 (밝기로 나눠 어두운 화면의 글자 변화도 잡는다).
카메라가 계속 움직이는 장면은 변화량이 높게 '이어지고', 장면 전환은 주변보다 '튀므로'
절대값이 아니라 주변(앞뒤 0.75~2초) 대비 얼마나 튀는지로 컷을 고른다. 봉우리 사이가 너무 짧으면 큰 쪽만 남긴다.
"""
import re
import subprocess
from pathlib import Path

import numpy as np

ANALYZE_FPS = 24          # 분석용 프레임 수(초당) — 대부분의 영상 fps와 같아 컷을 프레임 단위로 맞춘다
ANALYZE_SIZE = (160, 90)  # 분석용 프레임 크기 (가로, 세로) — 제목 카드의 작은 글자도 보이게
WINDOW_SEC = 0.5          # 앞뒤로 이만큼의 평균 화면을 비교 (디졸브 길이보다 길게)
NEIGHBOR_SEC = (0.75, 2.0)  # 주변 기준값을 잴 범위 (컷에서 떨어진 거리)


def _gray_frames(ffmpeg, path):
    w, h = ANALYZE_SIZE
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(path),
           "-vf", f"fps={ANALYZE_FPS},scale={w}:{h}:flags=area,format=gray",
           "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    frames = np.frombuffer(raw, dtype=np.uint8)
    n = len(frames) // (w * h)
    return frames[: n * w * h].reshape(n, h * w).astype(np.float32) / 255.0


def change_curve(frames):
    """curve[t] = |앞 0.5초 평균 화면 − 뒤 0.5초 평균 화면| / 평균 밝기. 봉우리가 장면 전환."""
    w = max(1, int(round(WINDOW_SEC * ANALYZE_FPS)))
    n = len(frames)
    curve = np.zeros(n, dtype=np.float32)
    if n <= 2 * w:
        return curve
    csum = np.concatenate([np.zeros((1, frames.shape[1]), np.float32), np.cumsum(frames, axis=0)])
    for t in range(w, n - w + 1):
        before = (csum[t] - csum[t - w]) / w
        after = (csum[t + w] - csum[t]) / w
        curve[t] = np.abs(before - after).mean() / (0.5 * (before.mean() + after.mean()) + 0.03)
    return curve


def _frame_jumps(frames):
    """바로 이웃한 프레임끼리의 차이 — 하드 컷의 정확한 프레임을 찾는 데 쓴다."""
    jumps = np.zeros(len(frames), dtype=np.float32)
    if len(frames) > 1:
        jumps[1:] = np.abs(frames[1:] - frames[:-1]).mean(axis=1)
    return jumps


def _refine(i, jumps, brightness):
    """봉우리 위치를 실제 경계 프레임으로 맞춘다.
    - 하드 컷: 이웃 프레임 차이가 확 튀는 프레임
    - 검은 화면을 거치는 페이드: 가장 어두운 프레임 (양쪽 장면보다 확실히 어두울 때만)
    - 그 밖의 디졸브: 봉우리(전환 한가운데) 그대로"""
    n = len(jumps)
    half = max(1, int(WINDOW_SEC * ANALYZE_FPS / 2))
    lo, hi = max(1, i - half), min(n - 1, i + half)
    j = max(range(lo, hi + 1), key=lambda k: jumps[k])
    typical = float(np.median(jumps[max(1, i - 4 * half): i + 4 * half])) or 1e-4
    if jumps[j] >= 4 * typical and jumps[j] >= 0.02:
        return j
    if brightness is not None:
        wide = int(0.75 * ANALYZE_FPS)
        lo, hi = max(0, i - wide), min(n - 1, i + wide)
        k = min(range(lo, hi + 1), key=lambda m: brightness[m])
        side = 2 * half
        before = brightness[max(0, lo - side): lo]
        after = brightness[hi + 1: hi + 1 + side]
        if len(before) and len(after) and brightness[k] < 0.85 * min(before.mean(), after.mean()):
            return k
    return i


def pick_cuts(curve, duration, min_scene_sec=1.0, sensitivity=1.0, jumps=None, brightness=None):
    """변화량 곡선 → 컷 시각(초) 목록. sensitivity가 클수록 더 많이 자른다."""
    n = len(curve)
    if n < 3:
        return []
    near, far = (int(s * ANALYZE_FPS) for s in NEIGHBOR_SEC)
    s = max(sensitivity, 0.1)
    peaks = []
    for i in range(1, n - 1):
        if not (curve[i] >= curve[i - 1] and curve[i] >= curve[i + 1]):
            continue
        around = np.concatenate([curve[max(0, i - far): max(0, i - near)], curve[i + near: i + far]])
        baseline = float(np.median(around)) if len(around) else 0.0
        # 주변보다 확실히 튀어야 컷: 카메라 이동처럼 계속 높은 구간은 제외
        if curve[i] >= 0.06 / s and curve[i] >= (2.5 * baseline + 0.02) / s:
            peaks.append(i)
    min_gap = int(min_scene_sec * ANALYZE_FPS)
    chosen = []
    for i in sorted(peaks, key=lambda i: -curve[i]):  # 큰 봉우리부터, 가까운 작은 봉우리는 버린다
        if all(abs(i - j) >= min_gap for j in chosen):
            chosen.append(i)
    if jumps is not None:
        chosen = [_refine(i, jumps, brightness) for i in chosen]
    cuts = sorted(round(i / ANALYZE_FPS, 2) for i in chosen)
    return [c for c in cuts if min_scene_sec <= c <= duration - min_scene_sec]


def detect_cuts(ffmpeg, path, duration, min_scene_sec=1.0, sensitivity=1.0):
    frames = _gray_frames(ffmpeg, path)
    curve = change_curve(frames)
    return pick_cuts(curve, duration, min_scene_sec, sensitivity,
                     jumps=_frame_jumps(frames), brightness=frames.mean(axis=1)), curve


def normalize_cuts(cuts, duration, min_scene_sec=0.5):
    """사용자가 고친 컷 목록 정리: 숫자만, 정렬, 영상 범위 안, 너무 가까운 컷은 하나로."""
    out = []
    for c in sorted(float(c) for c in cuts):
        if min_scene_sec <= c <= duration - min_scene_sec and all(abs(c - o) >= min_scene_sec for o in out):
            out.append(round(c, 2))
    return out


def segments(cuts, duration):
    edges = [0.0] + list(cuts) + [round(duration, 3)]
    return [(edges[i], edges[i + 1]) for i in range(len(edges) - 1)]


def split_clip(ffmpeg, src, start, end, out_path):
    """정확한 경계로 자르기 위해 다시 인코딩한다 (키프레임 단위 복사는 경계가 밀린다)."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.3f}", "-i", str(src),
           "-t", f"{end - start:.3f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out_path)]
    subprocess.run(cmd, check=True, capture_output=True)
    return out_path


def grab_frame(ffmpeg, src, t, out_path, width=None):
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    vf = ["-vf", f"scale={width}:-2"] if width else []
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{max(t, 0):.3f}", "-i", str(src),
           "-frames:v", "1", *vf, str(out_path)]
    subprocess.run(cmd, check=True, capture_output=True)
    return out_path


# ── 자막(.srt) → 장면별 문장 ──
_SRT_TIME = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")


def parse_srt(text):
    """[(시작초, 끝초, 문장)]"""
    items = []
    for block in re.split(r"\n\s*\n", (text or "").replace("\r\n", "\n").strip()):
        lines = [l for l in block.split("\n") if l.strip()]
        for i, line in enumerate(lines):
            m = _SRT_TIME.search(line)
            if m:
                g = [int(x) for x in m.groups()]
                start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
                end = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
                body = " ".join(l.strip() for l in lines[i + 1:])
                if body:
                    items.append((start, end, body))
                break
    return items


def subtitles_by_scene(subs, segs):
    """자막 한 줄은 겹치는 시간이 가장 긴 장면에 붙인다."""
    out = [[] for _ in segs]
    for start, end, body in subs:
        overlaps = [max(0.0, min(end, b) - max(start, a)) for a, b in segs]
        if overlaps and max(overlaps) > 0:
            out[overlaps.index(max(overlaps))].append(body)
    return [" ".join(x) for x in out]


def aspect_of(width, height):
    ratio = width / height if height else 1.0
    return min((("9:16", 9 / 16), ("16:9", 16 / 9), ("1:1", 1.0)), key=lambda a: abs(a[1] - ratio))[0]


# ── 프로젝트 만들기 ─────────────────────────────────────────────────────────
# 불러온 영상은 ③ 확인(video_ready) 상태의 1차 버전이 된다. 장면 클립·첫 프레임은 생성 결과와 같은 자리에 두므로
# 장면별 보기·피드백·컷 다시 만들기(첫 프레임 → I2V)·다음 버전의 부분 재생성이 그대로 동작한다.
# 장면 묘사(영어)는 ComfyUI의 Gemma가 장면 가운데 프레임을 보고 쓴다. ComfyUI가 꺼져 있으면 빈 자리로 남는다.
PENDING_SHOT_TEXT = "(장면 묘사 작성 중)"
MISSING_SHOT_TEXT = "(장면 묘사 없음 — 다음 버전 프롬프트에서 영어로 직접 적어 주세요)"

_CAPTION_SYSTEM = (
    "You write one shot description for an AI text-to-video model from a still frame of a video scene. "
    "Write a single English paragraph (40-70 words) describing the subject, setting, background, colors, "
    "lighting and camera framing so the model can recreate the shot. Ignore any subtitles or captions "
    "burned into the frame, but describe large on-screen title text if it is the main subject. "
    "Output only the description."
)


def shot_lines(durations, texts):
    return "\n".join(f"{i}. [{d:.2f}초] {t}" for i, (d, t) in enumerate(zip(durations, texts), start=1))


def build_prompt(title, genre, description, durations, texts, style=""):
    from .prompt_builder import render_sections

    sections = {
        "프로젝트": title,
        "영상 종류": genre or "불러온 영상",
        "버전": "1차 (완성 영상 불러오기)",
        "영상 설명": description or f"이미 만든 완성 영상 「{title}」을 장면별로 나눠 불러왔습니다.",
        "샷 목록": shot_lines(durations, texts),
    }
    if style:
        sections["영상 스타일"] = style
    return render_sections(sections)


def create_project(app, source_path, title, cuts, genre="", description="", style="", subtitles=None):
    """불러온 영상으로 프로젝트·1차 버전을 만들고 장면 나누기 작업을 큐에 넣는다. PipelineSession 반환."""
    import shutil

    from ..models import PipelineSession, VideoRound, db
    from . import approval, jobs, orchestrator
    from .video_generator.comfyui.rough_cut import probe_media, resolve_ffmpeg

    cfg = app.config
    ffmpeg = resolve_ffmpeg(cfg)
    info = probe_media(ffmpeg, source_path)
    if info is None:
        raise ValueError("영상 정보를 읽을 수 없습니다.")
    cuts = normalize_cuts(cuts, info["duration"])
    segs = segments(cuts, info["duration"])
    if len(segs) > cfg["LOCAL_VIDEO_MAX_SHOTS"]:
        raise ValueError(f"장면이 {len(segs)}개입니다. 한 영상은 장면 {cfg['LOCAL_VIDEO_MAX_SHOTS']}개까지라 "
                         f"컷을 줄이거나 LOCAL_VIDEO_MAX_SHOTS를 올려 주세요.")
    durations = [round(b - a, 2) for a, b in segs]
    prompt_text = build_prompt(title, genre, description, durations, [PENDING_SHOT_TEXT] * len(segs), style)

    session = PipelineSession(title=title, genre=genre or "불러온 영상", style=style,
                              aspect_ratio=aspect_of(info["width"], info["height"]), shot_seconds=0.0)
    db.session.add(session)
    db.session.flush()
    round_obj = VideoRound(session_id=session.id, round_number=1, prompt_text=prompt_text)
    db.session.add(round_obj)
    db.session.flush()
    approval.add_version(session.id, round_obj.id, "", prompt_text, "1차: 완성 영상 불러오기")

    out_dir = orchestrator.output_dir(app, round_obj)
    out_dir.mkdir(parents=True, exist_ok=True)
    source = out_dir / ("source" + Path(source_path).suffix.lower())
    shutil.move(str(source_path), source)
    round_obj.video_path = str(source)  # 올릴 영상 = 불러온 원본
    round_obj.adapter_used = "import"
    orchestrator.advance(round_obj, "generating", commit=False)
    round_obj.generation_progress = 0.0
    round_obj.generation_detail = round_obj.video_job_message = "불러온 영상을 장면별로 나누는 중"
    round_obj.video_job_status = "running"
    db.session.commit()
    jobs.enqueue("import_video", round_obj.id, {"cuts": cuts, "subtitles": subtitles or []})
    return session


def bgm_wanted(round_obj, config):
    """불러온 영상에도 배경음악을 깔지 — 영상에 소리가 없고 BGM_AUTO일 때만 (소리가 있으면 원래 소리를 지킨다)."""
    from .video_generator.comfyui.rough_cut import probe_media, resolve_ffmpeg

    if not config.get("BGM_AUTO", True) or round_obj.music_json:
        return False
    info = probe_media(resolve_ffmpeg(config), round_obj.video_path)
    return bool(info) and not info.get("audio")


def run_import(app, round_id, payload):
    """작업 큐에서 실행: 장면 클립 자르기 → 첫 프레임 → 장면 묘사 → ③ 확인으로."""
    import logging

    from ..models import ShotRender, VideoRound, db
    from . import comfy_text, orchestrator, storyboard_model as sbm
    from .prompt_builder import parse_sections, render_sections
    from .video_generator.comfyui.rough_cut import probe_media, resolve_ffmpeg
    from .video_generator.mock_adapter import build_storyboard as storyboard_text

    cfg = app.config
    ffmpeg = resolve_ffmpeg(cfg)
    round_obj = db.session.get(VideoRound, round_id)
    source = Path(round_obj.video_path)
    info = probe_media(ffmpeg, source)
    segs = segments(payload.get("cuts") or [], info["duration"])
    storyboard = sbm.build_storyboard(round_obj.prompt_text, cfg, aspect=round_obj.session.aspect_ratio)
    subs = subtitles_by_scene([tuple(s) for s in payload.get("subtitles") or []], segs)
    # .srt 문장: 영상에 소리가 없으면 [내레이션](TTS로 읽고 자막으로도 띄움), 소리가 있으면 [자막](화면에만).
    # 프롬프트 문서에 넣어야 다음 버전에도 이어진다 (스토리보드에만 두면 2차에서 사라졌다).
    speak = not info.get("audio")
    for shot, text in zip(storyboard["shots"], subs):
        shot["narration" if speak else "subtitle"] = text
    out_dir = orchestrator.output_dir(app, round_obj)
    total = len(segs)

    def progress(i, message):
        round_obj.generation_progress = round(i / max(total, 1) * 0.9, 3)
        round_obj.generation_detail = message
        db.session.commit()

    middles = []
    for i, ((start, end), shot) in enumerate(zip(segs, storyboard["shots"])):
        progress(i, f"장면 {i + 1}/{total} 나누는 중")
        shot_dir = out_dir / shot["id"]
        clip = split_clip(ffmpeg, source, start, end, shot_dir / "v1.mp4")
        # 컷 다시 만들기(I2V)의 시작 이미지: 디졸브가 섞이지 않게 장면 시작에서 조금 들어간 프레임
        first = grab_frame(ffmpeg, source, start + min(0.25, (end - start) / 4), shot_dir / "v1.png")
        middles.append(grab_frame(ffmpeg, source, (start + end) / 2, shot_dir / "middle.png", width=768))
        params = sbm.resolve_params(storyboard, shot, cfg)
        db.session.add(ShotRender(
            round_id=round_obj.id, shot_id=shot["id"], version=1, status="done", generator="local",
            provider="import", selected=True, workflow="import", prompt=shot["prompt"], seed=params["seed"],
            width=params["width"], height=params["height"], frames=params["frames"], fps=params["fps"],
            duration=shot["duration"], path=str(clip), image_path=str(first)))
        db.session.commit()

    texts = [MISSING_SHOT_TEXT] * total
    c = comfy_text.client(cfg)
    if c is not None:
        for i, img in enumerate(middles):
            progress(i, f"장면 {i + 1}/{total} 묘사 작성 중 (ComfyUI)")
            try:
                text = comfy_text.generate(c, cfg, _CAPTION_SYSTEM,
                                           "Describe this frame as a text-to-video shot description.",
                                           image_path=img, max_length=300)
            except Exception:
                logging.getLogger(__name__).warning("장면 묘사 실패", exc_info=True)
                break
            texts[i] = " ".join(text.split()) or MISSING_SHOT_TEXT

    sections = parse_sections(round_obj.prompt_text)
    sections["샷 목록"] = shot_lines([s["duration"] for s in storyboard["shots"]], texts)
    if any(subs):
        sections["내레이션" if speak else "자막"] = "\n".join(f"{i}. {t}" for i, t in enumerate(subs, start=1) if t)
    round_obj.prompt_text = render_sections(sections)
    for shot, text in zip(storyboard["shots"], texts):
        shot["prompt"] = text
    for r in round_obj.shot_renders:
        r.prompt = sbm.get_shot(storyboard, r.shot_id)["prompt"]
    version = round_obj.session.versions[-1] if round_obj.session.versions else None
    if version is not None and version.round_id == round_obj.id:
        version.content = round_obj.prompt_text  # 1차 버전 기록도 묘사가 채워진 문서로
    from . import edit_plan
    plan = edit_plan.loads(round_obj.edit_json or "")
    if any(subs):  # 자막 파일을 함께 올렸으면 자막을 띄우는 것이 기본
        plan["subtitles"] = True
    if speak:  # 원본에 소리가 없으면 장면 소리도 끈다 — 다시 만든 장면에만 LTX 효과음이 생기지 않게
        plan["scene_sound"] = False
    plan = edit_plan.normalize(plan, [s["id"] for s in storyboard["shots"]])
    if not edit_plan.is_default(plan):
        storyboard["edit"] = plan
        round_obj.edit_json = edit_plan.dumps(plan)
    round_obj.storyboard_json = sbm.dumps(storyboard)
    round_obj.storyboard = storyboard_text(round_obj.prompt_text)
    if speak and any(subs) or bgm_wanted(round_obj, cfg):
        # 소리 없는 영상 + 자막 → 내레이션(TTS)·배경음악을 얹은 올릴 영상을 바로 만든다 (③ 확인에서 들어 본다)
        progress(total, "내레이션·배경음악을 얹어 올릴 영상 만드는 중")
        try:
            orchestrator.rebuild_rough_cut(app, round_obj, storyboard)
        except Exception as exc:  # 실패해도 원본 영상으로 확인 단계에 간다
            logging.getLogger(__name__).warning("불러온 영상 러프컷 실패", exc_info=True)
            round_obj.video_path = str(source)
            round_obj.edit_note = f"올릴 영상을 새로 만들지 못해 원본을 씁니다: {exc}"
    round_obj.generation_progress = 1.0
    round_obj.generation_detail = ""
    round_obj.generation_error = ""
    round_obj.video_job_status, round_obj.video_job_message = "idle", ""
    orchestrator.advance(round_obj, "video_ready", commit=False)
    try:
        from .metrics import calculate_metrics
        calculate_metrics(round_obj, cfg)
    except Exception:
        logging.getLogger(__name__).warning("지표 계산 실패", exc_info=True)
    db.session.commit()

"""[팀 직접 구현] 파이프라인 오케스트레이터 — 상태 전이의 단일 진입점.

9단계: prompt_built → generating → video_ready → uploaded → feedback_collected
       → feedback_classified → clustering → pending_review → prompt_updated
실패: generating → generation_failed (실패한 장면만 재시도)
단계별 확인(COMFYUI_STAGED_REVIEW): generating → images_ready(이미지 확인) → generating → clips_ready(컷 확인)
       → generating(러프컷) → video_ready. images_ready·clips_ready도 ② 단계에 표시된다.

라우트는 round.status를 직접 바꾸지 않고 advance()만 호출한다.
영상 생성·재생성·클러스터링 본체(run_*)는 jobs.py의 워커 스레드에서 실행된다.
"""
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

from ..models import (
    ALL_STATUSES, CLIPS_READY, FAILED_STATUS, IMAGES_READY, REVIEW_STATUSES, ROUND_STATUSES,
    ClusterResult, Feedback, GenerationJob, ShotRender, VideoRound, db,
)
from . import approval, edit_plan, narration, quality_gate, storyboard_model as sbm, youtube_client
from .classifier import create_classifier
from .classifier.timestamp import extract_timestamp_sec
from .feedback_target import REGEN_TARGETS
from .clusterer import run_clustering
from .metrics import calculate_metrics
from .video_generator import commercial_available, create_commercial_adapter, resolve_local_adapter
from .video_generator.base import VideoGenerationResult
from .video_generator.comfyui import rough_cut
from .video_generator.factory import configured_name
from .video_generator.mock_adapter import build_storyboard

log = logging.getLogger(__name__)

CANCEL_MESSAGE = "사용자가 생성을 중지했습니다. 만들어진 장면은 남아 있고, '재시도'로 이어서 만들 수 있습니다."
_cancel_requests = set()  # 중지가 요청된 round id (워커가 장면 사이마다 확인한다)

# 같은 상태로의 전이(자기 자신)는 "같은 단계에서 반복 실행"을 뜻한다 (댓글 추가, 재분류 등)
TRANSITIONS = {
    "prompt_built": {"generating"},
    "generating": {"video_ready", FAILED_STATUS, IMAGES_READY, CLIPS_READY},
    # prompt_built: '다시 계획하기' — 만든 이미지·컷을 지우고 프롬프트에서 장면 계획부터 다시 (restart_planning)
    FAILED_STATUS: {"generating", "prompt_built"},
    IMAGES_READY: {"generating", "prompt_built"},   # 이미지 다시 뽑기 / 승인 후 컷 영상 만들기
    CLIPS_READY: {"generating", "prompt_built"},    # 컷 다시 만들기 / 러프컷 만들기
    "video_ready": {"generating", "uploaded"},   # generating: 품질 부족 장면 상용 API 재생성
    "uploaded": {"feedback_collected", "video_ready"},  # video_ready: 공개 취소 — 댓글을 모으기 전에만 (unpublish)
    "feedback_collected": {"feedback_collected", "feedback_classified"},
    "feedback_classified": {"feedback_classified", "clustering"},
    "clustering": {"pending_review", "feedback_classified"},  # 실패하면 분류 단계로 복귀
    "pending_review": {"pending_review", "clustering", "prompt_updated"},
    "prompt_updated": set(),
}

STEP_LABELS = [
    ("prompt_built", "프롬프트"), ("generating", "생성 중"), ("video_ready", "영상 완성"),
    ("uploaded", "업로드"), ("feedback_collected", "피드백 수집"),
    ("feedback_classified", "분류"), ("clustering", "클러스터링"),
    ("pending_review", "검토 대기"), ("prompt_updated", "반영 완료"),
]


class InvalidTransition(Exception):
    pass


class PipelineError(Exception):
    """사용자에게 그대로 보여줄 수 있는 흐름 오류."""


# ── 상태 전이 ────────────────────────────────────────────────────────────
def advance(round_obj, to_state, commit=True):
    """허용된 전이만 수행한다. 아니면 InvalidTransition."""
    if to_state not in ALL_STATUSES:
        raise InvalidTransition(f"알 수 없는 상태: {to_state}")
    allowed = TRANSITIONS.get(round_obj.status, set())
    if to_state not in allowed:
        raise InvalidTransition(f"{round_obj.status} → {to_state} 전이는 허용되지 않습니다.")
    round_obj.status = to_state
    if to_state == "generating":
        round_obj.generation_error = ""
    if commit:
        db.session.commit()
    return round_obj


def can_advance(round_obj, to_state):
    return to_state in TRANSITIONS.get(round_obj.status, set())


def _step_number(state):
    return ROUND_STATUSES.index("generating") + 1 if state == FAILED_STATUS or state in REVIEW_STATUSES \
        else ROUND_STATUSES.index(state) + 1


def compute_steps(round_obj, config):
    """상태 → 9단계 표시 정보. status: done/active/waiting/failed/pending."""
    state = round_obj.status
    current = _step_number(state) if state in ALL_STATUSES else 1
    require_approval = config.get("REQUIRE_VIDEO_APPROVAL", True)
    steps = []
    for no, (key, label) in enumerate(STEP_LABELS, start=1):
        step = {"no": no, "key": key, "label": label}
        if no < current:
            step["status"] = "done"
        elif no > current:
            step["status"] = "pending"
        elif state == FAILED_STATUS:
            step["status"] = "failed"
            step["error"] = round_obj.generation_error or "영상 생성 실패"
        elif state in REVIEW_STATUSES:
            step["status"] = "waiting"
            step["detail"] = ("장면 이미지 확인 대기" if state == IMAGES_READY
                              else "장면 컷 확인 대기 — 다 되면 올릴 영상 만들기")
        elif state in ("generating", "clustering"):
            step["status"] = "active"
            if state == "generating":
                step["progress"] = round(round_obj.generation_progress or 0.0, 3)
                step["detail"] = round_obj.generation_detail or ""
        elif state == "pending_review":
            step["status"] = "waiting"
        elif state == "video_ready" and require_approval and not round_obj.video_approved:
            step["status"] = "waiting"
            step["detail"] = "올릴 영상 확인 대기"
        else:
            step["status"] = "done"
        steps.append(step)
    _move_rough_cut_to_confirm(round_obj, steps)
    return steps


def rough_cut_in_progress(round_obj):
    """러프컷(올릴 영상) 합성 작업이 대기·실행 중인가. 장면 제작과 구분해 '확인' 단계에 표시한다."""
    from ..models import GenerationJob
    jobs_ = GenerationJob.query.filter(GenerationJob.round_id == round_obj.id,
                                       GenerationJob.status.in_(("queued", "running"))).all()
    return any(json.loads(j.payload_json or "{}").get("stage") == "rough" for j in jobs_)


def _move_rough_cut_to_confirm(round_obj, steps):
    """②제작 = 장면별 영상 만들기, ③확인 = 올릴 영상(러프컷) 만들기·확인.
    서버 상태는 둘 다 generating/generation_failed지만, 러프컷 작업이면 ③에 표시한다."""
    gen = next(s for s in steps if s["key"] == "generating")
    confirm = next(s for s in steps if s["key"] == "video_ready")
    rough_failed = (round_obj.status == FAILED_STATUS
                    and (round_obj.generation_error or "").startswith("러프컷"))
    rough_running = round_obj.status == "generating" and rough_cut_in_progress(round_obj)
    if not (rough_failed or rough_running):
        return
    moved = {k: gen.pop(k) for k in ("progress", "detail", "error") if k in gen}
    confirm.update(moved)
    confirm["status"] = gen["status"]
    gen["status"] = "done"
    if rough_running and not confirm.get("detail"):
        confirm["detail"] = "올릴 영상 만드는 중"


# ── 경로 ─────────────────────────────────────────────────────────────────
def output_dir(app, round_obj):
    base = app.config.get("VIDEO_OUTPUT_DIR") or os.path.join(app.instance_path, "videos")
    return Path(base) / f"session_{round_obj.session_id}" / f"round_{round_obj.round_number}"


# ── 스토리보드 스냅샷 / 부분 재생성 계획 ──────────────────────────────────
def _previous_round(round_obj):
    return (VideoRound.query
            .filter(VideoRound.session_id == round_obj.session_id,
                    VideoRound.round_number < round_obj.round_number)
            .order_by(VideoRound.round_number.desc()).first())


def ensure_storyboard(round_obj, config):
    """라운드 스토리보드 스냅샷을 돌려준다. 없으면 프롬프트 문서에서 만들어 저장한다."""
    existing = round_obj.storyboard_data
    if existing:
        return existing
    session = round_obj.session
    storyboard = sbm.build_storyboard(
        round_obj.prompt_text, config, aspect=session.aspect_ratio,
        shot_seconds=session.shot_seconds or None)
    prev = _previous_round(round_obj)
    if prev is not None and config.get("PARTIAL_REGEN_ENABLED", True):
        _apply_feedback_to_shots(storyboard, prev, round_obj)
    # 편집 설정(이전 버전 검토에서 정한 전환·소리·자막·길이·순서)은 러프컷 합성에만 쓴다 — 장면 서명에 들어가지 않는다
    plan = edit_plan.loads(round_obj.edit_json or "", [s["id"] for s in storyboard["shots"]])
    if not edit_plan.is_default(plan):
        storyboard["edit"] = plan
    round_obj.storyboard_json = sbm.dumps(storyboard)
    db.session.commit()
    return storyboard


def affected_shot_ids(prev_round):
    """이전 라운드에서 승인된 클러스터가 지적한 장면 ID → [(클러스터 요지, 반영 위치)].
    화면·움직임 의견만 장면을 다시 만든다 — 소리·편집·내용 의견은 장면을 그대로 둔다 (feedback_target.py)."""
    affected = {}
    for cluster in prev_round.clusters:
        if cluster.status != "approved" or cluster.target not in REGEN_TARGETS:
            continue
        for shot_id in cluster.affected_shots:
            affected.setdefault(shot_id, []).append((cluster.representative_text, cluster.target))
    return affected


def _apply_feedback_to_shots(storyboard, prev_round, round_obj):
    """지적된 장면은 시드를 바꿔 다시 만들고, 피드백 요지를 notes로 남긴다.
    움직임 의견만 있는 장면은 이전 버전의 첫 프레임을 그대로 써서 모양을 유지하고 컷 영상만 다시 만든다."""
    previous = {r.shot_id: r for r in prev_round.shot_renders if r.selected}
    for shot_id, items in affected_shot_ids(prev_round).items():
        shot = sbm.get_shot(storyboard, shot_id)
        if shot is None:
            continue
        shot["notes"] = list(dict.fromkeys(text for text, _target in items))
        shot["seed"] = int(storyboard["defaults"]["seed"]) + round_obj.round_number
        old = previous.get(shot_id)
        if all(t == "motion" for _text, t in items) and old is not None and old.image_path \
                and os.path.exists(old.image_path):
            shot["image"] = old.image_path


# 고쳐 쓰는 방법(지시문·모델)을 바꾸면 올린다 — 예전 방법으로 고쳐 둔 묘사를 다시 고쳐 쓰게.
# 2: 큰 모델(26B) 먼저 + 'X 대신 Y'면 X를 지우고 개수는 총수로 (2026-10-23)
# 3: 영상 묘사와 첫 프레임 묘사(동작 시작 전)를 따로 (2026-10-23)
# 4: 동작과 관계없는 사람은 하던 일을 계속, 요청한 동작 뒤엔 아무 일도 없게 — 로봇 팔이 큐브를 들자 옆 사람이 손을 뻗어 받아 갔다 (2026-10-23)
# 5: 최소 수정(바꿀 부분만) + 인물 생김새(옷·머리·안경)가 빠지면 다시 묻고 되살림 (2026-10-23)
# 6: 잡는 동작이면 물체를 그리퍼보다 작게 + 손가락이 양옆 바깥 면을 잡게 — 큰 큐브에 그리퍼가 박혔다 (2026-10-23)
# 7: 잡을 물체 크기는 익숙한 물건으로(주사위만 한), 놓인 자리(팔 받침대 옆, 사람 손과 떨어진 책상 위)도 — Z-Image가 상대 크기를 못 따라 큐브가 공중에 뜨거나 손에 닿았다 (2026-10-23)
REVISE_VERSION = 7


def _notes_key(shot):
    import hashlib
    raw = f"{REVISE_VERSION}|" + shot["prompt"].strip() + "|" + "|".join(shot.get("notes") or [])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def ensure_english_notes(round_obj, storyboard, config):
    """장면 notes(한국어 의견)를 영상 모델에 줄 영어로 바꿔 둔다. 한국어 원문은 절대 넣지 않는다 (글자로 찍힌다).
    1순위: 장면 묘사 자체를 의견대로 고쳐 쓴 prompt_fixed (feedback_target.revise_shot) — final_prompt가 원래 묘사 대신 쓴다.
          첫 프레임(이미지 모델)에는 동작이 시작되기 전 모습 image_prompt_fixed를 따로 쓴다.
    2순위(고쳐 쓰기 실패): 예전처럼 의견마다 짧은 영어 지시문 notes_en을 꼬리에 붙인다.
    의견·원래 묘사가 바뀌면(prompt_fixed_key) 다시 고쳐 쓴다. 화면과 무관한 의견은 빠진다."""
    from contextlib import nullcontext

    from .feedback_target import revise_shot, to_visual_instruction
    from . import comfy_text, llm

    client = None
    changed = False
    for shot in storyboard["shots"]:
        if not shot.get("notes") and shot.pop("prompt_fixed", None) is not None:
            shot.pop("prompt_fixed_key", None)
            shot.pop("image_prompt_fixed", None)
            changed = True
    pending = [s for s in storyboard["shots"] if s.get("notes") and s.get("prompt_fixed_key") != _notes_key(s)]
    # 고쳐 쓸 장면이 있으면 큰 모델을 한 번만 켜 두고 다 쓰면 끈다 (작업 안 — 장면을 만들기 전, ComfyUI가 쉬는 때)
    with llm.strong_server(config, in_job=True) if pending else nullcontext():
        for shot in pending:
            fixed = revise_shot(shot["prompt"], shot["notes"], config, client=client)
            if fixed is not None:
                shot["prompt_fixed"], shot["prompt_fixed_key"] = fixed[0], _notes_key(shot)
                shot["image_prompt_fixed"] = fixed[1] if fixed[1] != fixed[0] else ""
                changed = True
    todo = [s for s in storyboard["shots"]
            if s.get("notes") and s.get("prompt_fixed_key") != _notes_key(s)
            and len(s.get("notes_en") or []) != len(s["notes"])]
    if not todo:
        if changed:
            round_obj.storyboard_json = sbm.dumps(storyboard)
            db.session.commit()
        return storyboard
    client = client or comfy_text.client(config)
    for shot in todo:
        done = list(shot.get("notes_en") or [])
        for note in shot["notes"][len(done):]:
            converted = to_visual_instruction(note, config, client=client, shot_prompt=shot["prompt"])
            if converted is None:  # 변환 엔진 없음 → 이번에는 의견 없이, 다음 실행 때 다시 시도
                break
            done.append(converted)
        shot["notes_en"] = done
    round_obj.storyboard_json = sbm.dumps(storyboard)
    db.session.commit()
    return storyboard


def plan_reuse(round_obj, storyboard, config):
    """{shot_id: 이전 라운드 ShotRender} — 그대로 재사용할 장면."""
    prev = _previous_round(round_obj)
    if prev is None or not config.get("PARTIAL_REGEN_ENABLED", True):
        return {}
    prev_sb = prev.storyboard_data
    if not prev_sb:
        return {}
    affected = affected_shot_ids(prev)
    chosen = {}
    for r in ShotRender.query.filter_by(round_id=prev.id, selected=True).all():
        if r.status in ("done", "reused"):
            chosen[r.shot_id] = r
    reuse = {}
    for shot in storyboard["shots"]:
        old_shot = sbm.get_shot(prev_sb, shot["id"])
        old_render = chosen.get(shot["id"])
        if old_shot is None or old_render is None or shot["id"] in affected:
            continue
        if sbm.shot_signature(prev_sb, old_shot) == sbm.shot_signature(storyboard, shot):
            reuse[shot["id"]] = old_render
    return reuse


# ── 영상 생성 (워커에서 실행) ─────────────────────────────────────────────
def _next_version(round_id, shot_id):
    last = (ShotRender.query.filter_by(round_id=round_id, shot_id=shot_id)
            .order_by(ShotRender.version.desc()).first())
    return (last.version if last else 0) + 1


def _select(render):
    for other in ShotRender.query.filter_by(round_id=render.round_id, shot_id=render.shot_id):
        other.selected = other.id == render.id


def _progress_writer(round_obj, total, done_before):
    """ComfyUI 콜백 → DB 진행률. 1초 간격으로만 기록한다."""
    last = [0.0]

    def cb(fraction, message):
        now = time.time()
        if now - last[0] < 1.0:
            return
        last[0] = now
        inner = fraction if fraction is not None else 0.0
        round_obj.generation_progress = min((done_before + inner) / max(total, 1), 0.99)
        if message:
            round_obj.generation_detail = f"장면 {done_before + 1}/{total} — {message}"
        db.session.commit()

    return cb


def _summarize_adapter(round_obj):
    providers = {r.provider.split("-")[0] for r in round_obj.shot_renders
                 if r.selected and r.status in ("done", "reused") and r.provider}
    if not providers:
        return ""
    return providers.pop() if len(providers) == 1 else "mixed"


def rebuild_rough_cut(app, round_obj, storyboard):
    """선택된 버전들을 스토리보드 해상도·fps로 맞춰 이어 붙인다. 파일이 없으면(mock) 건너뜀."""
    renders = {r.shot_id: r for r in round_obj.shot_renders if r.selected and r.path}
    clips = [(s["id"], renders[s["id"]].path) for s in storyboard["shots"]
             if s["id"] in renders and os.path.exists(renders[s["id"]].path)]
    if not clips:
        round_obj.video_path = ""
        return None
    ffmpeg = rough_cut.resolve_ffmpeg(app.config)
    if not ffmpeg:
        raise PipelineError("ffmpeg를 찾을 수 없습니다 (FFMPEG_PATH를 설정하세요).")
    d = storyboard["defaults"]
    out = output_dir(app, round_obj) / "rough_cut.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    font = "C:/Windows/Fonts/malgun.ttf" if app.config.get("ROUGH_CUT_LABEL") else None

    from . import voice
    voice_settings = voice.load(round_obj.session)
    if voice_settings["enabled"]:
        narration_clips, note = _prepare_narration(voice.apply(app.config, voice_settings), storyboard, out.parent)
    else:
        narration_clips, note = {}, _VOICE_OFF
    report = {}
    # 자막 문장: 내레이션, 없으면 불러온 영상의 자막
    subtitles = {s["id"]: (s.get("narration") or s.get("subtitle") or "").strip() for s in storyboard["shots"]}
    from . import overlay
    logo = overlay.load_logo(round_obj.session)
    logo_file = overlay.logo_path(app.instance_path, round_obj.session_id, logo)
    music, music_note = _prepare_music(app, round_obj, storyboard, out.parent)
    # AI가 만든 컷(LTX)은 시작이 멈칫하고 끝이 느려져 이어 붙이면 어색하다 → 앞뒤를 조금 잘라 낸다.
    # 불러온 영상의 장면은 원본 그대로 이어지므로 자르지 않는다.
    head, tail = float(app.config.get("CLIP_TRIM_HEAD_SEC", 0.15)), float(app.config.get("CLIP_TRIM_TAIL_SEC", 0.1))
    trims = {sid: (head, tail) for sid, r in renders.items() if _is_generated(r)} if head or tail else {}
    rough_cut.build(ffmpeg, clips, out, d["width"], d["height"], d["fps"], label_font=font,
                    narration=narration_clips or None,
                    narration_options={
                        "volume": app.config["NARRATION_VOLUME"], "bg_volume": app.config["NARRATION_BG_VOLUME"],
                        "max_speedup": app.config["NARRATION_MAX_SPEEDUP"], "lead": app.config["NARRATION_LEAD_SEC"]},
                    report=report, edit=storyboard.get("edit"),
                    subtitles={k: v for k, v in subtitles.items() if v} or None,
                    logo=logo, logo_file=str(logo_file) if logo_file else None, music=music, trims=trims)
    round_obj.video_path = str(out)
    notes = [report.get("edit_note", ""), music_note]
    if logo.get("enabled") and logo.get("file") and not logo_file:
        notes.append("로고 파일을 찾지 못했습니다")
    round_obj.edit_note = " · ".join(n for n in notes if n)
    _record_narration(round_obj, storyboard, narration_clips, note, report)
    round_obj.timeline_json = json.dumps(report.get("timeline") or {}, ensure_ascii=False)
    return out


def _is_generated(render):
    """영상 모델이 만든 컷인지 (불러온 원본 장면·그것을 그대로 쓴 장면이면 False)."""
    seen = 0
    while render is not None and seen < 10:
        if render.provider == "import" or render.workflow == "import":
            return False
        if render.status != "reused" or not render.reused_from_id:
            return render.provider not in ("", "mock")
        render = db.session.get(ShotRender, render.reused_from_id)
        seen += 1
    return False


def _prepare_music(app, round_obj, storyboard, work_dir):
    """배경음악을 켰으면 곡 파일을 준비한다 (같은 설명·길이·시드로 만든 곡이 있으면 그대로).
    ({"path", "volume"} 또는 None, 사유). 만들지 못해도 영상은 음악 없이 계속 만든다."""
    from . import bgm

    settings = bgm.load(round_obj, app.config)
    if not settings["enabled"]:
        return None, ""
    if not settings["caption"]:
        settings["caption"], _src = bgm.suggest_caption(round_obj.prompt_text, app.config, sbm.total_duration(storyboard))
    round_obj.generation_detail = "배경음악 만드는 중 (처음 한 번은 몇 분 걸립니다)"
    db.session.commit()
    try:
        path, settings = bgm.ensure_track(app.config, settings, sbm.total_duration(storyboard), work_dir)
    except Exception as exc:  # ComfyUI 꺼짐·실패 — 음악 없이 계속
        log.warning("배경음악 생략: %s", exc)
        bgm.save(round_obj, settings)
        return None, f"배경음악을 만들지 못했습니다: {exc}"
    bgm.save(round_obj, settings)
    return {"path": str(path), "volume": settings["volume"], "offset": settings["offset"]}, ""


_VOICE_OFF = "voice-off"  # 제작자가 내레이션을 끈 경우 (실패와 구분)


def _prepare_narration(config, storyboard, work_dir):
    """장면별 내레이션 wav 준비. 음성을 쓸 수 없으면 영상은 내레이션 없이 계속 만들고 사유를 남긴다."""
    wanted = [s for s in storyboard["shots"] if (s.get("narration") or "").strip()]
    if not wanted:
        return {}, ""
    try:
        return narration.prepare_clips(config, storyboard, work_dir / "narration"), ""
    except narration.NarrationError as exc:
        log.warning("내레이션 생략: %s", exc)
        return {}, str(exc)


def _record_narration(round_obj, storyboard, clips, note, report):
    shots = {}
    for s in storyboard["shots"]:
        text = (s.get("narration") or "").strip()
        if not text:
            continue
        fit = report.get("narration", {}).get(s["id"], {})
        shots[s["id"]] = {"text": text, "applied": s["id"] in clips, **fit}
    if not shots:
        round_obj.narration_json = ""
        return
    truncated = [i for i, v in shots.items() if v.get("truncated")]
    if note == _VOICE_OFF:
        status, detail = "off", "내레이션을 껐습니다 (배경음악·음성 탭에서 켤 수 있음)"
    elif note:
        status, detail = "skipped", f"내레이션을 넣지 못했습니다: {note}"
    elif truncated:
        status, detail = "warn", f"{', '.join(truncated)} 장면의 문장이 길어 끝이 잘렸습니다 — 문장을 줄여 주세요"
    else:
        sped = [i for i, v in shots.items() if v.get("speedup", 1) > 1.001]
        status = "ok"
        detail = f"{len(shots)}장면에 내레이션 적용" + (f" ({', '.join(sped)}는 장면에 맞춰 빠르게)" if sped else "")
    round_obj.narration_json = json.dumps(
        {"status": status, "detail": detail, "shots": shots}, ensure_ascii=False)


def run_generation(app, round_id, payload):
    """payload: {"shot_ids": [...]|None, "generator": "api"|None}.
    shot_ids가 없으면 이 라운드에서 아직 결과가 없는 장면을 모두 만든다 (실패 재시도 포함)."""
    if payload.get("stage"):  # 단계별 확인 생성 (이미지 → 컷 → 러프컷)
        return run_staged(app, round_id, payload)
    if payload.get("demo_replay"):
        return run_demo_replay(app, round_id, payload)
    cfg = app.config
    _cancel_requests.discard(round_id)  # 이전 중지 요청이 이번 실행을 막지 않게
    round_obj = db.session.get(VideoRound, round_id)
    storyboard = ensure_english_notes(round_obj, ensure_storyboard(round_obj, cfg), cfg)
    shots = {s["id"]: s for s in storyboard["shots"]}
    if not shots:
        raise PipelineError("프롬프트의 [샷 목록]에 '1. ...' 형식의 샷이 없습니다.")

    requested = payload.get("shot_ids")
    forced_generator = payload.get("generator")
    existing = {r.shot_id for r in round_obj.shot_renders
                if r.selected and r.status in ("done", "reused")}

    if requested:
        targets = [s for s in requested if s in shots]
        if forced_generator:
            for sid in targets:
                shots[sid]["generator"] = forced_generator
            round_obj.storyboard_json = sbm.dumps(storyboard)
    else:
        if not round_obj.shot_renders:  # 새 생성 → 이전 라운드 결과 재사용 계획
            for shot_id, old in plan_reuse(round_obj, storyboard, cfg).items():
                reused = ShotRender(
                    round_id=round_obj.id, shot_id=shot_id, version=1, status="reused",
                    generator=old.generator, provider=old.provider, path=old.path,
                    selected=True, workflow=old.workflow, prompt=old.prompt, seed=old.seed,
                    width=old.width, height=old.height, frames=old.frames, fps=old.fps,
                    duration=old.duration, reused_from_id=old.id, elapsed_sec=0.0)
                db.session.add(reused)
                existing.add(shot_id)
        targets = [s["id"] for s in storyboard["shots"] if s["id"] not in existing]
    db.session.commit()

    total = len(shots)
    done_before = len(existing)
    out_dir = output_dir(app, round_obj)
    local_adapter, fallback_reason = None, ""
    whole_cache = {}  # 장면 단위 미지원 어댑터는 전체 영상을 한 번만 만든다
    failures = []
    stopped = False

    for shot_id in targets:
        if round_id in _cancel_requests:  # 장면 사이에서 중지 — 남은 장면은 만들지 않는다
            stopped = True
            break
        shot = shots[shot_id]
        params = sbm.resolve_params(storyboard, shot, cfg)
        if shot.get("generator") == "api":
            adapter = create_commercial_adapter(cfg, http=app.config.get("_COMMERCIAL_HTTP"))
        else:
            if local_adapter is None:
                local_adapter, fallback_reason = resolve_local_adapter(cfg)
            adapter = local_adapter

        version = _next_version(round_obj.id, shot_id)
        render = ShotRender(
            round_id=round_obj.id, shot_id=shot_id, version=version, status="running",
            generator=shot.get("generator", "local"), workflow=params["workflow"],
            prompt=params["prompt"], seed=params["seed"], width=params["width"],
            height=params["height"], frames=params["frames"], fps=params["fps"],
            duration=params["duration"])
        db.session.add(render)
        round_obj.generation_detail = f"장면 {done_before + 1}/{total} 생성 중 ({shot_id})"
        db.session.commit()

        if adapter is None:
            render.status, render.error = "failed", "상용 API를 사용할 수 없습니다 (키 없음)."
            failures.append(shot_id)
            db.session.commit()
            continue
        out_path = out_dir / shot_id / f"v{version}.mp4"
        try:
            if adapter.supports_shots:
                result = adapter.generate_shot(
                    storyboard, shot, out_path, _progress_writer(round_obj, total, done_before))
                _mute_if_needed(cfg, storyboard, getattr(result, 'path', ''))
            else:  # diffusers 'local' 어댑터 — 장면 단위 미지원: 전체를 한 번에 만든다
                if "result" not in whole_cache:
                    out_dir.mkdir(parents=True, exist_ok=True)
                    whole_cache["result"] = _WholeVideo(adapter.generate(
                        round_obj.prompt_text, str(out_dir / "rough_cut.mp4"), None))
                result = whole_cache["result"]
        except Exception as exc:
            log.exception("장면 %s 생성 실패", shot_id)
            db.session.rollback()
            render = db.session.get(ShotRender, render.id)
            render.status, render.error = "failed", f"{type(exc).__name__}: {exc}"
            failures.append(shot_id)
            db.session.commit()
            continue

        render.status = "done"
        render.path = result.path
        render.image_path = getattr(result, "image_path", "") or ""
        render.provider = result.provider
        render.elapsed_sec = result.elapsed_sec
        render.peak_vram_mb = result.peak_vram_mb
        render.cost_usd = result.cost_usd
        render.enhanced_prompt = getattr(result, "enhanced_prompt", "") or ""
        _select(render)
        done_before += 1
        round_obj.generation_progress = done_before / total
        db.session.commit()
        try:
            adapter.after_shot()
        except Exception:  # VRAM 정리 실패는 결과에 영향 없음
            log.warning("after_shot 실패", exc_info=True)

    cancelled = stopped or (failures and round_id in _cancel_requests)
    _cancel_requests.discard(round_id)
    if cancelled:
        round_obj.generation_error = CANCEL_MESSAGE
        advance(round_obj, FAILED_STATUS, commit=False)
        round_obj.video_job_status = "failed"
        round_obj.video_job_message = CANCEL_MESSAGE
        db.session.commit()
        return

    if failures:
        round_obj.generation_error = "장면 생성 실패: " + ", ".join(failures)
        advance(round_obj, FAILED_STATUS, commit=False)
        round_obj.video_job_status = "failed"
        round_obj.video_job_message = round_obj.generation_error
        db.session.commit()
        return

    finalize_generation(app, round_obj, storyboard, fallback_reason)


class _WholeVideo:
    """장면 단위를 지원하지 않는 어댑터의 전체 영상 결과를 ShotResult처럼 다룬다."""

    def __init__(self, whole: VideoGenerationResult):
        self.path = whole.video_path
        self.provider = "local"
        self.elapsed_sec = 0.0
        self.peak_vram_mb = 0.0
        self.cost_usd = 0.0


def finalize_generation(app, round_obj, storyboard, fallback_reason=""):
    legacy_whole = any(r.provider == "local" and r.path for r in round_obj.shot_renders
                       if r.selected)
    if legacy_whole:
        paths = {r.path for r in round_obj.shot_renders if r.selected and r.path}
        round_obj.video_path = next(iter(paths)) if len(paths) == 1 else ""
    else:
        try:
            rebuild_rough_cut(app, round_obj, storyboard)
        except Exception as exc:
            log.exception("러프컷 합성 실패")
            round_obj.generation_error = f"러프컷 합성 실패: {exc}"
            advance(round_obj, FAILED_STATUS, commit=False)
            db.session.commit()
            return
    round_obj.storyboard = build_storyboard(round_obj.prompt_text)
    round_obj.adapter_used = _summarize_adapter(round_obj)
    round_obj.generation_progress = 1.0
    round_obj.generation_detail = (f"mock으로 대체됨 — {fallback_reason}" if fallback_reason else "")
    round_obj.generation_error = ""
    round_obj.video_approved = False
    round_obj.video_job_status = "idle"
    round_obj.video_job_message = ""
    advance(round_obj, "video_ready", commit=False)
    calculate_metrics(round_obj, app.config)
    db.session.commit()



# ── 단계별 확인 생성: 이미지 → 컷(장면 영상) → 러프컷 ─────────────────────
# 장면 1개는 ShotRender 한 줄이 상태만 바꿔 가며 진행된다:
#   running → image_ready(이미지 확인 대기) → image_approved(승인) → running → done(컷 완성)
# 이미지를 다시 뽑으면 새 버전 줄이 생기고 이전 이미지 줄은 discarded가 된다.
# 컷을 다시 만들면 같은 이미지로 새 버전 줄이 생기고 이전 컷은 done으로 남아 고를 수 있다.
SEED_REROLL_STEP = 101


def staged_enabled(config):
    return bool(config.get("COMFYUI_STAGED_REVIEW")) and configured_name(config) == "comfyui"


def _selected_by_shot(round_obj):
    chosen = {}
    for r in round_obj.shot_renders:  # id 순 — 같은 장면은 나중 것이 이긴다
        if r.selected:
            chosen[r.shot_id] = r
    return chosen


def _stage_progress(round_obj, total, index, label):
    """장면 하나 안의 진행률을 전체 진행률로 바꿔 DB에 기록한다 (1초 간격)."""
    last = [0.0]

    def cb(fraction, message):
        now = time.time()
        if now - last[0] < 1.0:
            return
        last[0] = now
        inner = fraction if fraction is not None else 0.0
        round_obj.generation_progress = min((index + inner) / max(total, 1), 0.99)
        round_obj.generation_detail = f"{label} {index + 1}/{total} — {message}" if message else ""
        db.session.commit()

    return cb


def _mute_if_needed(config, storyboard, path):
    """장면 소리를 끈 버전(예: 소리 없는 완성 영상을 불러온 프로젝트)이면 새로 만든 컷에서 소리를 뺀다 —
    LTX는 컷과 함께 효과음을 만들어서, 다시 만든 장면에만 갑자기 소리가 생기지 않게."""
    from . import edit_plan

    if not path or edit_plan.normalize((storyboard or {}).get("edit")).get("scene_sound", True):
        return
    ffmpeg = rough_cut.resolve_ffmpeg(config)
    if ffmpeg and os.path.exists(path):
        try:
            rough_cut.strip_audio(ffmpeg, path)
        except Exception:
            log.warning("컷 소리 빼기 실패: %s", path, exc_info=True)


def _finish_stage(round_obj, round_id, failures, stopped, ok_state):
    cancelled = stopped or (failures and round_id in _cancel_requests)
    _cancel_requests.discard(round_id)
    if cancelled:
        round_obj.generation_error = CANCEL_MESSAGE
        advance(round_obj, FAILED_STATUS, commit=False)
        round_obj.video_job_status, round_obj.video_job_message = "failed", CANCEL_MESSAGE
    elif failures:
        round_obj.generation_error = "장면 생성 실패: " + ", ".join(failures)
        advance(round_obj, FAILED_STATUS, commit=False)
        round_obj.video_job_status, round_obj.video_job_message = "failed", round_obj.generation_error
    else:
        round_obj.generation_error = ""
        round_obj.generation_progress = 1.0
        round_obj.generation_detail = ""
        round_obj.video_job_status, round_obj.video_job_message = "idle", ""
        advance(round_obj, ok_state, commit=False)
    db.session.commit()


def _fail_render(render_id, exc):
    db.session.rollback()
    render = db.session.get(ShotRender, render_id)
    render.status, render.error = "failed", f"{type(exc).__name__}: {exc}"
    db.session.commit()


def run_staged(app, round_id, payload):
    """payload: {"stage": "auto"|"images"|"clips"|"rough", "shot_ids": [...], "reroll": bool}.
    auto는 라운드의 현재 데이터를 보고 다음에 할 단계를 정한다 (재시도·처음 생성 모두 이어서 진행)."""
    cfg = app.config
    _cancel_requests.discard(round_id)
    round_obj = db.session.get(VideoRound, round_id)
    storyboard = ensure_storyboard(round_obj, cfg)
    shots = {s["id"]: s for s in storyboard["shots"]}
    if not shots:
        raise PipelineError("프롬프트의 [샷 목록]에 '1. ...' 형식의 샷이 없습니다.")
    ids = [s["id"] for s in storyboard["shots"]]
    requested = [s for s in (payload.get("shot_ids") or []) if s in shots]
    stage, reroll = payload.get("stage", "auto"), bool(payload.get("reroll"))

    if stage == "rough":
        if round_obj.status == "video_ready":  # 합성 중에 컷 버전을 바꿔 한 번 더 걸린 합성 (select_render)
            _begin_stage(round_obj, "올릴 영상 다시 만드는 중 (고른 컷 반영)")
        finalize_generation(app, round_obj, storyboard)
        return
    storyboard = ensure_english_notes(round_obj, storyboard, cfg)
    shots = {s["id"]: s for s in storyboard["shots"]}

    adapter, _reason = resolve_local_adapter(cfg)
    if not hasattr(adapter, "generate_image"):  # ComfyUI를 쓸 수 없으면 예전 방식(mock 폴백)으로
        return run_generation(app, round_id, {"shot_ids": requested or None})

    if not round_obj.shot_renders:  # 새 생성 → 이전 라운드 결과 재사용 계획
        for shot_id, old in plan_reuse(round_obj, storyboard, cfg).items():
            db.session.add(ShotRender(
                round_id=round_obj.id, shot_id=shot_id, version=1, status="reused",
                generator=old.generator, provider=old.provider, path=old.path,
                selected=True, workflow=old.workflow, prompt=old.prompt, seed=old.seed,
                width=old.width, height=old.height, frames=old.frames, fps=old.fps,
                duration=old.duration, reused_from_id=old.id, elapsed_sec=0.0))
        db.session.commit()

    chosen = _selected_by_shot(round_obj)

    def needs_image(i):
        r = chosen.get(i)
        return r is None or (r.status == "failed" and not r.image_path)

    def needs_clip(i):
        r = chosen.get(i)
        return r is not None and (r.status == "image_approved" or (r.status == "failed" and r.image_path))

    if stage == "auto":
        stage = ("images" if any(needs_image(i) for i in ids)
                 else "clips" if any(needs_clip(i) for i in ids) else "rough")
        if stage == "rough":
            finalize_generation(app, round_obj, storyboard)
            return

    out_dir = output_dir(app, round_obj)
    failures, stopped = [], False

    if stage == "images":
        targets = requested or [i for i in ids if needs_image(i)]
        for n, shot_id in enumerate(targets):
            if round_id in _cancel_requests:
                stopped = True
                break
            shot = shots[shot_id]
            params = sbm.resolve_params(storyboard, shot, cfg)
            version = _next_version(round_obj.id, shot_id)
            render = ShotRender(
                round_id=round_obj.id, shot_id=shot_id, version=version, status="running",
                generator="local", workflow=params["workflow"], prompt=params["prompt"],
                seed=params["seed"], width=params["width"], height=params["height"],
                frames=params["frames"], fps=params["fps"], duration=params["duration"])
            db.session.add(render)
            round_obj.generation_detail = f"이미지 {n + 1}/{len(targets)} 생성 중 ({shot_id})"
            db.session.commit()
            given = shot.get("image")
            if given and os.path.exists(given) and not reroll:
                # 움직임만 고치는 장면: 이전 버전의 첫 프레임을 그대로 쓴다 (모양 유지)
                import shutil
                kept = out_dir / shot_id / f"v{version}.png"
                kept.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(given, kept)
                for other in ShotRender.query.filter_by(round_id=round_obj.id, shot_id=shot_id):
                    if other.id != render.id and other.status in ("image_ready", "image_approved"):
                        other.status = "discarded"
                render.status, render.image_path, render.provider = "image_ready", str(kept), "previous"
                _select(render)
                db.session.commit()
                continue
            try:
                result = adapter.generate_image(
                    storyboard, {**shot, "image": None} if given else shot, out_dir / shot_id / f"v{version}.mp4",
                    _stage_progress(round_obj, len(targets), n, "이미지"))
            except Exception as exc:
                log.exception("장면 %s 이미지 생성 실패", shot_id)
                _fail_render(render.id, exc)
                failures.append(shot_id)
                continue
            for other in ShotRender.query.filter_by(round_id=round_obj.id, shot_id=shot_id):
                if other.id != render.id and other.status in ("image_ready", "image_approved"):
                    other.status = "discarded"
            render.status = "image_ready"
            render.image_path = result.image_path
            render.provider, render.elapsed_sec = result.provider, result.elapsed_sec
            render.peak_vram_mb = result.peak_vram_mb
            _select(render)
            db.session.commit()
        try:
            adapter.after_shot()  # 이미지 모델 내리기
        except Exception:
            log.warning("after_shot 실패", exc_info=True)
        _finish_stage(round_obj, round_id, failures, stopped, IMAGES_READY)
        return

    # stage == "clips": 승인된 이미지로 컷 영상을 만든다
    targets = requested or [i for i in ids if needs_clip(i)]
    for n, shot_id in enumerate(targets):
        if round_id in _cancel_requests:
            stopped = True
            break
        shot = shots[shot_id]
        base = chosen.get(shot_id)
        end_frame = payload.get("end_frame") if requested == [shot_id] else None  # '이 순간을 끝으로 다시 만들기'
        if end_frame and payload.get("base_render"):  # 끝 순간을 고른 그 버전의 첫 프레임에서 시작
            picked = db.session.get(ShotRender, int(payload["base_render"]))
            base = picked if picked is not None and picked.shot_id == shot_id else base
        if base is None or not base.image_path:
            failures.append(shot_id)
            continue
        inputs = {**shot, "image": base.image_path, **({"end_image": end_frame} if end_frame else {})}
        if reroll:  # 같은 이미지로 새 시드의 새 버전
            render = ShotRender(
                round_id=round_obj.id, shot_id=shot_id, version=_next_version(round_obj.id, shot_id),
                status="running", generator="local", workflow=base.workflow, image_path=base.image_path,
                width=base.width, height=base.height, frames=base.frames, fps=base.fps,
                duration=base.duration)
            db.session.add(render)
        else:
            render = base
            render.status, render.error = "running", ""
        params = sbm.resolve_params(storyboard, inputs, cfg)
        render.prompt, render.seed, render.workflow = params["prompt"], params["seed"], params["workflow"]
        render.end_image_path = end_frame or ""
        render.duration, render.frames = params["duration"], params["frames"]  # 의견으로 길이가 바뀌었을 수 있다
        round_obj.generation_detail = f"컷 {n + 1}/{len(targets)} 생성 중 ({shot_id})"
        db.session.commit()
        try:
            result = adapter.generate_shot(
                storyboard, inputs,
                out_dir / shot_id / f"v{render.version}.mp4",
                _stage_progress(round_obj, len(targets), n, "컷"))
        except Exception as exc:
            log.exception("장면 %s 컷 생성 실패", shot_id)
            _fail_render(render.id, exc)
            failures.append(shot_id)
            continue
        _mute_if_needed(cfg, storyboard, result.path)
        render.status, render.path = "done", result.path
        render.provider, render.elapsed_sec = result.provider, result.elapsed_sec
        render.peak_vram_mb, render.cost_usd = result.peak_vram_mb, result.cost_usd
        render.enhanced_prompt = getattr(result, "enhanced_prompt", "") or ""
        _select(render)
        db.session.commit()
        try:
            adapter.after_shot()
        except Exception:
            log.warning("after_shot 실패", exc_info=True)
    _finish_stage(round_obj, round_id, failures, stopped, CLIPS_READY)


def _require(round_obj, state, what):
    if round_obj.status != state:
        raise PipelineError(f"{what}은(는) '{state}' 단계에서만 할 수 있습니다 (현재 {round_obj.status}).")


def _reroll_shot(round_obj, shot_id, note, config):
    """다시 뽑기 준비: 새 시드 + 의견. 의견이 길이 요청('3초로', '늘려줘')이면 장면 길이를 바꾸고
    ('늘려줘'·'나레이션이 빨라'처럼 얼마나를 말하지 않으면 내레이션이 1배속으로 들어가는 길이로)
    (프롬프트 [샷 목록]에도 [N초]로 남긴다), 길이만 말한 의견은 장면 묘사에 붙이지 않는다. 바뀐 길이(초) 또는 None."""
    from .feedback_target import is_length_only, length_request, needs_narration_fit
    from .prompt_builder import set_shot_seconds

    storyboard = ensure_storyboard(round_obj, config)
    shot = sbm.get_shot(storyboard, shot_id)
    if shot is None:
        raise PipelineError(f"장면 {shot_id}을(를) 찾을 수 없습니다.")
    shot["seed"] = int(shot.get("seed", storyboard["defaults"]["seed"])) + SEED_REROLL_STEP
    note = (note or "").strip()
    fit = _narration_fit_seconds(round_obj, storyboard, shot_id, config) if needs_narration_fit(note) else None
    new_len = length_request(note, float(shot["duration"]), max_sec=float(config.get("SHOT_MAX_SECONDS", 8)),
                             fit_sec=fit)
    if new_len is not None and abs(new_len - float(shot["duration"])) > 0.01:
        shot["duration"] = new_len
        index = next(i for i, s in enumerate(storyboard["shots"], start=1) if s["id"] == shot_id)
        round_obj.prompt_text = set_shot_seconds(round_obj.prompt_text, index, new_len)
    else:
        new_len = None
    if note and not (new_len is not None and is_length_only(note)):
        shot["notes"] = list(dict.fromkeys(list(shot.get("notes") or []) + [note]))
    round_obj.storyboard_json = sbm.dumps(storyboard)
    return new_len


def _narration_fit_seconds(round_obj, storyboard, shot_id, config):
    """이 장면의 내레이션이 1배속으로 들어가는 컷 길이(초). 내레이션이 없거나 음성을 만들 수 없으면 None.
    올릴 영상에서 줄어드는 몫(앞뒤 잘라 냄·디졸브 겹침·말 시작 지연)을 더한다 — 컷이 3초여도
    말할 수 있는 시간은 2.2초 남짓이라, 그냥 늘려 달라고 해도 여전히 빠르게 읽던 문제."""
    from flask import current_app
    from . import voice

    text = (sbm.get_shot(storyboard, shot_id).get("narration") or "").strip()
    voice_settings = voice.load(round_obj.session)
    ffmpeg = rough_cut.resolve_ffmpeg(config)
    if not text or not voice_settings["enabled"] or not ffmpeg:
        return None
    work_dir = output_dir(current_app, round_obj) / "narration"  # 러프컷과 같은 곳 → 합성 캐시를 같이 쓴다
    try:
        clips = narration.prepare_clips(voice.apply(config, voice_settings),
                                        {"shots": [{"id": shot_id, "narration": text}]}, work_dir)
        speech = narration.speech_duration(ffmpeg, clips[shot_id]["path"])
    except (narration.NarrationError, OSError, KeyError) as exc:
        log.warning("내레이션 길이를 재지 못함 (%s): %s", shot_id, exc)
        return None
    plan = edit_plan.normalize(storyboard.get("edit"))
    need = speech / plan["narration_tempo"] + float(config.get("NARRATION_LEAD_SEC", 0.2))
    order = edit_plan.ordered_ids([s["id"] for s in storyboard["shots"]], plan)
    if plan["transition"] == "dissolve" and len(order) > 1 and order[-1] != shot_id:
        need += plan["transition_sec"]
    v = plan["shots"].get(shot_id, {})
    need = need * v.get("speed", 1.0) / v.get("keep", 1.0)
    need += float(config.get("CLIP_TRIM_HEAD_SEC", 0.15)) + float(config.get("CLIP_TRIM_TAIL_SEC", 0.1))
    return round(need + 0.05, 1)


def _begin_stage(round_obj, detail):
    advance(round_obj, "generating", commit=False)
    round_obj.generation_progress = 0.0
    round_obj.generation_detail = detail
    round_obj.video_job_status, round_obj.video_job_message = "running", detail
    db.session.commit()


def approve_images(round_obj, config):
    """이미지 확인 완료 → 지금 선택된 이미지로 컷 영상을 만든다."""
    from . import jobs

    _require(round_obj, IMAGES_READY, "이미지 승인")
    chosen = _selected_by_shot(round_obj)
    sb = ensure_storyboard(round_obj, config)
    missing = [s["id"] for s in sb["shots"]
               if chosen.get(s["id"]) is None or chosen[s["id"]].status not in ("image_ready", "reused", "done")]
    if missing:
        raise PipelineError("이미지가 준비되지 않은 장면이 있습니다: " + ", ".join(missing))
    for r in chosen.values():
        if r.status == "image_ready":
            r.status = "image_approved"
    _begin_stage(round_obj, "컷 영상 생성 대기 중")
    return jobs.enqueue("generate_video", round_obj.id, {"stage": "clips"})


def remove_shot_note(round_obj, shot_id, index, config):
    """장면에 쌓인 의견 하나를 지운다 — 의견은 다시 만들 때마다 전부 다시 반영돼서, 지난 의견이 계속 남아
    문장이 길어지고 원래 묘사가 빠졌다(2026-10-23, 장면 2에 의견 4개). 이미 만든 컷은 그대로 두고, 다음에 만들 때부터 빠진다
    (_notes_key가 바뀌어 묘사를 다시 고쳐 쓰고, 의견이 하나도 없으면 원래 묘사로 돌아간다). 만드는 중에는 고치지 않는다."""
    if round_obj.status not in (IMAGES_READY, CLIPS_READY, "video_ready", FAILED_STATUS):
        raise PipelineError("의견은 이미지·컷 확인, 올릴 영상 확인 단계(공개 전, 만드는 중이 아닐 때)에서만 지울 수 있습니다.")
    storyboard = ensure_storyboard(round_obj, config)
    shot = sbm.get_shot(storyboard, shot_id)
    notes = list((shot or {}).get("notes") or [])
    if not 0 <= int(index) < len(notes):
        raise PipelineError("지울 의견을 찾을 수 없습니다 (화면을 새로 고쳐 보세요).")
    index = int(index)
    notes.pop(index)
    notes_en = list(shot.get("notes_en") or [])
    if index < len(notes_en):  # notes_en은 notes와 같은 순서로 앞에서부터 채워진다
        notes_en.pop(index)
    shot["notes"], shot["notes_en"] = notes, notes_en
    round_obj.storyboard_json = sbm.dumps(storyboard)
    db.session.commit()
    return notes


def regenerate_image(round_obj, shot_id, note, config):
    """마음에 안 드는 장면의 이미지만 새 시드로 다시 뽑는다 (피드백 문장은 프롬프트에 덧붙는다)."""
    from . import jobs

    _require(round_obj, IMAGES_READY, "이미지 다시 뽑기")
    new_len = _reroll_shot(round_obj, shot_id, note, config)
    _begin_stage(round_obj, f"{shot_id} 이미지 다시 뽑는 중" + (f" (길이 {new_len:g}초)" if new_len else ""))
    return jobs.enqueue("generate_video", round_obj.id,
                        {"stage": "images", "shot_ids": [shot_id], "reroll": True})


def regenerate_clip(round_obj, shot_id, note, config):
    """컷을 새 시드로 다시 만든다. 이전 컷은 버전으로 남아 고를 수 있다.
    의견이 생김새(scene)에 관한 것이면 첫 프레임 이미지부터 다시 뽑아 이미지 확인(images_ready)에서 멈춘다 —
    컷은 첫 프레임을 움직이기만 해서 같은 이미지로는 '나무 보드를 브레드 보드로' 같은 의견이 반영될 수 없었고(2026-10-23),
    이미지를 보고 승인해야(approve_images) 그 장면의 컷만 만든다. 움직임·길이 의견이나 의견 없이 다시 뽑기면 같은 이미지로 컷만."""
    from . import jobs
    from .feedback_target import suggest, visual_notes

    if round_obj.status not in (CLIPS_READY, "video_ready"):  # 러프컷 뒤에도 업로드 전이면 다시 만들 수 있다
        raise PipelineError(f"컷 다시 만들기는 '{CLIPS_READY}' 또는 'video_ready' 단계에서만 할 수 있습니다 "
                            f"(현재 {round_obj.status}).")
    new_len = _reroll_shot(round_obj, shot_id, note, config)
    with_image = any(suggest(None, n) == "scene" for n in visual_notes([note]))
    _begin_stage(round_obj, f"{shot_id} {'이미지 다시 뽑는 중 (확인 후 컷)' if with_image else '컷 다시 만드는 중'}"
                 + (f" (길이 {new_len:g}초)" if new_len else ""))
    if with_image:
        return jobs.enqueue("generate_video", round_obj.id,
                            {"stage": "images", "shot_ids": [shot_id], "reroll": True})
    return jobs.enqueue("generate_video", round_obj.id,
                        {"stage": "clips", "shot_ids": [shot_id], "reroll": True})


END_FRAME_MIN_SEC = 0.5  # 이보다 앞 순간은 첫 프레임과 거의 같아 움직임이 없다


def regenerate_clip_to_frame(round_obj, render, at_sec, config):
    """'이 순간을 끝으로 다시 만들기': 컷에서 고른 순간의 화면을 끝 프레임으로, 그 컷의 첫 프레임을 시작으로 고정해
    장면 길이 그대로 새 버전을 만든다 (처음·끝 프레임 워크플로 COMFYUI_WORKFLOW_FLF).
    요청한 동작 뒤에 엉뚱한 동작이 붙거나(사람이 물건을 받아 감) 동작이 끝에 몰릴 때 — 끝 모습을 고정하면
    그 사이를 장면 전체에 걸쳐 채운다 (2026-10-23 시험: 같은 컷에서 뽑은 끝 화면이면 변형 없이 이어졌다.
    따로 그린 그림을 끝으로 주면 두 그림이 겹쳐 보여 쓰지 않는다)."""
    import subprocess
    from flask import current_app
    from . import jobs

    if round_obj.status not in (CLIPS_READY, "video_ready"):
        raise PipelineError("끝 순간 고르기는 컷 확인·올릴 영상 확인 단계(공개 전)에서만 할 수 있습니다.")
    if render.round_id != round_obj.id or render.status != "done" or not render.path or not render.image_path:
        raise PipelineError("첫 프레임 이미지가 있는 완성된 컷에서만 끝 순간을 고를 수 있습니다.")
    at = float(at_sec)
    if at < END_FRAME_MIN_SEC:
        raise PipelineError(f"{END_FRAME_MIN_SEC:g}초보다 뒤의 순간을 골라 주세요 (처음 순간은 첫 프레임과 같아 움직임이 생기지 않습니다).")
    ffmpeg = rough_cut.resolve_ffmpeg(config)
    if not ffmpeg:
        raise PipelineError("ffmpeg를 찾을 수 없어 화면을 뽑지 못했습니다 (FFMPEG_PATH).")
    out = output_dir(current_app, round_obj) / render.shot_id / f"v{render.version}_end_{int(at * 1000)}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    done = subprocess.run([ffmpeg, "-v", "error", "-y", "-ss", f"{at:.3f}", "-i", render.path, "-frames:v", "1", str(out)],
                          capture_output=True, text=True)
    if done.returncode != 0 or not out.exists():
        raise PipelineError(f"{at:.1f}초 화면을 뽑지 못했습니다 (컷 길이보다 뒤일 수 있습니다). {done.stderr[-200:]}")
    _begin_stage(round_obj, f"{render.shot_id} v{render.version}의 {at:.1f}초 화면을 끝으로 컷 다시 만드는 중")
    return jobs.enqueue("generate_video", round_obj.id,
                        {"stage": "clips", "shot_ids": [render.shot_id], "reroll": True,
                         "end_frame": str(out), "base_render": render.id})


# ── 제작 중 기획 고치기: 고친 장면만 다시 만든다 ──
PLAN_EDITABLE = (IMAGES_READY, CLIPS_READY, "video_ready", FAILED_STATUS)


def plan_diff(round_obj, new_text, config):
    """지금 장면 정보와 고친 기획을 비교한다. 장면은 번호가 아니라 (묘사, 길이)로 짝짓는다 —
    중간에 장면을 끼워 넣어도 뒤 장면을 다시 만들지 않게. 스타일이 바뀌면 모든 장면을 다시 만든다.
    반환: {"mapping": {새 id: 옛 id}, "regen": [새 id], "dropped": [옛 id], "narration": bool, "style": bool, "new_sb", "old_sb"}"""
    old_sb = ensure_storyboard(round_obj, config)
    session = round_obj.session
    new_sb = sbm.build_storyboard(new_text, config, aspect=session.aspect_ratio,
                                  shot_seconds=session.shot_seconds or None)
    style_changed = old_sb["style"].strip() != new_sb["style"].strip()
    pool = {}
    for s in old_sb["shots"]:
        pool.setdefault((s["prompt"].strip(), round(float(s["duration"]), 2)), []).append(s["id"])
    mapping, regen = {}, []
    for s in new_sb["shots"]:
        candidates = pool.get((s["prompt"].strip(), round(float(s["duration"]), 2))) or []
        if candidates and not style_changed:
            old_id = s["id"] if s["id"] in candidates else candidates[0]
            candidates.remove(old_id)
            mapping[s["id"]] = old_id
        else:
            regen.append(s["id"])
    old_by = {s["id"]: s for s in old_sb["shots"]}
    used = set(mapping.values())
    new_ids = {s["id"] for s in new_sb["shots"]}
    narration = any((s.get("narration") or "") != (old_by[mapping[s["id"]]].get("narration") or "")
                    for s in new_sb["shots"] if s["id"] in mapping) or len(new_sb["shots"]) != len(old_sb["shots"])
    return {"mapping": mapping, "regen": regen, "style": style_changed, "narration": narration,
            "dropped": [i for i in old_by if i not in used and i not in new_ids],
            "new_sb": new_sb, "old_sb": old_sb}


def apply_plan_change(round_obj, new_text, config):
    """제작 중(이미지·컷 확인, 확인 단계, 실패)에 고친 기획을 반영한다.
    짝지어진 장면은 만들어 둔 이미지·컷을 그대로 옮기고(id가 바뀌면 결과 기록도 새 id로), 고친·새 장면은 선택을 풀어
    이미지부터 다시 만든다. 내레이션·장면 수만 바뀌었고 확인 단계면 올릴 영상만 다시 합성한다. 요약 dict 반환."""
    from . import edit_plan, jobs

    if round_obj.status not in PLAN_EDITABLE:
        raise PipelineError("지금 단계에서는 기획을 고칠 수 없습니다.")
    busy = GenerationJob.query.filter(GenerationJob.round_id == round_obj.id,
                                      GenerationJob.status.in_(("queued", "running"))).first()
    if busy is not None:
        raise PipelineError("만드는 중에는 기획을 고칠 수 없습니다. 끝나거나 중지한 뒤에 고치세요.")
    d = plan_diff(round_obj, new_text, config)
    old_by = {s["id"]: s for s in d["old_sb"]["shots"]}
    shots = []
    for s in d["new_sb"]["shots"]:
        if s["id"] in d["mapping"]:  # 그대로 쓰는 장면: 시드·의견·첫 프레임 등을 옮기고 문장만 새것으로
            kept = dict(old_by[d["mapping"][s["id"]]], id=s["id"], narration=s.get("narration", ""))
            kept.pop("subtitle", None)
            if s.get("subtitle"):
                kept["subtitle"] = s["subtitle"]
            shots.append(kept)
        else:
            shots.append(s)
    storyboard = dict(d["new_sb"], shots=shots)
    if d["old_sb"].get("edit"):  # 장면별 편집(길이·속도·순서)은 짝지은 id로 옮긴다
        plan = dict(d["old_sb"]["edit"])
        reverse = {old: new for new, old in d["mapping"].items()}
        plan["shots"] = {reverse[k]: v for k, v in (plan.get("shots") or {}).items() if k in reverse}
        plan["order"] = [reverse[k] for k in plan.get("order") or [] if k in reverse]
        storyboard["edit"] = edit_plan.normalize(plan, [s["id"] for s in shots])
    reverse = {old: new for new, old in d["mapping"].items()}
    renders = ShotRender.query.filter_by(round_id=round_obj.id).all()
    for r in renders:  # id를 먼저 정해 두고 한꺼번에 바꾼다 (s05→s04, s04→s03처럼 이어져도 섞이지 않게)
        r._target = reverse.get(r.shot_id)
    for r in renders:
        if r._target is not None:
            r.shot_id = r._target
        else:  # 고친·빠진 장면의 옛 결과: 선택을 풀고 확인 중인 이미지는 버린다 (파일은 남긴다)
            r.selected = False
            if r.status in ("image_ready", "image_approved", "queued", "running"):
                r.status = "discarded"
    round_obj.prompt_text = new_text
    round_obj.storyboard_json = sbm.dumps(storyboard)
    summary = {"regen": d["regen"], "dropped": d["dropped"], "style": d["style"], "narration": d["narration"],
               "kept": len(d["mapping"]), "rebuild": False}
    if d["regen"]:
        _begin_stage(round_obj, f"기획에서 고친 장면 {len(d['regen'])}개 이미지 만드는 중")
        jobs.enqueue("generate_video", round_obj.id, {"stage": "images", "shot_ids": d["regen"]})
    elif round_obj.status == "video_ready" and (d["narration"] or d["dropped"]):
        db.session.commit()
        rebuild_upload_video(round_obj, config)
        summary["rebuild"] = True
    db.session.commit()
    return summary


def restart_planning(app, round_obj):
    """이 버전에서 만든 이미지·컷을 지우고 ① 프롬프트 단계로 되돌린다.
    다음 '장면 만들기' 때 스토리보드를 프롬프트에서 새로 만들고, 이전 버전 재사용 계획도 다시 세운다."""
    import shutil

    if round_obj.status not in (IMAGES_READY, CLIPS_READY, FAILED_STATUS):
        raise PipelineError(f"다시 계획하기는 장면 제작 중(이미지·컷 확인, 실패)에만 할 수 있습니다 (현재 {round_obj.status}).")
    busy = GenerationJob.query.filter(GenerationJob.round_id == round_obj.id,
                                      GenerationJob.status.in_(("queued", "running"))).first()
    if busy is not None:
        raise PipelineError("작업이 진행 중입니다. 먼저 중지한 뒤 다시 계획하세요.")
    if round_obj.adapter_used == "import":
        raise PipelineError("불러온 영상의 1차 버전은 다시 계획할 수 없습니다.")
    for r in list(round_obj.shot_renders):
        db.session.delete(r)
    round_obj.storyboard_json = ""
    round_obj.storyboard = ""
    round_obj.video_path = ""
    round_obj.narration_json = ""
    round_obj.generation_progress = 0.0
    round_obj.generation_detail = round_obj.generation_error = ""
    round_obj.video_job_status, round_obj.video_job_message = "idle", ""
    round_obj.adapter_used = ""
    advance(round_obj, "prompt_built", commit=False)
    db.session.commit()
    shutil.rmtree(output_dir(app, round_obj), ignore_errors=True)  # 이 버전 폴더만 (재사용 원본은 이전 버전 폴더에 있다)


def start_rough_cut(round_obj, config):
    """컷 확인 완료 → 선택된 컷들을 이어 붙여 러프컷을 만든다 (③으로)."""
    from . import jobs

    _require(round_obj, CLIPS_READY, "러프컷 만들기")
    chosen = _selected_by_shot(round_obj)
    sb = ensure_storyboard(round_obj, config)
    missing = [s["id"] for s in sb["shots"]
               if chosen.get(s["id"]) is None or chosen[s["id"]].status not in ("done", "reused")]
    if missing:
        raise PipelineError("컷이 완성되지 않은 장면이 있습니다: " + ", ".join(missing))
    _begin_stage(round_obj, "올릴 영상(러프컷) 만들기 대기 중")
    return jobs.enqueue("generate_video", round_obj.id, {"stage": "rough"})


BEFORE_UPLOAD = ("prompt_built", "generating", FAILED_STATUS, IMAGES_READY, CLIPS_READY, "video_ready")


def set_round_edit(round_obj, config, **changes):
    """이 버전의 편집 설정 일부를 바꾼다 (자막 켜기·크기·위치 등). 공개 전에만 — 공개 후에는 다음 버전 검토 화면에서."""
    if round_obj.status not in BEFORE_UPLOAD:
        raise PipelineError("공개한 버전의 편집은 바꿀 수 없습니다. 다음 버전 편집 설정은 의견 검토 화면에서 고치세요.")
    storyboard = round_obj.storyboard_data
    ids = [s["id"] for s in storyboard["shots"]] if storyboard else None
    plan = edit_plan.loads(round_obj.edit_json or "", ids)
    plan.update(changes)
    plan = edit_plan.normalize(plan, ids)
    round_obj.edit_json = "" if edit_plan.is_default(plan) else edit_plan.dumps(plan)
    if storyboard is not None:  # 이미 만든 스냅샷에도 반영 (러프컷은 스냅샷의 편집 설정을 쓴다)
        if edit_plan.is_default(plan):
            storyboard.pop("edit", None)
        else:
            storyboard["edit"] = plan
        round_obj.storyboard_json = sbm.dumps(storyboard)
    db.session.commit()
    return plan


def rebuild_upload_video(round_obj, config):
    """③ 확인에서 올릴 영상(러프컷)만 다시 합성한다 — 로고·자막·편집 설정을 바꾼 뒤. 장면은 다시 만들지 않는다."""
    from . import jobs

    _require(round_obj, "video_ready", "올릴 영상 다시 만들기")
    if upload_in_progress(round_obj):
        raise PipelineError("YouTube에 올리는 중에는 다시 만들 수 없습니다.")
    _begin_stage(round_obj, "올릴 영상 다시 만드는 중 (로고·자막·편집 적용)")
    return jobs.enqueue("generate_video", round_obj.id, {"stage": "rough"})


# ── 상용 API 에스컬레이션 ─────────────────────────────────────────────────
def start_escalation(round_obj, shot_ids, flags, confirm_cost_usd, config, http=None):
    """검증을 통과하면 선택 장면만 상용 API로 재생성하도록 큐에 넣는다. (cost, job) 반환."""
    storyboard = ensure_storyboard(round_obj, config)
    ready = create_commercial_adapter(config, http=http) is not None
    cost = quality_gate.validate_escalation(
        round_obj, storyboard, shot_ids, confirm_cost_usd, config, ready)

    for shot_id in shot_ids:  # 체크 사유를 로컬 버전에 기록 (발표용 집계)
        reasons = [r for r in (flags or {}).get(shot_id, []) if r in quality_gate.QUALITY_REASONS]
        for r in ShotRender.query.filter_by(round_id=round_obj.id, shot_id=shot_id, selected=True):
            r.quality_flags_json = json.dumps(reasons, ensure_ascii=False)
    advance(round_obj, "generating", commit=False)
    round_obj.generation_progress = 0.0
    round_obj.generation_detail = "상용 API 재생성 대기 중"
    round_obj.video_approved = False
    db.session.commit()

    from . import jobs
    job = jobs.enqueue("regenerate_shots", round_obj.id,
                       {"shot_ids": list(shot_ids), "generator": "api"})
    return cost, job


def retry_failed(round_obj, config=None):
    from . import jobs

    last_import = (GenerationJob.query.filter_by(round_id=round_obj.id, kind="import_video")
                   .order_by(GenerationJob.id.desc()).first())
    if last_import is not None and round_obj.adapter_used == "import":  # 불러오기 중 실패 → 나누기부터 다시
        for r in list(round_obj.shot_renders):
            db.session.delete(r)
        advance(round_obj, "generating", commit=False)
        round_obj.generation_progress = 0.0
        round_obj.generation_detail = "불러온 영상을 장면별로 다시 나누는 중"
        db.session.commit()
        return jobs.enqueue("import_video", round_obj.id, json.loads(last_import.payload_json or "{}"))
    rough = (round_obj.generation_error or "").startswith("러프컷")  # 장면은 다 됐고 이어 붙이기만 실패
    advance(round_obj, "generating", commit=False)
    round_obj.generation_progress = 0.0
    round_obj.generation_detail = "올릴 영상 다시 만들기 대기 중" if rough else "실패한 장면 재시도 대기 중"
    db.session.commit()
    staged = config is not None and staged_enabled(config)
    if not staged:
        return jobs.enqueue("generate_video", round_obj.id, {})
    return jobs.enqueue("generate_video", round_obj.id, {"stage": "rough" if rough else "auto"})


def cancel_generation(config, round_obj):
    """생성 중지. 대기 중인 작업은 바로 취소하고, 실행 중인 장면은 ComfyUI에 중단을 요청한다.
    실행 중이던 작업은 현재 장면을 버리고 멈추며 라운드는 '재시도 가능' 상태가 된다."""
    from .video_generator.comfyui.comfy_client import ComfyClient

    if round_obj.status != "generating":
        raise PipelineError(f"생성 중일 때만 중지할 수 있습니다 (현재 {round_obj.status}).")
    running = False
    for job in GenerationJob.query.filter(
            GenerationJob.round_id == round_obj.id, GenerationJob.kind != "cluster",
            GenerationJob.status.in_(["queued", "running"])):
        if job.status == "queued":
            job.status, job.error = "failed", "cancelled"
            job.finished_at = datetime.utcnow()
        else:
            running = True
    if running:
        _cancel_requests.add(round_obj.id)
        round_obj.generation_detail = "중지하는 중… (현재 장면을 멈춥니다)"
        db.session.commit()
        try:  # 이미 돌고 있는 장면을 끊는다 (서버가 꺼져 있으면 무시)
            ComfyClient(config["COMFYUI_URL"]).interrupt()
        except Exception:
            log.warning("ComfyUI 중단 요청 실패", exc_info=True)
        return "stopping"
    round_obj.generation_error = CANCEL_MESSAGE
    advance(round_obj, FAILED_STATUS, commit=False)
    round_obj.video_job_status = "failed"
    round_obj.video_job_message = CANCEL_MESSAGE
    db.session.commit()
    return "cancelled"


def start_generation(round_obj, config=None):
    from . import jobs

    source = demo_source(round_obj)
    advance(round_obj, "generating", commit=False)
    round_obj.generation_progress = 0.0
    round_obj.generation_detail = "대기 중"
    round_obj.video_job_status = "running"
    round_obj.video_job_message = "대기 중"
    db.session.commit()
    if source is not None:  # 데모: 만들지 않고 준비해 둔 결과를 쓴다 (진행 표시만 몇 초 보여 준다)
        return jobs.enqueue("generate_video", round_obj.id, {"demo_replay": source.id})
    staged = config is not None and staged_enabled(config)
    return jobs.enqueue("generate_video", round_obj.id, {"stage": "auto"} if staged else {})


def demo_plan(round_obj):
    """이 차수의 데모 재생 설정 {"replay_round_id", "youtube_video_id"} (없으면 {}).
    PipelineSession.demo_json = {"rounds": {"1": {...}, "2": {...}}}. 예전 형식 {"replay_round_id": id}는 2차부터 전부.
    시연 영상을 찍을 때 장면 생성(GPU 수십 분)과 업로드를 기다리지 않으려고 둔다."""
    try:
        data = json.loads(round_obj.session.demo_json or "{}")
    except ValueError:
        return {}
    if "rounds" in data:
        return (data["rounds"] or {}).get(str(round_obj.round_number)) or {}
    return {"replay_round_id": data.get("replay_round_id")} if round_obj.round_number >= 2 else {}


def demo_source(round_obj):
    """데모 재생에 쓸 '준비된 결과' 버전, 없으면 None."""
    source = db.session.get(VideoRound, int(demo_plan(round_obj).get("replay_round_id") or 0))
    if source is None or source.id == round_obj.id or not source.video_path or not os.path.exists(source.video_path):
        return None
    return source


def run_demo_replay(app, round_id, payload):
    """[데모] 작업 큐에서: 장면마다 'n/N 만드는 중'을 DEMO_REPLAY_SEC_PER_SHOT초씩 보여 준 뒤 준비된 결과를 쓴다.
    시연 영상에서 제작 단계가 순식간에 지나가 버리지 않게 한다 (실제 생성은 장면당 몇 분)."""
    round_obj = db.session.get(VideoRound, round_id)
    source = db.session.get(VideoRound, int(payload["demo_replay"]))
    per = float(app.config.get("DEMO_REPLAY_SEC_PER_SHOT", 1.5))
    shots = sorted({r.shot_id for r in source.shot_renders if r.selected})
    steps = len(shots) + 1
    for n, shot_id in enumerate(shots):
        round_obj.generation_progress = round(n / steps, 3)
        round_obj.generation_detail = f"장면 {n + 1}/{len(shots)} ({shot_id}) 만드는 중"
        round_obj.video_job_message = round_obj.generation_detail
        db.session.commit()
        time.sleep(per)
    round_obj.generation_progress = round(len(shots) / steps, 3)
    round_obj.generation_detail = "올릴 영상 만드는 중 (장면 잇기·내레이션·배경음악)"
    round_obj.video_job_message = round_obj.generation_detail
    db.session.commit()
    time.sleep(per * 1.5)
    replay_demo(round_obj, source, app.config)


def replay_demo(round_obj, source, config):
    """[데모] 장면을 만들지 않고 준비해 둔 버전(source)의 장면·올릴 영상·자막·소리 설정을 이 버전의 결과로 바로 쓴다.
    프롬프트 문서는 이 버전 것(영상 설명·시청자 반영 사항·연출 지침)을 두고 장면 구성(샷 목록·내레이션·자막)만
    source 것으로 바꿔 버전 기록에 남긴다 (같으면 기록하지 않는다)."""
    import shutil

    from flask import current_app

    from .prompt_builder import extract_narration, parse_sections, render_sections

    if round_obj.status != "generating":
        advance(round_obj, "generating", commit=False)
    sections, src = parse_sections(round_obj.prompt_text or ""), parse_sections(source.prompt_text or "")
    for key in ("샷 목록", "내레이션", "자막"):  # 준비된 결과에 있는 것만 — 없으면 기획에 적어 둔 것을 그대로 둔다
        if key in src:
            sections[key] = src[key]
    new_text = render_sections(sections)
    if new_text != round_obj.prompt_text:
        approval.add_version(round_obj.session_id, round_obj.id, round_obj.prompt_text, new_text, "장면 구성 갱신")
        round_obj.prompt_text = new_text
    for field in ("storyboard_json", "edit_json", "music_json", "narration_json", "timeline_json", "edit_note"):
        setattr(round_obj, field, getattr(source, field) or "")
    storyboard = round_obj.storyboard_data
    lines = extract_narration(round_obj.prompt_text or "")
    if storyboard and lines and len(lines) == len(storyboard["shots"]):  # 장면 카드에도 기획의 내레이션 문장이 보이게
        for n, shot in enumerate(storyboard["shots"], start=1):
            shot["narration"] = shot.get("narration") or lines.get(n, "")
        round_obj.storyboard_json = sbm.dumps(storyboard)
    for old in list(round_obj.shot_renders):
        db.session.delete(old)
    for r in source.shot_renders:
        if r.selected and r.status in ("done", "reused") and r.path:
            db.session.add(ShotRender(
                round_id=round_obj.id, shot_id=r.shot_id, version=1, status="done", generator=r.generator,
                provider=r.provider, workflow=r.workflow, prompt=r.prompt, enhanced_prompt=r.enhanced_prompt,
                seed=r.seed, width=r.width, height=r.height, frames=r.frames, fps=r.fps, duration=r.duration,
                path=r.path, image_path=r.image_path, elapsed_sec=r.elapsed_sec, selected=True))
    out = output_dir(current_app, round_obj)
    out.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source.video_path, out / "rough_cut.mp4")  # 이 버전 폴더로 — 여기서 다시 만들어도 원본은 그대로
    srt = Path(source.video_path).parent / rough_cut.CAPTIONS_NAME
    if srt.exists():
        shutil.copyfile(srt, out / rough_cut.CAPTIONS_NAME)
    round_obj.video_path = str(out / "rough_cut.mp4")
    round_obj.adapter_used = source.adapter_used or ""
    round_obj.generation_progress = 1.0
    round_obj.generation_detail, round_obj.generation_error = "", ""
    round_obj.video_job_status, round_obj.video_job_message = "idle", ""
    db.session.flush()
    advance(round_obj, "video_ready", commit=False)
    if config is not None:
        calculate_metrics(round_obj, config)
    db.session.commit()
    return None


def select_render(app, round_obj, render):
    """올릴 영상에 쓸 컷 버전을 바꾼다. 선택은 바로 저장하고, 올릴 영상(러프컷)은 작업 큐에서 다시 합성한다.
    예전에는 요청 안에서 러프컷을 통째로 다시 합성해 고를 때마다 몇 초~몇 분 멈춘 듯했다(2026-10-23).
    컷 확인(clips_ready)에서는 러프컷이 아직 없으므로 선택만 바꾼다. 다시 합성하는 중에도 바꿀 수 있다 —
    기다리는 합성이 있으면 그것이 마지막 선택으로 만들고, 합성이 돌고 있으면 끝난 뒤 한 번 더 만든다."""
    from . import jobs

    rebuilding = round_obj.status == "generating" and rough_cut_in_progress(round_obj)
    if round_obj.status not in ("video_ready", CLIPS_READY) and not rebuilding:
        raise PipelineError("영상 확인 단계에서만 버전을 바꿀 수 있습니다.")
    if render.status != "done":
        raise PipelineError("완료된 버전만 선택할 수 있습니다.")
    _select(render)
    db.session.commit()
    if round_obj.status == CLIPS_READY:
        return
    waiting = GenerationJob.query.filter_by(round_id=round_obj.id, status="queued").all()
    if any(json.loads(j.payload_json or "{}").get("stage") == "rough" for j in waiting):
        return  # 아직 시작 안 한 합성이 지금 선택으로 만든다
    if round_obj.status == "video_ready":
        rebuild_upload_video(round_obj, app.config)
    else:  # 합성이 돌고 있다 — 그건 이전 선택으로 만들고 있으니 끝난 뒤 한 번 더
        jobs.enqueue("generate_video", round_obj.id, {"stage": "rough"})


def delete_render(app, round_obj, render):
    """마음에 안 드는 컷 버전(보통 피드백으로 다시 만든 것)을 지운다 — 공개 전에만, 장면마다 한 버전은 남긴다.
    지운 버전이 선택돼 있었으면 남은 것 중 가장 최근 버전을 고른다(올릴 영상이 있으면 다시 합성, select_render).
    파일은 다른 버전이 같이 쓰지 않을 때만 지운다 (같은 첫 프레임으로 컷만 다시 만든 버전은 이미지를 공유한다)."""
    if round_obj.status not in ("video_ready", CLIPS_READY):
        raise PipelineError("컷 확인·올릴 영상 확인 단계(공개 전)에서만 버전을 지울 수 있습니다.")
    if render.status != "done":
        raise PipelineError("완성된 컷 버전만 지울 수 있습니다.")
    others = [r for r in round_obj.shot_renders
              if r.shot_id == render.shot_id and r.id != render.id and r.status in ("done", "reused")]
    if not others:
        raise PipelineError("장면마다 컷이 하나는 있어야 합니다. 마지막 버전은 지울 수 없습니다.")
    if ShotRender.query.filter_by(reused_from_id=render.id).count():
        raise PipelineError("다음 버전이 이 컷을 다시 쓰고 있어 지울 수 없습니다.")
    was_selected = render.selected
    files = [p for p in (render.path, render.image_path, render.end_image_path) if p]
    db.session.delete(render)
    db.session.flush()
    db.session.expire(round_obj, ["shot_renders"])  # 지운 버전이 목록에 남아 러프컷에 쓰이지 않게
    for path in files:
        shared = ShotRender.query.filter((ShotRender.path == path) | (ShotRender.image_path == path)).count()
        if not shared and os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                log.warning("컷 파일을 지우지 못함: %s", path, exc_info=True)
    if was_selected:
        select_render(app, round_obj, max(others, key=lambda r: r.version))  # 커밋까지 한다
    else:
        db.session.commit()


# ── 업로드 · 수집 · 분류 · 클러스터링 ─────────────────────────────────────
def approve_and_upload(round_obj, config, manual_video_id=""):
    """③ 확인 완료 + ④ 업로드."""
    if round_obj.status != "video_ready":
        raise PipelineError(f"현재 단계({round_obj.status})에서는 업로드할 수 없습니다.")
    round_obj.youtube_video_id = youtube_client.upload_video(round_obj, config, manual_video_id)
    round_obj.video_approved = True
    advance(round_obj, "uploaded", commit=False)
    db.session.commit()


def upload(round_obj, config, manual_video_id=""):
    """'업로드 진행' 버튼 = 사람이 영상을 확인했다는 뜻이므로 확인(③)과 업로드(④)를 함께 처리한다.
    REQUIRE_VIDEO_APPROVAL의 효과는 자동으로 업로드하지 않고 ③에서 멈추는 것이다."""
    approve_and_upload(round_obj, config, manual_video_id)


def youtube_title(round_obj):
    return f"{round_obj.session.title} ({round_obj.round_number}차)"


def youtube_description(round_obj):
    from . import bgm
    from .prompt_builder import parse_sections
    from .youtube_upload import default_description
    text = default_description(parse_sections(round_obj.prompt_text).get("영상 설명", ""))
    music = bgm.load(round_obj)
    if music["enabled"] and music["file"]:  # MiniMax-Music3 라이선스: 상업 이용 시 표기
        text += f"\n{bgm.CREDIT}"
    return text


def unpublish(round_obj):
    """④ 공개 → ③ 확인으로 되돌린다 (올린 뒤 고칠 게 생겼을 때). 장면은 그대로 두고 올릴 영상만 다시 만들 수 있다.
    댓글을 모으기 시작했으면 의견이 그 영상에 묶이므로 되돌리지 않는다. YouTube의 예전 영상은 지우지 않는다."""
    if round_obj.status != "uploaded":
        raise PipelineError(f"공개 취소는 '공개' 단계에서만 할 수 있습니다 (현재 {round_obj.status}).")
    if round_obj.feedbacks:
        raise PipelineError("이미 댓글을 모은 버전은 되돌릴 수 없습니다. 고칠 점은 다음 버전에 반영하세요.")
    old = round_obj.youtube_video_id or ""
    advance(round_obj, "video_ready", commit=False)
    round_obj.video_approved = False
    round_obj.youtube_video_id = ""
    round_obj.video_job_status = "idle"
    round_obj.video_job_message = (f"공개를 취소했습니다. 예전에 올린 YouTube 영상({old})은 지우지 않았으니 "
                                   "필요하면 YouTube Studio에서 정리하세요." if old and not old.startswith("sim-") else "")
    db.session.commit()
    return old


def upload_in_progress(round_obj):
    return GenerationJob.query.filter(GenerationJob.round_id == round_obj.id, GenerationJob.kind == "youtube_upload",
                                      GenerationJob.status.in_(("queued", "running"))).first() is not None


def upload_message(round_obj):
    """video_job_message 중 YouTube 업로드에 관한 것만 (영상 생성 메시지와 같은 칸을 쓴다)."""
    message = round_obj.video_job_message or ""
    return message if "YouTube" in message else ""


def start_youtube_upload(app, round_obj, title="", description="", privacy=""):
    """③ 확인에서 제작자가 'YouTube에 올리기'를 눌렀을 때만 호출된다 — 이 버튼이 영상 확인(승인)이다.
    업로드는 작업 큐에서 하고, 끝나면 ④ uploaded로 넘어간다. 실패하면 ③에 머물고 사유를 남긴다."""
    from . import jobs, youtube_upload

    if round_obj.status != "video_ready":
        raise PipelineError(f"현재 단계({round_obj.status})에서는 업로드할 수 없습니다.")
    preset = demo_plan(round_obj).get("youtube_video_id")
    if preset:  # 데모: 다시 올리지 않고 이미 올려 둔 영상(댓글이 달린)에 연결한다
        round_obj.youtube_video_id = preset
        round_obj.video_approved = True
        round_obj.video_job_status = "idle"
        round_obj.video_job_message = f"YouTube에 올렸습니다 ({youtube_upload.PRIVACY.get(privacy or 'unlisted', '')})"
        advance(round_obj, "uploaded", commit=False)
        db.session.commit()
        return None
    if youtube_client.is_simulation(app.config):
        raise PipelineError("시뮬레이션 모드에서는 YouTube에 올리지 않습니다 (SIMULATION_MODE=false로 바꾸세요).")
    if not round_obj.video_path or not os.path.exists(round_obj.video_path):
        raise PipelineError("올릴 영상 파일이 없습니다. 먼저 올릴 영상을 만드세요.")
    if not youtube_upload.status(app.config, app.instance_path)["connected"]:
        raise PipelineError("YouTube 계정이 연결되지 않았습니다. 'YouTube 계정 연결'을 먼저 하세요.")
    if upload_in_progress(round_obj):
        raise PipelineError("이미 업로드 중입니다.")
    privacy = privacy or app.config.get("YOUTUBE_PRIVACY", "private")
    if privacy not in youtube_upload.PRIVACY:
        raise PipelineError(f"알 수 없는 공개 범위: {privacy}")
    round_obj.video_job_status = "running"
    round_obj.video_job_message = "YouTube 업로드 대기 중"
    db.session.commit()
    return jobs.enqueue("youtube_upload", round_obj.id, {
        "title": (title or youtube_title(round_obj)).strip()[:100],
        "description": (description or youtube_description(round_obj)).strip()[:5000],
        "privacy": privacy})


def captions_file(round_obj):
    """이 버전의 YouTube 자막 파일(.srt) 경로, 없거나 자막을 껐으면 None. 올릴 영상을 만들 때 옆에 생긴다."""
    if not round_obj.video_path or not edit_plan.round_plan(round_obj)["subtitles"]:
        return None
    path = Path(round_obj.video_path).parent / rough_cut.CAPTIONS_NAME
    return path if path.exists() and path.stat().st_size > 0 else None


def run_youtube_upload(app, round_id, payload):
    from . import youtube_upload

    round_obj = db.session.get(VideoRound, round_id)
    last = [0.0]

    def progress(fraction):
        now = time.time()
        if fraction < 1.0 and now - last[0] < 1.0:
            return
        last[0] = now
        round_obj.video_job_message = f"YouTube에 올리는 중 {round(fraction * 100)}%"
        db.session.commit()

    try:
        video_id = youtube_upload.upload(
            app.config, app.instance_path, round_obj.video_path, payload.get("title", ""),
            payload.get("description", ""), payload.get("privacy", "private"), progress=progress)
    except Exception as exc:  # 영상은 그대로 ③에 남고, 다시 누르면 처음부터 올린다
        log.exception("YouTube 업로드 실패")
        db.session.rollback()
        round_obj = db.session.get(VideoRound, round_id)
        round_obj.video_job_status = "failed"
        round_obj.video_job_message = f"YouTube 업로드 실패: {exc}"
        db.session.commit()
        return
    round_obj.youtube_video_id = video_id
    round_obj.video_approved = True
    round_obj.video_job_status = "idle"
    message = f"YouTube에 올렸습니다 ({youtube_upload.PRIVACY.get(payload.get('privacy'), '')})"
    srt = captions_file(round_obj)
    if srt:  # 자막은 영상에 굽지 않고 YouTube 자막 트랙으로 — 실패해도 영상 업로드는 그대로 둔다
        try:
            youtube_upload.upload_captions(app.config, app.instance_path, video_id, srt)
            message += " · 한국어 자막도 올렸습니다"
        except Exception as exc:
            log.warning("YouTube 자막 업로드 실패: %s", exc)
            message += f" · 자막은 올리지 못했습니다: {exc} (자막 파일을 받아 YouTube Studio에서 직접 올릴 수 있습니다)"
    round_obj.video_job_message = message
    advance(round_obj, "uploaded", commit=False)
    db.session.commit()


def collect(round_obj, config):
    comments = youtube_client.collect_comments(
        round_obj.youtube_video_id, config, seed=round_obj.id)
    existing = {f.comment_text for f in round_obj.feedbacks}
    added = [c for c in comments if c["text"].strip() and c["text"].strip() not in existing]
    add_comments(round_obj, added)
    return len(comments)


def add_comments(round_obj, comments):
    for c in comments:
        if c["text"].strip():
            db.session.add(Feedback(
                round_id=round_obj.id, author=c.get("author", ""),
                comment_text=c["text"].strip(), source=c["source"],
                timestamp_sec=c.get("timestamp_sec")))  # 장면별 의견은 그 장면의 시작 시각을 미리 지정
    advance(round_obj, "feedback_collected", commit=False)
    db.session.commit()


def classify(round_obj, classifier):
    """6축 분류 + 타임스탬프 추출. 건설적 개수 반환."""
    feedbacks = round_obj.feedbacks
    if not feedbacks:
        raise PipelineError("분류할 댓글이 없습니다.")
    started = time.time()
    results = classifier.classify_batch([f.comment_text for f in feedbacks])
    for f, r in zip(feedbacks, results):
        f.is_constructive = r.is_constructive
        f.specificity_score = r.specificity_score
        f.validity_score = r.validity_score
        f.severity_score = r.severity_score
        f.urgency_score = r.urgency_score
        f.quality_score = r.quality_score
        f.topic_category = r.topic_category
        f.topic_categories_json = json.dumps(r.topic_categories_multi, ensure_ascii=False)
        f.improvement_suggestion = r.improvement_suggestion
        f.language = r.language
        f.needs_review = r.needs_review
        f.classifier_name = r.classifier_name
        stamped = extract_timestamp_sec(f.comment_text)
        if stamped is not None or f.source != "scene":  # 장면별 의견의 지정 시각은 유지
            f.timestamp_sec = stamped
    round_obj.processing_seconds = time.time() - started
    advance(round_obj, "feedback_classified", commit=False)
    db.session.commit()
    return sum(1 for r in results if r.is_constructive)


def start_clustering(round_obj):
    constructive = [f for f in round_obj.feedbacks if f.is_constructive]
    if not constructive:
        raise PipelineError("건설적 피드백이 없어 클러스터링할 수 없습니다.")
    from . import jobs

    advance(round_obj, "clustering", commit=False)
    round_obj.generation_error = ""
    db.session.commit()
    return jobs.enqueue("cluster", round_obj.id, {})


def run_clustering_job(app, round_id):
    round_obj = db.session.get(VideoRound, round_id)
    constructive = [f for f in round_obj.feedbacks if f.is_constructive]
    started = time.time()
    try:
        clusters = run_clustering(
            texts=[f.comment_text for f in constructive],
            topic_categories=[f.topic_category for f in constructive],
            validity_scores=[f.validity_score for f in constructive],
            severity_scores=[f.severity_score for f in constructive],
            urgency_scores=[f.urgency_score for f in constructive],
            quality_scores=[f.quality_score for f in constructive],
            config=app.config,
        )
        approval.save_clusters(round_obj, clusters, constructive, app.config)
        storyboard = ensure_storyboard(round_obj, app.config)
        assign_affected_shots(round_obj, storyboard, app.config)
        round_obj.processing_seconds = (round_obj.processing_seconds or 0.0) + time.time() - started
        calculate_metrics(round_obj, app.config)
        advance(round_obj, "pending_review", commit=False)
    except Exception as exc:
        db.session.rollback()
        round_obj = db.session.get(VideoRound, round_id)
        round_obj.generation_error = f"클러스터링 실패: {type(exc).__name__}: {exc}"
        advance(round_obj, "feedback_classified", commit=False)
        db.session.commit()
        raise
    db.session.commit()


_ALL_SHOT_CATEGORIES = {"technical", "subtitle", "audio"}


def assign_affected_shots(round_obj, storyboard, config):
    """클러스터가 지적한 장면 계산: 멤버 댓글의 타임스탬프 → 장면.
    타임스탬프가 하나도 없으면 전체 톤 피드백(기술·자막·음향)은 전 장면, 그 외는 LLM 추정 → 실패 시 전 장면."""
    from .prompt_updater import estimate_affected_shots

    all_ids = [s["id"] for s in storyboard["shots"]]
    for cluster in round_obj.clusters:
        found = []
        for f in cluster.feedbacks:
            shot_id = sbm.shot_at(storyboard, f.timestamp_sec)
            if shot_id and shot_id not in found:
                found.append(shot_id)
        if not found:
            if cluster.topic_category in _ALL_SHOT_CATEGORIES:
                found = list(all_ids)
            else:
                found = estimate_affected_shots(
                    cluster.representative_text, storyboard, config) or list(all_ids)
        cluster.affected_shots_json = json.dumps(found)


# ── 상태 API 응답 ─────────────────────────────────────────────────────────
def status_payload(app, round_obj, url_for):
    cfg = app.config
    storyboard = round_obj.storyboard_data
    recommended = quality_gate.recommend_api_shots(round_obj)
    by_shot = {}
    for r in round_obj.shot_renders:
        by_shot.setdefault(r.shot_id, []).append(r)

    from . import translate
    shots = []
    ko = translate.lookup([s["prompt"] for s in (storyboard or {"shots": []})["shots"]])
    ranges = {i: (a, b) for i, a, b in sbm.shot_ranges(storyboard)} if storyboard else {}
    for shot in (storyboard or {"shots": []})["shots"]:
        renders = by_shot.get(shot["id"], [])
        chosen = next((r for r in renders if r.selected), None) or (renders[-1] if renders else None)
        start, end = ranges.get(shot["id"], (0.0, float(shot["duration"])))
        shots.append({
            "id": shot["id"], "prompt": shot["prompt"], "duration": shot["duration"],
            "prompt_ko": ko.get(translate.key(shot["prompt"]), ""),  # 화면 표시용 한글 번역 (없으면 빈 문자열)
            "start": start, "end": end,
            "status": chosen.status if chosen else "queued",
            "generator": chosen.generator if chosen else shot.get("generator", "local"),
            "provider": chosen.provider if chosen else "",
            "cost_usd": round(sum(r.cost_usd for r in renders), 4),
            "recommend_api": shot["id"] in recommended,
            "estimate_api_cost_usd": quality_gate.estimate_cost(shot["duration"], cfg),
            "render_id": chosen.id if chosen else None,
            "image_url": (url_for("session.shot_image", render_id=chosen.id)
                          if chosen and chosen.image_path and os.path.exists(chosen.image_path) else ""),
            "clip_url": (url_for("session.shot_video", render_id=chosen.id)
                         if chosen and chosen.path and os.path.exists(chosen.path) else ""),
            "seed": chosen.seed if chosen else shot.get("seed"),
            "notes": list(shot.get("notes") or []),
            "reused": bool(chosen and chosen.status == "reused"),
            "enhanced_prompt": chosen.enhanced_prompt if chosen and chosen.enhanced_prompt else "",
            "narration": shot.get("narration", ""),
            "error": chosen.error if chosen and chosen.status == "failed" else "",
            "versions": [{
                "render_id": r.id, "version": r.version, "generator": r.generator,
                "provider": r.provider, "status": r.status, "selected": r.selected,
                "cost_usd": r.cost_usd, "flags": r.quality_flags,
                "url": url_for("session.shot_video", render_id=r.id) if r.path else "",
            } for r in renders if r.status in ("done", "reused")],
        })

    feedbacks = round_obj.feedbacks
    constructive = [f for f in feedbacks if f.is_constructive]
    clusters = round_obj.clusters
    summary = {
        "feedback_total": len(feedbacks),
        "constructive": len(constructive),
        "constructive_ratio": round(len(constructive) / len(feedbacks), 3) if feedbacks else 0.0,
        "cluster_count": len(clusters),
        "avg_cohesion": round(sum(c.cohesion_score for c in clusters) / len(clusters), 3)
        if clusters else 0.0,
        "approved": sum(1 for c in clusters if c.status == "approved"),
        "changes": [c.representative_text for c in clusters if c.status == "approved"],
    }
    configured = configured_name(cfg)
    fallback = round_obj.adapter_used == "mock" and configured not in ("mock",)
    budget = quality_gate.budget_summary(round_obj.session_id, cfg)
    return {
        "round": round_obj.round_number,
        "round_id": round_obj.id,
        "state": round_obj.status,
        "review": ("images" if round_obj.status == IMAGES_READY
                   else "clips" if round_obj.status == CLIPS_READY else None),
        "staged": staged_enabled(cfg),
        "step": _step_number(round_obj.status),
        "steps": compute_steps(round_obj, cfg),
        "rough_running": round_obj.status == "generating" and rough_cut_in_progress(round_obj),
        "adapter_used": round_obj.adapter_used or configured,
        "adapter_configured": configured,
        "fallback_to_mock": fallback,
        "fallback_detail": round_obj.generation_detail if fallback else "",
        "shots": shots,
        "narration": _narration_status(round_obj),
        "edit": {"lines": edit_plan.describe(storyboard.get("edit"), storyboard) if storyboard else [],
                 "note": round_obj.edit_note or ""},
        "youtube": {"uploading": upload_in_progress(round_obj), "video_id": round_obj.youtube_video_id or "",
                    "message": upload_message(round_obj)},
        "video_url": url_for("session.video", round_id=round_obj.id) if round_obj.video_path else "",
        "require_video_approval": bool(cfg.get("REQUIRE_VIDEO_APPROVAL", True)),
        "video_approved": bool(round_obj.video_approved),
        "commercial": {
            "available": commercial_available(cfg),
            "adapter": cfg.get("COMMERCIAL_VIDEO_ADAPTER"),
            "reasons": quality_gate.QUALITY_REASONS,
        },
        "commercial_spent_usd": budget["spent_usd"],
        "commercial_budget_usd": budget["budget_usd"],
        "commercial_remaining_usd": budget["remaining_usd"],
        "cost_per_sec_usd": budget["cost_per_sec_usd"],
        "summary": summary,
        "error": round_obj.generation_error or None,
    }


def _narration_status(round_obj):
    """상태 API용 내레이션 요약. 내레이션이 없는 영상은 status='none'."""
    if not round_obj.narration_json:
        return {"status": "none", "detail": "", "shots": {}}
    try:
        return json.loads(round_obj.narration_json)
    except ValueError:
        return {"status": "none", "detail": "", "shots": {}}


def make_classifier(app):
    state_dir = app.config.get("CLASSIFIER_STATE_DIR") or app.instance_path
    return create_classifier(app.config, state_dir=state_dir)


__all__ = [
    "advance", "can_advance", "compute_steps", "InvalidTransition", "PipelineError",
    "TRANSITIONS", "STEP_LABELS",
]

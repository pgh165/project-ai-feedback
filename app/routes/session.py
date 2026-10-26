import json
import os
import re
import secrets
import shutil
from collections import Counter
from pathlib import Path

from flask import (
    Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request,
    send_file, url_for,
)

from ..models import (
    ClusterResult, Feedback, GenerationJob, GlobalTechnicalSetting, Metric, PipelineSession, PromptVersion,
    ShotRender, VideoRound, db,
)
from ..pipeline import approval, orchestrator, quality_gate, storyboard_import, youtube_client
from ..pipeline.classifier import create_classifier
from ..pipeline.metrics import calculate_metrics
from ..pipeline.orchestrator import InvalidTransition, PipelineError
from ..pipeline import ko_en, translate, voice
from ..pipeline.prompt_builder import build_initial_prompt, extract_shots
from ..pipeline.storyboard_model import ASPECTS
from ..pipeline.video_generator.factory import configured_name

bp = Blueprint("session", __name__)

_DECISIONS = {"approve": "approved", "reject": "rejected", "defer": "deferred", "reset": "pending"}


def _get_session(session_id):
    session = db.session.get(PipelineSession, session_id)
    if session is None:
        abort(404)
    return session


def _require_status(round_obj, *allowed):
    if round_obj.status not in allowed:
        flash(f"현재 단계({round_obj.status})에서는 실행할 수 없습니다.", "warning")
        return False
    return True


def _classifier():
    state_dir = current_app.config.get("CLASSIFIER_STATE_DIR") or current_app.instance_path
    return create_classifier(current_app.config, state_dir=state_dir)


def _back(session_id, tab=None):
    url = url_for("session.detail", session_id=session_id)
    return redirect(f"{url}#{tab}" if tab else url)


# ── 목록 / 생성 ──────────────────────────────────────────────────────────
# 첫 화면 예시 (누르면 입력창을 채운다)
_EXAMPLES = [  # 서로 다른 종류 하나씩 (GENRES와 같은 분류)
    ("학과 소개", "우리 학과의 실습실과 연구 장비, 학생들이 프로젝트를 만드는 모습을 보여 주는 30초 학과 소개 영상"),
    ("제품 광고", "새로 나온 무선 이어폰의 디자인과 착용감을 강조하는 15초 제품 광고"),
    ("사용법 설명", "로봇 팔 키트를 조립하고 처음 움직여 보는 과정을 순서대로 보여 주는 설명 영상"),
    ("뮤직비디오", "비 오는 밤 네온 거리를 혼자 걷는 사람의 감성적인 뮤직비디오"),
    ("짧은 이야기", "오래된 사진 한 장으로 할머니의 젊은 시절을 떠올리는 1분 단편"),
]
GENRES = ["홍보·광고", "소개·안내", "교육·설명", "뮤직비디오", "브이로그", "단편 영화"]


@bp.route("/")
def home():
    """첫 화면: 가운데 입력창 하나 — 만들고 싶은 영상을 적으면 세부사항 화면(create)으로 넘어간다."""
    recent = PipelineSession.query.order_by(PipelineSession.created_at.desc()).limit(4).all()
    total = PipelineSession.query.count()
    return render_template("home.html", recent=recent, total=total, examples=_EXAMPLES)


# 사람이 눌러야 다음으로 넘어가는 상태 = 왼쪽 대시보드 '할 일'
TODO_STATUSES = ("images_ready", "clips_ready", "video_ready", "generation_failed", "pending_review")


@bp.app_context_processor
def _nav_context():
    """왼쪽 대시보드 값 — 대시보드가 있는 화면에서만 부른다 (nav_info())."""
    def nav_info():
        from ..pipeline import youtube_upload

        rounds = [s.current_round for s in PipelineSession.query.all()]
        return {"total": len(rounds), "todo": sum(1 for r in rounds if r and r.status in TODO_STATUSES),
                "youtube": youtube_upload.status(current_app.config, current_app.instance_path)}
    return {"nav_info": nav_info}


@bp.route("/api/system")
def api_system():
    """대시보드 아래 GPU 상태: ComfyUI가 켜져 있는지, 작업 큐에 몇 개가 있는지."""
    from ..pipeline.video_generator.comfyui.comfy_client import ComfyClient

    alive = ComfyClient(current_app.config["COMFYUI_URL"], timeout_sec=2).is_alive(timeout=1)
    counts = dict(db.session.query(GenerationJob.status, db.func.count()).filter(
        GenerationJob.status.in_(("queued", "running"))).group_by(GenerationJob.status).all())
    return jsonify(comfyui=alive, running=counts.get("running", 0), queued=counts.get("queued", 0))


@bp.route("/projects")
def index():
    sessions = PipelineSession.query.order_by(PipelineSession.created_at.desc()).all()
    view = "todo" if request.args.get("view") == "todo" else "all"
    if view == "todo":
        sessions = [s for s in sessions if s.current_round and s.current_round.status in TODO_STATUSES]
    return render_template("index.html", sessions=sessions, view=view)


def _is_busy(session):
    round_ids = [r.id for r in session.rounds]
    return bool(round_ids) and GenerationJob.query.filter(
        GenerationJob.round_id.in_(round_ids),
        GenerationJob.status.in_(("queued", "running")),
    ).first() is not None


@bp.route("/session/<int:session_id>/delete", methods=["POST"])
def delete(session_id):
    session = _get_session(session_id)
    if _is_busy(session):
        flash("생성·분석 작업이 진행 중입니다. 먼저 중지(취소)한 뒤 삭제하세요.", "warning")
        return _back(session_id)
    title = session.title
    _delete_session(session)
    flash(f"'{title}' 프로젝트를 삭제했습니다.", "success")
    return redirect(url_for("session.index"))


@bp.route("/session/delete-selected", methods=["POST"])
def delete_selected():
    ids = {int(i) for i in request.form.getlist("session_ids") if i.isdigit()}
    deleted, skipped = 0, []
    for session in PipelineSession.query.filter(PipelineSession.id.in_(ids)).all() if ids else []:
        if _is_busy(session):
            skipped.append(session.title)
            continue
        _delete_session(session)
        deleted += 1
    if not ids:
        flash("삭제할 프로젝트를 선택하세요.", "warning")
    if deleted:
        flash(f"프로젝트 {deleted}개를 삭제했습니다.", "success")
    if skipped:
        flash("작업이 진행 중이라 건너뜀: " + ", ".join(skipped), "warning")
    return redirect(url_for("session.index"))


def _delete_session(session):
    """프로젝트와 딸린 DB 행·영상 파일을 지운다. 호출 전에 진행 중 작업이 없는지 확인할 것."""
    round_ids = [r.id for r in session.rounds]
    video_dirs = {orchestrator.output_dir(current_app, r).parent for r in session.rounds}
    if round_ids:
        cluster_ids = [c.id for c in ClusterResult.query.filter(
            ClusterResult.round_id.in_(round_ids)).all()]
        if cluster_ids:
            GlobalTechnicalSetting.query.filter(
                GlobalTechnicalSetting.source_cluster_id.in_(cluster_ids)
            ).update({"source_cluster_id": None}, synchronize_session=False)
        ShotRender.query.filter(ShotRender.round_id.in_(round_ids)).update(
            {"reused_from_id": None}, synchronize_session=False)
        Metric.query.filter(Metric.round_id.in_(round_ids)).delete(synchronize_session=False)
        GenerationJob.query.filter(GenerationJob.round_id.in_(round_ids)).delete(
            synchronize_session=False)
    db.session.expire_all()
    db.session.delete(session)
    db.session.commit()

    for d in video_dirs:  # 영상 파일 정리 (실패해도 DB 삭제는 유지)
        shutil.rmtree(d, ignore_errors=True)


_GENRE_HINTS = [  # 첫 화면 문장 → 영상 종류(GENRES) 추천. AI 자동 채우기 전까지 쓰는 간단한 규칙, 앞에서부터 맞춘다
    (r"뮤직\s*비디오|MV|노래", "뮤직비디오"), (r"단편|드라마|영화|이야기", "단편 영화"),
    (r"브이로그|일상|여행", "브이로그"), (r"설명|사용법|조립|강의|튜토리얼|교육", "교육·설명"),
    (r"제품|신제품|출시|광고", "홍보·광고"), (r"학과|학교|대학|입학|캠퍼스|축제|행사|박람회|소개|안내", "소개·안내"),
    (r"홍보", "홍보·광고"),
]


def _suggest_from_idea(idea):
    """첫 화면에서 적은 문장으로 제목·종류를 추천한다. 사용자가 세부사항 화면에서 고친다."""
    first = re.split(r"[\n.!?]", idea.strip(), maxsplit=1)[0].strip()
    title = first if len(first) <= 28 else first[:28].rsplit(" ", 1)[0] + "…"
    genre = next((g for pattern, g in _GENRE_HINTS if re.search(pattern, idea, re.I)), "")
    return title, genre


@bp.route("/session/create", methods=["GET", "POST"])
def create():
    if request.method == "GET":
        idea = request.args.get("idea", "").strip()[:2000]
        aspect = request.args.get("aspect", "9:16")
        title, genre = _suggest_from_idea(idea) if idea else ("", "")
        prefill = {"synopsis": idea, "title": title, "genre": genre,
                   "aspect_ratio": aspect if aspect in ASPECTS else "9:16"}
        return render_template("session/create.html", prefill=prefill, from_home=bool(idea), genres=GENRES,
                               open_import=request.args.get("attach") == "xlsx")

    title = request.form.get("title", "").strip()
    synopsis = request.form.get("synopsis", "").strip()
    if not title or not synopsis:
        flash("제목과 만들고 싶은 영상 설명은 필수입니다.", "warning")
        return render_template("session/create.html", prefill=request.form, from_home=False, genres=GENRES), 400

    settings = [s.instruction for s in GlobalTechnicalSetting.query.filter_by(active=True)]
    aspect = request.form.get("aspect_ratio", "9:16")
    if aspect not in ASPECTS:
        aspect = "9:16"
    try:
        shot_seconds = min(max(float(request.form.get("shot_seconds") or 0), 0.0), 8.0)
    except ValueError:
        shot_seconds = 0.0
    prompt_text, source = build_initial_prompt(
        title=title,
        genre=request.form.get("genre", "").strip(),
        characters=request.form.get("characters", "").strip(),
        synopsis=synopsis,
        shots_text=request.form.get("shots", ""),
        style=request.form.get("style", "").strip(),
        global_settings=settings,
        config=current_app.config,
        shot_seconds=shot_seconds,
        narration_text=request.form.get("narration", ""),
    )
    try:  # 한국어로 적은 장면·스타일은 영어로 (영상 모델에는 영어만)
        prompt_text, tr_source, originals = ko_en.translate_prompt_doc(prompt_text, current_app.config)
    except ko_en.KoEnError as exc:
        flash(f"{exc} 장면 목록·스타일을 영어로 적거나, 잠시 뒤 다시 시도하세요.", "warning")
        return render_template("session/create.html", prefill=request.form, from_home=False, genres=GENRES,
                               open_folds=True), 400
    session = PipelineSession(
        title=title, genre=request.form.get("genre", "").strip(),
        characters=request.form.get("characters", "").strip(),
        style=request.form.get("style", "").strip(),
        aspect_ratio=aspect, shot_seconds=shot_seconds)
    db.session.add(session)
    db.session.flush()
    round_obj = VideoRound(session_id=session.id, round_number=1, prompt_text=prompt_text)
    db.session.add(round_obj)
    db.session.flush()
    note = f"1차 초기 프롬프트 ({source})" + (f" · 한국어 → 영어 ({tr_source})" if tr_source else "")
    approval.add_version(session.id, round_obj.id, "", prompt_text, note)
    ko_en.remember_originals(originals)  # 화면에는 사용자가 쓴 한국어가 그대로 보이게
    db.session.commit()
    if request.form.get("start"):  # 저장하고 제작 시작 → ② 제작 탭
        orchestrator.start_generation(round_obj, current_app.config)
        return _back(session.id, "produce")
    return _back(session.id, "plan")  # 기획 저장 → 프로젝트의 ① 기획 탭에서 이어서 고친다


@bp.route("/session/draft/fill", methods=["POST"])
def draft_fill_route():
    """세부사항 화면 'AI로 채우기': 비어 있는 칸만 한국어 초안으로 채워 돌려준다 (저장하지 않음)."""
    from ..pipeline import draft_fill

    form = request.get_json(silent=True) or {}
    form = {k: str(form.get(k) or "")[:3000] for k in ("synopsis", "shot_seconds") + draft_fill.FIELDS}
    try:
        filled, source = draft_fill.fill(form, current_app.config, GENRES)
    except draft_fill.DraftFillError as exc:
        return _error(str(exc), 400)
    return jsonify(fields=filled, source=source)


@bp.route("/session/draft/refine", methods=["POST"])
def draft_refine_route():
    """기획 화면 '장면 다듬기': 장면 줄을 영상 모델용 영어 묘사로 고쳐 돌려준다 (저장하지 않음 — 화면에서 받아들일 때만 바뀜)."""
    from ..pipeline import scene_refine

    form = request.get_json(silent=True) or {}
    form = {k: str(form.get(k) or "")[:4000] for k in ("shots", "synopsis", "style", "characters")}
    try:
        lines, source, reason = scene_refine.refine(form, current_app.config)
    except scene_refine.SceneRefineError as exc:
        return _error(str(exc), 400)
    before = [(f"{m} {b}" if m else b) for m, b in scene_refine.split_lines(form["shots"])]
    return jsonify(before=before, after=lines, source=source,
                   note=f"{reason} 작은 모델로 다듬었습니다. 결과를 꼼꼼히 확인하세요." if reason else "")


# ── 완성 영상 불러오기: 컷 경계를 찾아 장면별로 나눈 프로젝트를 만든다 ─────────
_IMPORT_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
_TOKEN = re.compile(r"^[0-9a-f]{16}$")


def _import_dir(token=None):
    base = Path(current_app.instance_path) / "imports"
    if token is None:
        return base
    if not _TOKEN.match(token or ""):
        abort(404)
    return base / token


def _import_source(token):
    folder = _import_dir(token)
    found = next((p for p in folder.glob("source.*") if p.suffix.lower() in _IMPORT_EXTS), None) \
        if folder.is_dir() else None
    if found is None:
        abort(404)
    return found


@bp.route("/session/import")
def import_page():
    return render_template("session/import.html", max_shots=current_app.config["LOCAL_VIDEO_MAX_SHOTS"])


@bp.route("/session/import/analyze", methods=["POST"])
def import_analyze():
    """영상(+자막)을 받아 임시 폴더에 두고 컷 경계를 찾는다. 프로젝트는 아직 만들지 않는다."""
    from ..pipeline import video_import
    from ..pipeline.video_generator.comfyui.rough_cut import probe_media, resolve_ffmpeg

    upload = request.files.get("video")
    if upload is None or not upload.filename:
        return jsonify(error="영상 파일을 선택해 주세요."), 400
    ext = Path(upload.filename).suffix.lower()
    if ext not in _IMPORT_EXTS:
        return jsonify(error="mp4·mov·mkv·webm 영상만 불러올 수 있습니다."), 400
    token = secrets.token_hex(8)
    folder = _import_dir(token)
    folder.mkdir(parents=True, exist_ok=True)
    source = folder / f"source{ext}"
    upload.save(source)
    ffmpeg = resolve_ffmpeg(current_app.config)
    info = probe_media(ffmpeg, source)
    if info is None:
        shutil.rmtree(folder, ignore_errors=True)
        return jsonify(error="영상 정보를 읽을 수 없습니다. 다른 파일로 시도해 주세요."), 400
    sensitivity = request.form.get("sensitivity", type=float) or 1.0
    cuts, _curve = video_import.detect_cuts(ffmpeg, source, info["duration"], sensitivity=sensitivity)
    subtitles = []
    srt = request.files.get("subtitles")
    if srt is not None and srt.filename:
        subtitles = video_import.parse_srt(srt.read().decode("utf-8-sig", errors="replace"))
    return jsonify(token=token, name=Path(upload.filename).stem, duration=info["duration"],
                   width=info["width"], height=info["height"], fps=info["fps"],
                   aspect=video_import.aspect_of(info["width"], info["height"]),
                   cuts=cuts, subtitles=subtitles,
                   video_url=url_for("session.import_video_file", token=token),
                   frame_url=url_for("session.import_frame", token=token))


@bp.route("/session/import/<token>/detect", methods=["POST"])
def import_detect(token):
    """민감도를 바꿔 컷을 다시 찾는다 (다시 올리지 않고)."""
    from ..pipeline import video_import
    from ..pipeline.video_generator.comfyui.rough_cut import probe_media, resolve_ffmpeg

    source = _import_source(token)
    ffmpeg = resolve_ffmpeg(current_app.config)
    info = probe_media(ffmpeg, source)
    sensitivity = float((request.get_json(silent=True) or {}).get("sensitivity") or 1.0)
    cuts, _curve = video_import.detect_cuts(ffmpeg, source, info["duration"], sensitivity=sensitivity)
    return jsonify(cuts=cuts)


@bp.route("/session/import/<token>/video")
def import_video_file(token):
    return send_file(_import_source(token), conditional=True)


@bp.route("/session/import/<token>/frame")
def import_frame(token):
    """장면 미리보기 썸네일 (t초 프레임). 같은 시각은 한 번만 뽑는다."""
    from ..pipeline import video_import
    from ..pipeline.video_generator.comfyui.rough_cut import resolve_ffmpeg

    source = _import_source(token)
    t = max(0.0, request.args.get("t", type=float) or 0.0)
    thumb = source.parent / f"frame_{int(round(t * 100))}.jpg"
    if not thumb.exists():
        video_import.grab_frame(resolve_ffmpeg(current_app.config), source, t, thumb, width=320)
    return send_file(thumb, mimetype="image/jpeg", max_age=3600)


@bp.route("/session/import/create", methods=["POST"])
def import_create():
    from ..pipeline import video_import

    body = request.get_json(silent=True) or {}
    token = body.get("token", "")
    source = _import_source(token)
    title = (body.get("title") or "").strip()
    if not title:
        return jsonify(error="영상 제목을 적어 주세요."), 400
    try:
        cuts = [float(c) for c in body.get("cuts") or []]
        session = video_import.create_project(
            current_app._get_current_object(), source, title, cuts,
            genre=(body.get("genre") or "").strip(), description=(body.get("description") or "").strip(),
            subtitles=body.get("subtitles") or [])
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    shutil.rmtree(source.parent, ignore_errors=True)  # 원본은 프로젝트 폴더로 옮겼다
    return jsonify(redirect=url_for("session.detail", session_id=session.id))


@bp.route("/session/import-storyboard", methods=["POST"])
def import_storyboard():
    """콘티 엑셀(.xlsx)을 읽어 새 영상 폼에 채울 값을 돌려준다. 저장은 하지 않는다 (폼에서 확인·수정 후 생성)."""
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return jsonify(error="엑셀 파일을 선택해 주세요."), 400
    if not upload.filename.lower().endswith(".xlsx"):
        return jsonify(error="콘티는 .xlsx 파일만 읽을 수 있습니다."), 400
    try:
        result = storyboard_import.parse_xlsx(upload.read(), current_app.config)
    except storyboard_import.ImportError_ as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(result)


@bp.route("/session/<int:session_id>")
def detail(session_id):
    session = _get_session(session_id)
    round_number = request.args.get("round", type=int)
    round_obj = next((r for r in session.rounds if r.round_number == round_number),
                     session.current_round)
    feedbacks = sorted(round_obj.feedbacks, key=lambda f: (f.is_constructive is not True,
                                                           -(f.quality_score or 0)))
    scenes = _scene_cards(round_obj)
    plan_shots = extract_shots(round_obj.prompt_text)
    english = [sc["prompt"] for sc in scenes] + plan_shots
    translate.request(current_app._get_current_object(), english)  # 없는 번역은 백그라운드에서
    from ..pipeline import edit_plan
    storyboard = round_obj.storyboard_data
    edit_source = storyboard.get("edit") if storyboard else edit_plan.loads(round_obj.edit_json or "")
    rough_running = round_obj.status == "generating" and orchestrator.rough_cut_in_progress(round_obj)
    stage = _stage_of(round_obj, rough_running)
    return render_template(
        "session/detail.html", session=session, round=round_obj, feedbacks=feedbacks,
        stage=stage, stages=_stage_tabs(stage, round_obj), plan=_plan_view(session, round_obj),
        narration_report=orchestrator._narration_status(round_obj), narration_rows=_narration_rows(round_obj),
        narration_editable=(round_obj.id == session.current_round.id and round_obj.status in NARRATION_EDITABLE),
        scenes=scenes, plan_shots=plan_shots, edit_lines=edit_plan.describe(edit_source, storyboard),
        yt=_youtube_view(round_obj), deco=_decorate_view(session, round_obj),
        tr=translate.lookup(english), tr_key=translate.key,
        tr_url=url_for("session.api_translations", session_id=session.id, round=round_obj.round_number),
        rough_running=rough_running,
        is_current=round_obj.id == session.current_round.id,
        simulation=youtube_client.is_simulation(current_app.config),
        adapter_name=current_app.config["VIDEO_GENERATOR_ADAPTER"])


# ── 상세 화면 단계 탭: 기획 → 제작 → 확인 → 공개 → 의견 (모든 버전이 같은 탭) ──
STAGES = [("plan", "기획"), ("produce", "제작"), ("confirm", "확인"), ("publish", "공개"), ("feedback", "의견")]


def _stage_of(round_obj, rough_running):
    """지금 상태가 속한 단계. 반영이 끝난 지난 버전은 'done'."""
    status = round_obj.status
    rough_failed = status == "generation_failed" and (round_obj.generation_error or "").startswith("러프컷")
    if status == "prompt_built":
        return "plan"
    if status in ("generating", "generation_failed", "images_ready", "clips_ready"):
        return "confirm" if rough_running or rough_failed else "produce"
    if status == "video_ready":
        return "confirm"
    if status == "prompt_updated":
        return "done"
    return "feedback"  # 공개 뒤: 댓글 모으기·분류·묶기·검토


def _stage_tabs(stage, round_obj):
    """왼쪽 사이드 탭 [(key, 이름, 표시, 표시 class)] — 지난 단계 ✓, 지금 단계 '지금', 앞 단계는 표시 없음."""
    order = [k for k, _ in STAGES]
    now = len(order) if stage == "done" else order.index(stage)
    waiting = round_obj.status in TODO_STATUSES
    tabs = []
    for i, (key, label) in enumerate(STAGES):
        if i < now:
            tabs.append((key, label, "✓", "is-done"))
        elif i == now:
            tabs.append((key, label, "확인 필요" if waiting else "지금", "is-wait" if waiting else "is-now"))
        else:
            tabs.append((key, label, "", ""))
    return tabs


def _plan_view(session, round_obj):
    """기획 탭: 프롬프트 문서에서 처음 입력한 칸을 다시 꺼낸다. 장면·스타일은 사용자가 쓴 한국어(번역 캐시) 우선."""
    from ..pipeline.prompt_builder import _SHOT_LINE, _SHOT_MARK, extract_narration, parse_sections

    sections = parse_sections(round_obj.prompt_text or "")
    lines = []
    for m in filter(None, map(_SHOT_LINE.match, sections.get("샷 목록", "").splitlines())):
        line = m.group(1).strip()
        mark = _SHOT_MARK.match(line)
        prefix, body = (line[:mark.end()], line[mark.end():].strip()) if mark and mark.end() else ("", line)
        lines.append({"mark": prefix, "en": body})
    style = sections.get("영상 스타일", "")
    ko = translate.lookup([l["en"] for l in lines] + [style])
    for l in lines:
        l["ko"] = ko.get(translate.key(l["en"]), "")
    narration = extract_narration(round_obj.prompt_text or "")
    from ..pipeline.prompt_builder import extract_subtitles
    subtitles = extract_subtitles(round_obj.prompt_text or "")
    prefill = {  # 기획 입력 화면(_plan_form.html)에 다시 채울 값 — 처음 입력하던 화면과 같은 칸
        "title": sections.get("프로젝트", session.title), "genre": sections.get("영상 종류", session.genre or ""),
        "synopsis": sections.get("영상 설명", ""), "characters": sections.get("등장 요소", ""),
        "shots": "\n".join(l["mark"] + (l["ko"] or l["en"]) for l in lines),
        "narration": "\n".join(f"{k}. {narration[k]}" for k in sorted(narration)),
        "style": ko.get(translate.key(style), "") or style if style else "",
        "aspect_ratio": session.aspect_ratio or "9:16",
        "shot_seconds": (int(session.shot_seconds) if session.shot_seconds else ""),
    }
    return {
        "prefill": prefill,
        "subtitles": [subtitles[k] for k in sorted(subtitles)],
        "title": sections.get("프로젝트", session.title), "genre": sections.get("영상 종류", session.genre or ""),
        "characters": sections.get("등장 요소", ""), "synopsis": sections.get("영상 설명", ""),
        "shots": lines, "style": style, "style_ko": ko.get(translate.key(style), "") if style else "",
        "narration": [narration[k] for k in sorted(narration)],
        "applied": sections.get("시청자 반영 사항", ""), "directions": sections.get("연출 지침", ""),
        "editable": round_obj.id == session.current_round.id and round_obj.status == "prompt_built",
        "producing": (round_obj.id == session.current_round.id and round_obj.status in orchestrator.PLAN_EDITABLE
                      and not _is_busy(session)),
        "replannable": (round_obj.id == session.current_round.id and round_obj.adapter_used != "import"
                        and round_obj.status in ("images_ready", "clips_ready", "generation_failed")),
        "genres": GENRES,
    }


def _plan_text(session, round_obj, form, translate=True):
    """기획 칸(form) → 새 프롬프트 문서. 화면에 보인 그대로인 장면·스타일 줄은 원래 영어를 쓴다
    (다시 번역하면 영어가 바뀌어 장면을 다시 만들게 된다). (문서, 번역 엔진, [(영어, 한국어)], 스타일) 또는 ValueError."""
    from ..pipeline.prompt_builder import (DEFAULT_STYLE, _SHOT_LINE, _numbered_narration, number_lines,
                                           parse_sections, render_sections)

    view = _plan_view(session, round_obj)
    shown = {(l["mark"] + (l["ko"] or l["en"])).strip(): l["mark"] + l["en"] for l in view["shots"]}
    lines = []
    for raw in form.get("shots", "").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        m = _SHOT_LINE.match(raw)
        raw = m.group(1).strip() if m else raw
        lines.append(shown.get(raw, raw))
    if not form.get("title", "").strip() or not form.get("synopsis", "").strip() or not lines:
        raise ValueError("제목·만들고 싶은 영상·장면 목록은 비울 수 없습니다.")
    style = form.get("style", "").strip()
    if style and style == (view["style_ko"] or view["style"]):
        style = view["style"]
    limit = current_app.config["LOCAL_VIDEO_MAX_SHOTS"]
    sections = parse_sections(round_obj.prompt_text)
    sections.update({"프로젝트": form["title"].strip(), "영상 종류": form.get("genre", "").strip(),
                     "등장 요소": form.get("characters", "").strip(), "영상 설명": form["synopsis"].strip(),
                     "샷 목록": number_lines(lines[:limit]), "영상 스타일": style or DEFAULT_STYLE})
    narration = _numbered_narration(form.get("narration", ""), limit)
    if narration:
        sections["내레이션"] = narration
    else:
        sections.pop("내레이션", None)
    text = render_sections(sections)
    if not translate:
        return text, "", [], style
    try:
        text, tr_source, originals = ko_en.translate_prompt_doc(text, current_app.config)
    except ko_en.KoEnError as exc:
        raise ValueError(f"{exc} 저장하지 않았습니다.") from exc
    return text, tr_source, originals, style


@bp.route("/session/<int:session_id>/plan", methods=["POST"])
def save_plan(session_id):
    """기획 탭 저장. 제작 전이면 프롬프트만 고치고, 제작 중(이미지·컷 확인·확인 단계·실패)이면
    고친 장면만 다시 만든다 (orchestrator.apply_plan_change). preview=1이면 바뀔 내용만 JSON으로 돌려준다."""
    session = _get_session(session_id)
    round_obj = session.current_round
    f = request.form
    producing = round_obj.status in orchestrator.PLAN_EDITABLE
    if f.get("preview"):  # 번역 전 문서로 비교 — 한글로 고친 줄은 어차피 '고친 장면'이다
        if not producing:
            return jsonify(regen=[], dropped=[], style=False, narration=False, kept=0)
        try:
            text = _plan_text(session, round_obj, f, translate=False)[0]
        except ValueError as exc:
            return _error(str(exc), 400)
        d = orchestrator.plan_diff(round_obj, text, current_app.config)
        db.session.rollback()
        return jsonify(regen=d["regen"], dropped=d["dropped"], style=d["style"], narration=d["narration"],
                       kept=len(d["mapping"]), video_ready=round_obj.status == "video_ready")
    if not producing and not _require_status(round_obj, "prompt_built"):
        return _back(session_id, "plan")
    try:
        new_text, tr_source, originals, style = _plan_text(session, round_obj, f)
    except ValueError as exc:
        flash(str(exc), "warning")
        return _back(session_id, "plan")
    ko_en.remember_originals(originals)
    old_text = round_obj.prompt_text
    session.title, session.genre = f["title"].strip(), f.get("genre", "").strip()
    session.characters, session.style = f.get("characters", "").strip(), style
    if not producing:  # 제작 전에는 화면 비율·장면 길이도 바꿀 수 있다
        if f.get("aspect_ratio") in ASPECTS:
            session.aspect_ratio = f["aspect_ratio"]
        try:
            session.shot_seconds = min(max(float(f.get("shot_seconds") or 0), 0.0), 8.0)
        except ValueError:
            pass
        if new_text != old_text:
            note = "기획 수정" + (f" · 한국어 → 영어 ({tr_source})" if tr_source else "")
            approval.add_version(session_id, round_obj.id, old_text, new_text, note)
            round_obj.prompt_text = new_text
        db.session.commit()
        if f.get("start"):
            return generate(session_id)
        flash("기획을 저장했습니다.", "success")
        return _back(session_id, "plan")
    if new_text == old_text:
        db.session.commit()
        flash("바뀐 내용이 없습니다.", "info")
        return _back(session_id, "plan")
    try:
        summary = orchestrator.apply_plan_change(round_obj, new_text, current_app.config)
    except PipelineError as exc:
        db.session.rollback()
        flash(str(exc), "warning")
        return _back(session_id, "plan")
    note = (f"기획 수정 (제작 중) · 다시 만들 장면 {len(summary['regen'])}개"
            + (f" · 한국어 → 영어 ({tr_source})" if tr_source else ""))
    approval.add_version(session_id, round_obj.id, old_text, new_text, note)
    db.session.commit()
    if summary["regen"]:
        flash(f"고친 장면 {', '.join(summary['regen'])}만 이미지부터 다시 만듭니다. 나머지 {summary['kept']}개 장면은 그대로 씁니다.", "info")
        return _back(session_id, "produce")
    if summary["rebuild"]:
        flash("장면은 그대로 두고 내레이션·장면 구성만 반영해 올릴 영상을 다시 만드는 중입니다.", "info")
        return _back(session_id, "confirm")
    flash("기획을 저장했습니다. 다시 만들 장면은 없습니다.", "success")
    return _back(session_id, "plan")


NARRATION_EDITABLE = ("images_ready", "clips_ready", "video_ready", "generation_failed")


def _narration_rows(round_obj):
    """확인 탭 내레이션 칸: 장면마다 (id, 문장, 합성 결과). 문장이 없는 장면도 칸을 준다."""
    storyboard = round_obj.storyboard_data
    if not storyboard:
        return []
    report = orchestrator._narration_status(round_obj).get("shots") or {}
    return [{"id": s["id"], "text": (s.get("narration") or "").strip(), "fit": report.get(s["id"], {})}
            for s in storyboard["shots"]]


@bp.route("/session/<int:session_id>/narration", methods=["POST"])
def save_narration(session_id):
    """내레이션 문장 고치기 (제작 뒤에도). 문장은 장면 서명에 들어가지 않아 영상은 그대로, 음성만 다시 합성한다."""
    from ..pipeline import storyboard_model as sbm
    from ..pipeline.prompt_builder import parse_sections, render_sections

    session = _get_session(session_id)
    round_obj = session.current_round
    if not _require_status(round_obj, *NARRATION_EDITABLE):
        return _back(session_id, "confirm")
    storyboard = round_obj.storyboard_data
    lines = []
    for i, shot in enumerate(storyboard["shots"], start=1):
        text = " ".join(request.form.get(f"narr_{shot['id']}", shot.get("narration", "")).split())[:200]
        shot["narration"] = text
        if text:
            lines.append(f"{i}. {text}")
    sections = parse_sections(round_obj.prompt_text)
    if lines:
        sections["내레이션"] = "\n".join(lines)
    else:
        sections.pop("내레이션", None)
    new_text = render_sections(sections)
    if new_text != round_obj.prompt_text:
        approval.add_version(session_id, round_obj.id, round_obj.prompt_text, new_text, "내레이션 수정")
        round_obj.prompt_text = new_text
    round_obj.storyboard_json = sbm.dumps(storyboard)
    db.session.commit()
    if request.form.get("rebuild") and round_obj.status == "video_ready":
        try:
            orchestrator.rebuild_upload_video(round_obj, current_app.config)
            flash("내레이션을 고쳤습니다. 장면은 그대로 두고 음성만 다시 합성해 올릴 영상을 만드는 중입니다.", "info")
            return _back(session_id)
        except PipelineError as exc:
            flash(str(exc), "warning")
    else:
        flash("내레이션을 저장했습니다. 올릴 영상을 만들 때 이 문장으로 읽습니다.", "success")
    return _back(session_id, "confirm")


@bp.route("/round/<int:round_id>/captions.srt")
def round_captions(round_id):
    """이 버전의 YouTube 자막 파일 — 직접 올리거나 확인할 때 받는다."""
    round_obj = _get_round(round_id)
    path = orchestrator.captions_file(round_obj)
    if path is None:
        abort(404)
    name = f"{round_obj.session.title or 'video'}_{round_obj.round_number}차_자막.srt"
    return send_file(path, mimetype="application/x-subrip", as_attachment=True, download_name=name)


@bp.route("/round/<int:round_id>/music")
def round_music(round_id):
    """확인 탭에서 배경음악만 따로 들어 보기."""
    from ..pipeline import bgm

    settings = bgm.load(_get_round(round_id))
    if not settings["file"] or not os.path.exists(settings["file"]):
        abort(404)
    return send_file(settings["file"], conditional=True)


def _preview_frame(session, round_obj, storyboard):
    """로고 미리보기 바탕: 영상 크기와 첫·마지막 장면의 이미지. 이미지가 없으면 빈 화면."""
    from ..pipeline import storyboard_model as sbm

    if storyboard:
        width, height = int(storyboard["defaults"]["width"]), int(storyboard["defaults"]["height"])
    else:
        width, height = sbm.dimensions(session.aspect_ratio, current_app.config)
    images = {}
    for r in round_obj.shot_renders:
        if not r.selected:
            continue
        src = r
        if not (src.image_path and os.path.exists(src.image_path)) and r.reused_from_id:  # 이전 버전에서 그대로 쓴 장면
            src = db.session.get(ShotRender, r.reused_from_id) or r
        if src.image_path and os.path.exists(src.image_path):
            images[r.shot_id] = url_for("session.shot_image", render_id=src.id)
    order = [s["id"] for s in storyboard["shots"]] if storyboard else sorted(images)
    shown = [images[i] for i in order if i in images]
    return {"w": width, "h": height, "first": shown[0] if shown else "", "last": shown[-1] if shown else ""}


def _decorate_view(session, round_obj):
    """상세 화면의 '올릴 영상 꾸미기' 칸: 로고·자막 설정과 미리보기용 값."""
    from ..pipeline import bgm, edit_plan, overlay
    from ..pipeline.prompt_builder import parse_sections

    logo = overlay.load_logo(session)
    music = bgm.load(round_obj, current_app.config)
    sections = parse_sections(round_obj.prompt_text or "")
    has_file = overlay.logo_path(current_app.instance_path, session.id, dict(logo, enabled=True)) is not None
    plan = edit_plan.round_plan(round_obj)
    storyboard = round_obj.storyboard_data
    has_text = bool(storyboard and any((s.get("narration") or s.get("subtitle") or "").strip()
                                       for s in storyboard["shots"])) or "[내레이션]" in (round_obj.prompt_text or "")
    return {"logo": logo, "has_logo_file": has_file, "frame": _preview_frame(session, round_obj, storyboard),
            "logo_url": url_for("session.logo_image", session_id=session.id) if has_file else "",
            "plan": plan, "has_text": has_text, "editable": round_obj.status in orchestrator.BEFORE_UPLOAD,
            "can_rebuild": round_obj.status == "video_ready" and not orchestrator.upload_in_progress(round_obj),
            "positions": overlay.LOGO_POSITIONS, "shows": overlay.LOGO_SHOW,
            "sizes": edit_plan.SUBTITLE_SIZES, "sub_positions": edit_plan.SUBTITLE_POSITIONS,
            "music": music, "music_ready": bool(music["file"] and os.path.exists(music["file"])),
            "music_placeholder": bgm.template_caption(sections.get("영상 종류", ""), sections.get("영상 설명", "")),
            "summary": [line for line in [overlay.describe_logo(logo)] if line]
                       + [line for line in edit_plan.describe(plan, storyboard) if line.startswith("자막")],
            "sound": _sound_view(session, round_obj, plan, storyboard, music),
            "timeline": _timeline_view(round_obj, logo, has_file),
            "captions_url": (url_for("session.round_captions", round_id=round_obj.id)
                             if orchestrator.captions_file(round_obj) else "")}


def _timeline_view(round_obj, logo, has_logo_file):
    """③ 타임라인 편집기: 마지막으로 만든 올릴 영상의 시간표 (장면·자막·로고). 아직 없으면 None."""
    from ..pipeline import overlay

    try:
        data = json.loads(round_obj.timeline_json) if round_obj.timeline_json else {}
    except ValueError:
        data = {}
    if not data.get("total"):
        return None
    total = float(data["total"])
    # 로고는 지금 저장된 설정으로 다시 계산한다 (영상을 다시 만들기 전에 설정을 바꿨을 수 있다)
    data["logo"] = (list(overlay.logo_range(logo, total, float(data.get("last_start", total))))
                    if has_logo_file and logo["enabled"] else None)
    return data


def _sound_view(session, round_obj, plan, storyboard, music):
    """'배경음악·음성' 탭: 목소리(프로젝트 공통) + 내레이션 크기·빠르기(이 버전) + 배경음악."""
    from ..pipeline import bgm, edit_plan, narration

    cfg = current_app.config
    settings = voice.load(session)
    has_narration = bool(storyboard and any((s.get("narration") or "").strip() for s in storyboard["shots"]))         or "[내레이션]" in (round_obj.prompt_text or "")
    adapter = narration.adapter_name(cfg)
    summary = [bgm.describe(music)] if music["enabled"] else ["배경음악 없음"]
    if has_narration:
        summary.append(voice.describe(cfg, settings))
        summary += [line for line in edit_plan.describe(plan, storyboard) if line.startswith("내레이션")]
    return {"voice": settings, "speakers": voice.SPEAKERS, "tones": voice.TONES,
            "speaker": voice.effective_speaker(cfg, settings), "adapter": adapter,
            "can_choose": adapter != "none", "has_narration": has_narration,
            "engine": voice.engine_of(cfg, settings),
            "imported": round_obj.adapter_used == "import" and not has_narration,  # 소리 있는 영상 (자막만)
            "preview_url": url_for("session.voice_preview", session_id=session.id),
            "summary": summary}


def _youtube_view(round_obj):
    """상세 화면의 YouTube 업로드 칸: 계정 연결 상태·진행 중 여부·기본 제목/설명."""
    from ..pipeline import youtube_upload

    cfg = current_app.config
    view = youtube_upload.status(cfg, current_app.instance_path)
    view.update(
        uploading=orchestrator.upload_in_progress(round_obj),
        message=orchestrator.upload_message(round_obj),
        failed=round_obj.video_job_status == "failed" and bool(orchestrator.upload_message(round_obj)),
        privacy=cfg.get("YOUTUBE_PRIVACY", "private"), privacy_options=youtube_upload.PRIVACY,
        title=orchestrator.youtube_title(round_obj), description=orchestrator.youtube_description(round_obj),
        video_id=round_obj.youtube_video_id or "")
    return view


@bp.route("/session/<int:session_id>/api/translations")
def api_translations(session_id):
    """이 버전의 영어 장면 묘사 번역 상태 — 화면이 번역을 기다렸다가 채워 넣는다."""
    session = _get_session(session_id)
    round_number = request.args.get("round", type=int)
    round_obj = next((r for r in session.rounds if r.round_number == round_number), session.current_round)
    english = [sc["prompt"] for sc in _scene_cards(round_obj)] + extract_shots(round_obj.prompt_text)
    done = translate.lookup(english)
    pending = translate.is_pending(english)
    if not pending and len(done) < len({translate.key(t) for t in english if t.strip()}):
        pending = translate.request(current_app._get_current_object(), english)
    return jsonify(done=done, pending=pending)


def _scene_cards(round_obj):
    """장면별 클립 + 그 장면에 달린 의견. 스토리보드나 클립이 없으면 빈 목록."""
    from ..pipeline import storyboard_model as sbm
    storyboard = round_obj.storyboard_data
    if not storyboard or not storyboard.get("shots"):
        return []
    renders, images = {}, {}
    for r in round_obj.shot_renders:  # 선택된 버전 우선, 없으면 가장 최근 것
        if r.status in ("done", "reused") and r.path and os.path.exists(r.path):
            if r.shot_id not in renders or r.selected or not renders[r.shot_id].selected:
                renders[r.shot_id] = r
        if (r.status not in ("discarded", "failed") and r.image_path
                and os.path.exists(r.image_path)):
            if r.shot_id not in images or r.selected or not images[r.shot_id].selected:
                images[r.shot_id] = r
    image_step = round_obj.status == "images_ready"  # 첫 프레임 확인 중에는 이미지를 보여 준다
    cards = []
    for index, (shot_id, start, end) in enumerate(sbm.shot_ranges(storyboard), start=1):
        shot = sbm.get_shot(storyboard, shot_id)
        comments = [f for f in round_obj.feedbacks
                    if f.timestamp_sec is not None and start <= f.timestamp_sec < end]
        cards.append({"id": shot_id, "index": index, "start": start, "end": end,
                      "prompt": shot["prompt"], "narration": shot.get("narration", ""),
                      "subtitle": shot.get("subtitle", ""),  # 불러온 영상의 자막 (표시용)
                      "render": None if image_step else renders.get(shot_id),
                      "image": images.get(shot_id), "comments": comments,
                      "notes": shot.get("notes") or [],
                      "prompt_fixed": shot.get("prompt_fixed") or "",  # 의견대로 고쳐 써서 실제로 모델에 넣은 묘사
                      "image_prompt_fixed": shot.get("image_prompt_fixed") or "",  # 첫 프레임(동작 시작 전) 묘사
                      "versions": [r for r in round_obj.shot_renders
                                   if r.shot_id == shot_id and r.status in ("done", "reused")]})
    return cards


@bp.route("/session/<int:session_id>/shot-feedback", methods=["POST"])
def shot_feedback(session_id):
    """장면 1개에 대한 의견 — 그 장면의 시작 시각을 타임스탬프로 달아 기존 장면 매핑을 그대로 쓴다."""
    from ..pipeline import storyboard_model as sbm
    round_obj = _get_session(session_id).current_round
    if not _require_status(round_obj, "uploaded", "feedback_collected"):
        return _back(session_id)
    storyboard = round_obj.storyboard_data
    shot_id = request.form.get("shot_id", "")
    text = request.form.get("text", "").strip()
    start = next((s for sid, s, _e in sbm.shot_ranges(storyboard) if sid == shot_id), None) \
        if storyboard else None
    if start is None or not text:
        flash("장면과 의견 내용을 확인해 주세요.", "warning")
        return _back(session_id)
    orchestrator.add_comments(round_obj, [{"author": "manual", "text": text, "source": "scene",
                                           "timestamp_sec": start}])
    flash(f"장면 {shot_id} 의견을 추가했습니다.", "success")
    return _back(session_id)


# ── 프롬프트 수동 편집 ───────────────────────────────────────────────────
@bp.route("/session/<int:session_id>/prompt", methods=["POST"])
def edit_prompt(session_id):
    round_obj = _get_session(session_id).current_round
    if not _require_status(round_obj, "prompt_built"):
        return _back(session_id)
    new_text = request.form.get("prompt_text", "").replace("\r\n", "\n").strip() + "\n"
    note = "수동 편집"
    try:  # [샷 목록]·[영상 스타일]에 한국어로 적은 줄은 영어로
        new_text, tr_source, originals = ko_en.translate_prompt_doc(new_text, current_app.config)
    except ko_en.KoEnError as exc:
        flash(f"{exc} 저장하지 않았습니다.", "warning")
        return _back(session_id)
    if tr_source:
        note += f" · 한국어 → 영어 ({tr_source})"
        ko_en.remember_originals(originals)
    if new_text != round_obj.prompt_text:
        approval.add_version(session_id, round_obj.id, round_obj.prompt_text, new_text, note)
        round_obj.prompt_text = new_text
        db.session.commit()
        flash("프롬프트를 저장했습니다.", "success")
    return _back(session_id)


# ── 영상 생성 ────────────────────────────────────────────────────────────
@bp.route("/session/<int:session_id>/generate", methods=["POST"])
def generate(session_id):
    round_obj = _get_session(session_id).current_round
    if not _require_status(round_obj, "prompt_built", "generation_failed"):
        return _back(session_id)
    if round_obj.status == "generation_failed":
        orchestrator.retry_failed(round_obj, current_app.config)
    else:
        orchestrator.start_generation(round_obj, current_app.config)
    return _back(session_id)


@bp.route("/session/<int:session_id>/api/status")
def api_status(session_id):
    round_obj = _get_session(session_id).current_round
    return jsonify(status=round_obj.status, job=round_obj.video_job_status,
                   message=round_obj.generation_detail or round_obj.video_job_message)


@bp.route("/video/<int:round_id>")
def video(round_id):
    round_obj = db.session.get(VideoRound, round_id)
    if round_obj is None or not round_obj.video_path or not os.path.exists(round_obj.video_path):
        abort(404)
    return send_file(round_obj.video_path, mimetype="video/mp4", conditional=True)


# ── 파이프라인 상태 · 제어 API ───────────────────────────────────────────
def _get_round(round_id):
    round_obj = db.session.get(VideoRound, round_id)
    if round_obj is None:
        abort(404)
    return round_obj


def _error(message, status, **extra):
    return jsonify(error=message, **extra), status


@bp.route("/api/rounds/<int:round_id>/status")
def round_status(round_id):
    round_obj = _get_round(round_id)
    return jsonify(orchestrator.status_payload(current_app._get_current_object(), round_obj, url_for))


@bp.route("/shot/<int:render_id>/video")
def shot_video(render_id):
    render = db.session.get(ShotRender, render_id)
    if render is None or not render.path or not os.path.exists(render.path):
        abort(404)
    return send_file(render.path, mimetype="video/mp4", conditional=True)


@bp.route("/shot/<int:render_id>/image")
def shot_image(render_id):
    render = db.session.get(ShotRender, render_id)
    if render is None or not render.image_path or not os.path.exists(render.image_path):
        abort(404)
    return send_file(render.image_path, mimetype="image/png", conditional=True)


# ── 단계별 확인 (이미지 → 컷 → 러프컷) ──
def _staged_action(round_id, fn, *args):
    round_obj = _get_round(round_id)
    try:
        fn(round_obj, *args, current_app.config)
    except PipelineError as exc:
        return _error(str(exc), 409)
    return jsonify(state=db.session.get(VideoRound, round_id).status)


@bp.route("/api/rounds/<int:round_id>/images/approve", methods=["POST"])
def images_approve(round_id):
    """이미지 확인 완료 — 지금 선택된 이미지로 컷 영상을 만든다."""
    return _staged_action(round_id, orchestrator.approve_images)


def _vague_check(body):
    """모호한 의견('그리퍼 모양을 바꿔')이면 422로 고쳐 쓸 방향을 묻는다. 화면에서 '그래도 진행'하면 force=true로 다시 보낸다."""
    from ..pipeline.feedback_target import vague_reason

    reason = "" if body.get("force") else vague_reason(body.get("note", ""))
    return (jsonify(error=reason, vague=reason), 422) if reason else None


@bp.route("/api/rounds/<int:round_id>/images/regenerate", methods=["POST"])
def images_regenerate(round_id):
    body = request.get_json(silent=True) or {}
    if not body.get("shot_id"):
        return _error("shot_id가 필요합니다.", 400)
    return _vague_check(body) or _staged_action(round_id, orchestrator.regenerate_image, body["shot_id"],
                                                body.get("note", ""))


@bp.route("/api/rounds/<int:round_id>/clips/regenerate", methods=["POST"])
def clips_regenerate(round_id):
    body = request.get_json(silent=True) or {}
    if not body.get("shot_id"):
        return _error("shot_id가 필요합니다.", 400)
    return _vague_check(body) or _staged_action(round_id, orchestrator.regenerate_clip, body["shot_id"],
                                                body.get("note", ""))


@bp.route("/api/rounds/<int:round_id>/notes/delete", methods=["POST"])
def shot_note_delete(round_id):
    """장면에 쌓인 의견 하나 지우기 — body {"shot_id", "index"}. 다음에 만들 때부터 빠진다."""
    body = request.get_json(silent=True) or {}
    try:
        notes = orchestrator.remove_shot_note(_get_round(round_id), body.get("shot_id", ""), body.get("index", -1),
                                              current_app.config)
    except (PipelineError, ValueError, TypeError) as exc:
        return _error(str(exc), 409)
    return jsonify(notes=notes)


@bp.route("/api/rounds/<int:round_id>/replan", methods=["POST"])
def round_replan(round_id):
    """이 버전의 이미지·컷을 지우고 프롬프트 단계로 되돌린다 (다시 계획하기)."""
    round_obj = _get_round(round_id)
    try:
        orchestrator.restart_planning(current_app._get_current_object(), round_obj)
    except PipelineError as exc:
        return _error(str(exc), 409)
    return jsonify(state=round_obj.status)


@bp.route("/api/rounds/<int:round_id>/rough-cut", methods=["POST"])
def rough_cut_start(round_id):
    """컷 확인 완료 — 선택된 컷들로 러프컷을 만든다."""
    return _staged_action(round_id, orchestrator.start_rough_cut)


@bp.route("/api/rounds/<int:round_id>/retry", methods=["POST"])
def round_retry(round_id):
    round_obj = _get_round(round_id)
    if round_obj.status != "generation_failed":
        return _error(f"현재 단계({round_obj.status})에서는 재시도할 수 없습니다.", 409)
    orchestrator.retry_failed(round_obj, current_app.config)
    return jsonify(state=db.session.get(VideoRound, round_id).status)


@bp.route("/api/rounds/<int:round_id>/cancel", methods=["POST"])
def round_cancel(round_id):
    """생성 중지 — 대기 작업은 취소하고 실행 중인 장면은 멈춘다. 이후 '재시도'로 이어서 만들 수 있다."""
    round_obj = _get_round(round_id)
    try:
        outcome = orchestrator.cancel_generation(current_app.config, round_obj)
    except PipelineError as exc:
        return _error(str(exc), 409)
    return jsonify(result=outcome, state=db.session.get(VideoRound, round_id).status)


@bp.route("/api/rounds/<int:round_id>/advance", methods=["POST"])
def round_advance(round_id):
    """수동 진행 — 시뮬레이션 모드에서 업로드·수집·분류·클러스터링을 한 단계씩 넘긴다."""
    round_obj = _get_round(round_id)
    cfg = current_app.config
    state = round_obj.status
    try:
        if state == "video_ready":
            body = request.get_json(silent=True) or {}
            orchestrator.approve_and_upload(round_obj, cfg, body.get("video_id", ""))
        elif state == "uploaded":
            orchestrator.collect(round_obj, cfg)
        elif state == "feedback_collected":
            orchestrator.classify(round_obj, _classifier())
        elif state == "feedback_classified":
            orchestrator.start_clustering(round_obj)
        else:
            return _error(f"현재 단계({state})는 수동으로 넘길 수 없습니다.", 409)
    except (PipelineError, ValueError, InvalidTransition) as exc:
        return _error(str(exc), 409)
    return jsonify(state=db.session.get(VideoRound, round_id).status)


@bp.route("/api/rounds/<int:round_id>/approve-video", methods=["POST"])
def round_approve_video(round_id):
    round_obj = _get_round(round_id)
    body = request.get_json(silent=True) or {}
    try:
        orchestrator.approve_and_upload(round_obj, current_app.config, body.get("video_id", ""))
    except (PipelineError, ValueError, InvalidTransition) as exc:
        return _error(str(exc), 409)
    return jsonify(state=round_obj.status)


@bp.route("/api/rounds/<int:round_id>/escalate", methods=["POST"])
def round_escalate(round_id):
    round_obj = _get_round(round_id)
    body = request.get_json(silent=True) or {}
    try:
        cost, _job = orchestrator.start_escalation(
            round_obj, body.get("shot_ids") or [], body.get("flags") or {},
            body.get("confirm_cost_usd"), current_app.config,
            http=current_app.config.get("_COMMERCIAL_HTTP"))
    except quality_gate.EscalationError as exc:
        return _error(str(exc), exc.http_status, code=exc.code, **exc.extra)
    return jsonify(state=db.session.get(VideoRound, round_id).status, cost_usd=cost)


@bp.route("/api/shots/<int:render_id>/select", methods=["POST"])
def shot_select(render_id):
    render = db.session.get(ShotRender, render_id)
    if render is None:
        abort(404)
    try:
        orchestrator.select_render(current_app._get_current_object(), render.round, render)
    except PipelineError as exc:
        return _error(str(exc), 409)
    return jsonify(ok=True, render_id=render_id)


@bp.route("/api/shots/<int:render_id>/end-frame", methods=["POST"])
def shot_end_frame(render_id):
    """컷 확인 '이 순간을 끝으로 다시 만들기' — body {"at": 초}."""
    render = db.session.get(ShotRender, render_id)
    if render is None:
        abort(404)
    try:
        at = float((request.get_json(silent=True) or {}).get("at"))
    except (TypeError, ValueError):
        return _error("at(초)이 필요합니다.", 400)
    try:
        orchestrator.regenerate_clip_to_frame(render.round, render, at, current_app.config)
    except PipelineError as exc:
        return _error(str(exc), 409)
    return jsonify(state=render.round.status)


@bp.route("/api/shots/<int:render_id>/delete", methods=["POST"])
def shot_delete(render_id):
    """컷 버전 지우기 (공개 전, 장면마다 한 버전은 남긴다)."""
    render = db.session.get(ShotRender, render_id)
    if render is None:
        abort(404)
    try:
        orchestrator.delete_render(current_app._get_current_object(), render.round, render)
    except PipelineError as exc:
        return _error(str(exc), 409)
    return jsonify(ok=True)


# ── 업로드 / 댓글 수집 ───────────────────────────────────────────────────
@bp.route("/session/<int:session_id>/upload", methods=["POST"])
def upload(session_id):
    round_obj = _get_session(session_id).current_round
    if not _require_status(round_obj, "video_ready"):
        return _back(session_id)
    try:
        orchestrator.upload(round_obj, current_app.config, request.form.get("video_id", ""))
    except (ValueError, PipelineError) as exc:
        flash(str(exc), "warning")
    return _back(session_id)


@bp.route("/session/<int:session_id>/unpublish", methods=["POST"])
def unpublish(session_id):
    """④ 공개 → ③ 확인으로 되돌리기 (댓글을 모으기 전에만). 장면은 그대로, 올릴 영상만 다시 만들어 다시 올린다."""
    round_obj = _get_session(session_id).current_round
    try:
        orchestrator.unpublish(round_obj)
        flash("③ 확인 단계로 되돌렸습니다. 올릴 영상을 고쳐 다시 만든 뒤 다시 올리세요.", "info")
    except PipelineError as exc:
        flash(str(exc), "warning")
        return _back(session_id)
    return _back(session_id, "confirm")


_LOGO_TYPES = {".png", ".jpg", ".jpeg", ".webp", ".ai", ".pdf"}  # .ai·.pdf는 투명 PNG로 바꿔 쓴다
_LOGO_MAX_BYTES = 5 * 1024 * 1024
_LOGO_VECTOR_MAX_BYTES = 40 * 1024 * 1024  # Illustrator 파일은 미리보기·편집 정보가 들어 있어 크다


@bp.route("/session/<int:session_id>/decorate", methods=["POST"])
def decorate(session_id):
    """올릴 영상 꾸미기: 로고(프로젝트 설정)와 이 버전의 자막 설정을 저장한다. rebuild면 올릴 영상만 다시 합성."""
    from ..pipeline import overlay

    session = _get_session(session_id)
    round_obj = session.current_round
    tab = "confirm"  # 꾸미기·배경음악·음성은 모두 ③ 확인 탭에 있다
    logo = overlay.load_logo(session)
    upload = request.files.get("logo_file")
    if upload and upload.filename:
        ext = os.path.splitext(upload.filename)[1].lower()
        data = upload.read()
        if ext not in _LOGO_TYPES:
            flash("로고는 PNG·JPG·WEBP 이미지나 Illustrator(.ai)·PDF 파일로 올려 주세요.", "warning")
            return _back(session_id, tab)
        vector = ext in overlay.VECTOR_TYPES
        if len(data) > (_LOGO_VECTOR_MAX_BYTES if vector else _LOGO_MAX_BYTES):
            flash(f"로고 파일이 너무 큽니다 ({40 if vector else 5}MB 이하).", "warning")
            return _back(session_id, tab)
        if vector:  # 벡터 로고 → 투명 배경 PNG (원본도 함께 둔다)
            try:
                png = overlay.vector_to_png(data)
            except overlay.LogoError as exc:
                flash(str(exc), "warning")
                return _back(session_id, tab)
        folder = overlay.asset_dir(current_app.instance_path, session_id)
        folder.mkdir(parents=True, exist_ok=True)
        for old in list(folder.glob("logo.*")) + list(folder.glob("logo_source.*")):
            old.unlink()
        if vector:
            (folder / f"logo_source{ext}").write_bytes(data)
            data, ext = png, ".png"
            flash("벡터 로고를 투명 배경 PNG로 바꿔 넣었습니다.", "success")
        (folder / f"logo{ext}").write_bytes(data)
        logo["file"], logo["enabled"] = f"logo{ext}", True
    elif request.form.get("logo_settings"):  # 체크 해제는 폼에 값이 오지 않는다
        logo["enabled"] = request.form.get("logo_enabled") == "on"

    def pct(name, default):
        try:
            return float(request.form.get(name, default * 100)) / 100.0
        except (TypeError, ValueError):
            return default

    if request.form.get("logo_settings"):
        logo.update(position=request.form.get("logo_position", logo["position"]),
                    show=request.form.get("logo_show", logo["show"]),
                    size=pct("logo_size", logo["size"]), opacity=pct("logo_opacity", logo["opacity"]),
                    first_sec=request.form.get("logo_first_sec", logo["first_sec"]),
                    last_sec=request.form.get("logo_last_sec", logo["last_sec"]),
                    x=request.form.get("logo_x", logo["x"]), y=request.form.get("logo_y", logo["y"]))
    if request.form.get("timeline_settings") and logo["file"] and request.form.get("logo_start"):
        # 타임라인에서 끈 로고 구간 → '정한 구간' 모드. 끝까지 끌었으면 0(= 끝까지)으로 둔다
        try:
            start, end = float(request.form["logo_start"]), float(request.form["logo_end"])
            total = float(json.loads(round_obj.timeline_json or "{}").get("total") or 0)
        except (TypeError, ValueError, KeyError):
            start = end = None
        if start is not None and end - start >= 0.1:
            logo.update(show="range", start_sec=round(start, 2),
                        end_sec=0.0 if total and end >= total - 0.05 else round(end, 2))
    session.brand_json = json.dumps(overlay.normalize_logo(logo), ensure_ascii=False)
    db.session.commit()

    if request.form.get("music_settings"):
        from ..pipeline import bgm

        if round_obj.status not in orchestrator.BEFORE_UPLOAD:
            flash("공개한 버전의 배경음악은 바꿀 수 없습니다. 다음 버전은 의견 검토 화면에서 정하세요.", "warning")
            return _back(session_id, tab)
        music = bgm.load(round_obj)
        music["enabled"] = request.form.get("music_enabled") == "on"
        music["caption"] = request.form.get("music_caption", music["caption"])
        music["volume"] = pct("music_volume", music["volume"])
        if request.form.get("music_reroll") == "on":  # 같은 설명으로 다른 곡
            music.update(seed=0, file="", key="", seconds=0.0)
        if request.form.get("music_suggest"):
            music["caption"], source = bgm.suggest_caption(round_obj.prompt_text, current_app.config)
            bgm.save(round_obj, music)
            db.session.commit()
            flash("곡 설명을 " + ("ComfyUI Gemma가 제안했습니다." if source == "gemma" else
                                f"{source}로 제안했습니다." if source != "template" else "영상 종류에 맞는 기본 설명으로 채웠습니다."),
                  "info")
            return _back(session_id, tab)
        bgm.save(round_obj, music)
        db.session.commit()

    if request.form.get("voice_settings"):  # 목소리 (프로젝트 공통)
        voice.save(session, {"enabled": request.form.get("voice_enabled") == "on",
                             "speaker": request.form.get("voice_speaker", ""),
                             "tone": request.form.get("voice_tone", ""),
                             "instruct": request.form.get("voice_instruct", "")})
        db.session.commit()

    try:
        if request.form.get("scene_sound_settings"):  # 영상 모델이 만든 장면 소리 쓰기/빼기
            orchestrator.set_round_edit(round_obj, current_app.config,
                                        scene_sound=request.form.get("scene_sound") == "on")
        if request.form.get("narration_settings"):  # 내레이션 크기·빠르기 (이 버전 편집 설정)
            orchestrator.set_round_edit(round_obj, current_app.config,
                                        narration_gain=pct("narration_gain", 1.0),
                                        narration_tempo=pct("narration_tempo", 1.0))
        if request.form.get("timeline_settings"):  # 타임라인에서 정한 장면별 자막 구간 (비우면 말하는 구간에 자동으로)
            try:
                times = json.loads(request.form.get("subtitle_times") or "{}")
            except ValueError:
                times = {}
            orchestrator.set_round_edit(round_obj, current_app.config,
                                        subtitle_times=times if isinstance(times, dict) else {})
        if request.form.get("subtitle_settings"):
            orchestrator.set_round_edit(
                round_obj, current_app.config, subtitles=request.form.get("subtitles") == "on",
                subtitle_size=request.form.get("subtitle_size", "medium"),
                subtitle_position=request.form.get("subtitle_position", "bottom"))
        if request.form.get("rebuild"):
            orchestrator.rebuild_upload_video(round_obj, current_app.config)
            flash("올릴 영상을 다시 만드는 중입니다. 장면은 그대로 쓰고 로고·자막·소리만 새로 얹습니다.", "info")
        else:
            flash("꾸미기 설정을 저장했습니다.", "success")
    except PipelineError as exc:
        flash(str(exc), "warning")
    # 다시 만들기를 눌렀으면 진행 상황이 보이는 탭으로
    return _back(session_id, None if request.form.get("rebuild") else tab)


def _preview_text(round_obj):
    """미리 듣기 문장: 이 버전의 첫 내레이션, 없으면 안내 문장."""
    from ..pipeline.prompt_builder import extract_narration

    storyboard = round_obj.storyboard_data
    lines = [s.get("narration", "").strip() for s in storyboard["shots"]] if storyboard else []
    lines = [t for t in lines if t] or [t for _n, t in sorted(extract_narration(round_obj.prompt_text or "").items()) if t]
    return (lines[0] if lines else voice.SAMPLE_TEXT)[:120]


@bp.route("/session/<int:session_id>/voice/preview", methods=["POST"])
def voice_preview(session_id):
    """고른 목소리·말투로 한 문장만 합성해 들려준다 (저장하지 않음). 같은 설정·문장이면 파일을 다시 쓴다."""
    from ..pipeline import narration
    from ..pipeline.overlay import asset_dir

    session = _get_session(session_id)
    data = request.get_json(silent=True) or request.form
    settings = voice.normalize({"enabled": True, "speaker": data.get("speaker", ""),
                                "tone": data.get("tone", ""), "instruct": data.get("instruct", "")})
    cfg = voice.apply(current_app.config, settings)
    ok, reason = narration.is_available(cfg)
    if not ok:
        return _error(f"음성을 쓸 수 없습니다: {reason}", 400)
    text = _preview_text(session.current_round)
    key = voice.preview_key(current_app.config, settings, text)
    path = asset_dir(current_app.instance_path, session_id) / "voice_preview" / f"{key}.wav"
    if not path.exists():
        if _is_busy(session):
            return _error("영상을 만드는 중이라 GPU가 바쁩니다. 작업이 끝난 뒤 들어 보세요.", 409)
        try:
            narration.synthesize(text, path, cfg)
        except narration.NarrationError as exc:
            return _error(str(exc), 500)
    return jsonify(url=url_for("session.voice_preview_file", session_id=session_id, key=key), text=text)


@bp.route("/session/<int:session_id>/voice/preview/<key>")
def voice_preview_file(session_id, key):
    from ..pipeline.overlay import asset_dir

    if not re.fullmatch(r"[0-9a-f]{12}", key):
        abort(404)
    path = asset_dir(current_app.instance_path, session_id) / "voice_preview" / f"{key}.wav"
    if not path.exists():
        abort(404)
    return send_file(path, mimetype="audio/wav")


@bp.route("/session/<int:session_id>/logo")
def logo_image(session_id):
    from ..pipeline import overlay

    session = _get_session(session_id)
    logo = overlay.load_logo(session)
    path = overlay.asset_dir(current_app.instance_path, session_id) / logo["file"] if logo["file"] else None
    if path is None or not path.exists():
        abort(404)
    return send_file(path)


@bp.route("/session/<int:session_id>/logo/preview", methods=["POST"])
def logo_vector_preview(session_id):
    """저장 전 미리보기: 고른 벡터 로고(.ai·.pdf)를 투명 PNG로 바꿔 돌려준다 (저장하지 않음).
    브라우저는 벡터 파일을 그리지 못해, 이것 없이는 저장 전에 위치를 끌어 정할 수 없었다."""
    import io

    from ..pipeline import overlay

    _get_session(session_id)
    upload = request.files.get("logo_file")
    if not upload or os.path.splitext(upload.filename or "")[1].lower() not in overlay.VECTOR_TYPES:
        return _error("벡터 로고(.ai·.pdf)만 보내 주세요.", 400)
    data = upload.read()
    if len(data) > _LOGO_VECTOR_MAX_BYTES:
        return _error("로고 파일이 너무 큽니다 (40MB 이하).", 400)
    try:
        png = overlay.vector_to_png(data, max_side=800)
    except overlay.LogoError as exc:
        return _error(str(exc), 400)
    return send_file(io.BytesIO(png), mimetype="image/png")


@bp.route("/session/<int:session_id>/youtube-upload", methods=["POST"])
def youtube_upload_start(session_id):
    """③ 확인 → 제작자가 누른 경우에만 YouTube에 올린다 (작업 큐에서 진행, 끝나면 ④)."""
    round_obj = _get_session(session_id).current_round
    if not _require_status(round_obj, "video_ready"):
        return _back(session_id)
    try:
        orchestrator.start_youtube_upload(
            current_app._get_current_object(), round_obj, title=request.form.get("title", ""),
            description=request.form.get("description", ""), privacy=request.form.get("privacy", ""))
    except PipelineError as exc:
        flash(str(exc), "warning")
    return _back(session_id)


@bp.route("/youtube/connect")
def youtube_connect():
    """Google 로그인으로 보낸다. 돌아올 곳(next)과 PKCE 값은 세션에 둔다."""
    from flask import session as web_session
    from ..pipeline import youtube_upload

    nxt = request.args.get("next", "")
    try:
        auth_url, state, verifier = youtube_upload.authorization(
            current_app.config, url_for("session.youtube_callback", _external=True))
    except youtube_upload.YouTubeError as exc:
        flash(str(exc), "warning")
        return redirect(nxt if nxt.startswith("/") else url_for("session.index"))
    web_session["yt_oauth"] = {"state": state, "verifier": verifier, "next": nxt if nxt.startswith("/") else ""}
    return redirect(auth_url)


@bp.route("/youtube/callback")
def youtube_callback():
    from flask import session as web_session
    from ..pipeline import youtube_upload

    saved = web_session.pop("yt_oauth", None) or {}
    back = saved.get("next") or url_for("session.index")
    if request.args.get("error"):
        flash(f"YouTube 계정 연결을 취소했습니다 ({request.args['error']}).", "warning")
        return redirect(back)
    if not saved or request.args.get("state") != saved.get("state"):
        flash("YouTube 계정 연결 요청이 맞지 않습니다. 다시 연결해 주세요.", "warning")
        return redirect(back)
    try:
        channel = youtube_upload.finish_authorization(
            current_app.config, current_app.instance_path, request.args.get("code", ""),
            url_for("session.youtube_callback", _external=True), saved["verifier"])
    except youtube_upload.YouTubeError as exc:
        flash(str(exc), "danger")
        return redirect(back)
    flash(f"YouTube 계정을 연결했습니다{f' — {channel} 채널' if channel else ''}.", "success")
    return redirect(back)


@bp.route("/youtube/disconnect", methods=["POST"])
def youtube_disconnect():
    from ..pipeline import youtube_upload

    youtube_upload.disconnect(current_app.config, current_app.instance_path)
    flash("YouTube 계정 연결을 끊었습니다.", "info")
    nxt = request.form.get("next", "")
    return redirect(nxt if nxt.startswith("/") else url_for("session.index"))


@bp.route("/session/<int:session_id>/collect", methods=["POST"])
def collect(session_id):
    round_obj = _get_session(session_id).current_round
    if not _require_status(round_obj, "uploaded", "feedback_collected"):
        return _back(session_id)
    try:
        count = orchestrator.collect(round_obj, current_app.config)
    except Exception as exc:
        flash(f"댓글 수집 실패: {exc}", "danger")
        return _back(session_id)
    flash(f"댓글 {count}개를 수집했습니다.", "success")
    return _back(session_id)


@bp.route("/session/<int:session_id>/comments", methods=["POST"])
def add_comments(session_id):
    """시청자 댓글 직접 입력 (한 줄에 하나) — YouTube 없이 참여를 받을 때 사용."""
    round_obj = _get_session(session_id).current_round
    if not _require_status(round_obj, "uploaded", "feedback_collected"):
        return _back(session_id)
    lines = [l.strip() for l in request.form.get("comments", "").splitlines() if l.strip()]
    orchestrator.add_comments(
        round_obj, [{"author": "manual", "text": l, "source": "manual"} for l in lines])
    flash(f"댓글 {len(lines)}개를 추가했습니다.", "success")
    return _back(session_id)


# ── 분류 / 클러스터링 ────────────────────────────────────────────────────
@bp.route("/session/<int:session_id>/filter", methods=["POST"])
def filter_feedback(session_id):
    round_obj = _get_session(session_id).current_round
    if not _require_status(round_obj, "feedback_collected", "feedback_classified"):
        return _back(session_id)
    try:
        constructive = orchestrator.classify(round_obj, _classifier())
    except PipelineError as exc:
        flash(str(exc), "warning")
        return _back(session_id)
    flash(f"{len(round_obj.feedbacks)}개 중 건설적 피드백 {constructive}개를 찾았습니다.", "success")
    return _back(session_id)


@bp.route("/session/<int:session_id>/feedback/<int:feedback_id>/toggle", methods=["POST"])
def toggle_feedback(session_id, feedback_id):
    """분류 결과 수동 정정 (needs_review 항목 검토용)."""
    round_obj = _get_session(session_id).current_round
    feedback = db.session.get(Feedback, feedback_id)
    if feedback is None or feedback.round_id != round_obj.id:
        abort(404)
    if _require_status(round_obj, "feedback_classified"):
        feedback.is_constructive = not feedback.is_constructive
        feedback.needs_review = False
        db.session.commit()
    return _back(session_id)


@bp.route("/session/<int:session_id>/cluster", methods=["POST"])
def cluster(session_id):
    round_obj = _get_session(session_id).current_round
    if not _require_status(round_obj, "feedback_classified", "pending_review"):
        return _back(session_id)
    try:
        orchestrator.start_clustering(round_obj)
    except PipelineError as exc:
        flash(str(exc), "warning")
        return _back(session_id)
    db.session.refresh(round_obj)
    if round_obj.status == "pending_review":  # 동기 실행(JOBS_SYNC)이면 바로 검토 화면으로
        return redirect(url_for("session.review", session_id=session_id))
    if round_obj.generation_error:
        flash(round_obj.generation_error, "danger")
    return _back(session_id)


# ── 검토 / 승인 ──────────────────────────────────────────────────────────
@bp.route("/session/<int:session_id>/review")
def review(session_id):
    from ..pipeline.feedback_target import REGEN_TARGETS, TARGETS

    from ..pipeline import edit_plan

    session = _get_session(session_id)
    round_obj = session.current_round
    storyboard = round_obj.storyboard_data
    plan, manual = edit_plan.next_plan(round_obj)
    shot_ids = [s["id"] for s in storyboard["shots"]] if storyboard else []
    return render_template("session/review.html", session=session, round=round_obj,
                           clusters=round_obj.clusters, targets=TARGETS, regen_targets=REGEN_TARGETS,
                           edit_changes=edit_plan.cluster_changes(round_obj), edit=plan, edit_manual=manual,
                           edit_lines=edit_plan.describe(plan, storyboard),
                           edit_current=edit_plan.describe(edit_plan.round_plan(round_obj), storyboard),
                           edit_order=", ".join(str(shot_ids.index(s) + 1)
                                                for s in edit_plan.ordered_ids(shot_ids, plan)),
                           edit_shots=storyboard["shots"] if storyboard else [],
                           transitions=edit_plan.TRANSITIONS, subtitle_sizes=edit_plan.SUBTITLE_SIZES,
                           subtitle_positions=edit_plan.SUBTITLE_POSITIONS, music_changes=edit_plan.MUSIC_CHANGES)


@bp.route("/session/<int:session_id>/edit-plan", methods=["POST"])
def save_edit_plan(session_id):
    """다음 버전 편집 설정을 직접 고친다 (reset이면 승인한 의견의 자동 제안으로 되돌린다)."""
    from ..pipeline import edit_plan

    round_obj = _get_session(session_id).current_round
    if _require_status(round_obj, "pending_review"):
        if request.form.get("reset"):
            round_obj.next_edit_json = ""
        else:
            storyboard = round_obj.storyboard_data
            shot_ids = [s["id"] for s in storyboard["shots"]] if storyboard else []
            round_obj.next_edit_json = edit_plan.dumps(edit_plan.from_form(request.form, shot_ids))
        db.session.commit()
    return redirect(url_for("session.review", session_id=session_id) + "#edit-plan")


@bp.route("/session/<int:session_id>/cluster/<int:cluster_id>/target", methods=["POST"])
def set_target(session_id, cluster_id):
    """의견의 반영 위치 변경 — 화면/움직임이면 지적 장면을 다시 만들고, 나머지는 장면을 그대로 둔다."""
    from ..pipeline.feedback_target import TARGETS

    round_obj = _get_session(session_id).current_round
    cluster_row = db.session.get(ClusterResult, cluster_id)
    if cluster_row is None or cluster_row.round_id != round_obj.id:
        abort(404)
    target = request.form.get("target", "")
    if target not in TARGETS:
        abort(400)
    if _require_status(round_obj, "pending_review"):
        cluster_row.apply_target = target
        db.session.commit()
    return redirect(url_for("session.review", session_id=session_id) + f"#cluster-{cluster_id}")


@bp.route("/session/<int:session_id>/api/clusters")
def api_clusters(session_id):
    # 현재 라운드 기준 — prompt_updated 이후에는 새 라운드라 비어 있는 것이 정상
    round_obj = _get_session(session_id).current_round
    return jsonify(round=round_obj.round_number, status=round_obj.status,
                   clusters=[c.to_dict() for c in round_obj.clusters])


@bp.route("/session/<int:session_id>/cluster/<int:cluster_id>/decision", methods=["POST"])
def decide(session_id, cluster_id):
    round_obj = _get_session(session_id).current_round
    cluster_row = db.session.get(ClusterResult, cluster_id)
    if cluster_row is None or cluster_row.round_id != round_obj.id:
        abort(404)
    decision = _DECISIONS.get(request.form.get("decision", ""))
    if decision is None:
        abort(400)
    if _require_status(round_obj, "pending_review"):
        cluster_row.status = decision
        db.session.commit()
    return redirect(url_for("session.review", session_id=session_id) + f"#cluster-{cluster_id}")


@bp.route("/session/<int:session_id>/api/preview")
def api_preview(session_id):
    round_obj = _get_session(session_id).current_round
    return jsonify(diff=approval.preview_prompt_change(round_obj, current_app.config))


@bp.route("/session/<int:session_id>/apply", methods=["POST"])
def apply(session_id):
    round_obj = _get_session(session_id).current_round
    if not _require_status(round_obj, "pending_review"):
        return _back(session_id)
    try:
        next_round, source = approval.apply_approved_clusters(
            round_obj, current_app.config, classifier=_classifier())
    except ValueError as exc:
        flash(str(exc), "warning")
        return redirect(url_for("session.review", session_id=session_id))
    calculate_metrics(round_obj, current_app.config)
    db.session.commit()
    if source == "template":
        flash(f"{next_round.round_number}차 프롬프트를 만들었습니다. 영상 설명·샷 목록·내레이션은 그대로이고, "
              "반영한 의견은 시청자 반영 사항·연출 지침에 들어갔습니다. 내용을 바꾸려면 '프롬프트 편집'에서 고치세요.", "info")
    else:
        engine = {"gemma": "ComfyUI Gemma", "claude": "Claude", "ollama": "Ollama"}.get(source, source)
        flash(f"{next_round.round_number}차 프롬프트를 만들었습니다. 내용 의견을 반영해 {engine}가 영상 설명·내레이션을 "
              "다듬었습니다 (샷 목록은 그대로) — '버전 비교'에서 확인하세요.", "success")
    return _back(session_id)


# ── 버전 관리 ────────────────────────────────────────────────────────────
@bp.route("/session/<int:session_id>/versions")
def versions(session_id):
    session = _get_session(session_id)
    return render_template("session/versions.html", session=session, versions=session.versions)


@bp.route("/session/<int:session_id>/versions/<int:version_id>")
def version_diff(session_id, version_id):
    session = _get_session(session_id)
    version = PromptVersion.query.filter_by(id=version_id, session_id=session_id).first_or_404()
    return render_template("session/version_diff.html", session=session, version=version)


@bp.route("/session/<int:session_id>/versions/<int:version_id>/rollback", methods=["POST"])
def rollback(session_id, version_id):
    round_obj = _get_session(session_id).current_round
    if not _require_status(round_obj, "prompt_built"):
        return redirect(url_for("session.versions", session_id=session_id))
    try:
        approval.rollback_to_version(session_id, version_id)
    except ValueError as exc:
        flash(str(exc), "warning")
    else:
        db.session.commit()
        flash("프롬프트를 되돌렸습니다.", "success")
    return redirect(url_for("session.versions", session_id=session_id))


# ── 리포트 ───────────────────────────────────────────────────────────────
@bp.route("/session/<int:session_id>/report")
def report(session_id):
    session = _get_session(session_id)
    rows = []
    for r in session.rounds:
        metric = Metric.query.filter_by(round_id=r.id).first()
        if metric:
            rows.append({"round": r.round_number, "metric": metric})
    categories = Counter(
        f.topic_category for r in session.rounds for f in r.feedbacks if f.is_constructive)
    return render_template("session/report.html", session=session, rows=rows,
                           categories=dict(categories))

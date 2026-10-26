"""[팀 직접 구현] 파이프라인 점검용 개발 페이지 — /dev/pipeline-test.

로컬에서 영상 파이프라인(ComfyUI·작업 큐·부분 재생성 등)이 의도대로 동작하는지 눈으로 보고
자동 판정한다. `[TEST]` 접두어가 붙은 세션만 만들고 지우며, 상용 API는 호출하지 않는다
(상용 재생성은 스테퍼 ③ 패널에서 사람이 직접 눌러야만 실행된다).
DEV_TEST_PAGE=false로 끌 수 있다.
"""
import json
import os
import shutil
from datetime import datetime

from flask import Blueprint, abort, current_app, jsonify, render_template, request, url_for

from ..models import (
    ALL_STATUSES, GenerationJob, PipelineSession, VideoRound, db,
)
from ..pipeline import approval, orchestrator, quality_gate, storyboard_model as sbm, youtube_client
from ..pipeline.orchestrator import InvalidTransition, PipelineError
from ..pipeline import narration
from ..pipeline.prompt_builder import build_initial_prompt, extract_shots
from ..pipeline.video_generator import commercial_available
from ..pipeline.video_generator.comfyui import rough_cut, workflow
from ..pipeline.video_generator.comfyui.comfy_client import ComfyClient
from ..pipeline.video_generator.factory import configured_name

bp = Blueprint("devtest", __name__, url_prefix="/dev")

TEST_PREFIX = "[TEST]"
_SAMPLE_SHOTS = [
    "A person in a beige coat holding a clear umbrella waits at a rainy bus stop at night, cinematic",
    "A city bus with glowing windows pulls up to the bus stop through the rain, headlights reflecting on the wet road",
    "Close-up of raindrops falling on the clear umbrella, streetlight reflections, shallow depth of field",
    "The person in the beige coat steps onto the bus as the doors close, neon signs glowing behind them",
]


@bp.before_request
def _gate():
    if not current_app.config.get("DEV_TEST_PAGE", True):
        abort(404)


def _err(message, status=409, **extra):
    return jsonify(error=message, **extra), status


def _test_sessions():
    return PipelineSession.query.filter(PipelineSession.title.like(f"{TEST_PREFIX}%")).all()


# ── 페이지 ───────────────────────────────────────────────────────────────
@bp.route("/pipeline-test")
def page():
    round_id = request.args.get("round", type=int)
    round_obj = db.session.get(VideoRound, round_id) if round_id else None
    sessions = _test_sessions()
    if round_obj is None and sessions:  # 가장 최근 테스트 세션의 현재 라운드
        round_obj = max(sessions, key=lambda s: s.id).current_round
    return render_template(
        "dev/pipeline_test.html", round=round_obj,
        session=round_obj.session if round_obj else None,
        test_sessions=sorted(sessions, key=lambda s: -s.id),
        simulation=youtube_client.is_simulation(current_app.config))


# ── 환경 점검 ────────────────────────────────────────────────────────────
@bp.route("/api/env")
def env():
    cfg = current_app.config
    configured = configured_name(cfg)
    items = []

    def add(key, label, status, detail=""):
        items.append({"key": key, "label": label, "status": status, "detail": detail})

    add("adapter", "영상 어댑터 설정", "pass" if configured != "mock" else "warn",
        f"VIDEO_GENERATOR_ADAPTER={configured}" + (" (mock: 실제 영상 없음)" if configured == "mock" else ""))
    if configured == "comfyui":
        client = ComfyClient(cfg["COMFYUI_URL"])
        alive = client.is_alive(timeout=cfg["COMFYUI_AVAILABLE_TIMEOUT_SEC"])
        add("comfyui", "ComfyUI 서버 응답", "pass" if alive else "fail", cfg["COMFYUI_URL"])
        ok, reason = workflow.workflow_ready(cfg)
        add("workflow", "워크플로·mapping.yaml", "pass" if ok else "fail",
            f"{cfg['COMFYUI_WORKFLOW_T2V']} @ {cfg['COMFYUI_WORKFLOWS_DIR']}" if ok else reason)
    ffmpeg = rough_cut.resolve_ffmpeg(cfg)
    add("ffmpeg", "ffmpeg (러프컷)", "pass" if ffmpeg else "fail", ffmpeg or "FFMPEG_PATH를 설정하세요")
    add("simulation", "댓글 시뮬레이션 모드",
        "pass" if youtube_client.is_simulation(cfg) else "warn",
        "SIMULATION_MODE=true — 가짜 댓글로 ④~⑨ 진행" if youtube_client.is_simulation(cfg)
        else "SIMULATION_MODE=false — 이 페이지의 '다음 단계'는 시뮬레이션 모드에서만 동작합니다")
    add("approval", "영상 확인 후 업로드", "pass",
        f"REQUIRE_VIDEO_APPROVAL={cfg['REQUIRE_VIDEO_APPROVAL']}")
    add("commercial", "상용 API", "pass",
        "키 없음 — 상용 재생성 비활성화 (이 점검에서 호출되지 않음)" if not commercial_available(cfg)
        else "키 있음 — ③ 패널에서 사람이 누르기 전에는 호출되지 않음")
    voice_ok, voice_detail = narration.is_available(cfg)
    add("voice", "내레이션 음성 (TTS)", "pass" if voice_ok else "warn",
        f"{cfg.get('VOICE_ADAPTER')} — {voice_detail}" if voice_ok
        else f"사용 불가 — {voice_detail} (내레이션 문장이 있어도 영상에는 얹히지 않음)")
    add("enhance", "Gemma 프롬프트 강화", "pass",
        "켜짐 — 장면 묘사를 Gemma가 영상 모델에 맞게 다듬어 넣음 (재현성 낮음)"
        if cfg.get("COMFYUI_PROMPT_ENHANCE") else "꺼짐 — 입력한 샷 묘사가 그대로 전달됨")
    add("partial", "부분 재생성", "pass" if cfg.get("PARTIAL_REGEN_ENABLED") else "warn",
        f"PARTIAL_REGEN_ENABLED={cfg.get('PARTIAL_REGEN_ENABLED')}")
    add("queue", "작업 큐", "pass" if not cfg.get("JOBS_SYNC") else "warn",
        "워커 스레드(비동기)" if not cfg.get("JOBS_SYNC") else "JOBS_SYNC=true — 요청 안에서 동기 실행(진행 표시 불가)")
    return jsonify(items=items, test_mode=bool(cfg.get("COMFYUI_TEST_MODE")),
                   shot_seconds=cfg["STORYBOARD_SHOT_SECONDS"],
                   size=f"{cfg['STORYBOARD_WIDTH']}x{cfg['STORYBOARD_HEIGHT']}",
                   prompt_enhance=bool(cfg.get("COMFYUI_PROMPT_ENHANCE")))


@bp.route("/api/prompt-enhance", methods=["POST"])
def set_prompt_enhance():
    """Gemma 프롬프트 강화를 이 서버 프로세스 동안만 켜고 끈다 (.env는 바꾸지 않는다)."""
    body = request.get_json(silent=True) or {}
    current_app.config["COMFYUI_PROMPT_ENHANCE"] = bool(body.get("enabled"))
    return jsonify(prompt_enhance=bool(current_app.config["COMFYUI_PROMPT_ENHANCE"]))


# ── 테스트 시작 / 단계 실행 ───────────────────────────────────────────────
@bp.route("/api/start", methods=["POST"])
def start():
    """테스트 세션 시작. 영상 설명을 직접 넣으면 그 영상으로, 비우면 샘플 장면으로 만든다."""
    cfg = current_app.config
    body = request.get_json(silent=True) or {}
    description = (body.get("description") or "").strip()
    shots_text = (body.get("shots_text") or "").strip()
    aspect = body.get("aspect") if body.get("aspect") in sbm.ASPECTS else "9:16"
    seconds = max(1.0, min(float(body.get("seconds", cfg["STORYBOARD_SHOT_SECONDS"])), 8.0))
    if not description:  # 샘플: 장면 수만 고른다
        n = max(1, min(int(body.get("shots", 2)), len(_SAMPLE_SHOTS)))
        description = "비 오는 밤 버스 정류장에서 우산을 든 사람이 버스를 기다리는 짧은 영상."
        shots_text = shots_text or chr(10).join(_SAMPLE_SHOTS[:n])
    title = f"{TEST_PREFIX} {(body.get('title') or '파이프라인 점검').strip()} {datetime.now():%m-%d %H:%M:%S}"

    genre = (body.get("genre") or "").strip() or "테스트"
    prompt_text, source = build_initial_prompt(
        title=title, genre=genre, characters=(body.get("subjects") or "").strip(),
        synopsis=description, shots_text=shots_text, style=(body.get("style") or "").strip(),
        global_settings=[], config=cfg, shot_seconds=seconds,
        narration_text=(body.get("narration") or "").strip())
    session = PipelineSession(title=title, genre=genre, characters="", style="",
                              aspect_ratio=aspect, shot_seconds=seconds)
    db.session.add(session)
    db.session.flush()
    round_obj = VideoRound(session_id=session.id, round_number=1, prompt_text=prompt_text)
    db.session.add(round_obj)
    db.session.flush()
    approval.add_version(session.id, round_obj.id, "", prompt_text, f"테스트 세션 ({source})")
    round_obj.storyboard_json = sbm.dumps(
        sbm.build_storyboard(prompt_text, cfg, aspect=aspect, shot_seconds=seconds))
    db.session.commit()

    if body.get("start", True):
        orchestrator.start_generation(round_obj)
    return jsonify(session_id=session.id, round_id=round_obj.id, shot_source=source,
                   url=url_for("devtest.page", round=round_obj.id))


@bp.route("/api/rounds/<int:round_id>/step", methods=["POST"])
def step(round_id):
    """현재 상태에서 다음 단계를 한 번 실행한다 (시뮬레이션 모드 전용)."""
    round_obj = db.session.get(VideoRound, round_id)
    if round_obj is None or not round_obj.session.title.startswith(TEST_PREFIX):
        return _err("테스트 세션의 라운드가 아닙니다.", 404)
    cfg = current_app.config
    if not youtube_client.is_simulation(cfg):
        return _err("SIMULATION_MODE=true에서만 단계를 자동으로 넘길 수 있습니다.")
    state = round_obj.status
    next_round_id = None
    try:
        if state == "prompt_built":
            orchestrator.start_generation(round_obj)
            message = "영상 생성을 시작했습니다."
        elif state == "generation_failed":
            orchestrator.retry_failed(round_obj)
            message = "실패한 장면을 다시 생성합니다."
        elif state == "video_ready":
            orchestrator.approve_and_upload(round_obj, cfg)
            message = "영상 확인 → 업로드(시뮬레이션) 완료."
        elif state == "uploaded":
            count = orchestrator.collect(round_obj, cfg)
            message = f"시뮬레이션 댓글 {count}개 수집."
        elif state == "feedback_collected":
            constructive = orchestrator.classify(round_obj, orchestrator.make_classifier(
                current_app._get_current_object()))
            message = f"분류 완료 — 건설적 피드백 {constructive}개."
        elif state == "feedback_classified":
            orchestrator.start_clustering(round_obj)
            message = "클러스터링을 시작했습니다."
        elif state == "pending_review":
            # 장면을 지정한 클러스터(지적 장면이 전체의 일부)가 있으면 그것만 승인한다 —
            # 그래야 나머지 장면이 재사용되는 부분 재생성을 확인할 수 있다. 없으면 전부 승인.
            total = len(sbm.shot_ranges(round_obj.storyboard_data)) if round_obj.storyboard_data else 0
            clusters = list(round_obj.clusters)
            targeted = [c for c in clusters if 0 < len(c.affected_shots) < total]
            chosen = targeted or clusters
            for c in clusters:
                c.status = "approved" if c in chosen else "rejected"
            db.session.commit()
            next_round, source = approval.apply_approved_clusters(round_obj, cfg)
            db.session.commit()
            next_round_id = next_round.id
            # 이 테스트 세션의 장면 길이(1화에서 지정)를 다음 화에도 이어받는다
            prev_board = round_obj.storyboard_data
            if prev_board:
                board = sbm.build_storyboard(next_round.prompt_text, cfg)
                for shot, old_shot in zip(board["shots"], prev_board["shots"]):
                    shot["duration"] = old_shot["duration"]
                orchestrator._apply_feedback_to_shots(board, round_obj, next_round)
                next_round.storyboard_json = sbm.dumps(board)
                db.session.commit()
            scope = "장면을 지정한 클러스터만" if targeted else "클러스터 전체"
            message = (f"{scope} {len(chosen)}개를 승인·반영 → {next_round.round_number}차 생성 ({source}).")
        else:
            return _err(f"현재 단계({state})에서는 실행할 다음 단계가 없습니다.")
    except (PipelineError, InvalidTransition, ValueError) as exc:
        return _err(str(exc))
    db.session.refresh(round_obj)
    return jsonify(message=message, state=round_obj.status, next_round_id=next_round_id,
                   next_url=url_for("devtest.page", round=next_round_id) if next_round_id else None)


@bp.route("/api/rounds/<int:round_id>/targeted-feedback", methods=["POST"])
def targeted_feedback(round_id):
    """특정 장면을 가리키는(타임스탬프 포함) 시청자 댓글을 넣어 부분 재생성을 시험한다."""
    round_obj = db.session.get(VideoRound, round_id)
    if round_obj is None or not round_obj.session.title.startswith(TEST_PREFIX):
        return _err("테스트 세션의 라운드가 아닙니다.", 404)
    if round_obj.status not in ("uploaded", "feedback_collected"):
        return _err("업로드(④) 이후 분류(⑥) 이전 단계에서만 넣을 수 있습니다.")
    storyboard = round_obj.storyboard_data
    shot_id = (request.get_json(silent=True) or {}).get("shot_id")
    ranges = {i: (a, b) for i, a, b in sbm.shot_ranges(storyboard)}
    if shot_id not in ranges:
        return _err("없는 장면입니다.", 400)
    mid = (ranges[shot_id][0] + ranges[shot_id][1]) / 2
    stamp = f"{int(mid) // 60}:{int(mid) % 60:02d}"
    texts = [
        f"{stamp}쯤 화면이 너무 어두워서 인물 표정이 안 보여요. 조명을 밝게 해주세요.",
        f"{stamp} 장면이 너무 어두워요. 얼굴이 안 보여서 답답했어요. 밝기를 높여주세요.",
        f"The scene at {stamp} is too dark to see the actors' faces, please brighten the lighting.",
    ]
    orchestrator.add_comments(
        round_obj, [{"author": "dev-test", "text": t, "source": "manual"} for t in texts])
    return jsonify(message=f"{shot_id}({stamp})를 가리키는 댓글 {len(texts)}개를 추가했습니다.",
                   shot_id=shot_id, timestamp=stamp)


@bp.route("/api/cleanup", methods=["POST"])
def cleanup():
    """[TEST] 세션과 그 영상 폴더를 지운다. 다른 세션은 건드리지 않는다."""
    removed = 0
    base = current_app.config.get("VIDEO_OUTPUT_DIR") or os.path.join(
        current_app.instance_path, "videos")
    for session in _test_sessions():
        folder = os.path.join(base, f"session_{session.id}")
        if os.path.isdir(folder):
            shutil.rmtree(folder, ignore_errors=True)
        db.session.delete(session)
        removed += 1
    db.session.commit()
    return jsonify(removed=removed)


@bp.route("/api/jobs")
def job_log():
    rows = GenerationJob.query.order_by(GenerationJob.id.desc()).limit(12).all()
    return jsonify(jobs=[{
        "id": j.id, "kind": j.kind, "round_id": j.round_id, "status": j.status,
        "error": j.error, "created": j.created_at.strftime("%H:%M:%S") if j.created_at else "",
        "seconds": round((j.finished_at - j.started_at).total_seconds(), 1)
        if j.finished_at and j.started_at else None,
    } for j in rows])


# ── 자동 판정 ────────────────────────────────────────────────────────────
@bp.route("/api/rounds/<int:round_id>/verify")
def verify(round_id):
    round_obj = db.session.get(VideoRound, round_id)
    if round_obj is None:
        return _err("라운드를 찾을 수 없습니다.", 404)
    checks = _run_checks(current_app._get_current_object(), round_obj)
    counts = {k: sum(1 for c in checks if c["status"] == k) for k in ("pass", "fail", "warn", "skip")}
    return jsonify(round=round_obj.round_number, state=round_obj.status, checks=checks, counts=counts)


def _run_checks(app, round_obj):
    cfg = app.config
    checks = []

    def add(key, label, expect, status, detail=""):
        checks.append({"key": key, "label": label, "expect": expect, "status": status,
                       "detail": detail})

    state = round_obj.status
    storyboard = round_obj.storyboard_data
    renders = list(round_obj.shot_renders)
    selected = {r.shot_id: r for r in renders if r.selected}
    configured = configured_name(cfg)
    ffmpeg = rough_cut.resolve_ffmpeg(cfg)
    generated = state not in ("prompt_built", "generating") and bool(renders)

    # 1. 상태 머신
    add("state", "유효한 상태값", "상태가 정의된 9단계 + generation_failed 중 하나",
        "pass" if state in ALL_STATUSES else "fail", state)
    steps = orchestrator.compute_steps(round_obj, cfg)
    statuses = [s["status"] for s in steps]
    first_open = next((i for i, s in enumerate(statuses) if s != "done"), len(statuses))
    consistent = all(s == "pending" for s in statuses[first_open + 1:])
    add("steps", "스테퍼 단계 일관성", "완료 단계 뒤에 현재 단계가 하나 있고, 그 뒤는 모두 대기",
        "pass" if consistent else "fail", " → ".join(f"{s['no']}:{s['status']}" for s in steps))

    # 2. 스토리보드
    if storyboard is None:
        add("storyboard", "스토리보드 스냅샷", "영상 생성 시작 시 저장됨", "skip", "아직 생성 전")
    else:
        prompt_shots = extract_shots(round_obj.prompt_text)[: cfg["LOCAL_VIDEO_MAX_SHOTS"]]
        same = [s["prompt"] for s in storyboard["shots"]] == prompt_shots
        add("storyboard", "스토리보드 = 프롬프트 [샷 목록]",
            "장면 수·내용이 프롬프트 문서와 일치", "pass" if same else "fail",
            f"스냅샷 {len(storyboard['shots'])}장면 / 프롬프트 {len(prompt_shots)}장면")

    # 3. 장면 결과
    if not generated:
        for key, label in (("results", "장면 결과"), ("files", "클립 파일"), ("specs", "클립 규격"),
                           ("rough", "러프컷"), ("adapter", "사용 어댑터")):
            add(key, label, "생성 완료 후 판정", "skip", f"현재 단계: {state}")
    else:
        missing = [s["id"] for s in storyboard["shots"]
                   if s["id"] not in selected or selected[s["id"]].status not in ("done", "reused")]
        add("results", "모든 장면에 선택된 결과", "각 장면마다 done/reused 결과가 1개 선택됨",
            "pass" if not missing else "fail",
            f"{len(selected)}/{len(storyboard['shots'])}장면" + (f" · 누락 {missing}" if missing else ""))

        providers = sorted({r.provider for r in selected.values()})
        if configured == "mock":
            add("adapter", "사용 어댑터", "mock 설정이면 mock", "pass", "mock")
        elif round_obj.adapter_used == configured:
            add("adapter", "사용 어댑터", f"설정({configured})대로 실제 어댑터 사용", "pass",
                ", ".join(providers))
        else:
            add("adapter", "사용 어댑터", f"설정({configured})대로 실제 어댑터 사용", "fail",
                f"실제 사용: {round_obj.adapter_used or '-'} — {round_obj.generation_detail or '폴백 사유 없음'}")

        wanted_narration = [s["id"] for s in storyboard["shots"] if (s.get("narration") or "").strip()]
        if wanted_narration:
            nstat = orchestrator._narration_status(round_obj)
            label = "내레이션 합성·믹스"
            expect = "[내레이션] 문장이 있는 장면마다 음성이 만들어져 러프컷 위에 얹힘"
            if nstat["status"] == "ok":
                add("narration", label, expect, "pass", nstat["detail"])
            elif nstat["status"] in ("warn", "skipped"):
                add("narration", label, expect, "warn", nstat["detail"])
            else:
                add("narration", label, expect, "skip", "러프컷이 없어(mock) 내레이션을 얹지 않음")

        if cfg.get("COMFYUI_PROMPT_ENHANCE"):
            fresh =[r for r in selected.values() if r.provider != "mock" and r.status == "done"]
            unchanged = [r.shot_id for r in fresh
                         if not r.enhanced_prompt or r.enhanced_prompt.strip() == r.prompt.strip()]
            if not fresh:
                add("enhance", "Gemma 프롬프트 강화", "켜져 있으면 새로 만든 장면마다 강화된 문장이 기록됨",
                    "skip", "새로 생성된 장면 없음")
            else:
                add("enhance", "Gemma 프롬프트 강화", "켜져 있으면 새로 만든 장면마다 강화된 문장이 기록됨",
                    "pass" if not unchanged else "warn",
                    f"{len(fresh) - len(unchanged)}/{len(fresh)}장면 강화됨"
                    + (f" · 원문과 같거나 비어 있음 {unchanged}" if unchanged else ""))

        real = [r for r in selected.values() if r.provider != "mock"]
        if not real:
            add("files", "클립 파일", "실제 어댑터면 장면마다 mp4 존재", "skip", "mock — 파일 없음")
            add("specs", "클립 규격", "해상도·프레임(8n+1)·fps가 기대값과 일치", "skip", "mock")
        else:
            bad_files = [r.shot_id for r in real if not (r.path and os.path.exists(r.path)
                                                         and os.path.getsize(r.path) > 0)]
            add("files", "클립 파일", "장면마다 비어 있지 않은 mp4가 존재",
                "pass" if not bad_files else "fail",
                f"{len(real) - len(bad_files)}/{len(real)}" + (f" · 문제 {bad_files}" if bad_files else ""))
            problems = []
            for r in real:
                if (r.frames - 1) % 8 != 0:
                    problems.append(f"{r.shot_id}: 프레임 {r.frames}는 8n+1이 아님")
                    continue
                info = rough_cut.probe_media(ffmpeg, r.path) if ffmpeg and r.path and os.path.exists(r.path) else None
                if info is None:
                    problems.append(f"{r.shot_id}: 파일을 읽을 수 없음")
                elif (info["width"], info["height"]) != (r.width, r.height):
                    problems.append(f"{r.shot_id}: 해상도 {info['width']}x{info['height']} ≠ {r.width}x{r.height}")
                elif abs(info["duration"] - r.frames / max(r.fps, 1)) > 0.3:
                    problems.append(f"{r.shot_id}: 길이 {info['duration']:.2f}s ≠ {r.frames / r.fps:.2f}s")
            add("specs", "클립 규격", "해상도·길이(frames/fps)가 생성 요청값과 일치",
                "pass" if not problems else "fail", "; ".join(problems) or f"{len(real)}개 클립 일치")

        # 러프컷
        if not real:
            add("rough", "러프컷", "실제 클립이 있으면 합성됨", "skip", "mock")
        elif not round_obj.video_path or not os.path.exists(round_obj.video_path):
            add("rough", "러프컷", "선택된 클립을 이어 붙인 mp4가 존재", "fail", "러프컷 파일 없음")
        else:
            info = rough_cut.probe_media(ffmpeg, round_obj.video_path) if ffmpeg else None
            if info is None:
                add("rough", "러프컷", "읽을 수 있는 mp4", "fail", "ffprobe 실패")
            else:
                d = storyboard["defaults"]
                want = sum(r.frames / max(r.fps, 1) for r in real)
                problems = []
                if (info["width"], info["height"]) != (d["width"], d["height"]):
                    problems.append(f"해상도 {info['width']}x{info['height']} ≠ {d['width']}x{d['height']}")
                if abs(info["duration"] - want) > 0.3 * len(real):
                    problems.append(f"길이 {info['duration']:.2f}s ≠ 장면 합 {want:.2f}s")
                status = "pass" if not problems else "fail"
                add("rough", "러프컷", "스토리보드 해상도, 길이 = 선택 장면 길이의 합", status,
                    "; ".join(problems) or f"{info['width']}x{info['height']} {info['duration']:.1f}s"
                    f" · 오디오 {'있음' if info['audio'] else '없음'}")

    # 4. 자동 업로드 금지
    if state == "video_ready" and cfg.get("REQUIRE_VIDEO_APPROVAL"):
        ok = not round_obj.youtube_video_id and not round_obj.video_approved
        add("approval", "영상 확인 전 업로드 금지", "③에서 사람이 확인하기 전에는 업로드되지 않음",
            "pass" if ok else "fail", "대기 중" if ok else "확인 없이 업로드됨")
    else:
        add("approval", "영상 확인 전 업로드 금지", "③에서 확인 대기", "skip", f"현재 단계: {state}")

    # 5. 상용 API 미사용
    spent = quality_gate.session_spent(round_obj.session_id)
    add("cost", "상용 API 비용", "사람이 누르지 않았다면 $0",
        "pass" if spent == 0 else "warn", f"${spent:.2f}")

    # 6. GPU 동시 실행 1개
    running = GenerationJob.query.filter_by(status="running").count()
    add("gpu", "작업 동시 실행", "실행 중 작업은 최대 1개", "pass" if running <= 1 else "fail",
        f"running={running}")

    # 7. 피드백 → 장면 매핑
    stamped = [f for f in round_obj.feedbacks if f.timestamp_sec is not None]
    if not stamped or storyboard is None:
        add("timestamp", "타임스탬프 → 장면 매핑", "댓글의 시각이 장면에 연결됨", "skip",
            "타임스탬프가 있는 분류된 댓글 없음")
    else:
        problems = []
        for f in stamped:
            expected = sbm.shot_at(storyboard, f.timestamp_sec)
            if f.cluster is None:
                continue
            if expected and expected not in f.cluster.affected_shots:
                problems.append(f"{f.timestamp_sec:.0f}초 → {expected} 이어야 하나 {f.cluster.affected_shots}")
        add("timestamp", "타임스탬프 → 장면 매핑", "댓글 시각이 속한 장면이 클러스터의 지적 장면에 포함됨",
            "pass" if not problems else "fail",
            "; ".join(problems) or f"{len(stamped)}개 댓글 확인")

    # 8. 부분 재생성
    prev = orchestrator._previous_round(round_obj)
    if prev is None or storyboard is None or not generated or not prev.storyboard_data:
        add("partial", "부분 재생성", "이전 화에서 지적되지 않은 장면은 재사용, 지적된 장면만 재생성",
            "skip", "2차 이상이고 생성이 끝나야 판정")
    elif not cfg.get("PARTIAL_REGEN_ENABLED", True):
        add("partial", "부분 재생성", "PARTIAL_REGEN_ENABLED=false면 전체 재생성", "skip", "기능 꺼짐")
    else:
        affected = orchestrator.affected_shot_ids(prev)
        prev_sb = prev.storyboard_data
        problems, summary = [], []
        for shot in storyboard["shots"]:
            old = sbm.get_shot(prev_sb, shot["id"])
            same = old is not None and sbm.shot_signature(prev_sb, old) == sbm.shot_signature(storyboard, shot)
            expect_reuse = same and shot["id"] not in affected
            actual = selected.get(shot["id"])
            got = actual.status if actual else "-"
            summary.append(f"{shot['id']}:{got}" + ("" if same else "(내용 변경)"))
            if expect_reuse and got != "reused":
                problems.append(f"{shot['id']}는 재사용되어야 하나 {got}")
            if not expect_reuse and got == "reused":
                problems.append(f"{shot['id']}는 재생성되어야 하나 reused")
        add("partial", "부분 재생성", "지적되지 않고 프롬프트가 같은 장면은 reused, 나머지는 새로 생성",
            "pass" if not problems else "fail", "; ".join(problems) or " ".join(summary)
            + f" (지적 장면: {sorted(affected) or '없음'})")
        notes_ok = all(sbm.get_shot(storyboard, sid) and sbm.get_shot(storyboard, sid).get("notes")
                       for sid in affected if sbm.get_shot(storyboard, sid))
        add("notes", "지적 장면에 피드백 요지 반영", "재생성 장면의 notes에 승인된 피드백이 들어감",
            "pass" if notes_ok else "fail", f"대상 {sorted(affected) or '없음'}")

    return checks


__all__ = ["bp"]

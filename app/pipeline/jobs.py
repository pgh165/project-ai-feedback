"""[팀 직접 구현] GPU 작업 큐 — SQLite 작업 테이블 + 워커 스레드 1개 (Celery·Redis 없음).

- enqueue()가 GenerationJob 행을 만들고 워커를 깨운다. 작업은 한 번에 하나씩만 실행된다.
- 워커는 첫 enqueue 때 지연 시작한다 → 개발 서버 리로더의 부모 프로세스에서는 뜨지 않는다.
- 앱 재시작 시 'running'으로 남은 작업은 failed(interrupted)로 바꾸고, 라운드는 재시도할 수 있게 한다.
- JOBS_SYNC=true면 큐 없이 호출 스레드에서 바로 실행한다 (run_check·테스트용).
"""
import json
import logging
import threading
from datetime import datetime

from flask import current_app

from ..models import FAILED_STATUS, GenerationJob, VideoRound, db
from . import llm

log = logging.getLogger(__name__)

_workers = {}  # id(app) -> (thread, wake_event) — 앱마다 워커 1개
_worker_lock = threading.Lock()
_GPU_KINDS = ("generate_video", "regenerate_shots", "import_video")  # ComfyUI(GPU)를 쓰는 작업


def _handlers():
    from . import orchestrator, video_import

    return {
        "generate_video": lambda app, job, payload: orchestrator.run_generation(app, job.round_id, payload),
        "regenerate_shots": lambda app, job, payload: orchestrator.run_generation(app, job.round_id, payload),
        "cluster": lambda app, job, payload: orchestrator.run_clustering_job(app, job.round_id),
        "import_video": lambda app, job, payload: video_import.run_import(app, job.round_id, payload),
        "youtube_upload": lambda app, job, payload: orchestrator.run_youtube_upload(app, job.round_id, payload),
    }


def enqueue(kind, round_id, payload=None):
    """작업 등록. 동기 모드면 즉시 실행하고, 아니면 워커를 깨운다."""
    if kind not in _handlers():
        raise ValueError(f"알 수 없는 작업 종류: {kind}")
    job = GenerationJob(kind=kind, round_id=round_id, payload_json=json.dumps(payload or {}))
    db.session.add(job)
    db.session.commit()

    app = current_app._get_current_object()
    if app.config.get("JOBS_SYNC"):
        execute(app, job.id)
    else:
        ensure_worker(app)[1].set()
    return job


def execute(app, job_id):
    """작업 1건 실행 (app context 안에서 호출). 실패해도 예외를 밖으로 던지지 않는다."""
    job = db.session.get(GenerationJob, job_id)
    if job is None or job.status != "queued":
        return
    job.status, job.started_at = "running", datetime.utcnow()
    db.session.commit()
    try:
        if job.kind in _GPU_KINDS:  # 큰 글 모델(장면 다듬기)이 GPU에 떠 있으면 끝날 때까지 기다린다
            with llm.strong_model_lock:
                _handlers()[job.kind](app, job, json.loads(job.payload_json or "{}"))
        else:
            _handlers()[job.kind](app, job, json.loads(job.payload_json or "{}"))
    except Exception as exc:
        log.exception("작업 %s(%s) 실패", job.id, job.kind)
        db.session.rollback()
        job = db.session.get(GenerationJob, job_id)
        job.status, job.error = "failed", f"{type(exc).__name__}: {exc}"
        _mark_round_failed(job, str(exc))
    else:
        job = db.session.get(GenerationJob, job_id)
        job.status = "done"
    job.finished_at = datetime.utcnow()
    db.session.commit()


def _mark_round_failed(job, message):
    """영상 작업이 예외로 끝났는데 라운드가 아직 '생성 중'이면 실패 상태로 둔다."""
    round_obj = db.session.get(VideoRound, job.round_id)
    if round_obj is None or job.kind == "cluster":
        return
    if round_obj.status == "generating":
        round_obj.status = FAILED_STATUS
        round_obj.generation_error = message
        round_obj.video_job_status = "failed"
        round_obj.video_job_message = message


def recover_interrupted(app):
    """재시작 시 중단된 작업 정리. 반환값: 처리한 작업 수."""
    with app.app_context():
        stuck = GenerationJob.query.filter_by(status="running").all()
        for job in stuck:
            job.status, job.error = "failed", "interrupted"
            job.finished_at = datetime.utcnow()
            round_obj = db.session.get(VideoRound, job.round_id)
            if round_obj is None:
                continue
            if job.kind == "youtube_upload":  # 영상은 ③에 그대로 있다 — 다시 누르면 처음부터 올린다
                round_obj.video_job_status = "failed"
                round_obj.video_job_message = "앱이 재시작되어 YouTube 업로드가 중단되었습니다. 다시 올려 주세요."
            elif round_obj.status == "generating":
                round_obj.status = FAILED_STATUS
                round_obj.generation_error = "앱이 재시작되어 생성이 중단되었습니다. 재시도하세요."
            elif round_obj.status == "clustering":
                round_obj.status = "feedback_classified"
                round_obj.generation_error = "앱이 재시작되어 클러스터링이 중단되었습니다."
        db.session.commit()
        return len(stuck)


def ensure_worker(app):
    """앱의 워커 스레드를 (없으면) 시작하고 (thread, wake_event)를 반환한다."""
    with _worker_lock:
        entry = _workers.get(id(app))
        if entry is None or not entry[0].is_alive():
            wake = threading.Event()
            thread = threading.Thread(target=_loop, args=(app, wake), name="job-worker", daemon=True)
            entry = (thread, wake)
            _workers[id(app)] = entry
            thread.start()
        return entry


def _loop(app, wake):
    while True:
        wake.wait(timeout=5)
        wake.clear()
        while True:
            with app.app_context():
                job = (GenerationJob.query.filter_by(status="queued")
                       .order_by(GenerationJob.id).first())
                if job is None:
                    break
                execute(app, job.id)


def wait_idle(app, timeout=30.0):
    """테스트용: 대기·실행 중인 작업이 없어질 때까지 기다린다."""
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        with app.app_context():
            db.session.expire_all()
            busy = GenerationJob.query.filter(
                GenerationJob.status.in_(["queued", "running"])).count()
        if not busy:
            return True
        time.sleep(0.1)
    return False

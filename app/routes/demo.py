"""[팀 직접 구현] 시연 촬영용 '대화형 시작' 데모 — /demo.

앱의 첫 화면·질문 화면과 똑같이 생긴 페이지에서 설명을 입력하고 AI 질문에 답하면, 준비된 데모 프로젝트
(demo_reset.py가 만든 것, 기본 '임베디드소프트웨어학과 홍보영상')의 실제 프로젝트 화면으로 넘어간다.
  - '이렇게 가정했어요'는 실제 규칙 분석(intake.analyze, 큰 모델 없이)을 쓰고, AI 질문만 준비된 것을 보여 준다.
  - 'AI에게 맡기기'를 누르면 프로젝트 화면에서 'AI가 장면을 짜는 중' 단계 문구가 실제 AI 기획처럼 흐른 뒤
    제작을 시작한다 — 제작은 데모 재생(orchestrator.replay_demo)이라 준비된 1차 결과가 몇 초 만에 나온다.
  - 이후 ③ 확인·YouTube·댓글·2차는 실제 앱 기능 그대로다.
데모 앱에서만 켜진다 (DEMO_MODE — run_demo.py가 따로 된 작업 폴더 instance_demo로 5001번에 띄운다).
실제 앱(5000)과 DB·영상 폴더를 나누어 쓰므로 데모에서 무엇을 눌러도 실제 프로젝트는 바뀌지 않는다.
데모 앱 안에서는 첫 화면(/)·질문 화면(/session/new)이 데모 페이지로 이어지고, 데모 재생 소스 프로젝트('[데모 소스]'로
시작하는 제목)는 목록·숫자에서 숨긴다. 촬영 순서는 D:\\capstone\\demo_kit\\촬영_순서.md.
"""
import json
import threading
import time
from datetime import datetime

from flask import Blueprint, abort, current_app, redirect, render_template, request, url_for

from ..models import GenerationJob, PipelineSession, db

bp = Blueprint("demo", __name__, url_prefix="/demo")

DEMO_TITLE = "임베디드소프트웨어학과 홍보영상"  # 실제 시연처럼 이름에 '데모'를 넣지 않는다 (2026-11-07 사용자)
# 처음 입력은 짧게 — 장소·분위기·카메라는 이어지는 AI 질문으로 정한다 (2026-11-07 사용자)
IDEA = "군산대 임베디드소프트웨어학과 홍보영상 만들어 줘. 자율주행 로버랑 로봇팔이 나오게."
QUESTIONS = [  # 실제 26B가 내는 질문 모양 (내용 질문 2개 + 꼭 지킬 조건 2개)
    {"question": "영상에 나올 학생들은 어떤 모습이면 좋을까요?",
     "options": ["20대 초반 한국인 남학생들", "남녀 학생이 함께", "사람 없이 장비 위주"]},
    {"question": "자율주행 로버와 로봇팔은 어디에서 보여 줄까요?",
     "options": ["학생이 작업하는 책상 위", "장비만 가까이", "시연 무대 위"]},
    {"question": "카메라는 어떻게 움직일까요?", "rule": True,
     "options": ["천천히 다가가는 시네마틱 카메라", "고정 카메라로 차분하게"]},
    {"question": "화면 분위기는 어떻게 할까요?", "rule": True,
     "options": ["어두운 실험실의 푸른 빛과 주황빛", "밝고 깨끗한 실습실"]},
]
# 'AI 기획' 단계 문구와 머무는 시간(초) — 실제 run_auto_plan이 남기는 문구와 같다
PLAN_STEPS = [
    ("AI가 장면 6개를 짜는 중이에요", 7),
    ("장면 묘사를 영상 모델에 맞게 다듬는 중이에요", 5),
    ("장면을 영어로 옮기는 중이에요", 3),
    ("기획 완료 — 장면 6개. 이제 장면을 만들어요", 2),
]


def _demo_project():
    sid = request.args.get("project", type=int) or request.form.get("project", type=int)
    if sid:
        return db.session.get(PipelineSession, sid)
    return PipelineSession.query.filter_by(title=DEMO_TITLE).order_by(PipelineSession.id.desc()).first()


SOURCE_PREFIX = "[데모 소스]"  # 데모 재생에 쓰는 준비된 결과 프로젝트 — 데모 앱 화면에는 보이지 않게


def _visible():
    return PipelineSession.query.filter(~PipelineSession.title.startswith(SOURCE_PREFIX))


@bp.before_app_request
def _demo_routes():
    """데모 앱 안에서는 실제 첫 화면·질문 화면 대신 데모 페이지로, 프로젝트 목록은 소스를 뺀 것으로."""
    ep = request.endpoint or ""
    if ep == "session.home":
        return redirect(url_for("demo.home"))
    if ep == "session.new":
        return redirect(url_for("demo.intake", **request.args))
    if ep == "session.index":
        from .session import TODO_STATUSES

        sessions = _visible().order_by(PipelineSession.created_at.desc()).all()
        view = "todo" if request.args.get("view") == "todo" else "all"
        if view == "todo":
            sessions = [s for s in sessions if s.current_round and s.current_round.status in TODO_STATUSES]
        return render_template("index.html", sessions=sessions, view=view)
    return None


@bp.app_context_processor
def _demo_nav_context():
    """왼쪽 대시보드 숫자도 소스 프로젝트를 빼고 센다 (session.py의 nav_info를 덮어쓴다)."""
    def nav_info():
        from ..pipeline import youtube_upload
        from .session import TODO_STATUSES

        rounds = [s.current_round for s in _visible().all()]
        return {"total": len(rounds), "todo": sum(1 for r in rounds if r and r.status in TODO_STATUSES),
                "youtube": youtube_upload.status(current_app.config, current_app.instance_path)}
    return {"nav_info": nav_info}


@bp.route("")
def home():
    """첫 화면과 같은 모양. 최근 프로젝트 목록은 보이지 않게 한다 (촬영 화면에 작업 중인 다른 프로젝트가 나오지 않게)."""
    from .session import _EXAMPLES

    return render_template("demo/home.html", recent=[], total=0, examples=_EXAMPLES, demo_idea=IDEA)


@bp.route("/intake")
def intake():
    """질문·가정 화면과 같은 모양. 가정은 실제 규칙 분석, 질문은 준비된 것."""
    from ..pipeline import intake as intake_mod
    from .session import GENRES

    idea = request.args.get("idea", "").strip()[:2000] or IDEA
    aspect = request.args.get("aspect", "16:9")
    data = intake_mod.analyze(idea, current_app.config, aspect=aspect, genres=GENRES, ask_llm=False)
    for a in data["assumptions"]:
        if a["key"] == "genre":  # 설명의 '홍보영상'에 맞춘다 (규칙 추측은 '소개·안내'로 읽었다)
            a.update(value="홍보·광고", shown="홍보·광고", from_text=True)
    data["questions"] = QUESTIONS
    return render_template("demo/intake.html", idea=idea, aspect=aspect, data=data)


def _shown(form):
    """auto_create와 같은 '맡긴 내용' 요약 줄."""
    from ..pipeline import intake as intake_mod

    aspect = form.get("aspect", "16:9")
    shown = [f"영상 종류: {form.get('genre') or '홍보·광고'}", f"길이: {form.get('seconds') or 30}초",
             f"화면 비율: {intake_mod.ASPECT_LABELS.get(aspect, aspect)}",
             "내레이션: " + ("없이" if form.get("narration") == "off" else "넣기"),
             "배경음악: " + ("없이" if form.get("music") == "off" else "넣기")]
    qa, rules = [], []
    for key in sorted(k for k in form if k.startswith("question_")):
        answer = form.get("answer_" + key[len("question_"):], "").strip()
        qa.append((form[key], answer))
        if answer:
            shown.append(f"{form[key]} → {answer}")
    for key in sorted(k for k in form if k.startswith("rule_question_")):
        answer = form.get("rule_answer_" + key[len("rule_question_"):], "").strip()
        if answer:
            rules.append(answer)
            shown.append(f"제작 원칙: {answer}")
    return qa, rules, shown


def _fake_planning(app, round_id, job_id):
    """프로젝트 화면이 'AI가 장면을 짜는 중'을 보이는 동안 단계 문구를 흘리고, 끝나면 제작(데모 재생)을 시작한다."""
    from ..models import VideoRound
    from ..pipeline import auto_pipeline, orchestrator

    with app.app_context():
        for text, sec in PLAN_STEPS:
            round_obj = db.session.get(VideoRound, round_id)
            auto_pipeline.note(round_obj, text)
            db.session.commit()
            time.sleep(sec)
        job = db.session.get(GenerationJob, job_id)
        job.status, job.finished_at = "done", datetime.utcnow()
        db.session.commit()
        round_obj = db.session.get(VideoRound, round_id)
        if round_obj.status == "prompt_built":
            orchestrator.start_generation(round_obj, app.config)


@bp.route("/start", methods=["POST"])
def start():
    """'AI에게 맡기기' — 데모 프로젝트에 맡긴 내용을 적고 AI 기획을 연출한 뒤 ② 제작으로."""
    demo = _demo_project()
    if demo is None or demo.current_round is None:
        abort(404, "데모 프로젝트가 없습니다. demo_reset.py를 먼저 실행하세요.")
    round_obj = demo.current_round
    if round_obj.status != "prompt_built" or round_obj.round_number != 1:
        return redirect(url_for("session.detail", session_id=demo.id))
    from ..pipeline.classifier import factory
    factory._cache.clear()  # 분류기를 기준 파일(demo_reset.py가 되돌린 것)에서 다시 읽게 — 촬영마다 같은 분류·묶음
    qa, rules, shown = _shown(request.form)
    demo.auto_mode = True
    demo.intake_json = json.dumps({"idea": request.form.get("idea") or IDEA, "qa": qa, "rules": rules,
                                   "assumptions": {k: request.form.get(k) for k in ("genre", "seconds", "aspect", "narration", "music")},
                                   "shown": shown}, ensure_ascii=False)
    round_obj.auto_json = json.dumps({"log": []}, ensure_ascii=False)
    job = GenerationJob(kind="auto_plan", round_id=round_obj.id, payload_json="{}", status="running",
                        started_at=datetime.utcnow())  # 프로젝트 화면이 'AI 기획 중'으로 보이게 (작업 큐는 running을 집지 않는다)
    db.session.add(job)
    db.session.commit()
    threading.Thread(target=_fake_planning, args=(current_app._get_current_object(), round_obj.id, job.id),
                     daemon=True).start()
    return redirect(url_for("session.detail", session_id=demo.id) + "#produce")

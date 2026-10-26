"""시연 영상용 데모 프로젝트를 ① 기획 단계로 되돌린다 (리허설 뒤 다시 찍을 때).

  .\\venv\\Scripts\\python demo_reset.py            # 데모 프로젝트(27번)를 처음 상태로
  .\\venv\\Scripts\\python demo_reset.py 27         # 프로젝트 번호를 직접 지정

1차 기획만 채워 두고, 1차·2차 '제작 시작'과 'YouTube에 올리기'는 준비된 결과로 바로 끝난다
(PipelineSession.demo_json, orchestrator.replay_demo / demo_plan — CLAUDE.md '데모 재생').
  1차 = 24번 프로젝트의 처음 올린 영상(round 25, YouTube G95N4KGwnPA — 실제 댓글이 달려 있음)
  2차 = 26번 final_nosub(round 28, YouTube wMahJiUlwcw)
서버가 켜져 있어도 되지만, 데모 프로젝트 화면은 새로 고쳐야 한다.
"""
import json
import re
import shutil
import sys

from run import app
from app.models import (ClusterResult, Feedback, GenerationJob, Metric, PipelineSession, PromptVersion, ShotRender,
                        VideoRound, db)
from app.pipeline import ko_en, orchestrator
from app.pipeline.prompt_builder import parse_sections, render_sections
from app.routes import session as routes

DEMO = int(sys.argv[1]) if len(sys.argv) > 1 else 27
FIRST_ROUND, FIRST_YOUTUBE = 25, "G95N4KGwnPA"
SECOND_ROUND, SECOND_YOUTUBE = 28, "wMahJiUlwcw"
TITLE, GENRE = "임베디드소프트웨어학과 홍보", "학과 홍보"
SYNOPSIS = ("국립군산대학교 임베디드소프트웨어학과를 알리는 20초 홍보 영상. MCU 제어에서 시작해 로봇팔, "
            "센서·AI, 자율주행으로 이어지며 임베디드 소프트웨어가 '기계의 두뇌'임을 보여 주고 학과 이름으로 마무리한다.")
NARRATION = [  # 1차 영상에 화면 자막으로 들어 있던 문장 (음성은 없었다 — 그래서 '음성으로 읽어 달라'는 댓글이 달렸다)
    "모든 움직임은 MCU의 제어에서 시작됩니다.",
    "그 제어가 로봇팔의 정확한 움직임이 되고,",
    "센서와 AI가 주변을 인식하면,",
    "자율주행차는 스스로 판단하고 움직입니다.",
    "제어하고, 움직이고, 판단하는 기계의 두뇌 임베디드 소프트웨어",
    "국립군산대학교 임베디드소프트웨어학과 피지컬 AI를 향한 도전",
]
SHOTS_KO = [  # 1차 장면(영어)을 기획 화면에 보일 한국어 — 영어 줄은 그대로라 저장해도 다시 번역하지 않는다
    "초록 회로 기판 위 검은 MCU 칩을 아주 가까이 비춘다. 칩 둘레로 푸른 빛이 회로를 따라 흐른다.",
    "하얀 로봇팔이 빨간 집게로 트레이 위 물건을 집는다. 밝고 깔끔한 스튜디오.",
    "어두운 디지털 격자 속, 푸른 네온 선으로 그린 미래형 차량을 낮은 각도에서 비춘다.",
    "작은 자율주행 RC카가 밝은 실내 바닥을 가로질러 화면 가운데로 달려온다. 카메라는 바닥 가까이 낮게.",
    "어두운 남색 격자 화면 가운데에 \"제어하고, 움직이고, 판단하는 기계의 두뇌 임베디드 소프트웨어\" 문구.",
    "어두운 남색 격자 화면 가운데에 큰 글씨로 \"임베디드소프트웨어학과\".",
]

with app.test_request_context():
    demo = db.session.get(PipelineSession, DEMO)
    if demo is None:
        sys.exit(f"{DEMO}번 프로젝트가 없습니다.")
    busy = [r.id for r in demo.rounds if orchestrator.upload_in_progress(r)
            or GenerationJob.query.filter(GenerationJob.round_id == r.id,
                                          GenerationJob.status.in_(("queued", "running"))).first()]
    if busy:
        sys.exit("데모 프로젝트에서 작업이 진행 중입니다. 끝난 뒤 다시 하세요.")
    for rnd in list(demo.rounds):
        for model in (ShotRender, Feedback, ClusterResult, GenerationJob, Metric):
            model.query.filter_by(round_id=rnd.id).delete()
        shutil.rmtree(orchestrator.output_dir(app, rnd), ignore_errors=True)  # 데모 재생이 복사해 둔 영상
    PromptVersion.query.filter_by(session_id=DEMO).delete()
    for rnd in list(demo.rounds):
        db.session.delete(rnd)
    db.session.flush()

    shots = parse_sections(db.session.get(VideoRound, FIRST_ROUND).prompt_text)["샷 목록"]
    english = [re.sub(r"^\d+\.\s*(\[[^\]]*\]\s*)?", "", line).strip() for line in shots.splitlines() if line.strip()]
    assert len(english) == len(SHOTS_KO), english
    ko_en.remember_originals(list(zip(english, SHOTS_KO)))
    text = render_sections({"프로젝트": TITLE, "영상 종류": GENRE, "버전": "1차", "영상 설명": SYNOPSIS, "샷 목록": shots,
                            "내레이션": "\n".join(f"{n}. {line}" for n, line in enumerate(NARRATION, start=1))})
    first = VideoRound(session_id=DEMO, round_number=1, status="prompt_built", prompt_text=text)
    db.session.add(first)
    db.session.flush()
    demo.title, demo.genre = TITLE, GENRE
    # 화면에서 그대로 저장했을 때와 같은 모양으로 (안 고치고 저장해도 '기획 수정'이 생기지 않게)
    first.prompt_text, *_ = routes._plan_text(demo, first, dict(routes._plan_view(demo, first)["prefill"]), translate=False)
    db.session.add(PromptVersion(session_id=DEMO, round_id=first.id, version_number=1, content=first.prompt_text,
                                 diff_text="", note="1차 초기 프롬프트 (기획)"))
    demo.demo_json = json.dumps({"rounds": {
        "1": {"replay_round_id": FIRST_ROUND, "youtube_video_id": FIRST_YOUTUBE},
        "2": {"replay_round_id": SECOND_ROUND, "youtube_video_id": SECOND_YOUTUBE}}})
    db.session.commit()
    print(f"데모 프로젝트 {DEMO}번을 ① 기획 단계로 되돌렸습니다: http://127.0.0.1:5000/session/{DEMO}")

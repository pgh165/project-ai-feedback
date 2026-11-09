"""시연 영상용 데모 프로젝트를 ① 기획 단계로 되돌린다 (리허설 뒤 다시 찍을 때).

  .\\venv\\Scripts\\python demo_reset.py            # 새 데모 (team) — 없으면 프로젝트를 만든다
  .\\venv\\Scripts\\python demo_reset.py old        # 예전 데모 (27번)

1차 기획만 채워 두고, 1차·2차 '제작 시작'과 'YouTube에 올리기'는 준비된 결과로 바로 끝난다
(PipelineSession.demo_json, orchestrator.replay_demo / demo_plan — CLAUDE.md '데모 재생').
  team (2026-11-07): 1차 = 51번 이전 최종 (round 56, '[데모 소스] … 이전 최종', 중복 장면 있음), 2차 = 51번 최종 (round 54, 드론·휴머노이드·기판 클로즈업)
       YouTube 영상 ID는 올린 뒤 아래 PRESETS["team"]의 first_youtube·second_youtube에 적는다 (비우면 업로드 단계에서 실제로 올린다)
  old: 1차 = 24번 프로젝트 round 25 (YouTube G95N4KGwnPA), 2차 = 26번 round 28 (YouTube wMahJiUlwcw)
서버가 켜져 있어도 되지만, 데모 프로젝트 화면은 새로 고쳐야 한다.
"""
import json
import re
import shutil
import os
import sys

if (sys.argv[1] if len(sys.argv) > 1 else "team") == "team":
    # 새 데모는 데모 앱의 작업 폴더(instance_demo)에 — 실제 앱과 섞이지 않게 (demo_setup.py가 만든다)
    from run_demo import DEMO_INSTANCE, app
    if not os.path.exists(os.path.join(DEMO_INSTANCE, "pipeline.db")):
        sys.exit("데모 작업 폴더가 없습니다. 먼저 .\\venv\\Scripts\\python demo_setup.py")
    import glob
    for base in glob.glob(os.path.join(DEMO_INSTANCE, "prototypes_*.npz.base")):  # 분류기는 승인 때 학습한다 —
        shutil.copyfile(base, base[:-len(".base")])  # 데모에서 승인해도 다음 촬영의 분류·묶음이 같게 기준 상태로 되돌린다
else:  # 예전 데모(27번)는 실제 앱 DB에 있다
    from run import app
from app.models import (ClusterResult, Feedback, GenerationJob, Metric, PipelineSession, PromptVersion, ShotRender,
                        VideoRound, db)
from app.pipeline import ko_en, orchestrator
from app.pipeline.prompt_builder import parse_sections, render_sections
from app.routes import session as routes

PRESETS = {
    "team": {
        "project": None,  # None이면 같은 제목의 프로젝트를 찾고, 없으면 52번 설정을 복사해 만든다
        "copy_settings_from": 51,
        "first_round": 56, "first_youtube": "UjJ4e_PpGx8",   # 51번 이전 최종 ('[데모 소스] … 이전 최종' 프로젝트) — XwnfBIpFabg가 채널에서 사라져 2026-11-07 다시 올림·댓글 9개
        "second_round": 54, "second_youtube": "jEycl9zmPIM",  # 새 51번 최종 (엔딩 카드 KSNU 로고·흰 테두리), 2026-11-07 일부 공개
        "title": "임베디드소프트웨어학과 홍보영상", "genre": "홍보·광고",  # app/routes/demo.py DEMO_TITLE과 같게
        "version_note": "1차 AI 자동 기획 (gemma4-26b)",  # /demo(대화형 시작)에서 AI가 짠 기획처럼 보이게
        "synopsis": ("국립군산대학교 임베디드소프트웨어학과를 알리는 24초 시네마틱 홍보 영상. 피지컬 AI 시대 — "
                     "자율주행, 로보틱스, AIoT, 엣지 컴퓨팅을 배우고 만드는 학생들을 보여 주고 학과 이름으로 마무리한다."),
        "narration": None,  # None이면 1차 결과 버전의 [내레이션]을 그대로 쓴다
        "shots_ko": [  # 1차(51번 기획 버전 9, 이전 최종) 장면 줄의 한국어 — 영어 줄은 그대로라 저장해도 다시 번역하지 않는다
            "밤의 어두운 임베디드 실험실, 책상 높이의 넓은 고정 화면. 앞쪽에서 짙은 회색 바퀴 로버가 흰 책상 위를 왼쪽에서 "
            "오른쪽으로 주황 고깔 사이를 누비며 천천히 지나가고, 뒤쪽에는 일자 앞머리에 검은 사각 뿔테, 흰 티 위에 검정 집업 "
            "후드를 입은 학생이 로고 없는 노트북 화면을 본다.",
            "푸른 빛과 주황빛의 어두운 실험실, 고정 화면. 큰 모니터 왼쪽에서 두 손가락 그리퍼를 단 은색 로봇팔이 손가락을 "
            "벌린 채 책상 위 작은 빨간 정육면체 쪽으로 천천히 뻗고, 모니터 오른쪽에는 짧은 머리에 검정 티셔츠를 입은 학생이 "
            "서서 키보드를 친다. 팔과 학생은 모니터를 사이에 두고 떨어져 있다.",
            "어두운 실험실의 따뜻한 스탠드 불빛. 곧은 버섯 머리에 얇은 금테 안경을 쓴 학생과 짧은 머리의 학생(둘 다 검정 "
            "티셔츠)이 함께 모니터를 보며 웃는다. 앞 책상에는 빨간 전선으로 연결된 센서 보드. 두 사람 모두 화면 안에 나란히, 고정 화면.",
            "푸른 실험실 조명, 책상 높이의 넓은 고정 화면. 앞쪽에서 주황색 바퀴 로버가 오른쪽을 향해 고깔 사이를 앞으로 "
            "천천히 달리며 화면 안에 머물고, 뒤쪽에는 곧은 버섯 머리에 금테 안경을 쓴 학생이 로고 없는 노트북으로 작업한다.",
            "푸른 조명, 흰 책상을 옆에서 본 고정 화면. 두 손가락 그리퍼를 단 은색 로봇팔이 화면 가운데 주인공으로, 손가락을 "
            "넓게 벌린 채 작은 빨간 정육면체 위에 있다가 내려가 집어 들어 올린다. 오른쪽 가장자리에는 검정 집업 후드의 학생이 "
            "흐릿하게 키보드를 친다.",
            "따뜻하고 어두운 주황 조명. 짧은 머리에 검정 티셔츠를 입은 학생과 일자 앞머리에 검은 뿔테, 검정 집업 후드를 입은 "
            "학생이 실험대에서 작은 화면이 달린 컴퓨터 보드를 계속 내려다본다. 뒤에는 모니터, 고정 화면.",
        ],
        "shots_ko_second": [  # 2차(51번 최종) 장면 줄의 한국어 — 2차 기획 화면에서 영어 대신 보이게
            "밤의 어두운 임베디드 실험실, 책상 높이의 넓은 고정 화면. 앞쪽에서 짙은 회색 바퀴 로버가 흰 책상 위를 왼쪽에서 "
            "오른쪽으로 주황 고깔 사이를 누비며 천천히 지나가고, 뒤쪽에는 일자 앞머리에 검은 사각 뿔테, 흰 티 위에 검정 집업 "
            "후드를 입은 학생이 로고 없는 노트북 화면을 본다.",
            "푸른 빛과 주황빛의 어두운 실험실, 책상 높이의 고정 화면. 작은 검정 쿼드콥터 드론이 프로펠러를 돌리며 흰 책상 위 "
            "공중에 크게 떠 있고, 뒤에는 짧은 머리에 검정 티셔츠를 입은 학생이 컴퓨터 앞에 앉아 키보드로 명령을 입력하며 드론을 "
            "본다. 옆 모니터에는 비행 그래프 화면.",
            "어두운 실험실의 따뜻한 스탠드 불빛. 곧은 버섯 머리에 얇은 금테 안경을 쓴 학생과 짧은 머리의 학생(둘 다 검정 "
            "티셔츠)이 함께 모니터를 보며 웃는다. 앞 책상에는 빨간 전선으로 연결된 센서 보드. 두 사람 모두 화면 안에 나란히, 고정 화면.",
            "푸른 실험실 조명, 책상 높이의 넓은 고정 화면. 앞쪽 흰 책상 위에 키 40cm쯤의 하얀 휴머노이드 로봇이 카메라를 보고 "
            "서서 오른팔을 들어 손을 흔들고, 뒤쪽에는 곧은 버섯 머리에 금테 안경을 쓴 학생이 모니터 앞에서 키보드를 친다.",
            "푸른 조명, 흰 책상을 옆에서 본 고정 화면. 두 손가락 그리퍼를 단 은색 로봇팔이 화면 가운데 주인공으로, 손가락을 "
            "넓게 벌린 채 작은 빨간 정육면체 위에 있다가 내려가 집어 들어 올린다. 오른쪽 가장자리에는 검정 집업 후드의 학생이 "
            "흐릿하게 키보드를 친다.",
            "따뜻하고 어두운 주황 조명, 책상 높이에서 가까이 본 고정 화면. 앞쪽에 검은 칩과 금색 핀이 달린 큰 초록 PCB 기판, "
            "바로 뒤에서 일자 앞머리에 검은 사각 뿔테, 검정 집업 후드를 입은 학생이 차분한 얼굴로 기판을 내려다보며 빨간 점퍼선을 "
            "꽂고, 기판의 초록 LED가 켜져 깜빡인다.",
        ],
    },
    "old": {
        "project": 27, "copy_settings_from": None,
        "first_round": 25, "first_youtube": "G95N4KGwnPA",
        "second_round": 28, "second_youtube": "wMahJiUlwcw",
        "title": "임베디드소프트웨어학과 홍보", "genre": "학과 홍보",
        "version_note": "1차 초기 프롬프트 (기획)",
        "synopsis": ("국립군산대학교 임베디드소프트웨어학과를 알리는 20초 홍보 영상. MCU 제어에서 시작해 로봇팔, "
                     "센서·AI, 자율주행으로 이어지며 임베디드 소프트웨어가 '기계의 두뇌'임을 보여 주고 학과 이름으로 마무리한다."),
        "narration": [  # 1차 영상에 화면 자막으로 들어 있던 문장 (음성은 없었다 — 그래서 '음성으로 읽어 달라'는 댓글이 달렸다)
            "1. 모든 움직임은 MCU의 제어에서 시작됩니다.",
            "2. 그 제어가 로봇팔의 정확한 움직임이 되고,",
            "3. 센서와 AI가 주변을 인식하면,",
            "4. 자율주행차는 스스로 판단하고 움직입니다.",
            "5. 제어하고, 움직이고, 판단하는 기계의 두뇌 임베디드 소프트웨어",
            "6. 국립군산대학교 임베디드소프트웨어학과 피지컬 AI를 향한 도전",
        ],
        "shots_ko": [
            "초록 회로 기판 위 검은 MCU 칩을 아주 가까이 비춘다. 칩 둘레로 푸른 빛이 회로를 따라 흐른다.",
            "하얀 로봇팔이 빨간 집게로 트레이 위 물건을 집는다. 밝고 깔끔한 스튜디오.",
            "어두운 디지털 격자 속, 푸른 네온 선으로 그린 미래형 차량을 낮은 각도에서 비춘다.",
            "작은 자율주행 RC카가 밝은 실내 바닥을 가로질러 화면 가운데로 달려온다. 카메라는 바닥 가까이 낮게.",
            "어두운 남색 격자 화면 가운데에 \"제어하고, 움직이고, 판단하는 기계의 두뇌 임베디드 소프트웨어\" 문구.",
            "어두운 남색 격자 화면 가운데에 큰 글씨로 \"임베디드소프트웨어학과\".",
        ],
        "shots_ko_second": None,
    },
}


def _english_lines(prompt_text):
    shots = parse_sections(prompt_text)["샷 목록"]
    return shots, [re.sub(r"^\d+\.\s*(\[[^\]]*\]\s*)?", "", line).strip() for line in shots.splitlines() if line.strip()]


def _project(preset):
    """데모 프로젝트 — 번호를 정했으면 그것, 아니면 같은 제목을 찾고 없으면 설정을 복사해 만든다."""
    if preset["project"]:
        return db.session.get(PipelineSession, preset["project"])
    found = PipelineSession.query.filter_by(title=preset["title"]).order_by(PipelineSession.id.desc()).first()
    if found is not None:
        return found
    src = db.session.get(PipelineSession, preset["copy_settings_from"])
    demo = PipelineSession(title=preset["title"], genre=preset["genre"], characters=src.characters, style=src.style,
                           aspect_ratio=src.aspect_ratio, shot_seconds=src.shot_seconds, brand_json=src.brand_json,
                           voice_json=src.voice_json, auto_mode=False)
    db.session.add(demo)
    db.session.flush()
    return demo


name = sys.argv[1] if len(sys.argv) > 1 else "team"
if name not in PRESETS:
    sys.exit(f"데모 이름은 {', '.join(PRESETS)} 중 하나입니다.")
P = PRESETS[name]

with app.test_request_context():
    demo = _project(P)
    if demo is None:
        sys.exit("데모 프로젝트가 없습니다.")
    DEMO = demo.id
    if P.get("copy_settings_from") and name == "team":
        # 로고는 넣기 전 상태로 시작한다 — 시연에서 ③ 꾸미기로 직접 올린다 (2026-11-07 사용자: 넣기 전인데 로고가 보였다).
        # 엔딩 카드 설정(문구·색, '로고도 넣기')은 원본 프로젝트 그대로, 로고 파일·영상 위 로고만 비운다
        demo_assets = os.path.join(app.instance_path, "assets", f"session_{DEMO}")
        if os.path.isdir(demo_assets):
            for fn in os.listdir(demo_assets):
                if fn.startswith("logo"):
                    os.remove(os.path.join(demo_assets, fn))
        brand = json.loads(db.session.get(PipelineSession, P["copy_settings_from"]).brand_json or "{}")
        brand.update(enabled=False, file="")
        demo.brand_json = json.dumps(brand, ensure_ascii=False)
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

    source = db.session.get(VideoRound, P["first_round"])
    shots, english = _english_lines(source.prompt_text)
    assert len(english) == len(P["shots_ko"]), english
    ko_en.remember_originals(list(zip(english, P["shots_ko"])))
    if P.get("shots_ko_second"):  # 2차 장면 줄도 화면에 한국어로 보이게
        _s2, english2 = _english_lines(db.session.get(VideoRound, P["second_round"]).prompt_text)
        assert len(english2) == len(P["shots_ko_second"]), english2
        ko_en.remember_originals(list(zip(english2, P["shots_ko_second"])))
    narration = "\n".join(P["narration"]) if P["narration"] else parse_sections(source.prompt_text).get("내레이션", "")
    text = render_sections({"프로젝트": P["title"], "영상 종류": P["genre"], "버전": "1차", "영상 설명": P["synopsis"],
                            "샷 목록": shots, "내레이션": narration})
    first = VideoRound(session_id=DEMO, round_number=1, status="prompt_built", prompt_text=text)
    db.session.add(first)
    db.session.flush()
    db.session.expire(demo, ["rounds"])  # 새로 만든 프로젝트는 방금 넣은 1차 버전을 다시 읽어야 current_round가 잡힌다
    demo.title, demo.genre = P["title"], P["genre"]
    # 화면에서 그대로 저장했을 때와 같은 모양으로 (안 고치고 저장해도 '기획 수정'이 생기지 않게)
    first.prompt_text, *_ = routes._plan_text(demo, first, dict(routes._plan_view(demo, first)["prefill"]), translate=False)
    db.session.add(PromptVersion(session_id=DEMO, round_id=first.id, version_number=1, content=first.prompt_text,
                                 diff_text="", note=P["version_note"]))
    demo.auto_mode, demo.intake_json = False, ""  # /demo에서 'AI에게 맡기기'를 누르면 맡긴 내용과 함께 켠다
    rounds = {"1": {"replay_round_id": P["first_round"]}, "2": {"replay_round_id": P["second_round"]}}
    for key, yt in (("1", P["first_youtube"]), ("2", P["second_youtube"])):
        if yt:
            rounds[key]["youtube_video_id"] = yt
    demo.demo_json = json.dumps({"rounds": rounds})
    db.session.commit()
    port = 5001 if name == "team" else 5000  # team = 데모 앱(run_demo.py)
    print(f"데모 프로젝트 {DEMO}번({name})을 ① 기획 단계로 되돌렸습니다: http://127.0.0.1:{port}/session/{DEMO}")
    if not (P["first_youtube"] and P["second_youtube"]):
        print("  YouTube 영상 ID가 비어 있어 'YouTube에 올리기'는 실제로 올립니다 — 미리 올렸다면 PRESETS에 ID를 적으세요.")

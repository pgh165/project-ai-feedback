"""전체 라우트·API 기능 검증 스크립트.

임시 DB + 시뮬레이션 + mock 어댑터로 2차(버전)까지 파이프라인을 끝까지 돌린다.
실제 instance/pipeline.db와 학습된 프로토타입은 건드리지 않는다.
"""
import os
import sys
import tempfile

from app import create_app
from app.models import ClusterResult, GlobalTechnicalSetting, PipelineSession, db
from app.models import GenerationJob as GenerationJobModel

_failures = []


def check(label, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {label}{' — ' + str(detail) if detail else ''}")
    if not condition:
        _failures.append(label)


def run_round(client, app, sid, approve_top=4, expect_next="prompt_built"):
    """현재 화를 prompt_built → prompt_updated까지 진행. 자동 진행·데모 프로젝트는 반영하면 다음 버전 제작까지 이어져
    expect_next="video_ready"."""
    base = f"/session/{sid}"
    for step, expected in [("generate", "video_ready"), ("upload", "uploaded")]:
        res = client.post(f"{base}/{step}")
        status = client.get(f"{base}/api/status").get_json()["status"]
        check(f"POST {step} → {expected}", res.status_code == 302 and status == expected, status)

    # 공개 뒤 화면이 부르는 자동 수집 — 누르지 않아도 분류 → 묶기까지 하고 검토 화면 주소를 돌려준다 (2026-11-08)
    with app.app_context():
        rid = db.session.get(PipelineSession, sid).current_round.id
    auto = client.post(f"/api/rounds/{rid}/auto-collect").get_json()
    data = client.get(f"{base}/api/clusters").get_json()
    check("자동 수집 → 분류·묶기까지 → pending_review + 검토 화면 주소",
          auto["state"] == "pending_review" and "/review" in auto.get("review_url", "") and len(data["clusters"]) > 0,
          (auto, len(data["clusters"])))
    again = client.post(f"/api/rounds/{rid}/auto-collect").get_json()
    check("묶은 뒤에는 자동 수집이 돌지 않음 (결정을 지우지 않게)", again["state"] == "pending_review" and again["added"] == 0, again)
    res = client.post(f"{base}/collect")
    check("POST collect (새 댓글 없음) → 검토 단계 유지", client.get(f"{base}/api/status").get_json()["status"] == "pending_review")
    check("AI 추천: 반대 의견이 없는 묶음은 '반영'으로 미리 골라 둠 (사람은 반영 한 번)",
          all(c["status"] == ("pending" if c["contradicted_by_key"] else "approved") for c in data["clusters"]),
          [(c["status"], bool(c["contradicted_by_key"])) for c in data["clusters"]][:5])
    first = data["clusters"][0]["id"]
    client.post(f"{base}/cluster/{first}/decision", data={"decision": "reject"})
    client.post(f"{base}/collect")  # 같은 시뮬레이션 댓글 — 새 댓글이 없으면 묶음·결정을 지우지 않는다
    data = client.get(f"{base}/api/clusters").get_json()
    check("새 댓글 없이 다시 모으기 → 검토 결정 유지",
          data["status"] == "pending_review" and any(c["id"] == first and c["status"] == "rejected" for c in data["clusters"]),
          [(c["id"], c["status"]) for c in data["clusters"]][:3])
    with app.app_context():
        n_before = len(db.session.get(PipelineSession, sid).current_round.feedbacks)
    res = client.post(f"{base}/comments", data={"comments": "1분 10초쯤 배경음악이 너무 커서 내레이션이 안 들려요"})
    data = client.get(f"{base}/api/clusters").get_json()
    with app.app_context():
        n_after = len(db.session.get(PipelineSession, sid).current_round.feedbacks)
    check("묶은 뒤 의견 추가 → 다시 분류·묶기 (결정은 새로)",
          n_after == n_before + 1 and data["status"] == "pending_review" and len(data["clusters"]) > 0
          and not any(c["status"] == "rejected" for c in data["clusters"]), (n_before, n_after, data["status"]))
    check("GET review", client.get(f"{base}/review").status_code == 200)

    for c in data["clusters"][:approve_top]:
        client.post(f"{base}/cluster/{c['id']}/decision", data={"decision": "approve"})
    if len(data["clusters"]) > approve_top:
        client.post(f"{base}/cluster/{data['clusters'][approve_top]['id']}/decision",
                    data={"decision": "reject"})
    preview = client.get(f"{base}/api/preview").get_json()["diff"]
    check("GET api/preview diff", "+++" in preview and "@@" in preview)

    res = client.post(f"{base}/apply")
    status = client.get(f"{base}/api/status").get_json()["status"]
    check(f"POST apply → 다음 버전 {expect_next}", res.status_code == 302 and status == expect_next, status)
    with app.app_context():
        rounds_before = len(db.session.get(PipelineSession, sid).rounds)
    client.post(f"{base}/apply")  # 반영을 한 번 더 눌러도 (2026-11-07: 여러 번 눌러 2차가 3개) 다음 버전을 또 만들지 않음
    with app.app_context():
        rounds_after = len(db.session.get(PipelineSession, sid).rounds)
    check("반영을 두 번 눌러도 다음 버전은 하나", rounds_after == rounds_before, (rounds_before, rounds_after))
    return data["clusters"]


def _voice_checks(client, app, sid, base):
    """배경음악·음성 탭: 목소리(프로젝트 공통)·내레이션 크기/빠르기 저장, 목소리가 바뀌면 음성만 다시 합성."""
    from app.pipeline import narration, orchestrator, voice

    res = client.post(f"{base}/decorate", data={
        "voice_settings": "1", "voice_enabled": "on", "voice_speaker": "supertonic:M1",
        "narration_settings": "1", "narration_gain": "130", "narration_tempo": "90"})
    with app.app_context():
        session = db.session.get(PipelineSession, sid)
        saved = voice.load(session)
        plan = orchestrator.edit_plan.round_plan(session.current_round)
        check("음성 저장 → 목소리(프로젝트) + 크기·빠르기(이 버전), 확인 탭으로 돌아옴",
              saved["speaker"] == "supertonic:M1" and res.headers["Location"].endswith("#confirm")
              and plan["narration_gain"] == 1.3 and plan["narration_tempo"] == 0.9, (saved, res.headers.get("Location")))
        cfg = voice.apply(app.config, saved)
        edge = voice.apply(app.config, {"speaker": "edge:ko-KR-InJoonNeural"})
        check("목소리 설정 → 엔진·목소리 덮어쓰기 + 음성 캐시 키가 달라짐(다시 합성)",
              cfg["VOICE_ADAPTER"] == "supertonic" and cfg["SUPERTONIC_VOICE"] == "M1"
              and edge["VOICE_ADAPTER"] == "edge" and edge["EDGE_TTS_VOICE"] == "ko-KR-InJoonNeural"
              and narration._cache_key("문장", edge) != narration._cache_key("문장", dict(edge, EDGE_TTS_VOICE="ko-KR-SunHiNeural"))
              and voice.effective_speaker(dict(app.config, VOICE_ADAPTER="edge"), {}) == "edge:ko-KR-SunHiNeural")
        old = [voice.normalize({"speaker": s, "tone": "anchor", "instruct": "x"}) for s in ("sohee", "qwen:sohee")]
        check("예전 Qwen 저장값은 기본 목소리로", all(o == voice.DEFAULTS for o in old), old)
        rnd = session.current_round
        sb = {"shots": [{"id": "s01", "narration": "첫 장면"}]}
        orchestrator._record_narration(rnd, sb, {}, orchestrator._VOICE_OFF, {})
        check("내레이션을 끄면 실패가 아니라 '끔'으로 기록", orchestrator._narration_status(rnd)["status"] == "off")
        rnd.narration_json = ""
    client.post(f"{base}/decorate", data={"tab": "sound", "voice_settings": "1"})  # 체크 해제 = 끔
    with app.app_context():
        session = db.session.get(PipelineSession, sid)
        check("내레이션 음성 끄기 저장", voice.load(session)["enabled"] is False)
        voice.save(session, {})
        db.session.commit()
        check("기본값으로 되돌리면 저장 칸 비움", session.voice_json == "")


def _ko_en_checks(client, app):
    """한국어로 적은 장면·스타일 → 영어(영상 모델용), 화면에는 한국어 원문. LLM은 가짜로 바꿔 끼운다."""
    import re as _re

    from app.pipeline import ko_en, llm, translate

    fake = {"학생들이 실습실에서 토론한다": "Students discuss in a lab, wide shot",
            "로봇 팔이 상자를 옮긴다": "A robot arm moves a box, close-up",
            "밝은 다큐멘터리 느낌": "bright documentary look, natural light"}
    calls = []

    def fake_generate(system, user, config, comfy=True, max_length=None):
        calls.append(user)
        lines = []
        for line in user.splitlines():
            m = _re.match(r"^(\d+)\.\s*(.+)$", line)
            body = m.group(2) if m else line
            en = fake.get(body.strip(), "")
            lines.append(f"{m.group(1)}. {en}" if m else en)
        return "\n".join(lines), "gemma"

    real = llm.generate
    llm.generate = fake_generate
    try:
        doc = ("[샷 목록]\n1. [6초] 학생들이 실습실에서 토론한다\n2. A white robot arm on a bench\n"
               "3. 로봇 팔이 상자를 옮긴다\n\n[내레이션]\n1. 한국어 해설은 그대로\n\n[영상 스타일]\n밝은 다큐멘터리 느낌\n")
        with app.app_context():
            out, source, pairs = ko_en.translate_prompt_doc(doc, app.config)
        check("한국어 장면만 영어로: 번호·[6초] 표시·영어 줄·내레이션은 그대로, 장면은 한 번에 번역",
              "1. [6초] Students discuss in a lab, wide shot" in out and "2. A white robot arm on a bench" in out
              and "3. A robot arm moves a box, close-up" in out and "1. 한국어 해설은 그대로" in out
              and "bright documentary look" in out and source == "gemma" and len(calls) == 2, out)
        res = client.post("/session/create", data={
            "title": "한국어 입력", "genre": "소개·안내", "synopsis": "학과 소개",
            "shots": "학생들이 실습실에서 토론한다\n로봇 팔이 상자를 옮긴다", "style": "밝은 다큐멘터리 느낌"})
        with app.app_context():
            session = PipelineSession.query.order_by(PipelineSession.id.desc()).first()
            prompt = session.current_round.prompt_text
            from app.pipeline.prompt_builder import extract_shots
            shots = extract_shots(prompt)
            shown = translate.lookup(shots)
            check("한국어로 만든 프로젝트: 영상 모델용 [샷 목록]은 영어, 화면 번역 칸에는 사용자가 쓴 한국어",
                  res.status_code == 302 and shots == ["Students discuss in a lab, wide shot", "A robot arm moves a box, close-up"]
                  and shown.get(translate.key(shots[0])) == "학생들이 실습실에서 토론한다"
                  and not ko_en.has_korean(_parse_style(prompt)), (shots, shown))
        # 기획 탭: 화면에 보인 한국어 그대로인 줄은 원래 영어를 유지, 고친 줄만 번역
        fake["강아지가 공원에서 뛴다"] = "A puppy runs in a park, tracking shot"
        page = client.get(f"/session/{session.id}").get_data(as_text=True)
        check("제작 전 기획 탭도 같은 입력 화면 (edit 모드, 화면 비율 고칠 수 있음)",
              'data-mode="edit"' in page and 'id="ai-fill-btn"' in page and 'name="aspect_ratio" value="16:9"  >' in page
              or ('data-mode="edit"' in page and 'value="16:9"' in page and "disabled" not in page.split('value="16:9"')[1][:20]))
        res = client.post(f"/session/{session.id}/plan", data={"aspect_ratio": "16:9", 
            "title": "한국어 입력 (고침)", "genre": "소개·안내", "synopsis": "학과 소개",
            "shots": "1. 학생들이 실습실에서 토론한다\n2. 강아지가 공원에서 뛴다", "narration": "1. 첫 장면 해설",
            "style": "밝은 다큐멘터리 느낌"})
        with app.app_context():
            session = db.session.get(PipelineSession, session.id)
            shots = extract_shots(session.current_round.prompt_text)
            check("기획 탭: 처음 입력한 한국어로 보이고, 저장하면 고친 줄만 번역 (안 고친 줄·스타일은 원래 영어 그대로)",
                  "학생들이 실습실에서 토론한다" in page and 'name="synopsis"' in page
                  and res.status_code == 302 and res.headers["Location"].endswith("#plan")
                  and shots == ["Students discuss in a lab, wide shot", "A puppy runs in a park, tracking shot"]
                  and session.title == "한국어 입력 (고침)" and "1. 첫 장면 해설" in session.current_round.prompt_text
                  and "bright documentary look" in _parse_style(session.current_round.prompt_text)
                  and session.versions[-1].note.startswith("기획 수정") and session.aspect_ratio == "16:9", shots)
        res = client.post("/session/create", data={"title": "바로 제작", "synopsis": "x",
                                                   "shots": "A white robot arm on a bench", "start": "1"})
        with app.app_context():
            started = PipelineSession.query.order_by(PipelineSession.id.desc()).first().current_round.status
        check("저장하고 제작 시작 → 바로 제작이 시작되고 ② 제작 탭으로",
              res.headers["Location"].endswith("#produce") and started != "prompt_built", started)
        with app.app_context():
            made = PipelineSession.query.order_by(PipelineSession.id.desc()).first()
            made_id, before_shots = made.id, extract_shots(made.current_round.prompt_text)
        client.post(f"/session/{made_id}/narration", data={"narr_s01": "로봇 팔이 일을 시작합니다."})
        with app.app_context():
            made = db.session.get(PipelineSession, made_id)
            rnd = made.current_round
            check("제작 뒤 내레이션 고치기: [내레이션]·장면 정보만 바뀌고 장면 묘사는 그대로, 버전 기록",
                  "[내레이션]\n1. 로봇 팔이 일을 시작합니다." in rnd.prompt_text
                  and rnd.storyboard_data["shots"][0]["narration"] == "로봇 팔이 일을 시작합니다."
                  and extract_shots(rnd.prompt_text) == before_shots and made.versions[-1].note == "내레이션 수정"
                  and rnd.status == "video_ready")
        from app.pipeline import feedback_target as _ft, orchestrator as _orch
        cases = {"3초 정도로 늘려줘": 3.0, "영상 길이를 좀더 늘려줘": 3.0, "2초 더 늘려줘": 4.0, "두 배로 길게": 4.0,
                 "좀 짧게 해줘": 1.5, "영상 길이를 더 늘릴 수 있나?": 3.0, "로봇이 더 빨리 움직였으면": None,
                 "자동차가 3대 나왔으면": None}
        got = {t: _ft.length_request(t, 2.0) for t in cases}
        check("의견의 길이 요청 읽기 (N초로·N초 더·두 배·늘려·짧게, 길이와 무관한 문장은 그대로)", got == cases, got)
        with app.app_context():
            rnd = db.session.get(PipelineSession, made_id).current_round
            new_len = _orch._reroll_shot(rnd, "s01", "3초로 늘려줘", app.config)
            shot = rnd.storyboard_data["shots"][0]
            check("길이 요청 → 장면 길이·프롬프트 [3초] 바꾸고, 길이만 말한 의견은 장면 묘사에 붙이지 않음",
                  new_len == 3.0 and shot["duration"] == 3.0 and "1. [3초] " in rnd.prompt_text
                  and not shot.get("notes"), (shot["duration"], shot.get("notes")))
            _orch._reroll_shot(rnd, "s01", "배경을 밝게 하고 4초로", app.config)
            shot = rnd.storyboard_data["shots"][0]
            check("길이 + 화면 의견이 함께면 길이도 바꾸고 화면 의견도 붙임",
                  shot["duration"] == 4.0 and shot.get("notes") == ["배경을 밝게 하고 4초로"])
            db.session.rollback()
        fit_cases = {"늘려줘": 3.8, "나레이션이 아직 빠르네": 3.8, "내레이션에 맞춰줘": 3.8, "3초로": 3.0, "2초 더": 5.0}
        got = {t: _ft.length_request(t, 3.0, fit_sec=3.8) for t in fit_cases}
        check("얼마나를 말하지 않은 길이 요청은 내레이션이 1배속으로 들어가는 길이로 (숫자를 말하면 그 숫자)",
              got == fit_cases and _ft.length_request("늘려줘", 3.0, fit_sec=2.5) == 4.5
              and _ft.length_request("내레이션에 맞춰줘", 5.0, fit_sec=3.2) == 3.2
              and _ft.needs_narration_fit("늘려줘") and not _ft.needs_narration_fit("3초로")
              and _ft.is_length_only("나레이션이 아직 빠르네"), got)
        from app.pipeline import narration as _nar
        real_prepare, real_speech = _nar.prepare_clips, _nar.speech_duration
        _nar.prepare_clips = lambda config, sb, out_dir: {s["id"]: {"text": s["narration"], "path": "x.wav"}
                                                         for s in sb["shots"]}
        _nar.speech_duration = lambda ffmpeg, path: 5.0
        try:
            with app.test_request_context():
                rnd = db.session.get(PipelineSession, made_id).current_round
                new_len = _orch._reroll_shot(rnd, "s01", "영상 길이를 늘려줘", app.config)
                shot = rnd.storyboard_data["shots"][0]
                # 장면 5초에 5초 말 + 0.2 지연 + 0.25 앞뒤 잘라 냄 ≈ 5.5초 (마지막 장면이라 디졸브로 겹치는 몫은 없음)
                check("'늘려줘' → 내레이션 길이를 재서 장면 길이를 맞춤 (잘라 냄·디졸브·말 시작 지연 포함)",
                      new_len == 5.5 and shot["duration"] == 5.5 and not shot.get("notes"), new_len)
                db.session.rollback()
        finally:
            _nar.prepare_clips, _nar.speech_duration = real_prepare, real_speech
        page = client.get(f"/session/{made_id}").get_data(as_text=True)
        check("확인 탭에 내레이션 입력 칸 + ! 도움말", 'name="narr_s01"' in page and 'class="tip"' in page)
        page = client.get("/session/create").get_data(as_text=True)
        check("새 프로젝트 화면 = ① 기획 단계 (다섯 단계 표시, 기획 저장 / 저장하고 제작 시작)",
              "① 기획" in page and "stage-strip" in page and "기획 저장" in page and "저장하고 제작 시작" in page)
        fake.clear()  # 번역 실패 → 한국어를 몰래 넣지 않고 막는다
        res = client.post("/session/create", data={"title": "실패", "synopsis": "x", "shots": "알 수 없는 장면"})
        check("번역 실패면 만들지 않고 입력을 그대로 둔 채 안내 (400)",
              res.status_code == 400 and "알 수 없는 장면" in res.get_data(as_text=True))
        check("따옴표 안의 한국어(화면 글자)만 있는 영어 묘사는 다시 번역하지 않음",
              not ko_en.needs_translation('A sign reads "임베디드소프트웨어학과".')
              and ko_en.needs_translation("로봇 팔이 움직인다") and ko_en.needs_translation('글자 "안녕"이 보인다'))
    finally:
        llm.generate = real


def _draft_fill_checks(client, app):
    """AI로 채우기: 비어 있는 칸만, 작은 모델의 괄호·지시문 정리, 적어 둔 칸은 덮어쓰지 않음."""
    from app.pipeline import draft_fill, llm

    answer = ("제목: 미래를 만드는 공간\n영상 종류: 소개·안내\n등장 요소: 밝은 실습실과 학생들\n[장면]\n"
              "1. (넓은 실습실 전경, 학생들이 움직인다)\n2. 로봇 팔이 상자를 옮긴다\n3. 학생들이 웃으며 나온다\n"
              "[내레이션]\n1. (활기찬 음악과 함께) 미래를 만드는 곳입니다.)\n2. 함께 성장합니다\n"
              "스타일: 밝은 다큐멘터리 느낌")
    seen = []

    def fake_generate(system, user, config, comfy=True, max_length=None):
        seen.append(user)
        return answer, "gemma"

    real = llm.generate
    llm.generate = fake_generate
    try:
        res = client.post("/session/draft/fill", json={"synopsis": "30초 학과 소개 영상", "title": "내가 정한 제목",
                                                      "shot_seconds": "5"})
        data = res.get_json()
        f = data.get("fields", {})
        check("AI로 채우기: 빈칸만 채움(제목은 그대로), 괄호·지시문 정리, 내레이션 번호",
              res.status_code == 200 and "title" not in f and f.get("genre") == "소개·안내"
              and f.get("shots", "").splitlines()[0] == "넓은 실습실 전경, 학생들이 움직인다"
              and f.get("narration", "").splitlines()[0] == "1. 미래를 만드는 곳입니다."
              and "내가 정한 제목" in seen[-1], f)
        check("설명이 'N초'면 장면 수를 길이에 맞춤 (30초 / 5초 = 6)",
              draft_fill.shot_count("30초 학과 소개", app.config, 5) == 6
              and draft_fill.shot_count("학과 소개", app.config, 5) == 6
              and draft_fill.shot_count("10초 광고", app.config, 5) == 3)
        res = client.post("/session/draft/fill", json={"synopsis": ""})
        check("설명 없이 AI로 채우기 → 400 안내", res.status_code == 400 and "설명" in res.get_json()["error"])
    finally:
        llm.generate = real


def _scene_refine_checks(client, app):
    """장면 다듬기: [N초] 표시 유지·영어 줄 검사·저장 안 함 / 큰 모델 → 못 쓰면 작은 모델(이유 표시)."""
    from app.pipeline import llm, scene_refine

    answer = ("1. Bright clean modern lab, soft studio lighting, a small toy robot car drives between orange cones, slow push-in\n"
              "2. Bright clean modern lab, two Korean students smile at a monitor, eye-level medium shot, slow pull-back")
    reply = {"text": answer, "source": "gemma4-26b", "reason": ""}
    seen = []

    def fake_strong(system, user, config, max_tokens=1200, fallback_config=None, in_job=False):
        seen.append(user)
        return reply["text"], reply["source"], reply["reason"]

    real = llm.generate_strong
    llm.generate_strong = fake_strong
    try:
        res = client.post("/session/draft/refine", json={"shots": "[6초] 로봇 차가 콘 사이를 달린다\n2. 학생들이 웃는다",
                                                         "style": "밝은 광고 느낌"})
        d = res.get_json()
        check("장면 다듬기: [6초] 표시는 그대로, 사용자 번호는 떼고 영어 줄로, 전·후를 함께 돌려줌",
              res.status_code == 200 and d["after"][0].startswith("[6초] Bright clean") and len(d["after"]) == 2
              and d["before"] == ["[6초] 로봇 차가 콘 사이를 달린다", "학생들이 웃는다"]
              and "1. 로봇 차가" in seen[-1] and "밝은 광고 느낌" in seen[-1] and d["note"] == "", d)
        reply.update(text=answer.replace("two Korean students", "학생 두 명이"))
        res = client.post("/session/draft/refine", json={"shots": "로봇\n학생"})
        check("장면 다듬기: 한글이 섞인 줄은 받지 않고 몇 번인지 안내 (400)",
              res.status_code == 400 and "2번" in res.get_json()["error"])
        reply.update(text=answer, source="gemma", reason="영상을 만드는 중이라")
        d = client.post("/session/draft/refine", json={"shots": "로봇\n학생"}).get_json()
        check("장면 다듬기: 큰 모델을 못 쓰면 이유와 함께 '작은 모델로 다듬었다'고 알림",
              "영상을 만드는 중이라 작은 모델로" in d.get("note", ""), d)
        res = client.post("/session/draft/refine", json={"shots": "  "})
        check("장면 목록 없이 장면 다듬기 → 400 안내", res.status_code == 400 and "장면" in res.get_json()["error"])
    finally:
        llm.generate_strong = real

    # generate_strong: 큰 모델 설정 없음 / 영상 생성 중 / 켜지 못함 → 작은 모델, 떠 있으면 큰 모델
    started, called = [], []
    saved = (llm.generate, llm._gpu_busy, llm._strong_alive, llm._start_strong, llm._strong_chat)
    llm.generate = lambda system, user, config, comfy=True, max_length=None: ("small", "gemma")
    llm._start_strong = lambda config, url: started.append(url)  # None을 돌려준다 = 켜지 못함
    llm._strong_chat = lambda system, user, config, url, max_tokens: called.append(url) or "big"
    try:
        cfg = dict(app.config, REFINE_LLM_URL="")
        r1 = llm.generate_strong("s", "u", cfg)
        cfg = dict(app.config, REFINE_LLM_URL="http://127.0.0.1:1", REFINE_LLM_LABEL="gemma4-26b")
        llm._gpu_busy, llm._strong_alive = (lambda: True), (lambda url: False)
        r2 = llm.generate_strong("s", "u", cfg)
        llm._gpu_busy = lambda: False
        r3 = llm.generate_strong("s", "u", cfg)
        llm._strong_alive = lambda url: True
        r4 = llm.generate_strong("s", "u", cfg)
        check("큰 모델: 설정 없음·영상 생성 중·켜지 못함이면 작은 모델로(이유 남김), 영상 생성 중엔 켜지도 않음, 떠 있으면 큰 모델",
              r1 == ("small", "gemma", "큰 모델이 설정되지 않아") and r2 == ("small", "gemma", "영상을 만드는 중이라")
              and r3 == ("small", "gemma", "큰 모델을 켜지 못해") and len(started) == 1
              and r4 == ("big", "gemma4-26b", "") and called == ["http://127.0.0.1:1"], (r1, r2, r3, r4, started))
    finally:
        llm.generate, llm._gpu_busy, llm._strong_alive, llm._start_strong, llm._strong_chat = saved
    page = client.get("/session/create").get_data(as_text=True)
    check("기획 화면에 '장면 다듬기' 버튼·전후 비교 칸", 'id="refine-btn"' in page and 'id="refine-panel"' in page
          and 'data-url="/session/draft/refine"' in page)
    check("기획 화면에 '장면 쓰는 법' 안내 (사진 한 장 + 움직임 하나, 순서·피할 것·예)",
          'id="prompt-guide"' in page and "사진 한 장 + 짧은 움직임 하나" in page and "guide-example" in page)
    check("장면 다듬기 split_lines: 번호·표시·빈 줄 처리",
          scene_refine.split_lines("1. [3초 시드 7] 로봇\n\n학생") == [("[3초 시드 7]", "로봇"), ("", "학생")])


def _feedback_revise_checks(app):
    """의견 → 장면 묘사 고쳐 쓰기: 큰 모델 먼저(작업 안), 실패하면 작은 모델 / 방향 의견은 움직임 / 고치는 방법이 바뀌면 다시 고쳐 씀."""
    from app.pipeline import comfy_text, feedback_target as ft, llm, orchestrator

    check("분류: '뒤로 가는데 앞으로 가야 해'는 움직임, '나무 보드를 브레드 보드로'는 생김새, '소리가 커'는 소리",
          ft.suggest(None, "로봇 차가 뒤로 가는데 앞으로 가야 해") == "motion"
          and ft.suggest(None, "나무 보드를 브레드 보드로 바꿔줘") == "scene" and ft.suggest(None, "소리가 너무 커") == "audio")
    calls = []
    saved = (llm.generate_strong_only, comfy_text.client)
    comfy_text.client = lambda config, **kw: calls.append("small") or None  # 작은 모델 경로로 넘어갔는지만 본다
    try:
        llm.generate_strong_only = lambda s, u, c, max_tokens=1200, in_job=False: (
            calls.append(("big", in_job)) or "Revised: Two students smile behind a white plastic breadboard.", "")
        fixed = ft.revise_shot_prompt("Two students smile behind a wooden board.", ["나무 보드를 브레드 보드로 바꿔줘"], app.config)
        check("의견대로 고쳐 쓰기: 큰 모델 먼저(작업 안 in_job), 'Revised:' 머리말 제거, 작은 모델은 안 부름",
              fixed == "Two students smile behind a white plastic breadboard." and calls == [("big", True)], (fixed, calls))
        calls.clear()
        llm.generate_strong_only = lambda s, u, c, max_tokens=1200, in_job=False: (None, "큰 모델을 켜지 못해")
        ft.revise_shot_prompt("A robot.", ["로봇을 빨간색으로"], app.config)
        check("큰 모델을 못 쓰면 작은 모델(ComfyUI e2b)로", calls == ["small"], calls)
    finally:
        llm.generate_strong_only, comfy_text.client = saved
    from app.pipeline import storyboard_model as sbm

    video, first = ft.split_revision("Video: A robotic arm lowers its open gripper over a red box, grabs it, then lifts it.\n"
                                     "First frame: A red box rests on the desk below a robotic arm with an open gripper.")
    loose = ft.split_revision("Revised: A cat runs on a beach at sunset.")
    sb = {"style": "clean look", "shots": [], "defaults": {"width": 960, "height": 544, "fps": 24, "seed": 42},
          "negative": ""}
    shot = {"id": "s01", "prompt": "A robotic arm.", "duration": 5.0, "notes": ["상자를 집게 해줘"],
            "prompt_fixed": video, "image_prompt_fixed": first}
    params = sbm.resolve_params(sb, shot, app.config)
    check("동작 의견: 영상 묘사(집어서 들어 올림)와 첫 프레임 묘사(집기 전)를 나눠, 이미지 모델엔 첫 프레임을 넣음",
          video.startswith("A robotic arm lowers") and first.startswith("A red box rests")
          and loose == ("A cat runs on a beach at sunset.", "")
          and params["prompt"].startswith("A robotic arm lowers") and params["image_prompt"].startswith("A red box rests")
          and "clean look" in params["image_prompt"], (video, first, loose, params["image_prompt"]))  # 끝에는 text_free 꼬리가 붙는다
    # 원래 묘사의 인물 생김새 지키기: 빠지면 한 번 더 묻고, 그래도 빠지면 사람 낱말 뒤에 되살린다 (옷 의견이면 안 지킴)
    orig = ("Bright lab, a young Korean woman with long black hair in a cream silk blouse stands beside a white robotic arm, "
            "eye-level medium shot")
    asked = []
    saved_strong = llm.generate_strong_only
    llm.generate_strong_only = lambda s, u, c, max_tokens=1200, in_job=False: (
        asked.append(u) or "Video: A young Korean woman types while the white robotic arm with a claw gripper lifts a cube.\n"
                           "First frame: A young Korean woman types beside a white robotic arm with a claw gripper above a cube.", "")
    try:
        kept = ft.revise_shot(orig, ["그리퍼를 두 손가락 집게로 바꿔"], app.config)
        retried = len(asked) == 2 and "Keep these phrases exactly" in asked[1] and "cream silk blouse" in asked[1]
        asked.clear()
        changed = ft.revise_shot(orig, ["블라우스를 흰 티셔츠로 바꿔"], app.config)
    finally:
        llm.generate_strong_only = saved_strong
    check("고쳐 쓰다 원래 인물 생김새(옷·머리)가 빠지면 한 번 더 묻고, 그래도 빠지면 사람 낱말 뒤에 되살림 (옷 의견이면 안 지킴)",
          retried and kept[0].startswith("A young Korean woman with long black hair in a cream silk blouse types")
          and "in a cream silk blouse" in kept[1] and len(asked) == 1 and "blouse" not in changed[0], (kept, changed))
    check("모호한 의견 안내: '그리퍼 모양을 바꿔'·'얼굴이 이상해'는 방향을 묻고, '브레드 보드로 바꿔'·'더 밝게'·'2초 더'는 그대로",
          ft.vague_reason("로봇의 그리퍼 모양을 바꿔").startswith("무엇으로") and ft.vague_reason("얼굴이 이상해")
          and not ft.vague_reason("나무 보드를 브레드 보드로 바꿔줘") and not ft.vague_reason("더 밝게")
          and not ft.vague_reason("2초 더 길게") and not ft.vague_reason("로봇 차를 빨간색으로 바꿔줘")
          and not ft.vague_reason("큐브를 그리퍼 사이에 들어갈 만큼 작게 바꿔줘"))
    shot = {"prompt": "A robot.", "notes": ["빨간색으로"]}
    key = orchestrator._notes_key(shot)
    orchestrator.REVISE_VERSION += 1
    try:
        check("고쳐 쓰는 방법(REVISE_VERSION)이 바뀌면 예전에 고쳐 둔 묘사를 다시 고쳐 씀",
              orchestrator._notes_key(shot) != key)
    finally:
        orchestrator.REVISE_VERSION -= 1


def _logo_vector_checks(client, app, sid, base):
    """로고 .ai(PDF 호환)·.pdf → 투명 배경 PNG로 바꿔 쓰고 원본도 둔다. PDF 호환이 아닌 .ai는 안내."""
    import io

    from PIL import Image

    from app.pipeline import overlay

    page = Image.new("RGB", (400, 200), "white")  # Pillow로 만든 한 쪽짜리 PDF (= PDF 호환 .ai와 같은 구조)
    page.paste((40, 60, 200), (120, 50, 280, 150))
    pdf = io.BytesIO()
    page.save(pdf, format="PDF")
    res = client.post(f"{base}/decorate", data={"logo_settings": "1", "logo_file": (io.BytesIO(pdf.getvalue()), "brand.ai")},
                      content_type="multipart/form-data")
    folder = overlay.asset_dir(app.instance_path, sid)
    with app.app_context():
        logo = overlay.load_logo(db.session.get(PipelineSession, sid))
    png = folder / "logo.png"
    ok = png.exists() and Image.open(png).mode == "RGBA" and (folder / "logo_source.ai").exists()
    check("로고 .ai → 투명 배경 PNG로 바꿔 넣고(원본 .ai도 보관) 로고 켜짐",
          res.status_code == 302 and ok and logo["file"] == "logo.png" and logo["enabled"], logo)
    res = client.post(f"{base}/decorate", data={"logo_settings": "1", "logo_file": (io.BytesIO(b"%!PS-Adobe-3.0 old ai"), "old.ai")},
                      content_type="multipart/form-data", follow_redirects=True)
    check("PDF 호환이 아닌 .ai는 안내만 하고 기존 로고 유지",
          "PDF 호환" in res.get_data(as_text=True) and png.exists())
    before = png.read_bytes()
    res = client.post(f"{base}/logo/preview", data={"logo_file": (io.BytesIO(pdf.getvalue()), "brand2.ai")},
                      content_type="multipart/form-data")
    shown = Image.open(io.BytesIO(res.data)) if res.status_code == 200 else None
    bad = client.post(f"{base}/logo/preview", data={"logo_file": (io.BytesIO(b"%!PS old"), "old.ai")},
                      content_type="multipart/form-data")
    check("저장 전 벡터 로고 미리보기: 투명 PNG로 바꿔 돌려주고(위치를 끌어 정할 수 있게) 저장된 로고는 그대로",
          shown is not None and shown.mode == "RGBA" and res.mimetype == "image/png"
          and png.read_bytes() == before and bad.status_code == 400 and "PDF 호환" in bad.get_json()["error"])
    client.post(f"{base}/decorate", data={"logo_settings": "1", "logo_enabled": "on", "logo_position": "custom",
                                          "logo_x": "0.95", "logo_y": "0.2", "logo_size": "20"})
    with app.app_context():
        logo = overlay.load_logo(db.session.get(PipelineSession, sid))
    _prep, args = overlay.logo_filter(logo, 960, 576, 2.0, 1.0)
    check("로고 위치 직접 지정(미리보기에서 끌기) → 비율로 저장, 합성은 가운데를 그 자리에·화면 밖으로 안 나가게",
          logo["position"] == "custom" and (logo["x"], logo["y"]) == (0.95, 0.2)
          and "W*0.9500-w/2" in args and "W-w)" in args and "직접 지정" in overlay.describe_logo(logo), (logo, args))
    client.post(f"{base}/decorate", data={"logo_settings": "1", "logo_enabled": "on", "logo_show": "last", "logo_last_sec": "2"})
    with app.app_context():
        logo = overlay.load_logo(db.session.get(PipelineSession, sid))
    _prep, last2 = overlay.logo_filter(logo, 960, 576, 10.0, 6.0)
    _prep, last_scene = overlay.logo_filter(dict(logo, last_sec=0.0), 960, 576, 10.0, 6.0)
    client.post(f"{base}/decorate", data={"logo_settings": "1", "logo_enabled": "on", "logo_show": "last", "logo_last_sec": ""})
    with app.app_context():
        cleared = overlay.load_logo(db.session.get(PipelineSession, sid))
    check("로고 '마지막 장면에서만'도 몇 초 동안 나올지 정함 (끝에서 2초 → 8초부터, 비우면 마지막 장면 내내)",
          logo["last_sec"] == 2.0 and "gte(t,8.00)" in last2 and "gte(t,6.00)" in last_scene
          and "마지막 2초" in overlay.describe_logo(logo) and cleared["last_sec"] == 0.0, (logo, last2))
    _end_card_logo_checks(client, app, sid, base)


def _end_card_logo_checks(client, app, sid, base):
    """엔딩 카드 로고 (2026-11-06 사용자: 로고를 넣으면 엔딩 카드에 맞게 들어가게).
    상자 안에 비율을 지켜 맞추고, 흰 바탕은 가장자리와 이어진 부분만 투명하게, 카드에 넣기로 했으면 영상 위에는 겹쳐 얹지 않는다."""
    import io
    import tempfile as _tempfile

    from PIL import Image, ImageDraw

    from app.pipeline import overlay
    from app.pipeline.video_generator.comfyui import rough_cut as rc

    with _tempfile.TemporaryDirectory() as tmp:
        wide = Image.new("RGB", (1600, 300), "white")
        d = ImageDraw.Draw(wide)
        d.ellipse((40, 40, 260, 260), fill=(0, 70, 160))
        d.ellipse((100, 100, 200, 200), fill="white")  # 로고 안쪽의 흰 부분 — 지우면 안 된다
        d.rectangle((320, 110, 1540, 190), fill=(0, 70, 160))
        src = f"{tmp}/wide.jpg"
        wide.save(src, quality=95)
        name, w, h = rc._card_logo(src, tmp, 480, 160)
        with Image.open(f"{tmp}/{name}") as out:
            corner, inner = out.getpixel((0, 0))[3], out.getpixel((int(out.width * 110 / 1500), out.height // 2))[3]  # 안쪽 흰 원 가운데 (여백을 잘라 낸 뒤 좌표)
        check("엔딩 카드 로고: 가로로 긴 로고도 상자(480×160) 안에 비율대로 맞추고, 흰 바탕은 투명하게 하되 로고 안쪽 흰 부분은 남김",
              w <= 480 and h <= 160 and (w == 480 or h == 160) and corner == 0 and inner == 255, (w, h, corner, inner))

    client.post(f"{base}/decorate", data={"end_card_settings": "1", "end_card_enabled": "on", "end_card_logo": "on",
                                          "end_card_seconds": "4", "end_card_text": "군산대학교"})
    png = io.BytesIO()
    Image.new("RGBA", (40, 40), (255, 200, 0, 255)).save(png, format="PNG")
    res = client.post(f"{base}/decorate", data={"logo_settings": "1", "logo_file": (io.BytesIO(png.getvalue()), "logo.png")},
                      content_type="multipart/form-data", follow_redirects=True)
    with app.app_context():
        session = db.session.get(PipelineSession, sid)
        logo, card = overlay.load_logo(session), overlay.load_end_card(session)
    check("엔딩 카드에 로고를 넣기로 했으면 로고를 올려도 영상 위(모서리)에는 켜지 않고 카드에만 넣음 (안내 문구)",
          logo["file"] == "logo.png" and not logo["enabled"] and card["enabled"] and card["logo"]
          and "엔딩 카드에 맞춰" in res.get_data(as_text=True), (logo, card))

    # 타임라인 로고 막대 (2026-11-07 사용자): 엔딩 카드에만 로고여도 막대가 보이고(카드 구간),
    # 앞 장면으로 늘리면 그 구간 영상 위 로고가 켜지고, 카드 안으로 줄이면 꺼진다. 미리보기 바탕은 엔딩 카드 화면
    import json as _json
    with app.app_context():
        rnd = db.session.get(PipelineSession, sid).current_round
        saved_tl = rnd.timeline_json
        rnd.timeline_json = _json.dumps({"total": 20.0, "scenes": [{"id": "s01", "start": 0.0, "end": 14.0}],
                                         "end_card": [14.0, 20.0], "cues": [], "logo": None, "last_start": 10.0})
        db.session.commit()
    from app.routes import session as routes

    def bar():
        with app.test_request_context():
            sess = db.session.get(PipelineSession, sid)
            return routes._timeline_view(sess.current_round, overlay.load_logo(sess), True)["logo"]
    page = client.get(base).get_data(as_text=True)
    card_bar = bar()
    client.post(f"{base}/decorate", data={"timeline_settings": "1", "logo_start": "6", "logo_end": "20"})
    with app.app_context():
        into_scenes = overlay.load_logo(db.session.get(PipelineSession, sid))
    scene_bar = bar()
    client.post(f"{base}/decorate", data={"timeline_settings": "1", "logo_start": "15", "logo_end": "20"})
    with app.app_context():
        card_only = overlay.load_logo(db.session.get(PipelineSession, sid))
        rnd = db.session.get(PipelineSession, sid).current_round
        rnd.timeline_json = saved_tl
        db.session.commit()
    check("타임라인 로고 막대: 엔딩 카드에만 로고여도 카드 구간 막대 → 앞 장면까지 늘리면 그 구간 영상 위 로고 켜짐(장면 끝까지=0) "
          "→ 카드 안으로 줄이면 꺼짐, 꾸미기에 '보이는 때' 선택 칸 없음",
          card_bar == [14.0, 20.0] and 'id="logo-show"' not in page
          and into_scenes["enabled"] and into_scenes["show"] == "range" and into_scenes["start_sec"] == 6.0
          and into_scenes["end_sec"] == 0.0 and scene_bar == [6.0, 20.0] and not card_only["enabled"],
          (card_bar, scene_bar, into_scenes["enabled"], card_only["enabled"]))
    client.post(f"{base}/decorate", data={"end_card_settings": "1"})  # 엔딩 카드 끄기 (뒤 검증에 영향 없게)


def _auto_motion_checks(app):
    """자동 움직임 경로 (2026-11-06 사용자: 프리셋 없이 장면 묘사를 보고 자동으로). 26B 비전·ComfyUI는 가짜로 바꿔 끼운다."""
    import contextlib
    import json as _json
    import tempfile as _tempfile

    from PIL import Image

    from app.pipeline import auto_motion as am
    from app.pipeline import llm
    from app.pipeline import storyboard_model as sbm

    print("\n── 자동 움직임 경로 (auto_motion) ──")
    W, H = 960, 576
    named = am.parse_plan('```json {"kind": "move", "direction": "right", "object": "orange rover", "gripper": "", '
                          '"anchors": ["orange traffic cones", "man"]} ```')
    broken = am.parse_plan('{"kind": "pick", "direction": "none", "object": "red cube", "gripper": "gripper", "anchors": ["monitor", "man"')
    check("1차 답(움직임 종류·방향·이름)을 읽고, JSON이 깨져도 이름은 건지며, 이상한 답은 '경로 없음'",
          named == {"kind": "move", "direction": "right", "object": "orange rover", "gripper": "", "anchors": ["orange traffic cones", "man"]}
          and broken["kind"] == "pick" and broken["object"] == "red cube" and "monitor" in broken["anchors"]
          and am.parse_plan('{"kind": "typing", "object": "x"}') == {"kind": "none"} and am.parse_plan("") is None, (named, broken))
    targets = {"object": "orange rover", "anchor1": "orange traffic cones", "anchor2": "man"}
    boxes = am.parse_boxes('```json {"the orange rover": {"x_min": 200, "y_min": 600, "x_max": 470, "y_max": 900}, '
                           '"anchor1": [{"x_min": 560, "y_min": 780, "x_max": 620, "y_max": 900}, '
                           '{"x_min": 250, "y_min": 620, "x_max": 330, "y_max": 700}], '
                           '"anchor2": {"x_min": 500, "y_min": 50, "x_max": 700, "y_max": 300}, "junk": {"x_min": 1}', targets, W, H)
    check("2차 답: 이름 붙은 칸(x_min…)을 픽셀로 바꾸고, 키 대신 물체 이름을 써도 맞추며, 한 키의 상자 여러 개(고깔)도 모두 읽음",
          boxes.get("object") == [(192.0, 345.6, 451.2, 518.4)] and len(boxes.get("anchor1", [])) == 2
          and len(boxes.get("anchor2", [])) == 1 and "junk" not in boxes, boxes)
    plan = {"kind": "move", "direction": "right", "object": boxes["object"][0],
            "anchors": boxes["anchor1"] + boxes["anchor2"]}
    tracks, direction = am.build_move(plan, W, H)
    moving = [t for t in tracks if len(t) > 1]
    still = [t[0] for t in tracks if len(t) == 1]
    dx = moving[0][-1][0] - moving[0][0][0]
    check("이동: 물체 위 점들이 같은 높이로 오른쪽으로 평행 이동(위로 휘면 '떠오름'), 거리는 화면 폭 40% 이하·화면 안, "
          "지나가는 자리의 고정점은 빼고 나머지는 고정",
          direction == "right" and len(moving) == 9 and all(abs(p[1] - t[0][1]) < 1e-6 for t in moving for p in t)
          and 0 < dx <= 0.4 * W and max(p[0] for t in moving for p in t) <= W * 0.96
          and not any(abs(p[0] - 566.4) < 0.01 and abs(p[1] - 483.84) < 0.01 for p in still)  # 차가 지나가는 고깔 → 뺌
          and any(abs(p[0] - 576.0) < 0.01 and abs(p[1] - 100.8) < 0.01 for p in still), (dx, still))  # 뒤쪽 모니터 → 고정
    check("이동: 방향을 모르면 앞이 더 비어 있는 쪽, 움직일 공간이 없으면 경로를 만들지 않음",
          am.build_move(dict(plan, direction="none"), W, H)[1] == "right"
          and am.build_move(dict(plan, direction="left", object=(20.0, 300.0, 300.0, 500.0)), W, H) is None)
    pick = {"kind": "pick", "object": (503.0, 513.0, 527.0, 537.0), "gripper": (470.0, 300.0, 540.0, 380.0), "anchors": []}
    ptracks, _d = am.build_pick(pick, W, H)
    tip_l, obj_c = ptracks[0], ptracks[4]
    mid = am.POINTS // 2 - 2
    check("집기: 손가락 끝이 내려가 물체 양옆까지 닫히고, 물체는 그때까지 가만히 있다가 함께 올라감 (팔 관절은 찍지 않음)",
          len([t for t in ptracks if len(t) > 1]) == 7 and obj_c[mid] == obj_c[0] and obj_c[-1][1] < obj_c[0][1] - 50
          and abs(tip_l[mid][0] - 503.0) < 2 and tip_l[mid][1] > 500, (tip_l[mid], obj_c[-1]))
    check("집기: 집게가 물체보다 좁거나 집게를 못 찾으면 경로를 만들지 않음",
          am.build_pick(dict(pick, gripper=(500.0, 300.0, 525.0, 380.0)), W, H) is None
          and am.build_pick(dict(pick, gripper=None), W, H) is None)
    fingers = am.build_pick(dict(pick, gripper=(400.0, 230.0, 560.0, 380.0), finger_r=(470.0, 330.0, 492.0, 384.0),
                                 finger_l=(520.0, 320.0, 540.0, 376.0)), W, H)[0]
    check("집기: 손가락을 따로 찾았으면 각 손가락 상자의 아래 가운데를 손가락 끝으로 씀 (집게 상자는 손목까지 넓게 잡혔다), 좌우가 바뀌어도 바로잡음",
          fingers[0][0] == (481.0, 384.0) and fingers[1][0] == (530.0, 376.0), (fingers[0][0], fingers[1][0]))

    with _tempfile.TemporaryDirectory() as tmp:
        img, img2 = f"{tmp}/v1.png", f"{tmp}/v2.png"
        Image.new("RGB", (W, H), (40, 40, 40)).save(img)
        Image.new("RGB", (W, H), (60, 60, 60)).save(img2)
        board = {"style": "Cinematic", "negative": "", "defaults": {"width": W, "height": H, "fps": 24, "seed": 42},
                 "shots": [{"id": "s01", "prompt": "An orange rover drives from left to right between cones.", "duration": 3.0},
                           {"id": "s02", "prompt": "A student types on a keyboard.", "duration": 3.0},
                           {"id": "s03", "prompt": "A rover drives right.", "duration": 3.0,
                            "motion": {"width": W, "height": H, "image": img, "tracks": [[[1, 1], [5, 5]]]}}]}
        answers = {"An orange rover": '{"kind": "move", "direction": "right", "object": "orange rover", "anchors": []}',
                   "A student": '{"kind": "none"}'}
        box_answer = '{"object": {"x_min": 200, "y_min": 600, "x_max": 470, "y_max": 900}}'

        class _Adapter:
            problem = ""

            def motion_problem(self):
                return self.problem

        old = (am.ask, am.ask_boxes, llm.vision_ready, llm.strong_server)
        am.ask = lambda path, desc, cfg: next((v for k, v in answers.items() if desc.startswith(k)), None)
        am.ask_boxes = lambda path, targets, cfg: box_answer
        llm.vision_ready = lambda cfg: True
        llm.strong_server = lambda cfg, in_job=False: contextlib.nullcontext()
        try:
            notes = []
            done = am.prepare(None, board, {"s01": img, "s02": img, "s03": img}, app.config, _Adapter(), note=notes.append)
            s01, s02, s03 = board["shots"]
            params = sbm.resolve_params(board, {**s01, "image": img}, app.config)
            check("자동 진행: 차가 움직이는 장면에만 경로를 붙이고(auto 표시·컷 1개), 타이핑 장면은 그대로, 사람이 준 경로는 건드리지 않음",
                  done == ["s01"] and s01["motion"]["auto"] == {"kind": "move", "direction": "right"}
                  and s01["motion"]["takes"] == 1 and "motion" not in s02 and "auto" not in s03["motion"]
                  and any("s01" in n for n in notes), (done, s02.get("motion"), notes))
            check("자동 경로가 붙은 장면은 움직임 경로 워크플로(ltx25_motion)로 만든다",
                  params["workflow"] == app.config.get("COMFYUI_WORKFLOW_MOTION", "ltx25_motion") and params["tracks"], params["workflow"])
            first = _json.dumps(s01["motion"])
            again = am.prepare(None, board, {"s01": img}, app.config, _Adapter())
            answers["An orange rover"] = '{"kind": "none"}'
            redrawn = am.prepare(None, board, {"s01": img2}, app.config, _Adapter())
            check("같은 첫 프레임이면 다시 계산하지 않고, 첫 프레임이 바뀌어 경로가 필요 없어지면 예전 자동 경로를 지움",
                  again == [] and _json.dumps(board["shots"][0].get("motion", {})) != first and redrawn == []
                  and "motion" not in board["shots"][0], board["shots"][0].get("motion"))
            answers["An orange rover"] = '{"kind": "move", "direction": "right", "object": "orange rover", "anchors": []}'
            blocked = _Adapter()
            blocked.problem = "ComfyUI-LTXVideo 노드가 없습니다"
            llm.vision_ready = lambda cfg: False
            no_vision = am.prepare(None, board, {"s01": img}, app.config, _Adapter())
            llm.vision_ready = lambda cfg: True
            check("26B 비전이나 Motion Control 노드가 없으면 경로 없이 그냥 만든다 (멈추지 않음)",
                  no_vision == [] and am.prepare(None, board, {"s01": img}, app.config, blocked) == []
                  and "motion" not in board["shots"][0])
        finally:
            am.ask, am.ask_boxes, llm.vision_ready, llm.strong_server = old

    # 자동 진행: 첫 프레임 점검으로 26B를 켰을 때 자동 경로도 같이 계산 (26B를 또 켜지 않게, 2026-11-06 사용자)
    from types import SimpleNamespace

    from app.pipeline import auto_pipeline as ap
    from app.pipeline import video_generator as vg

    events = []

    @contextlib.contextmanager
    def _server(cfg, in_job=False):
        events.append("on")
        yield
        events.append("off")

    old2 = (llm.strong_server, llm.vision_ready, ap._review_shots, ap._renders, am.prepare, vg.resolve_local_adapter)
    llm.strong_server, llm.vision_ready = _server, (lambda cfg: True)
    ap._review_shots = lambda app_, r, sb, kind, failing: events.append(f"review:{kind}") or failing.append(("s02", {}))
    ap._renders = lambda r, sid: [SimpleNamespace(selected=True, image_path=f"/img/{sid}.png")]
    am.prepare = lambda r, sb, images, cfg, adapter, note=None: events.append(("motion", sorted(images))) or []
    vg.resolve_local_adapter = lambda cfg: (object(), "")
    try:
        board = {"shots": [{"id": "s01"}, {"id": "s02"}, {"id": "s03"}]}
        from app.pipeline import orchestrator as orch
        old_sb = orch.ensure_storyboard
        orch.ensure_storyboard = lambda r, cfg: board
        try:
            with app.app_context():
                ap._review_pass(app, SimpleNamespace(id=0), "image")
                events.append("|")
                ap._review_pass(app, SimpleNamespace(id=0), "clip")
        finally:
            orch.ensure_storyboard = old_sb
        check("자동 진행: 첫 프레임 점검과 같은 26B 켜기 안에서 통과한 장면(s01·s03)의 자동 경로를 계산, 컷 점검 때는 계산하지 않음",
              events == ["on", "review:image", ("motion", ["s01", "s03"]), "off", "|", "on", "review:clip", "off"], events)
    finally:
        llm.strong_server, llm.vision_ready, ap._review_shots, ap._renders, am.prepare, vg.resolve_local_adapter = old2


def _affected_shot_timeline_checks(app):
    """댓글 시각 → 장면은 시청자가 본 영상의 실제 구간(timeline_json)으로 (2026-11-06: 기획 길이로 계산해 한 장면씩 밀림)."""
    import json as _json
    from types import SimpleNamespace

    from app.pipeline import orchestrator

    board = {"shots": [{"id": f"s0{i}", "duration": d} for i, d in enumerate((5, 5, 5, 3, 3, 3), start=1)]}
    timeline = {"scenes": [{"id": "s01", "start": 0, "end": 4.39}, {"id": "s02", "start": 4.39, "end": 8.78},
                           {"id": "s03", "start": 8.78, "end": 13.17}, {"id": "s04", "start": 13.17, "end": 15.56},
                           {"id": "s05", "start": 15.56, "end": 17.95}, {"id": "s06", "start": 17.95, "end": 20.34}],
                "end_card": [20.34, 26.34]}

    def cluster(*secs):
        return SimpleNamespace(feedbacks=[SimpleNamespace(timestamp_sec=s) for s in secs], topic_category="technical",
                               representative_text="", affected_shots_json=None)

    car, grip = cluster(14.0), cluster(16.0, 17.0)
    orchestrator.assign_affected_shots(SimpleNamespace(clusters=[car, grip], timeline_json=_json.dumps(timeline)), board, app.config)
    old_car = cluster(14.0)
    orchestrator.assign_affected_shots(SimpleNamespace(clusters=[old_car], timeline_json=""), board, app.config)
    check("댓글 시각 → 장면: 실제 합성 타임라인 기준 (0:14 → s04, 0:16·0:17 → s05), 타임라인이 없으면 기획 길이로",
          _json.loads(car.affected_shots_json) == ["s04"] and _json.loads(grip.affected_shots_json) == ["s05"]
          and _json.loads(old_car.affected_shots_json) == ["s03"],
          (car.affected_shots_json, grip.affected_shots_json, old_car.affected_shots_json))


def _demo_replay_checks(client, app):
    """데모 재생: 2차부터 '제작 시작'을 누르면 만들지 않고 준비해 둔 버전의 결과를 바로 쓴다."""
    import json as _json
    import tempfile as _tempfile

    from app.models import ShotRender, VideoRound

    def make(title, shots):
        client.post("/session/create", data={"title": title, "synopsis": "x", "shots": shots,
                                             "narration": "1. 준비된 첫 문장입니다."})
        with app.app_context():
            return PipelineSession.query.order_by(PipelineSession.id.desc()).first().id

    src_sid = make("준비된 결과", "A robot arm moves\nA car turns a corner")
    folder = _tempfile.mkdtemp(prefix="demo_src_")
    video = os.path.join(folder, "rough_cut.mp4")
    with open(video, "wb") as fh:
        fh.write(b"fake mp4")
    with open(os.path.join(folder, "subtitles.srt"), "w", encoding="utf-8") as fh:
        fh.write("1\n00:00:00,200 --> 00:00:01,000\n준비된 첫 문장입니다.\n")
    with app.app_context():
        src = db.session.get(PipelineSession, src_sid).current_round
        src.video_path, src.status = video, "video_ready"
        for n, sid in enumerate(("s01", "s02")):
            db.session.add(ShotRender(round_id=src.id, shot_id=sid, version=3, status="done", selected=True,
                                      path=os.path.join(folder, f"{sid}.mp4"), duration=3.0 + n, provider="comfyui",
                                      review_json=_json.dumps({"image": {"checked": True, "passed": True, "score": 9.0,
                                                                         "problems": []}})))
        src.auto_json = _json.dumps({"phase": "done", "flagged": ["s02"],
                                     "log": [{"at": "00:00:00", "text": "준비된 점검 기록"}]}, ensure_ascii=False)
        db.session.commit()
        src_id = src.id

    demo_sid = make("데모", "An old shot one\nAn old shot two\nAn old shot three")
    with app.app_context():
        db.session.get(PipelineSession, demo_sid).demo_json = _json.dumps({"replay_round_id": src_id})
        db.session.commit()
    run_round(client, app, demo_sid, expect_next="video_ready")  # 1차: 평소처럼 — 반영하면 2차는 데모 재생으로 바로 ③
    client.post(f"/session/{demo_sid}/generate")  # 이미 ③이라 아무 일도 하지 않는다
    with app.app_context():
        rnd = db.session.get(PipelineSession, demo_sid).current_round
        renders = {r.shot_id: (r.status, r.selected, r.duration) for r in rnd.shot_renders}
        state, number, path, text = rnd.status, rnd.round_number, rnd.video_path, rnd.prompt_text
        jobs_made = len(GenerationJobModel.query.filter_by(round_id=rnd.id).all())
        notes = [v.note for v in db.session.get(PipelineSession, demo_sid).versions]
        reviews = {r.shot_id: r.review.get("image", {}).get("score") for r in rnd.shot_renders}
        auto = rnd.auto_data
    check("데모 재생: 반영하면 2차 제작을 바로 시작 → 만들지 않고 준비된 결과로 ③ (장면·영상·자막 복사, 진행 표시용 작업 1건)",
          number == 2 and state == "video_ready" and jobs_made == 1
          and renders == {"s01": ("done", True, 3.0), "s02": ("done", True, 4.0)}
          and path and os.path.exists(path) and path != video
          and os.path.exists(os.path.join(os.path.dirname(path), "subtitles.srt"))
          and "A car turns a corner" in text and "An old shot two" not in text and "장면 구성 갱신" in notes,
          (state, renders, notes[-2:]))
    check("데모 재생: 준비된 결과의 AI 점검 기록(장면별 점수·미달 장면·진행 기록)도 복사 → ③ 'AI 점검 결과'에 보임",
          reviews == {"s01": 9.0, "s02": 9.0} and auto.get("flagged") == ["s02"] and auto.get("phase") == "done"
          and any(e.get("text") == "준비된 점검 기록" for e in auto.get("log") or []), (reviews, auto))

    # 차수별 설정: 1차도 바로 만들고, 'YouTube에 올리기'는 이미 올린 영상(댓글이 달린)에 연결
    first_sid = make("데모 처음부터", "An old shot one\\nAn old shot two")
    with app.app_context():
        db.session.get(PipelineSession, first_sid).demo_json = _json.dumps(
            {"rounds": {"1": {"replay_round_id": src_id, "youtube_video_id": "demo-yt-1"}}})
        db.session.commit()
    client.post(f"/session/{first_sid}/plan", data={"title": "데모 처음부터", "synopsis": "x", "start": "1",
                                                    "shots": "An old shot one\\nAn old shot two"})
    with app.app_context():
        r1 = db.session.get(PipelineSession, first_sid).current_round
        state1, renders1 = r1.status, len(r1.shot_renders)
    client.post(f"/session/{first_sid}/youtube-upload", data={"privacy": "unlisted", "title": "데모"})
    with app.app_context():
        r1 = db.session.get(PipelineSession, first_sid).current_round
        state2, vid = r1.status, r1.youtube_video_id
    check("데모 재생(차수별): ① 기획 '저장하고 제작 시작' → 1차도 바로 ③, 'YouTube에 올리기' → 이미 올린 영상에 연결 (④)",
          state1 == "video_ready" and renders1 == 2 and state2 == "uploaded" and vid == "demo-yt-1",
          (state1, renders1, state2, vid))


def _plan_change_checks(client, app):
    """제작 뒤 기획 고치기: 고친 장면만 다시 만들고, 끼워 넣어도 나머지 장면 결과는 그대로 옮긴다."""
    from app.models import ShotRender

    client.post("/session/create", data={"title": "기획 고치기", "synopsis": "x", "start": "1",
                                         "shots": "A red cube on a table\nA blue ball rolls\nA green cone spins"})
    with app.app_context():
        sid = PipelineSession.query.order_by(PipelineSession.id.desc()).first().id

    def selected():
        with app.app_context():
            rnd = db.session.get(PipelineSession, sid).current_round
            return rnd.status, {r.shot_id: r.id for r in ShotRender.query.filter_by(round_id=rnd.id, selected=True)}

    status, before = selected()
    page = client.get(f"/session/{sid}").get_data(as_text=True)
    check("기획 탭 = 처음 기획 화면과 같은 입력 화면 (제작 중 모드: AI로 채우기·콘티 엑셀·화면 비율 잠금)",
          'id="create-form"' in page and 'data-mode="producing"' in page and 'id="ai-fill-btn"' in page
          and 'id="storyboard-file"' in page and 'name="aspect_ratio" value="9:16" checked disabled' in page)
    form = {"title": "기획 고치기", "synopsis": "x", "narration": "", "style": "",
            "shots": "A red cube on a table\nA blue ball bounces high\nA green cone spins"}
    d = client.post(f"/session/{sid}/plan", data=dict(form, preview="1")).get_json()
    check("제작 뒤 기획 미리보기: 고친 줄만 다시 만들 장면", status == "video_ready" and d["regen"] == ["s02"] and d["kept"] == 2, d)
    res = client.post(f"/session/{sid}/plan", data=form)
    status, after = selected()
    check("저장 → s02만 새로 만들고 s01·s03은 만들어 둔 결과 그대로",
          res.headers["Location"].endswith("#produce") and status == "video_ready"
          and after["s01"] == before["s01"] and after["s03"] == before["s03"] and after["s02"] != before["s02"],
          (before, after))
    form["shots"] = "A yellow star glows\n" + form["shots"]
    d = client.post(f"/session/{sid}/plan", data=dict(form, preview="1")).get_json()
    client.post(f"/session/{sid}/plan", data=form)
    status, moved = selected()
    check("맨 앞에 장면을 끼워 넣으면 새 장면만 만들고, 뒤 장면 결과는 한 칸씩 옮김 (다시 만들지 않음)",
          d["regen"] == ["s01"] and d["kept"] == 3 and moved["s02"] == after["s01"] and moved["s03"] == after["s02"]
          and moved["s04"] == after["s03"] and status == "video_ready", (d, after, moved))
    form["narration"] = "1. 별이 빛납니다."
    d = client.post(f"/session/{sid}/plan", data=dict(form, preview="1")).get_json()
    check("내레이션만 고치면 다시 만들 장면 없음 (음성만)", d["regen"] == [] and d["narration"], d)


def _parse_style(prompt):
    from app.pipeline.prompt_builder import parse_sections
    return parse_sections(prompt).get("영상 스타일", "")


def main():
    tmp = tempfile.mkdtemp(prefix="ai_video_check_")
    app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///" + os.path.join(tmp, "check.db").replace("\\", "/"),
        "CLASSIFIER_STATE_DIR": tmp,
        "SIMULATION_MODE": True,
        "VIDEO_GENERATOR_ADAPTER": "mock",
        "JOBS_SYNC": True,
        "REQUIRE_VIDEO_APPROVAL": False,
        "ANTHROPIC_API_KEY": "",
        "OLLAMA_MODEL": "",
        "REFINE_LLM_URL": "",  # 큰 모델(llama-server)을 켜지 않게 — 장면 다듬기 검증은 함수를 바꿔 끼운다
        "LLM_COMFY_ENABLED": False,  # 켜져 있는 ComfyUI의 Gemma를 부르지 않게 (결과가 매번 달라진다)
        "BGM_AUTO": False,  # 배경음악 곡 생성(ComfyUI 음악 모델)을 부르지 않게
        "DEMO_REPLAY_SEC_PER_SHOT": 0,  # 데모 재생의 '만드는 중' 표시를 기다리지 않게
        "COMFYUI_URL": "http://127.0.0.1:9",  # 실제 ComfyUI(/interrupt·/free)에 닿지 않게 — 2026-11-06 실제 생성이 끊겼다
    })
    client = app.test_client()

    check("GET /", client.get("/").status_code == 200)
    check("GET create", client.get("/session/create").status_code == 200)
    home = client.get("/").get_data(as_text=True)
    check("첫 화면: 가운데 입력창 → AI에게 맡기기 화면, 로그인 버튼 없음",
          'id="ask-form"' in home and 'action="/session/new"' in home and 'id="google-login"' not in home
          and 'class="app-side"' not in home)  # 첫 화면은 입력창만 — 왼쪽 대시보드 없음
    check("GET /projects (내 프로젝트 목록)", client.get("/projects").status_code == 200)
    dash = client.get("/projects").get_data(as_text=True)
    sysinfo = client.get("/api/system").get_json()
    check("왼쪽 대시보드: 새 영상·내 프로젝트·할 일·YouTube·GPU 상태",
          'class="app-side"' in dash and "view=todo" in dash and 'id="dash-system"' in dash
          and set(sysinfo) == {"comfyui", "running", "queued"}, sysinfo)
    page = client.get("/session/create", query_string={"idea": "우리 학과 실습실을 보여 주는 홍보 영상", "aspect": "16:9"})
    page = page.get_data(as_text=True)
    check("세부사항 화면: 첫 화면 문장·비율로 미리 채움 (제목·종류 추천)",
          "우리 학과 실습실을 보여 주는 홍보 영상" in page and 'value="16:9" checked' in page and 'value="소개·안내"' in page)
    check("POST create (필수값 누락) → 400", client.post("/session/create", data={"title": ""}).status_code == 400)
    res = client.post("/session/create", data={
        "title": "버스 정류장", "genre": "로맨스",
        "characters": "지우: 20대 후반 여성, 긴 검은 머리, 베이지 코트",
        "synopsis": "비 오는 밤, 지우는 버스 정류장에서 헤어진 연인 민호를 우연히 마주친다.",
        "shots": "A woman in her late 20s with long black hair and a beige coat waits at a rainy bus stop at night\n"
                 "A man in his early 30s in a navy shirt steps off a bus and freezes when he sees her",
    })
    check("기획 저장 → 프로젝트의 ① 기획 탭으로 (아직 제작 전)", res.status_code == 302 and res.headers["Location"].endswith("#plan"))
    with app.app_context():
        sid = PipelineSession.query.first().id
    base = f"/session/{sid}"
    check("GET detail", client.get(base).status_code == 200)

    # 프롬프트 편집 → 버전 2
    with app.app_context():
        prompt = db.session.get(PipelineSession, sid).current_round.prompt_text
    client.post(f"{base}/prompt", data={"prompt_text": prompt.replace("로맨스", "로맨스 스릴러")})
    with app.app_context():
        session = db.session.get(PipelineSession, sid)
        check("프롬프트 편집 → v2 생성", len(session.versions) == 2 and "스릴러" in session.current_round.prompt_text)
        v1 = session.versions[0].id
    res = client.post(f"{base}/versions/{v1}/rollback")
    with app.app_context():
        session = db.session.get(PipelineSession, sid)
        check("rollback → v3, 내용 복원", len(session.versions) == 3 and "스릴러" not in session.current_round.prompt_text)

    print("\n── 1차 ──")
    clusters = run_round(client, app, sid)
    for c in clusters[:6]:
        print(f"     #{c['rank']} {c['cluster_key']} n={c['feedback_count']} P={c['priority_score']} | {c['representative_text'][:50]}")

    with app.app_context():
        session = db.session.get(PipelineSession, sid)
        check("2차 라운드 생성", session.current_round.round_number == 2)
        check("2차 프롬프트에 [버전] 2차", ("[버전]" + chr(10) + "2차") in session.current_round.prompt_text)
    html = client.get(base).get_data(as_text=True)
    stage_keys = ("plan", "produce", "confirm", "publish", "feedback")
    check("단계 탭: 1차·2차 버전 링크 + 기획·제작·확인·공개·의견 탭이 본문 패널과 짝, 지금 단계(기획)부터",
          "round=1" in html and "round=2" in html
          and all(f'data-tab="{k}"' in html and f'data-panel="{k}"' in html for k in stage_keys)
          and 'var fallback = "plan"' in html and 'name="synopsis"' in html)  # 2차는 아직 기획 — 고칠 수 있는 칸
    html1 = client.get(f"{base}?round=1").get_data(as_text=True)
    check("지난 1차도 같은 단계 탭 — 기획은 읽기 전용, 댓글·올릴 영상은 그 버전 것",
          all(f'data-panel="{k}"' in html1 for k in stage_keys) and 'name="synopsis"' not in html1
          and "시청자 댓글" in html1 and "지난 버전입니다" in html1)
    html = client.get(f"{base}/versions").get_data(as_text=True)
    check("다른 화면에서는 단계 탭이 상세 화면 링크", f'href="{base}?round=2#plan"' in html)
    _voice_checks(client, app, sid, base)
    _logo_vector_checks(client, app, sid, base)
    with app.app_context():
        session = db.session.get(PipelineSession, sid)
        check("PromptChange 기록", len(session.versions[-1].changes) > 0)
        approved_technical = sum(1 for c in ClusterResult.query.filter_by(status="approved", scope="technical")
                                 if c.promotes_to_global)
        check("기술 클러스터 → 전역 설정 승격",
              GlobalTechnicalSetting.query.count() == approved_technical, f"{approved_technical}건")
        check("프로토타입 온라인 학습 파일 저장", any(f.startswith("prototypes_") for f in os.listdir(tmp)))

    check("api/clusters — 새 라운드는 비어 있음", client.get(f"{base}/api/clusters").get_json()["clusters"] == [])
    check("GET detail?round=1", client.get(f"{base}?round=1").status_code == 200)

    print("\n── 2차 (재발 추적) ──")
    clusters2 = run_round(client, app, sid)
    check("재발 이슈 감지", any(c["reoccurrence_count"] > 0 for c in clusters2),
          f"{sum(1 for c in clusters2 if c['reoccurrence_count'])}개 클러스터")

    check("GET versions", client.get(f"{base}/versions").status_code == 200)
    with app.app_context():
        vid = db.session.get(PipelineSession, sid).versions[-1].id
    check("GET version diff", client.get(f"{base}/versions/{vid}").status_code == 200)
    check("GET report", client.get(f"{base}/report").status_code == 200)
    check("GET 없는 세션 → 404", client.get("/session/9999").status_code == 404)
    todo = client.get("/projects", query_string={"view": "todo"}).get_data(as_text=True)
    check("할 일: 사람이 눌러야 넘어가는 프로젝트만 (테스트 프로젝트는 prompt_built라 없음), 프로젝트 화면엔 대시보드 없음",
          "지금 할 일이 없습니다" in todo and 'class="app-side"' not in client.get(base).get_data(as_text=True))

    print("\n── 한국어 입력 → 영어 ──")
    _ko_en_checks(client, app)
    _draft_fill_checks(client, app)
    _scene_refine_checks(client, app)
    _feedback_revise_checks(app)
    _plan_change_checks(client, app)
    _auto_motion_checks(app)
    _affected_shot_timeline_checks(app)
    _demo_replay_checks(client, app)

    from tests import video_checks

    video_checks.run(check)

    from tests import auto_checks  # AI 자동 진행 (질문 → 기획 → 생성·점검 → ③ 확인)
    from app.pipeline.video_generator.comfyui import rough_cut as _rc

    _ffmpeg = _rc.resolve_ffmpeg({"FFMPEG_PATH": os.getenv("FFMPEG_PATH", "")})
    if _ffmpeg and os.path.isdir(video_checks.WORKFLOWS_DIR):
        auto_checks.run(check, video_checks._app, _ffmpeg, video_checks.WORKFLOWS_DIR)
    else:
        check("AI 자동 진행 검증에 필요한 ffmpeg·워크플로", False, f"ffmpeg={_ffmpeg}")

    print(f"\n{'모든 검증 통과' if not _failures else f'실패 {len(_failures)}건: ' + ', '.join(_failures)}")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())

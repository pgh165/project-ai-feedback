"""영상 파이프라인 통합 검증 (VIDEO_PIPELINE_INTEGRATION.md 14절 7단계).

run_check.py에서 호출한다. 가짜 ComfyUI 서버와 가짜 상용 API HTTP를 쓰므로
GPU·모델·API 키 없이 돌아가며, 실제 상용 API는 호출하지 않는다.
"""
import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from app import create_app
from app.models import (
    ClusterResult, FAILED_STATUS, Feedback, GenerationJob, Metric, PipelineSession, ShotRender, VideoRound, db,
)
from app.pipeline import approval, jobs, orchestrator
from app.pipeline import storyboard_model as sbm
from app.pipeline.classifier.timestamp import extract_timestamp_sec
from app.pipeline.video_generator.comfyui import rough_cut, workflow
from tests.fake_comfyui import FakeComfyUI, FakeRunwayHTTP, make_sample_mp4

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOWS_DIR = os.path.join(_ROOT, "promo_draft", "workflows")
if not os.path.isdir(WORKFLOWS_DIR):  # 워크스페이스 통합 전 배치 호환
    WORKFLOWS_DIR = os.path.join(os.path.dirname(_ROOT), "ai", "promo_draft", "workflows")
_SHOTS = (
    "A woman in her late 20s with long black hair waits at a rainy bus stop at night\n"
    "A man in his early 30s in a navy shirt steps off a bus and freezes\n"
    "The two stand facing each other under one umbrella in the rain"
)


def _app(tmp, name, **overrides):
    config = {
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///" + os.path.join(tmp, f"{name}.db").replace("\\", "/"),
        "CLASSIFIER_STATE_DIR": tmp,
        "SIMULATION_MODE": True,
        "VIDEO_GENERATOR_ADAPTER": "mock",
        "JOBS_SYNC": True,
        "REQUIRE_VIDEO_APPROVAL": False,
        "ANTHROPIC_API_KEY": "",
        "OLLAMA_MODEL": "",
        "LLM_COMFY_ENABLED": False,  # 실제 ComfyUI의 Gemma를 부르지 않는다
        "REFINE_LLM_URL": "",  # 큰 모델(llama-server)도 켜지 않는다
        "BGM_AUTO": False,  # 배경음악은 _music_checks에서 따로 켜서 본다
        "RUNWAY_API_KEY": "",
        "VIDEO_OUTPUT_DIR": os.path.join(tmp, f"out_{name}"),
        "STORYBOARD_SHOT_SECONDS": 5,
    }
    config.update(overrides)
    return create_app(config)


def _create(client, app, shots=_SHOTS):
    client.post("/session/create", data={
        "title": "버스 정류장", "genre": "로맨스", "characters": "지우: 20대 후반 여성",
        "synopsis": "비 오는 밤, 지우는 버스 정류장에서 헤어진 연인 민호를 마주친다.", "shots": shots})
    with app.app_context():
        session = PipelineSession.query.order_by(PipelineSession.id.desc()).first()
        return session.id, session.current_round.id


def _status(client, round_id):
    return client.get(f"/api/rounds/{round_id}/status").get_json()


def _import_checks(check, tmp, ffmpeg):
    """완성 영상 불러오기: 디졸브·하드 컷 감지 → 장면별 클립으로 나눈 프로젝트(③ 확인)."""
    import io
    import subprocess

    from app.pipeline import video_import

    print("\n── 완성 영상 불러오기 (컷 감지·장면 나누기) ──")
    src = os.path.join(tmp, "import_src.mp4")
    # 움직이는 화면 2.5초 →(0.4초 디졸브)→ 남색 2.5초 →(하드 컷)→ 주황 2초. 기대 컷: 약 2.3초, 4.6초
    subprocess.run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc=size=160x90:rate=24:duration=2.5",
        "-f", "lavfi", "-i", "color=c=navy:size=160x90:rate=24:duration=2.5",
        "-f", "lavfi", "-i", "color=c=orange:size=160x90:rate=24:duration=2",
        "-filter_complex", "[0][1]xfade=transition=fade:duration=0.4:offset=2.1[a];[a][2]concat=n=2:v=1[v]",
        "-map", "[v]", "-pix_fmt", "yuv420p", src], check=True)
    info = rough_cut.probe_media(ffmpeg, src)
    cuts, _curve = video_import.detect_cuts(ffmpeg, src, info["duration"])
    check("컷 감지: 디졸브 + 하드 컷", len(cuts) == 2 and abs(cuts[0] - 2.3) <= 0.25 and abs(cuts[1] - 4.6) <= 0.06,
          cuts)
    check("컷 정리: 정렬·범위 밖 제거·가까운 컷 병합",
          video_import.normalize_cuts([5.0, "1.0", 0.1, 1.2, 9.9], 10.0) == [1.0, 5.0])
    subs = video_import.parse_srt("1\n00:00:00,300 --> 00:00:02,000\n첫 자막\n\n2\n00:00:04,700 --> 00:00:06,000\n끝\n자막\n")
    check("자막(.srt) 읽기 + 장면 배정",
          subs == [(0.3, 2.0, "첫 자막"), (4.7, 6.0, "끝 자막")]
          and video_import.subtitles_by_scene(subs, [(0, 2.3), (2.3, 4.6), (4.6, 6.6)]) == ["첫 자막", "", "끝 자막"])

    # 화면 흐름: 올리기 → 컷 확인 → 프로젝트 만들기 (ComfyUI 없음 → 장면 묘사는 빈 자리)
    app = _app(tmp, "import", COMFYUI_URL="http://127.0.0.1:9", TRANSLATE_ENABLED=False, VOICE_ADAPTER="none")
    client = app.test_client()
    with open(src, "rb") as fh:
        srt = "1\n00:00:00,300 --> 00:00:02,000\n첫 자막\n"
        r = client.post("/session/import/analyze", data={
            "video": (io.BytesIO(fh.read()), "sample.mp4"),
            "subtitles": (io.BytesIO(srt.encode("utf-8")), "sample.srt")}, content_type="multipart/form-data")
    a = r.get_json()
    check("불러오기 분석 API: 컷·자막·화면비", r.status_code == 200 and len(a["cuts"]) == 2
          and a["subtitles"] == [[0.3, 2.0, "첫 자막"]] and a["aspect"] == "16:9", r.status_code)
    check("불러오기 썸네일", client.get(f"{a['frame_url']}?t=1.0").status_code == 200)
    r = client.post("/session/import/create", json={
        "token": a["token"], "title": "불러온 영상", "cuts": a["cuts"], "subtitles": a["subtitles"]})
    with app.app_context():
        session = PipelineSession.query.order_by(PipelineSession.id.desc()).first()
        round_obj = session.current_round
        renders = sorted(round_obj.shot_renders, key=lambda x: x.shot_id)
        sb = round_obj.storyboard_data
        ok_files = all(os.path.exists(x.path) and os.path.exists(x.image_path) for x in renders)
        check("불러오기 → ③ 확인(video_ready) + 장면 3개 클립·첫 프레임",
              r.status_code == 200 and round_obj.status == "video_ready" and len(renders) == 3
              and all(x.status == "done" and x.selected for x in renders) and ok_files,
              f"{r.status_code} {round_obj.status} {len(renders)}")
        lens = [rough_cut.probe_media(ffmpeg, x.path)["duration"] for x in renders]
        check("장면 클립 길이 = 컷 사이 길이", all(abs(l - s["duration"]) < 0.1 for l, s in zip(lens, sb["shots"])),
              lens)
        from app.pipeline import edit_plan as _ep
        check("소리 없는 영상 + .srt → 자막 문장이 [내레이션]으로 (TTS로 읽고 자막도 켬, 올릴 영상 새로 합성)",
              video_import.MISSING_SHOT_TEXT in round_obj.prompt_text and "[내레이션]\n1. 첫 자막" in round_obj.prompt_text
              and sb["shots"][0]["narration"] == "첫 자막" and _ep.round_plan(round_obj)["subtitles"]
              and round_obj.video_path.endswith("rough_cut.mp4"), round_obj.video_path)
        check("소리 없는 영상을 불러오면 장면 소리(LTX 효과음)도 끔 — 다시 만든 장면에만 소리가 생기지 않게",
              _ep.round_plan(round_obj)["scene_sound"] is False and sb["edit"]["scene_sound"] is False)
        from app.pipeline.prompt_builder import extract_shot_durations
        check("다음 버전용 프롬프트에 장면 길이 표시 ([2.30초] …)",
              [round(d, 2) for d in extract_shot_durations(round_obj.prompt_text)]
              == [s["duration"] for s in sb["shots"]], extract_shot_durations(round_obj.prompt_text))
        page = client.get(f"/session/{session.id}").get_data(as_text=True)
        check("상세 화면: 올릴 영상 + 장면별 영상 + 자막", "stage-video" in page and "scene-section" in page
              and "첫 자막" in page)

    voiced = os.path.join(tmp, "import_voiced.mp4")  # 소리가 있는 영상
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", src,
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=7", "-shortest", "-c:v", "copy", "-c:a", "aac", voiced],
                   check=True)
    with open(voiced, "rb") as fh:
        r = client.post("/session/import/analyze", data={
            "video": (io.BytesIO(fh.read()), "voiced.mp4"),
            "subtitles": (io.BytesIO(srt.encode("utf-8")), "voiced.srt")}, content_type="multipart/form-data")
    a = r.get_json()
    client.post("/session/import/create", json={"token": a["token"], "title": "소리 있는 영상", "cuts": a["cuts"],
                                                 "subtitles": a["subtitles"]})
    with app.app_context():
        round_obj = PipelineSession.query.order_by(PipelineSession.id.desc()).first().current_round
        sb = round_obj.storyboard_data
        again = sbm.build_storyboard(round_obj.prompt_text, app.config)  # 다음 버전도 프롬프트에서 다시 만든다
        from app.pipeline import edit_plan as _ep2
        check("소리 있는 영상은 장면 소리를 그대로 둠", _ep2.round_plan(round_obj)["scene_sound"] is True)
        # 장면 소리 끄기: 컷 파일에서 소리 빼기 + 러프컷에도 장면 소리 없음
        import shutil as _sh
        loud = os.path.join(tmp, "loud_clip.mp4")
        _sh.copyfile(voiced, loud)
        rough_cut.strip_audio(ffmpeg, loud)
        muted_out = os.path.join(tmp, "muted_rough.mp4")
        rough_cut.build(ffmpeg, [("s01", voiced)], muted_out, 160, 90, 24, edit={"scene_sound": False})
        vol = subprocess.run([ffmpeg, "-hide_banner", "-i", muted_out, "-af", "volumedetect", "-vn", "-f", "null", "-"],
                             capture_output=True, text=True, errors="replace").stderr
        check("장면 소리 끔: 컷 파일 소리 빼기(영상은 그대로) + 러프컷 장면 소리 무음",
              not rough_cut._has_audio(ffmpeg, loud) and rough_cut.probe_media(ffmpeg, loud) is not None
              and "max_volume: -91" in vol, vol[-160:])
        check("소리 있는 영상 + .srt → [자막](화면 자막만, TTS 없음), 다음 버전에도 자막이 이어짐",
              "[자막]\n1. 첫 자막" in round_obj.prompt_text and "[내레이션]" not in round_obj.prompt_text
              and sb["shots"][0]["subtitle"] == "첫 자막" and not any(x.get("narration") for x in sb["shots"])
              and again["shots"][0].get("subtitle") == "첫 자막", round_obj.prompt_text[-200:])


def _target_checks(check, tmp):
    """의견 반영 위치: 화면·움직임 의견만 장면을 다시 만들고, 한국어 의견 원문은 영상 프롬프트에 넣지 않는다."""
    from types import SimpleNamespace

    from app.pipeline import feedback_target as ft

    print("\n── 의견 반영 위치 (장면을 다시 만들 의견만 다시 만든다) ──")
    cases = {("audio", "음악과 음성설명이 같이 있으면 좋겠어요."): "audio",
             ("content", "학과 설명에대한 내용이 조금 부족한거 같아요. 영상이나 설명이 추가됐으면 좋겠어요"): "content",
             ("visual", "화면이 전환될 때 조금 더 자연스러우면 좋을 것 같습니다."): "edit",
             ("visual", "집을 수 있는 정육면체 물체를 만들어주고 그 물체를 집는 움직임을 표현하면 좋겠어"): "scene",
             ("visual", "로봇팔이 너무 빠르게 움직여요"): "motion",
             ("visual", "2:10쯤 화면이 너무 어두워요"): "scene",
             ("subtitle", "자막이 너무 작아요"): "edit"}
    wrong = {t: ft.suggest(c, t) for (c, t), want in cases.items() if ft.suggest(c, t) != want}
    check("반영 위치 자동 제안 (소리·내용·전환 → 장면 그대로)", not wrong, wrong)

    def cluster(text, target, shots, status="approved"):
        return SimpleNamespace(status=status, target=target, affected_shots=shots, representative_text=text)

    prev = SimpleNamespace(clusters=[
        cluster("음악이 있으면 좋겠어요", "audio", ["s01", "s02", "s03"]),
        cluster("화면 전환이 자연스러웠으면", "edit", ["s01", "s02", "s03"]),
        cluster("정육면체를 집게 해 주세요", "scene", ["s02"]),
        cluster("너무 빨라요", "motion", ["s03"]),
        cluster("반영 안 한 의견", "scene", ["s01"], status="rejected")])
    affected = orchestrator.affected_shot_ids(prev)
    check("소리·편집 의견은 장면 재생성 없음 — 화면(s02)·움직임(s03)만",
          sorted(affected) == ["s02", "s03"] and affected["s03"] == [("너무 빨라요", "motion")], affected)

    board = {"style": "clean", "defaults": {"seed": 1}, "shots": [
        {"id": "s01", "prompt": "A robot arm.", "notes": ["음악이 있으면 좋겠어요", "정육면체를 집게"],
         "notes_en": ["", "the arm grips a red cube"]}]}
    final = sbm.final_prompt(board, board["shots"][0])
    check("영상 프롬프트에 한국어 의견 원문을 넣지 않음 (영어 지시문만)",
          final == "A robot arm, the arm grips a red cube, clean", final)
    board["shots"][0].pop("notes_en")
    check("영어로 바꾸지 못한 의견은 넣지 않음", sbm.final_prompt(board, board["shots"][0]) == "A robot arm, clean")
    check("화면과 무관한 의견은 영어 변환 없이 빠짐",
          ft.to_visual_instruction("배경음악이 너무 커요", {}) == ""
          and ft.to_visual_instruction("brighter lighting", {}) == "brighter lighting")

    # 의견대로 장면 묘사를 고쳐 쓰기 — 지적된 문제("사진이 없어")를 묘사하지 않고 고친 모습으로
    from app.pipeline import comfy_text
    asked = []
    real_generate, real_client = comfy_text.generate, comfy_text.client
    comfy_text.client = lambda config, timeout_sec=None: object()
    ctx = _app(tmp, "notes").app_context()
    ctx.push()
    comfy_text.generate = lambda c, config, system, prompt, image_path=None, max_length=400: (
        asked.append((system, prompt)) or
        "Revised: Close-up of an elderly hand beside an old wooden frame holding a photo of a young woman, low angle")
    try:
        board = {"style": "warm", "defaults": {"seed": 1}, "shots": [
            {"id": "s01", "prompt": "Close-up of an elderly hand in front of an old wooden frame, low angle.",
             "notes": ["액자에 사진이 없어", "배경음악이 커요"]}]}
        import types as _types
        orchestrator.ensure_english_notes(_types.SimpleNamespace(storyboard_json=""), board, {})
        shot = board["shots"][0]
        final = sbm.final_prompt(board, shot)
        check("의견 → 장면 묘사 자체를 고친 모습으로 다시 씀 (꼬리에 붙이지 않음, 소리 의견은 빼고, 문제 묘사 금지 지시)",
              final == "Close-up of an elderly hand beside an old wooden frame holding a photo of a young woman, low angle, warm"
              and "배경음악" not in asked[0][1] and "Never describe the mistake" in asked[0][0]
              and not shot.get("notes_en"), final)
        n_asked = len(asked)
        orchestrator.ensure_english_notes(_types.SimpleNamespace(storyboard_json=""), board, {})
        shot["notes"].append("할머니가 웃고 있었으면")
        orchestrator.ensure_english_notes(_types.SimpleNamespace(storyboard_json=""), board, {})
        check("같은 의견이면 다시 묻지 않고, 의견이 늘면 다시 고쳐 씀", len(asked) == n_asked + 1)
        check("원래 장면 묘사(prompt)는 그대로 — 버전 간 장면 비교(서명)는 바뀌지 않음",
              shot["prompt"].startswith("Close-up of an elderly hand in front of"))
    finally:
        ctx.pop()
        comfy_text.generate, comfy_text.client = real_generate, real_client


def run(check):
    tmp = tempfile.mkdtemp(prefix="ai_video_vcheck_")
    ffmpeg = rough_cut.resolve_ffmpeg({"FFMPEG_PATH": os.getenv("FFMPEG_PATH", "")})

    # ── 1. 순수 함수 ──
    print("\n── 영상 파이프라인: 스토리보드·타임스탬프·상태 머신 ──")
    check("프레임 수 8n+1 올림 (5초×24fps → 121)", sbm.frames_for(5, 24) == 121)
    check("프레임 수 8n+1 (2초×24fps → 49)", sbm.frames_for(2, 24) == 49)
    cases = {"2:10쯤 너무 어두워요": 130.0, "2분 10초에 소리가 끊겨요": 130.0,
             "1:05~1:20 구간이 지루해요": 65.0, "at 0:12 the audio breaks": 12.0,
             "화면 비율 9:16이 좋아요": None, "전반적으로 좋아요": None, "12초쯤 어색해요": 12.0}
    bad = {t: extract_timestamp_sec(t) for t, want in cases.items() if extract_timestamp_sec(t) != want}
    check("타임스탬프 추출 정규식", not bad, bad)
    board = {"style": "s", "negative": "n", "defaults": {"width": 64, "height": 64, "fps": 24, "seed": 1,
             "generator": "local"}, "shots": [{"id": "s01", "prompt": "a", "duration": 5.0},
                                              {"id": "s02", "prompt": "b", "duration": 6.0}]}
    check("타임스탬프 → 장면 매핑", sbm.shot_at(board, 4.9) == "s01" and sbm.shot_at(board, 5.0) == "s02"
          and sbm.shot_at(board, 11.0) is None)

    _import_checks(check, tmp, ffmpeg)
    _target_checks(check, tmp)

    app = _app(tmp, "mock")
    client = app.test_client()
    sid, rid = _create(client, app)

    with app.app_context():
        round_obj = db.session.get(VideoRound, rid)
        try:
            orchestrator.advance(round_obj, "uploaded")
            rejected = False
        except orchestrator.InvalidTransition:
            rejected = True
        check("허용되지 않은 전이 거부 (prompt_built → uploaded)", rejected)

    st = _status(client, rid)
    check("상태 API 9단계 형식",
          len(st["steps"]) == 9 and [s["no"] for s in st["steps"]] == list(range(1, 10))
          and st["steps"][0]["status"] == "done" and st["steps"][1]["status"] == "pending"
          and {"round", "state", "step", "steps", "adapter_used", "shots", "error"} <= set(st))
    client.post(f"/session/{sid}/generate")
    st = _status(client, rid)
    check("mock 생성 → video_ready, 장면 3개 완료",
          st["state"] == "video_ready" and len(st["shots"]) == 3
          and all(s["status"] == "done" for s in st["shots"]), st["state"])
    check("스토리보드 스냅샷 저장",
          len(json.loads(_get_storyboard_json(app, rid))["shots"]) == 3)

    # ── 2. ComfyUI 꺼짐 → mock 폴백 ──
    print("\n── ComfyUI 폴백 · 장면 단위 생성 ──")
    app = _app(tmp, "down", VIDEO_GENERATOR_ADAPTER="comfyui", COMFYUI_URL="http://127.0.0.1:9",
               COMFYUI_WORKFLOWS_DIR=WORKFLOWS_DIR, COMFYUI_AVAILABLE_TIMEOUT_SEC=1)
    client = app.test_client()
    sid, rid = _create(client, app)
    client.post(f"/session/{sid}/generate")
    st = _status(client, rid)
    check("ComfyUI 꺼짐 → mock으로 끝까지 진행 + '대체됨' 표시",
          st["state"] == "video_ready" and st["adapter_used"] == "mock" and st["fallback_to_mock"]
          and "mock" in st["fallback_detail"], f"{st['state']} / {st['fallback_detail'][:60]}")

    if not os.path.isdir(WORKFLOWS_DIR) or not ffmpeg:
        check("ComfyUI 통합 검증에 필요한 워크플로 폴더·ffmpeg", False,
              f"workflows={os.path.isdir(WORKFLOWS_DIR)} ffmpeg={ffmpeg}")
        return

    fake = FakeComfyUI(ffmpeg).start()
    try:
        base = dict(VIDEO_GENERATOR_ADAPTER="comfyui", COMFYUI_URL=fake.url,
                    COMFYUI_WORKFLOWS_DIR=WORKFLOWS_DIR, COMFYUI_POLL_INTERVAL_SEC=0.1,
                    COMFYUI_SHOT_TIMEOUT_SEC=30, FFMPEG_PATH=ffmpeg, STORYBOARD_WIDTH=128,
                    STORYBOARD_HEIGHT=192, STORYBOARD_SHOT_SECONDS=2, COMFYUI_FREE_BETWEEN_SHOTS=True,
                    COMFYUI_IMAGE_FIRST=False,  # .env 값과 무관하게 T2V 경로를 검증 (이미지 먼저는 아래에서 따로)
                    COMFYUI_STAGED_REVIEW=False)  # 단계별 확인 생성도 아래에서 따로 켜서 검증한다
        app = _app(tmp, "comfy", **base)
        client = app.test_client()
        sid, rid = _create(client, app)
        client.post(f"/session/{sid}/generate")
        st = _status(client, rid)
        with app.app_context():
            round_obj = db.session.get(VideoRound, rid)
            renders = ShotRender.query.filter_by(round_id=rid).all()
            video_ok = bool(round_obj.video_path) and os.path.getsize(round_obj.video_path) > 0
            providers = {r.provider for r in renders}
        check("ComfyUI(가짜) 생성 → video_ready + 러프컷 합성",
              st["state"] == "video_ready" and video_ok and st["adapter_used"] == "comfyui"
              and not st["fallback_to_mock"], f"{st['state']} {providers}")
        check("장면 3개 제출 + 장면 사이 /free 호출", len(fake.submitted) == 3 and fake.frees >= 3,
              f"submitted={len(fake.submitted)} frees={fake.frees}")

        mapping = workflow.load_template({"COMFYUI_WORKFLOWS_DIR": WORKFLOWS_DIR,
                                          "COMFYUI_MAPPING_PATH": ""}, "ltx25_t2v")[1]
        first = fake.submitted[0]
        w = first[str(mapping["width"]["node"])]["inputs"][mapping["width"]["input"]]
        h = first[str(mapping["height"]["node"])]["inputs"][mapping["height"]["input"]]
        f = first[str(mapping["frames"]["node"])]["inputs"][mapping["frames"]["input"]]
        p = first[str(mapping["prompt"]["node"])]["inputs"][mapping["prompt"]["input"]]
        check("mapping.yaml 노드에 값 주입 (해상도·프레임 8n+1·프롬프트)",
              (w, h) == (128, 192) and f == "49" and "bus stop" in p and "Cinematic" in p.replace(
                  "cinematic", "Cinematic"), f"{w}x{h} frames={f}")

        # ── 이미지 먼저 (Z-Image → I2V) ──
        print("\n── 이미지 먼저: Z-Image 첫 프레임 → I2V ──")
        before_sub, before_up = len(fake.submitted), fake.uploads
        app = _app(tmp, "imgfirst", **{**base, "COMFYUI_IMAGE_FIRST": True})
        client = app.test_client()
        sid, rid = _create(client, app)
        client.post(f"/session/{sid}/generate")
        st = _status(client, rid)
        new = fake.submitted[before_sub:]
        kinds = ["image" if any(n.get("class_type") == "SaveImage" for n in wf.values()) else "video"
                 for wf in new]
        with app.app_context():
            renders = ShotRender.query.filter_by(round_id=rid).order_by(ShotRender.shot_id).all()
            workflows = {r.workflow for r in renders}
            images_ok = all(r.image_path and os.path.getsize(r.image_path) > 0 for r in renders)
        check("이미지 먼저 → 장면마다 이미지 1회 + 영상 1회 (이미지가 항상 먼저)",
              st["state"] == "video_ready" and kinds == ["image", "video"] * 3, f"{st['state']} {kinds}")
        check("이미지는 업로드되어 I2V 입력이 되고 기록되는 워크플로는 I2V",
              fake.uploads - before_up == 3 and workflows == {"ltx25_i2v"} and images_ok,
              f"uploads={fake.uploads - before_up} workflows={workflows} images_ok={images_ok}")
        img_map = workflow.load_template({"COMFYUI_WORKFLOWS_DIR": WORKFLOWS_DIR,
                                          "COMFYUI_MAPPING_PATH": ""}, "zimage_t2i")[1]
        img_wf = new[0]
        iw = img_wf[str(img_map["width"]["node"])]["inputs"][img_map["width"]["input"]]
        ip = img_wf[str(img_map["prompt"]["node"])]["inputs"][img_map["prompt"]["input"]]
        check("Z-Image 워크플로에 해상도·프롬프트 주입", iw == 128 and "bus stop" in ip, f"w={iw}")

        # 큐에서 사라진 작업은 타임아웃까지 기다리지 않고 바로 실패
        from app.pipeline.video_generator.comfyui.comfy_client import ComfyClient, ComfyError

        cc = ComfyClient(fake.url, timeout_sec=60, poll_interval_sec=0.1)
        cc.missing_grace_sec = 1.0
        fake.drop_markers = {"DROPME"}
        pid = cc.submit({"1": {"class_type": "CLIPTextEncode", "inputs": {"text": "DROPME"}}})
        t0, err = time.time(), ""
        try:
            cc.wait(pid)
        except ComfyError as exc:
            err = str(exc)
        fake.drop_markers = set()
        waited = time.time() - t0
        check("ComfyUI 큐에서 사라진 작업은 타임아웃(60초)까지 기다리지 않고 바로 실패",
              "사라졌습니다" in err and waited < 10, f"{waited:.1f}s {err[:40]}")
        pid = cc.submit({"1": {"class_type": "CLIPTextEncode", "inputs": {"text": "ok"}}})
        check("정상 작업은 대기 중으로 보여 실패 처리되지 않음", bool(cc.wait(pid)))

        # ── 단계별 확인: 이미지 → 컷 → 러프컷 ──
        print("\n── 단계별 확인: 이미지 확인 → 컷 확인 → 러프컷 ──")

        def _kinds(workflows):  # 글 생성(의견 고쳐 쓰기·번역, TextGenerate)은 빼고 이미지·영상만
            out = []
            for wf in workflows:
                types = {n.get("class_type", "") for n in wf.values()}
                if any("TextGenerate" in t for t in types) and "SaveVideo" not in types:
                    continue
                out.append("image" if "SaveImage" in types else "video")
            return out

        def _shot(st, shot_id):
            return next(x for x in st["shots"] if x["id"] == shot_id)

        base_sub = len(fake.submitted)
        app = _app(tmp, "staged", **{**base, "COMFYUI_STAGED_REVIEW": True})
        client = app.test_client()
        sid, rid = _create(client, app)
        client.post(f"/session/{sid}/generate")
        st = _status(client, rid)
        check("이미지 단계: 이미지 3장만 만들고 확인 대기(images_ready), 러프컷은 아직 없음",
              st["state"] == "images_ready" and st["review"] == "images"
              and _kinds(fake.submitted[base_sub:]) == ["image"] * 3 and not st["video_url"]
              and all(x["image_url"] and not x["clip_url"] for x in st["shots"]),
              f"{st['state']} {_kinds(fake.submitted[base_sub:])}")
        check("② 단계가 '이미지 확인 대기'(waiting)로 표시됨",
              st["steps"][1]["status"] == "waiting" and "이미지" in st["steps"][1]["detail"])

        seed_before = _shot(st, "s02")["seed"]
        r = client.post(f"/api/rounds/{rid}/images/regenerate",
                        json={"shot_id": "s02", "note": "brighter lighting"})
        st = _status(client, rid)
        s02 = _shot(st, "s02")
        last_prompt = next(iter(
            n["inputs"].get("text") for n in fake.submitted[-1].values()
            if n.get("class_type") == "CLIPTextEncode"), "")
        check("이미지 다시 뽑기: s02만 새 시드·피드백 반영해 1장 추가, 다시 확인 대기",
              r.status_code == 200 and st["state"] == "images_ready" and s02["seed"] != seed_before
              and "brighter lighting" in s02["notes"] and "brighter lighting" in last_prompt
              and len(fake.submitted) - base_sub == 4 and _shot(st, "s01")["seed"] == _shot(st, "s03")["seed"],
              f"{r.status_code} {st['state']} seed {seed_before}->{s02['seed']}")
        with app.app_context():
            discarded = ShotRender.query.filter_by(round_id=rid, shot_id="s02", status="discarded").count()
        check("이전 s02 이미지는 폐기(discarded)되고 새 이미지가 선택됨", discarded == 1)

        before_up = fake.uploads
        r = client.post(f"/api/rounds/{rid}/images/approve")
        st = _status(client, rid)
        check("이미지 승인 → 컷 영상 3개 생성, 컷 확인 대기(clips_ready), 러프컷은 아직 없음",
              r.status_code == 200 and st["state"] == "clips_ready" and st["review"] == "clips"
              and _kinds(fake.submitted[base_sub:]) == ["image"] * 4 + ["video"] * 3
              and not st["video_url"] and all(x["clip_url"] and x["status"] == "done" for x in st["shots"])
              and fake.uploads - before_up == 3, f"{r.status_code} {st['state']}")
        check("이미지 확인 단계가 아니면 승인은 409", client.post(f"/api/rounds/{rid}/images/approve").status_code == 409)

        r = client.post(f"/api/rounds/{rid}/clips/regenerate", json={"shot_id": "s01"})
        st = _status(client, rid)
        with app.app_context():
            s01_versions = sorted(x.version for x in ShotRender.query.filter_by(
                round_id=rid, shot_id="s01", status="done"))
        check("컷 다시 만들기: s01만 같은 이미지로 새 버전 1개 추가, 다시 확인 대기",
              r.status_code == 200 and st["state"] == "clips_ready" and s01_versions == [1, 2]
              and len(_shot(st, "s01")["versions"]) == 2 and not st["video_url"], f"{s01_versions}")

        r = client.post(f"/api/rounds/{rid}/rough-cut")
        st = _status(client, rid)
        with app.app_context():
            rough = db.session.get(VideoRound, rid).video_path
        check("러프컷 만들기 → ③ video_ready + 러프컷 파일 생성",
              r.status_code == 200 and st["state"] == "video_ready" and bool(rough)
              and os.path.exists(rough) and bool(st["video_url"]), f"{r.status_code} {st['state']}")
        # ③ 확인(업로드 전)에서도 컷을 다시 만들 수 있다 → 다시 ② 컷 확인으로 돌아가 올릴 영상을 새로 만든다
        with app.app_context():
            old_image = next(x.image_path for x in ShotRender.query.filter_by(round_id=rid, shot_id="s01")
                             if x.selected)
        sub0 = len(fake.submitted)
        r = client.post(f"/api/rounds/{rid}/clips/regenerate", json={"shot_id": "s01", "note": "더 밝게"})
        st = _status(client, rid)
        s01 = next(s for s in st["shots"] if s["id"] == "s01")
        check("③ 확인에서 생김새 의견('더 밝게')으로 컷 다시 만들기 → 이미지만 새로 뽑고 이미지 확인(images_ready)에서 멈춤",
              r.status_code == 200 and st["state"] == "images_ready" and _kinds(fake.submitted[sub0:]) == ["image"]
              and "더 밝게" in s01["notes"], f"{r.status_code} {st['state']} {_kinds(fake.submitted[sub0:])}")
        r = client.post(f"/api/rounds/{rid}/images/approve")
        st = _status(client, rid)
        s01 = next(s for s in st["shots"] if s["id"] == "s01")
        with app.app_context():
            new = next(x for x in ShotRender.query.filter_by(round_id=rid, shot_id="s01") if x.selected)
        check("이미지를 승인하면 그 장면의 컷만 새 이미지로 만들고 컷 확인으로 (다른 장면은 그대로)",
              r.status_code == 200 and st["state"] == "clips_ready" and _kinds(fake.submitted[sub0:]) == ["image", "video"]
              and new.status == "done" and new.image_path and new.image_path != old_image
              and len(s01["versions"]) >= 2, _kinds(fake.submitted[sub0:]))
        # 버전 지우기: 지금 쓰는 새 버전을 지우면 남은 가장 최근 버전이 선택되고, 파일도 지워진다 (공유하지 않는 파일만)
        new_id, new_clip, new_img = new.id, new.path, new.image_path
        r = client.post(f"/api/shots/{new_id}/delete")
        with app.app_context():
            left = [x for x in ShotRender.query.filter_by(round_id=rid, shot_id="s01") if x.status in ("done", "reused")]
            chosen = [x.version for x in left if x.selected]
        check("컷 버전 지우기: 행·파일 삭제, 남은 것 중 가장 최근 버전이 선택됨",
              r.status_code == 200 and db_get_none(app, new_id) and not os.path.exists(new_clip)
              and not os.path.exists(new_img) and chosen == [max(x.version for x in left)], (r.status_code, chosen))
        with app.app_context():
            others = [x.id for x in ShotRender.query.filter_by(round_id=rid, shot_id="s03") if x.status == "done"]
        r = client.post(f"/api/shots/{others[0]}/delete")
        check("장면의 마지막 컷 버전은 지울 수 없음 (409)", len(others) == 1 and r.status_code == 409, len(others))
        sub0 = len(fake.submitted)
        r = client.post(f"/api/rounds/{rid}/clips/regenerate", json={"shot_id": "s02", "note": "카메라를 더 천천히 움직여"})
        check("움직임 의견으로 컷 다시 만들기 → 같은 이미지로 컷만",
              r.status_code == 200 and _kinds(fake.submitted[sub0:]) == ["video"]
              and _status(client, rid)["state"] == "clips_ready", _kinds(fake.submitted[sub0:]))
        # 이 순간을 끝으로 다시 만들기: 그 컷의 첫 프레임 = 시작, 고른 순간의 화면 = 끝 (처음·끝 프레임 워크플로)
        with app.app_context():
            base_r = next(x for x in ShotRender.query.filter_by(round_id=rid, shot_id="s02") if x.selected)
            base_id, base_img = base_r.id, base_r.image_path
        r_early = client.post(f"/api/shots/{base_id}/end-frame", json={"at": 0.2})
        sub0 = len(fake.submitted)
        r = client.post(f"/api/shots/{base_id}/end-frame", json={"at": 0.6})
        last_wf = fake.submitted[-1] if len(fake.submitted) > sub0 else {}
        guides = sorted(n["inputs"].get("frame_idx") for n in last_wf.values() if n.get("class_type") == "LTXVAddGuide")
        loads = [n["inputs"].get("image") for n in last_wf.values() if n.get("class_type") == "LoadImage"]
        with app.app_context():
            new_r = next(x for x in ShotRender.query.filter_by(round_id=rid, shot_id="s02") if x.selected)
            new_info = (new_r.id != base_id, new_r.status, new_r.workflow, new_r.image_path == base_img,
                        bool(new_r.end_image_path) and os.path.exists(new_r.end_image_path))
        check("이 순간을 끝으로 다시 만들기: 첫 프레임 0번·고른 화면 마지막(-1) 고정, 새 버전(같은 첫 프레임), 컷 확인으로",
              r_early.status_code == 409 and r.status_code == 200 and guides == [-1, 0] and len(loads) == 2
              and new_info == (True, "done", "ltx25_flf2v", True, True)
              and _status(client, rid)["state"] == "clips_ready", (r.status_code, guides, new_info))
        # 모호한 의견은 422로 방향을 묻고 아무것도 만들지 않는다
        sub0 = len(fake.submitted)
        r = client.post(f"/api/rounds/{rid}/clips/regenerate", json={"shot_id": "s03", "note": "로봇의 그리퍼 모양을 바꿔"})
        check("모호한 의견('그리퍼 모양을 바꿔')으로 다시 만들기 → 422 + 무엇으로 바꿀지 안내, 작업 없음",
              r.status_code == 422 and "무엇으로" in r.get_json().get("vague", "") and len(fake.submitted) == sub0
              and _status(client, rid)["state"] == "clips_ready", r.status_code)
        # 쌓인 의견 지우기: s01에는 '더 밝게'가 있다
        before_notes = next(s for s in _status(client, rid)["shots"] if s["id"] == "s01")["notes"]
        r = client.post(f"/api/rounds/{rid}/notes/delete", json={"shot_id": "s01", "index": 0})
        after_notes = next(s for s in _status(client, rid)["shots"] if s["id"] == "s01")["notes"]
        bad = client.post(f"/api/rounds/{rid}/notes/delete", json={"shot_id": "s01", "index": 9})
        check("장면 의견 지우기: 하나만 빠지고 나머지는 그대로, 없는 번호는 409",
              r.status_code == 200 and after_notes == before_notes[1:] and bad.status_code == 409,
              (before_notes, after_notes))
        app.config["TRANSLATE_ENABLED"] = False  # 화면 번역이 뒤에서 가짜 ComfyUI에 글 생성을 보내 다음 검증의 제출 수가 달라진다
        html = client.get(f"/session/{sid}").get_data(as_text=True)
        check("장면 카드에 '피드백대로 고쳐 쓴 묘사'를 보여 줌 (고쳐 쓴 경우)",
              "피드백대로 고쳐 쓴 묘사" in html or not any(
                  s.get("prompt_fixed") for s in json.loads(_get_storyboard_json(app, rid))["shots"]))
        r = client.post(f"/api/rounds/{rid}/rough-cut")
        check("올릴 영상 다시 만들기 → ③ video_ready", _status(client, rid)["state"] == "video_ready")
        with app.app_context():
            db.session.get(VideoRound, rid).status = "uploaded"
            db.session.commit()
        check("공개 후에는 컷 다시 만들기 불가(409)",
              client.post(f"/api/rounds/{rid}/clips/regenerate", json={"shot_id": "s01"}).status_code == 409)
        with app.app_context():
            db.session.get(VideoRound, rid).status = "video_ready"
            db.session.commit()

        # 실패한 장면만 재시도
        fake.fail_markers = {"FAILME"}
        before = len(fake.submitted)
        app = _app(tmp, "retry", **base)
        client = app.test_client()
        shots = _SHOTS.replace("steps off a bus", "FAILME steps off a bus")
        sid, rid = _create(client, app, shots)
        client.post(f"/session/{sid}/generate")
        st = _status(client, rid)
        failed_ids = [s["id"] for s in st["shots"] if s["status"] == "failed"]
        check("일부 장면 실패 → generation_failed (② 빨강)",
              st["state"] == FAILED_STATUS and failed_ids == ["s02"]
              and st["steps"][1]["status"] == "failed" and st["error"], f"{st['state']} {failed_ids}")
        fake.fail_markers = set()
        mid = len(fake.submitted)
        res = client.post(f"/api/rounds/{rid}/retry")
        st = _status(client, rid)
        with app.app_context():
            versions = {r.shot_id: sorted(x.version for x in ShotRender.query.filter_by(
                round_id=rid, shot_id=r.shot_id)) for r in ShotRender.query.filter_by(round_id=rid)}
        check("재시도는 실패한 장면만 다시 생성",
              res.status_code == 200 and st["state"] == "video_ready"
              and len(fake.submitted) - mid == 1 and versions["s01"] == [1] and versions["s02"] == [1, 2],
              f"추가 제출 {len(fake.submitted) - mid}건 {[sorted({n.get('class_type') for n in wf.values()})[:4] for wf in fake.submitted[mid:]]}, versions={versions}")
        check("정상 상태에서 retry → 409", client.post(f"/api/rounds/{rid}/retry").status_code == 409)

        # ── 상용 API 에스컬레이션 ──
        print("\n── 상용 API 품질 에스컬레이션 ──")
        sample = make_sample_mp4(ffmpeg, Path(tmp) / "api_sample.mp4", size="64x96")
        fake_http = FakeRunwayHTTP(sample)
        app = _app(tmp, "escalate", **base, REQUIRE_VIDEO_APPROVAL=True, RUNWAY_API_KEY="test-key",
                   _COMMERCIAL_HTTP=fake_http, COMMERCIAL_COST_PER_SEC_USD=0.12,
                   COMMERCIAL_BUDGET_USD=30, RUNWAY_POLL_INTERVAL_SEC=0.01)
        client = app.test_client()
        sid, rid = _create(client, app)
        client.post(f"/session/{sid}/generate")
        st = _status(client, rid)
        check("③ 영상 확인 대기(waiting) — 자동 업로드 안 함",
              st["state"] == "video_ready" and st["steps"][2]["status"] == "waiting"
              and st["commercial"]["available"], st["steps"][2]["status"])
        api_calls = len(fake_http.requests)
        r = client.post(f"/api/rounds/{rid}/escalate", json={
            "shot_ids": ["s02"], "flags": {"s02": ["face_hands"]}, "confirm_cost_usd": 0.01})
        check("확인 비용 불일치 → 409 cost_mismatch",
              r.status_code == 409 and r.get_json()["code"] == "cost_mismatch"
              and abs(r.get_json()["server_cost_usd"] - 0.24) < 1e-6 and len(fake_http.requests) == api_calls)
        app.config["COMMERCIAL_BUDGET_USD"] = 0.1
        r = client.post(f"/api/rounds/{rid}/escalate", json={"shot_ids": ["s02"], "confirm_cost_usd": 0.24})
        check("예산 초과 → 409 budget_exceeded, API 미호출",
              r.status_code == 409 and r.get_json()["code"] == "budget_exceeded"
              and len(fake_http.requests) == api_calls)
        app.config["COMMERCIAL_BUDGET_USD"] = 30
        r = client.post(f"/api/rounds/{rid}/escalate", json={
            "shot_ids": ["s02"], "flags": {"s02": ["face_hands"]}, "confirm_cost_usd": 0.24})
        st = _status(client, rid)
        by_id = {s["id"]: s for s in st["shots"]}
        check("선택 장면만 API로 재생성, 나머지는 로컬 유지",
              r.status_code == 200 and st["state"] == "video_ready"
              and by_id["s02"]["generator"] == "api" and by_id["s02"]["provider"] == "runway"
              and by_id["s01"]["generator"] == "local" and by_id["s03"]["generator"] == "local"
              and len(fake_http.requests) == api_calls + 1, f"{by_id['s02']['provider']}")
        check("비용 기록·누적 (s02 2초 × $0.12 = $0.24)",
              abs(by_id["s02"]["cost_usd"] - 0.24) < 1e-6 and abs(st["commercial_spent_usd"] - 0.24) < 1e-6
              and st["adapter_used"] == "mixed", f"{st['commercial_spent_usd']} {st['adapter_used']}")
        with app.app_context():
            local_render = ShotRender.query.filter_by(
                round_id=rid, shot_id="s02", generator="local").first()
            flags = local_render.quality_flags
            metric = Metric.query.filter_by(round_id=rid).first()
            video_path = db.session.get(VideoRound, rid).video_path
            local_id = local_render.id
        check("품질 부족 사유 기록 + 지표(api_shots/cost)",
              flags == ["face_hands"] and metric.api_shots == 1 and abs(metric.api_cost_usd - 0.24) < 1e-6)
        check("러프컷이 다시 합성됨", bool(video_path) and os.path.getsize(video_path) > 0)
        with app.app_context():
            jobs_before = GenerationJob.query.filter_by(round_id=rid).count()
        check("로컬 버전 다시 선택 가능 (/select)",
              client.post(f"/api/shots/{local_id}/select").status_code == 200
              and {s["id"]: s for s in _status(client, rid)["shots"]}["s02"]["generator"] == "local")
        with app.app_context():
            new_jobs = [json.loads(j.payload_json or "{}") for j in
                        GenerationJob.query.filter_by(round_id=rid).order_by(GenerationJob.id).all()[jobs_before:]]
        check("③에서 버전을 고르면 올릴 영상은 요청 안이 아니라 작업 큐(러프컷 작업)에서 다시 합성",
              [p.get("stage") for p in new_jobs] == ["rough"] and _status(client, rid)["state"] == "video_ready",
              new_jobs)
        # 합성이 기다리는 중이면 또 고를 때 합성을 더 걸지 않는다 (기다리는 합성이 마지막 선택으로 만든다)
        with app.app_context():
            rnd = db.session.get(VideoRound, rid)
            orchestrator.advance(rnd, "generating")
            waiting = GenerationJob(kind="generate_video", round_id=rid, payload_json=json.dumps({"stage": "rough"}),
                                    status="queued")
            db.session.add(waiting)
            db.session.commit()
            count = GenerationJob.query.filter_by(round_id=rid).count()
            other = next(r for r in ShotRender.query.filter_by(round_id=rid, shot_id="s02", status="done")
                         if r.generator != "local")
            other_id, waiting_id = other.id, waiting.id
        r = client.post(f"/api/shots/{other_id}/select")
        with app.app_context():
            same_count = GenerationJob.query.filter_by(round_id=rid).count() == count
            chosen = db.session.get(ShotRender, other_id).selected
            db.session.delete(db.session.get(GenerationJob, waiting_id))
            orchestrator.advance(db.session.get(VideoRound, rid), "video_ready")
        check("다시 합성하는 중(대기)에도 버전을 바꿀 수 있고, 합성을 또 걸지 않음",
              r.status_code == 200 and same_count and chosen, r.status_code)
        client.post(f"/api/shots/{local_id}/select")
        r = client.post(f"/api/rounds/{rid}/approve-video", json={})
        check("업로드 진행 → ④ uploaded",
              r.status_code == 200 and _status(client, rid)["state"] == "uploaded")

        # API 키 없음: 버튼 비활성화 + 요청 거부 (mock으로 몰래 대체하지 않음)
        app = _app(tmp, "nokey", **base, REQUIRE_VIDEO_APPROVAL=True)
        client = app.test_client()
        sid, rid = _create(client, app)
        client.post(f"/session/{sid}/generate")
        st = _status(client, rid)
        r = client.post(f"/api/rounds/{rid}/escalate", json={"shot_ids": ["s01"], "confirm_cost_usd": 0.24})
        check("API 키 없음 → 재생성 불가 표시 + 409",
              st["commercial"]["available"] is False and r.status_code == 409
              and r.get_json()["code"] == "commercial_unavailable")
    finally:
        fake.stop()

    # ── 3. 부분 재생성 ──
    print("\n── 장면 단위 부분 재생성 ──")
    app = _app(tmp, "partial")
    client = app.test_client()
    sid, rid = _create(client, app)
    client.post(f"/session/{sid}/generate")
    with app.app_context():
        round1 = db.session.get(VideoRound, rid)
        storyboard = round1.storyboard_data
        cluster = ClusterResult(round_id=rid, cluster_key="visual-1", topic_category="visual",
                                representative_text="2번째 장면이 너무 어두워요", feedback_count=2,
                                status="approved",
                                affected_shots_json=json.dumps([sbm.shot_at(storyboard, 6.0)]))
        db.session.add(cluster)
        round1.status = "pending_review"
        db.session.commit()
        next_round, _src = approval.apply_approved_clusters(round1, app.config)
        db.session.commit()
        next_id = next_round.id
    client.post(f"/session/{sid}/generate")
    st = _status(client, next_id)
    with app.app_context():
        rows = {r.shot_id: r for r in ShotRender.query.filter_by(round_id=next_id).all()}
        metric = Metric.query.filter_by(round_id=next_id).first()
        board2 = db.session.get(VideoRound, next_id).storyboard_data
    check("지적된 장면(s02)만 재생성, 나머지는 reused",
          st["state"] == "video_ready" and rows["s01"].status == "reused" and rows["s03"].status == "reused"
          and rows["s02"].status == "done" and rows["s01"].reused_from_id is not None,
          {k: v.status for k, v in rows.items()})
    check("재생성 장면에 피드백 요지 note + 새 시드",
          board2["shots"][1]["notes"] == ["2번째 장면이 너무 어두워요"]
          and board2["shots"][1]["seed"] != board2["shots"][0]["seed"])
    check("부분 재생성 지표 (재사용 2 / 재생성 1)",
          metric.shots_reused == 2 and metric.shots_regenerated == 1, (metric.shots_reused,
                                                                      metric.shots_regenerated))

    if ffmpeg and os.path.isdir(WORKFLOWS_DIR):
        _edit_checks(check, tmp, ffmpeg)
        _decorate_checks(check, tmp, ffmpeg)
        _music_checks(check, tmp, ffmpeg)
    _refine_checks(check, tmp)
    _youtube_checks(check, tmp)

    # ── 4. 작업 큐(워커 스레드) · 재시작 복구 ──
    print("\n── 작업 큐 · 재시작 복구 ──")
    app = _app(tmp, "queue", JOBS_SYNC=False, MOCK_DELAY_SEC=0.4)
    client = app.test_client()
    sid, rid = _create(client, app)
    client.post(f"/session/{sid}/generate")
    first_state = _status(client, rid)["state"]
    done = jobs.wait_idle(app, timeout=30)
    st = _status(client, rid)
    check("비동기 생성: 요청은 즉시 반환(generating) → 워커가 완료(video_ready)",
          first_state == "generating" and done and st["state"] == "video_ready",
          f"{first_state} → {st['state']}")
    with app.app_context():
        stale_round = VideoRound(session_id=sid, round_number=99, status="generating")
        db.session.add(stale_round)
        db.session.flush()
        db.session.add(GenerationJob(kind="generate_video", round_id=stale_round.id, status="running"))
        db.session.commit()
        stale_id = stale_round.id
    n = jobs.recover_interrupted(app)
    with app.app_context():
        job = GenerationJob.query.filter_by(round_id=stale_id).first()
        state = db.session.get(VideoRound, stale_id).status
    check("재시작 시 running 작업 → failed(interrupted), 라운드는 재시도 가능",
          n == 1 and job.status == "failed" and job.error == "interrupted" and state == FAILED_STATUS)

    from tests import custom_video_checks

    custom_video_checks.run(check, tmp, _app)

    shutil.rmtree(tmp, ignore_errors=True)


def _edit_checks(check, tmp, ffmpeg):
    """소리·편집 의견 → 장면은 모두 재사용하고 올릴 영상(러프컷)만 편집 설정으로 다시 만든다."""
    from app.pipeline import edit_plan

    print("\n── 편집 설정: 장면을 다시 만들지 않는 의견 반영 ──")
    rules = {
        "배경음악이 너무 시끄러워요": [("sound_gain", 0.7)],
        "배경음악 좀 더 크게 해주세요": [("sound_gain", 1.3)],
        "음악 때문에 내레이션이 안 들려요": [("narration_gain", 1.3), ("sound_gain", 0.7)],
        "해설이 너무 빨라요": [("narration_tempo", 0.88)],
        "장면 전환이 너무 갑작스러워요": [("transition", "dissolve")],
        "페이드로 넘어가면 좋겠어요": [("transition", "fade")],
        "자막이 너무 작아요": [("subtitles", True)],  # 자막은 YouTube 자막 — 크기는 보는 사람이 정한다
        "2번 장면이 너무 길어서 지루해요": [("keep", 0.8)],
        "장면이 너무 빨리 넘어가요": [("speed", 0.8)],
    }
    ids = ["s01", "s02", "s03"]
    wrong = {t: [(c["key"], c["value"]) for c in edit_plan.suggest_changes(t, "edit", ids, ids)]
             for t in rules}
    wrong = {t: got for t, got in wrong.items() if got != rules[t]}
    check("편집·소리 의견 → 편집 설정 제안 (크기·빠르기 방향 포함)", not wrong, wrong)
    check("화면·움직임 의견은 편집 설정을 바꾸지 않음",
          edit_plan.suggest_changes("배경이 너무 어두워요", "scene", ids, ids) == [])

    fake = FakeComfyUI(ffmpeg).start()
    try:
        app = _app(tmp, "edit", VIDEO_GENERATOR_ADAPTER="comfyui", COMFYUI_URL=fake.url,
                   COMFYUI_WORKFLOWS_DIR=WORKFLOWS_DIR, COMFYUI_POLL_INTERVAL_SEC=0.1,
                   COMFYUI_SHOT_TIMEOUT_SEC=30, FFMPEG_PATH=ffmpeg, STORYBOARD_WIDTH=128,
                   STORYBOARD_HEIGHT=192, STORYBOARD_SHOT_SECONDS=2, COMFYUI_IMAGE_FIRST=False,
                   COMFYUI_STAGED_REVIEW=False, VOICE_ADAPTER="none", CLIP_TRIM_HEAD_SEC=0, CLIP_TRIM_TAIL_SEC=0)
        client = app.test_client()
        sid, rid = _create(client, app)
        with app.app_context():  # 1차는 바로 넘김(컷)으로 — 의견으로 디졸브가 되는지 본다
            orchestrator.set_round_edit(db.session.get(VideoRound, rid), app.config, transition="cut")
        client.post(f"/session/{sid}/generate")
        with app.app_context():
            round1 = db.session.get(VideoRound, rid)
            board = round1.storyboard_data
            len1 = rough_cut.probe_media(ffmpeg, round1.video_path)["duration"]
            s02 = next(r for r in round1.shot_renders if r.shot_id == "s02" and r.selected)
            s02_len = rough_cut.probe_media(ffmpeg, s02.path)["duration"]
            s02_path = s02.path
            for key, text, shots in (("audio-1", "배경음악이 너무 시끄러워요", ids),
                                     ("pacing-1", "장면 전환이 너무 갑작스러워요", ids),
                                     ("pacing-2", "2번 장면이 너무 길어서 지루해요", ["s02"])):
                db.session.add(ClusterResult(round_id=rid, cluster_key=key, topic_category=key.split("-")[0],
                                             representative_text=text, feedback_count=2, status="approved",
                                             affected_shots_json=json.dumps(shots)))
            round1.status = "pending_review"
            db.session.commit()
        submitted = len(fake.submitted)

        page = client.get(f"/session/{sid}/review").get_data(as_text=True)
        check("검토 화면: 의견별 바뀌는 편집 + 다음 버전 편집 설정",
              "바뀌는 편집" in page and "다음 버전 편집 설정" in page and "디졸브" in page
              and "배경 소리 70%" in page and "2번 장면: 길이 80%" in page)
        client.post(f"/session/{sid}/edit-plan", data={
            "transition": "fade", "transition_sec": "0.4", "sound_gain": "100", "narration_gain": "100",
            "narration_tempo": "100", "order": "3, 1, 2", "keep_s01": "100", "speed_s01": "100",
            "keep_s02": "100", "speed_s02": "100", "keep_s03": "100", "speed_s03": "100"})
        with app.app_context():
            manual = edit_plan.next_plan(db.session.get(VideoRound, rid))
        check("편집 설정 직접 고치기 → 저장", manual[1] and manual[0]["transition"] == "fade"
              and manual[0]["order"] == ["s03", "s01", "s02"], manual)
        client.post(f"/session/{sid}/edit-plan", data={"reset": "1"})
        with app.app_context():
            round1 = db.session.get(VideoRound, rid)
            check("자동 제안으로 되돌리기", not round1.next_edit_json)
            next_round, _src = approval.apply_approved_clusters(round1, app.config)
            db.session.commit()
            next_id = next_round.id
            plan2 = edit_plan.loads(next_round.edit_json)
        check("다음 버전에 편집 설정 전달 (디졸브 · 배경 소리 70% · 2번 장면 80%)",
              plan2["transition"] == "dissolve" and plan2["sound_gain"] == 0.7
              and plan2["shots"] == {"s02": {"keep": 0.8}}, plan2)

        client.post(f"/session/{sid}/generate")
        st = _status(client, next_id)
        with app.app_context():
            round2 = db.session.get(VideoRound, next_id)
            statuses = {r.shot_id: r.status for r in round2.shot_renders}
            len2 = rough_cut.probe_media(ffmpeg, round2.video_path)["duration"] if round2.video_path else 0
            board2 = round2.storyboard_data
        check("소리·편집 의견만 → 장면 모두 재사용 (ComfyUI 제출 없음)",
              st["state"] == "video_ready" and set(statuses.values()) == {"reused"}
              and len(fake.submitted) == submitted, (statuses, len(fake.submitted) - submitted))
        overlap = min(0.4, s02_len * 0.8 / 2)  # 디졸브 겹침(기본 0.4초)은 가장 짧은 장면의 절반을 넘지 않는다
        expect = len1 - s02_len * 0.2 - 2 * overlap
        check("올릴 영상에 편집 반영 (2번 장면 20% 잘림 + 디졸브 겹침만큼 짧아짐)",
              abs(len2 - expect) < 0.08, f"1차 {len1:.2f}s → 2차 {len2:.2f}s (기대 {expect:.2f}s)")
        ranges = sbm.shot_ranges(board2)
        check("타임스탬프 → 장면 매핑이 편집된 시각을 따름",
              abs(ranges[-1][2] - (sbm.total_duration(board))
                  + board["shots"][1]["duration"] * 0.2 + 0.8) < 0.01 and st["edit"]["lines"],
              (ranges, st["edit"]))
        page = client.get(f"/session/{sid}").get_data(as_text=True)
        check("상세 화면에 적용된 편집 표시", "편집 반영" in page and "배경 소리 70%" in page)

        # 기본 전환: 디졸브 + AI 컷의 앞뒤 다듬기 (불러온 원본 장면은 자르지 않음)
        from tests.fake_comfyui import make_sample_mp4 as _sample
        clip = _sample(ffmpeg, os.path.join(tmp, "t_src.mp4"), size="128x192", seconds=1, with_audio=True)
        s02_len = rough_cut.probe_media(ffmpeg, clip)["duration"]
        plain = rough_cut.build(ffmpeg, [("s01", clip), ("s02", clip)], os.path.join(tmp, "t_cut.mp4"), 128, 192, 12,
                                edit={"transition": "cut"})
        smooth = rough_cut.build(ffmpeg, [("s01", clip), ("s02", clip)], os.path.join(tmp, "t_dis.mp4"), 128, 192, 12,
                                 trims={"s01": (0.15, 0.1), "s02": (0.15, 0.1)})
        a = rough_cut.probe_media(ffmpeg, plain)["duration"]
        b = rough_cut.probe_media(ffmpeg, smooth)["duration"]
        expect = a - 2 * 0.25 - min(0.4, (s02_len - 0.25) / 2)
        check("기본 전환 = 디졸브 0.4초 + AI 컷 앞 0.15초·뒤 0.1초 다듬기",
              edit_plan.normalize({})["transition"] == "dissolve" and abs(b - expect) < 0.12, f"컷 {a:.2f}s → {b:.2f}s (기대 {expect:.2f}s)")
    finally:
        fake.stop()


def _pixel(ffmpeg, video, t, x, y):
    """영상의 t초 프레임에서 (x, y) 픽셀의 (R, G, B)."""
    import subprocess
    raw = subprocess.run([ffmpeg, "-loglevel", "error", "-ss", f"{t}", "-i", str(video), "-frames:v", "1",
                          "-vf", f"format=rgb24,crop=2:2:{x}:{y}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True, check=True).stdout
    return tuple(raw[:3])


def _decorate_checks(check, tmp, ffmpeg):
    """로고·자막: 장면을 다시 만들지 않고 올릴 영상만 다시 합성한다."""
    import subprocess

    from app.pipeline import overlay

    print("\n── 올릴 영상 꾸미기: 로고·자막 ──")
    fake = FakeComfyUI(ffmpeg).start()
    try:
        app = _app(tmp, "deco", VIDEO_GENERATOR_ADAPTER="comfyui", COMFYUI_URL=fake.url,
                   COMFYUI_WORKFLOWS_DIR=WORKFLOWS_DIR, COMFYUI_POLL_INTERVAL_SEC=0.1,
                   COMFYUI_SHOT_TIMEOUT_SEC=30, FFMPEG_PATH=ffmpeg, STORYBOARD_WIDTH=256,
                   STORYBOARD_HEIGHT=448, STORYBOARD_SHOT_SECONDS=2, COMFYUI_IMAGE_FIRST=False,
                   COMFYUI_STAGED_REVIEW=False, VOICE_ADAPTER="none")
        client = app.test_client()
        client.post("/session/create", data={
            "title": "꾸미기", "genre": "홍보", "characters": "", "synopsis": "로고와 자막 시험",
            "shots": _SHOTS, "narration": "1. 첫 장면입니다.\n2. 둘째 장면 해설입니다.\n3. 마지막 장면입니다."})
        with app.app_context():
            session = PipelineSession.query.order_by(PipelineSession.id.desc()).first()
            sid, rid = session.id, session.current_round.id
        client.post(f"/session/{sid}/generate")
        submitted = len(fake.submitted)
        with app.app_context():
            first_path = db.session.get(VideoRound, rid).video_path
        before = _pixel(ffmpeg, first_path, 0.5, 256 - 20, 30)

        logo_png = os.path.join(tmp, "logo_src.png")  # 빨간 로고 (불투명)
        subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:s=200x100",
                        "-frames:v", "1", logo_png], check=True)
        page = client.get(f"/session/{sid}").get_data(as_text=True)
        check("③ 확인: '올릴 영상 꾸미기' 칸 + 로고 미리보기(영상 크기·첫 장면 이미지)", "올릴 영상 꾸미기" in page and "저장하고 올릴 영상 다시 만들기" in page
              and 'id="logo-preview"' in page and 'data-w="' in page)
        with open(logo_png, "rb") as fh:
            client.post(f"/session/{sid}/decorate", data={
                "logo_settings": "1", "logo_file": (fh, "logo.png"), "logo_position": "top-right",
                "logo_size": "30", "logo_opacity": "100", "logo_show": "all", "logo_first_sec": "3",
                "subtitle_settings": "1", "subtitles": "on", "subtitle_size": "large",
                "subtitle_position": "bottom", "rebuild": "1"}, content_type="multipart/form-data")
        st = _status(client, rid)
        with app.app_context():
            round_obj = db.session.get(VideoRound, rid)
            session = db.session.get(PipelineSession, sid)
            logo = overlay.load_logo(session)
            board = round_obj.storyboard_data
            video = round_obj.video_path
            srt = Path(video).parent / "subtitles.srt"
        check("로고 저장 (프로젝트 설정) + 자막 켜기 (이 버전 편집 설정)",
              logo["enabled"] and logo["file"] == "logo.png" and board.get("edit", {}).get("subtitles") is True)
        check("올릴 영상만 다시 합성 → ③ 복귀, 장면은 다시 만들지 않음",
              st["state"] == "video_ready" and len(fake.submitted) == submitted, (st["state"], len(fake.submitted) - submitted))
        after = _pixel(ffmpeg, video, 0.5, 256 - 20, 30)
        check("오른쪽 위에 로고가 얹힘 (픽셀 색이 빨강으로)",
              after[0] > 200 and after[1] < 80 and after[2] < 80 and before != after, (before, after))
        text = srt.read_text(encoding="utf-8") if srt.exists() else ""
        check("자막은 YouTube 자막 파일(.srt): 내레이션 3문장, 시각 형식 00:00:00,000",
              text.count(" --> ") == 3 and "둘째 장면 해설입니다." in text and "00:00:00," in text, text[-300:])
        low_before = _pixel(ffmpeg, first_path, 1.0, 128, 448 - 70)
        low_after = _pixel(ffmpeg, video, 1.0, 128, 448 - 70)
        check("자막을 영상에 굽지 않음 (화면 아래 픽셀이 자막 넣기 전과 같음)",
              max(abs(a - b) for a, b in zip(low_before, low_after)) < 40, (low_before, low_after))
        page = client.get(f"/session/{sid}").get_data(as_text=True)
        got = client.get(f"/round/{rid}/captions.srt")
        check("자막 파일 받기 링크 + 내려받기", "자막 파일(.srt) 받기" in page and got.status_code == 200
              and "둘째 장면 해설입니다." in got.get_data(as_text=True))
        with app.app_context():
            prompts = [r.prompt or "" for r in ShotRender.query.filter_by(round_id=rid)]
        check("영상 모델 프롬프트에 자막 문장이 들어가지 않음", prompts and all("장면입니다" not in p for p in prompts))

        # 타임라인: 합성할 때 쓴 시간표를 남기고, 끌어서 정한 자막·로고 구간으로 다시 합성한다
        with app.app_context():
            tl = json.loads(db.session.get(VideoRound, rid).timeline_json or "{}")
        page = client.get(f"/session/{sid}").get_data(as_text=True)
        check("타임라인: 장면 3개·자막 3개·로고(처음부터 끝까지) 시간표 + 화면에 타임라인 칸",
              len(tl.get("scenes", [])) == 3 and len(tl.get("cues", [])) == 3 and tl.get("logo") == [0.0, tl.get("total")]
              and 'id="timeline"' in page and 'name="subtitle_times"' in page, tl)
        total = tl["total"]  # 시험용 컷은 짧다 — 구간을 영상 길이에 맞춰 잡는다
        l_start, l_end = round(total * 0.5, 2), round(total * 0.85, 2)
        client.post(f"/session/{sid}/decorate", data={
            "timeline_settings": "1", "logo_start": str(l_start), "logo_end": str(l_end),
            "subtitle_times": json.dumps({"s02": [0.2, 1.2]}), "rebuild": "1"})
        with app.app_context():
            round_obj = db.session.get(VideoRound, rid)
            logo = overlay.load_logo(db.session.get(PipelineSession, sid))
            tl = json.loads(round_obj.timeline_json or "{}")
            video = round_obj.video_path
        s02 = [c for c in tl.get("cues", []) if c["shot"] == "s02"]
        early = _pixel(ffmpeg, video, round(total * 0.2, 2), 256 - 20, 30)
        inside = _pixel(ffmpeg, video, round(total * 0.7, 2), 256 - 20, 30)
        check("타임라인에서 끈 로고 구간 → '정한 구간' 모드, 그 구간에만 로고 (앞쪽엔 없고 구간 안엔 있음)",
              logo["show"] == "range" and (logo["start_sec"], logo["end_sec"]) == (l_start, l_end)
              and tl.get("logo") == [l_start, l_end] and inside[0] > 200 and inside[1] < 80 and early != inside,
              (logo, early, inside))
        check("타임라인에서 옮긴 자막 구간 → 그 장면 자막이 정한 시각에 (장면 경계를 넘어도)",
              s02 and s02[0]["start"] == 0.2 and s02[-1]["end"] == 1.2
              and round_obj.storyboard_data["edit"]["subtitle_times"] == {"s02": [0.2, 1.2]}, s02)

        client.post(f"/session/{sid}/upload")  # 공개 (시뮬레이션)
        client.post(f"/session/{sid}/decorate", data={"subtitle_settings": "1", "subtitle_size": "small"})
        with app.app_context():
            round_obj = db.session.get(VideoRound, rid)
            size_after = (round_obj.storyboard_data.get("edit") or {}).get("subtitle_size")
        check("공개 후에는 이 버전의 자막 설정이 바뀌지 않음 (다음 버전은 검토 화면에서)",
              round_obj.status == "uploaded" and size_after == "large", size_after)
    finally:
        fake.stop()


def _band_level(ffmpeg, path, start, dur, freq):
    """start~start+dur 구간에서 freq 근처 소리의 평균 음량(dB)."""
    import subprocess
    err = subprocess.run([ffmpeg, "-hide_banner", "-ss", f"{start}", "-t", f"{dur}", "-i", str(path), "-af",
                          f"bandpass=f={freq}:width_type=q:w=8,volumedetect", "-f", "null", "-"],
                         capture_output=True, text=True, errors="replace").stderr
    line = next((l for l in err.splitlines() if "mean_volume" in l), "mean_volume: -99 dB")
    return float(line.split(":")[-1].replace("dB", "").strip())


def _music_checks(check, tmp, ffmpeg):
    """배경음악: 영상 전체에 한 곡, 내레이션 때 낮아짐, 같은 곡 재사용, 다음 버전 이어짐, '다른 곡으로'."""
    import subprocess

    from app.pipeline import bgm, edit_plan, music

    print("\n── 배경음악 ──")
    work = Path(tmp) / "music_unit"
    work.mkdir(exist_ok=True)

    def tone(path, freq, sec, start=0.0):
        src = f"sine=frequency={freq}:duration={sec}"
        af = ["-af", f"adelay={int(start * 1000)}:all=1,apad=whole_dur=8"] if start else []
        subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi", "-i", src] + af + [str(path)], check=True)
        return path

    video = work / "v.mp4"  # 8초, 소리 없음
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=gray:s=128x128:d=8:r=24",
                    "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "8", "-c:v", "libx264", "-pix_fmt",
                    "yuv420p", "-c:a", "aac", str(video)], check=True)
    nar = tone(work / "nar.wav", 1000, 2.5, start=3.0)       # 3~5.5초에 말
    song = tone(work / "song.wav", 300, 10)                   # 음악 = 300Hz
    out = work / "mixed.mp4"
    rough_cut._mix_with_music(ffmpeg, video, nar, {"path": str(song), "volume": 0.5}, out, 8.0, 1.0, 0.35)
    quiet = _band_level(ffmpeg, out, 3.5, 1.5, 300)
    loud = _band_level(ffmpeg, out, 1.0, 1.5, 300)
    check("내레이션이 나오는 동안 배경음악이 낮아짐 (6dB 이상)", loud - quiet >= 6, f"말 없을 때 {loud} / 말할 때 {quiet} dB")
    check("배경음악은 영상 길이에 맞춰 잘림", abs(rough_cut.probe_media(ffmpeg, out)["duration"] - 8.0) < 0.2)

    wrong = {t: [c["value"] for c in edit_plan.suggest_changes(t, "audio", ["s01"], ["s01"]) if c["key"] == "music"]
             for t in ("배경음악이 영상 분위기랑 안 맞아요", "배경음악이 없어서 심심해요", "음악은 빼 주세요")}
    check("의견 → 배경음악 다른 곡으로 / 넣기 / 빼기",
          wrong == {"배경음악이 영상 분위기랑 안 맞아요": ["reroll"], "배경음악이 없어서 심심해요": ["on"],
                    "음악은 빼 주세요": ["off"]}, wrong)

    # 고른 구간 고르기: 조용한 도입(0~4초) → 고른 본곡(4~14초) → 거의 무음(14~16초) → 본곡(16~24초)
    parts = [("sine=frequency=300:duration=4", "volume=0.02"), ("sine=frequency=300:duration=10", "volume=0.5"),
             ("sine=frequency=300:duration=2", "volume=0.001"), ("sine=frequency=300:duration=8", "volume=0.5")]
    pieces = []
    for n, (src, vol) in enumerate(parts):
        piece = work / f"piece{n}.wav"
        subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi", "-i", src, "-af", vol, str(piece)], check=True)
        pieces.append(piece)
    (work / "pieces.txt").write_text("".join(f"file '{p.as_posix()}'\n" for p in pieces), encoding="utf-8")
    bumpy = work / "bumpy.wav"
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(work / "pieces.txt"),
                    "-c", "copy", str(bumpy)], check=True)
    pick = bgm.steady_offset({"FFMPEG_PATH": ffmpeg}, bumpy, 8.0)
    check("배경음악: 곡에서 소리 크기가 고른 구간을 골라 씀 (조용한 도입·끊긴 곳을 피함)",
          4.0 <= pick <= 6.0, pick)
    shifted = work / "shifted.mp4"
    rough_cut._mix_with_music(ffmpeg, video, None, {"path": str(bumpy), "volume": 0.5, "offset": 4.0}, shifted, 8.0, 1.0, 1.0)
    check("고른 구간부터 섞음 (처음부터 본곡 크기)", _band_level(ffmpeg, shifted, 1.0, 1.5, 300) > -30,
          _band_level(ffmpeg, shifted, 1.0, 1.5, 300))

    calls = []
    original = music.generate

    def fake_generate(config, caption, seconds, out_path, seed=None, lyrics=None, progress=None):
        calls.append((caption, round(seconds, 1), seed))
        tone(Path(out_path), 300, seconds)
        return Path(out_path), seed

    music.generate = fake_generate
    fake = FakeComfyUI(ffmpeg).start()
    try:
        app = _app(tmp, "music", VIDEO_GENERATOR_ADAPTER="comfyui", COMFYUI_URL=fake.url,
                   COMFYUI_WORKFLOWS_DIR=WORKFLOWS_DIR, COMFYUI_POLL_INTERVAL_SEC=0.1,
                   COMFYUI_SHOT_TIMEOUT_SEC=30, FFMPEG_PATH=ffmpeg, STORYBOARD_WIDTH=128,
                   STORYBOARD_HEIGHT=192, STORYBOARD_SHOT_SECONDS=2, COMFYUI_IMAGE_FIRST=False,
                   COMFYUI_STAGED_REVIEW=False, VOICE_ADAPTER="none")
        client = app.test_client()
        sid, rid = _create(client, app)
        client.post(f"/session/{sid}/generate")
        page = client.get(f"/session/{sid}").get_data(as_text=True)
        check("꾸미기 칸에 배경음악 설정", "배경음악 넣기" in page and "영상에 맞게 제안받기" in page)
        client.post(f"/session/{sid}/decorate", data={"music_settings": "1", "music_suggest": "1"})
        with app.app_context():
            suggested = bgm.load(db.session.get(VideoRound, rid))["caption"]
        check("곡 설명 제안 (LLM 없으면 영상 종류에 맞는 기본 설명)", "no vocals" in suggested and not calls, suggested)
        form = {"music_settings": "1", "music_enabled": "on", "music_caption": "calm piano, no vocals",
                "music_volume": "60", "rebuild": "1"}
        client.post(f"/session/{sid}/decorate", data=form)
        with app.app_context():
            r1 = db.session.get(VideoRound, rid)
            m1 = bgm.load(r1)
            level = _band_level(ffmpeg, r1.video_path, 0.5, 2.0, 300)
            desc = orchestrator.youtube_description(r1)
        check("배경음악 켜고 다시 만들기 → 한 곡 생성, 영상에 섞임",
              len(calls) == 1 and m1["file"] and level > -60 and _status(client, rid)["state"] == "video_ready",
              (calls, level))
        client.post(f"/session/{sid}/decorate", data=dict(form, music_volume="40"))
        check("크기만 바꿔 다시 만들면 같은 곡 재사용 (새로 만들지 않음)", len(calls) == 1, calls)
        check("YouTube 설명에 배경음악 출처 표기 (MiniMax-Music3)", bgm.CREDIT in desc)

        with app.app_context():
            round1 = db.session.get(VideoRound, rid)
            db.session.add(ClusterResult(round_id=rid, cluster_key="audio-1", topic_category="audio",
                                         representative_text="배경음악이 영상 분위기랑 안 맞아요", feedback_count=3,
                                         status="approved", affected_shots_json=json.dumps(["s01", "s02", "s03"])))
            round1.status = "pending_review"
            db.session.commit()
            next_round, _src = approval.apply_approved_clusters(round1, app.config)
            db.session.commit()
            next_id = next_round.id
            m2 = bgm.load(next_round)
            plan2 = edit_plan.loads(next_round.edit_json)
        check("'분위기랑 안 맞아요' → 다음 버전은 다른 곡 (시드 바뀜·곡 설명 다시 제안), 편집 설정에 남지 않음",
              m2["enabled"] and m2["seed"] != m1["seed"] and not m2["file"] and not m2["caption"]
              and plan2["music"] == "", (m1["seed"], m2))
        client.post(f"/session/{sid}/generate")
        with app.app_context():
            r2 = db.session.get(VideoRound, next_id)
            statuses = {r.shot_id: r.status for r in r2.shot_renders}
        check("다음 버전: 장면은 모두 재사용하고 새 곡만 만듦",
              set(statuses.values()) == {"reused"} and len(calls) == 2 and calls[1][2] != calls[0][2], (statuses, calls))
    finally:
        music.generate = original
        fake.stop()


def _refine_checks(check, tmp):
    """LLM은 [영상 설명]·[내레이션]만 다듬고, 형식이 틀리면 버린다 (샷 목록은 절대 바꾸지 않는다)."""
    from app.pipeline import llm, prompt_updater
    from app.pipeline.prompt_builder import parse_sections

    print("\n── 프롬프트 다듬기 (LLM 출력 검사) ──")
    doc = ("[프로젝트]\n시험\n\n[영상 종류]\n홍보\n\n[버전]\n1차\n\n[등장 요소]\n\n[이전 버전 요약]\n(첫 버전)\n\n"
           "[영상 설명]\n로봇팔을 보여 주는 영상\n\n[샷 목록]\n1. [4초 시드 42] A robot arm\n2. A small car\n\n"
           "[내레이션]\n1. MCU가 로봇팔을 움직입니다.\n2. 자율주행차가 달립니다.\n\n[영상 스타일]\nclean\n\n"
           "[시청자 반영 사항]\n(아직 없음)\n\n[연출 지침]\n(아직 없음)")
    content = [{"topic_category": "content", "text": "학과 소개가 부족해요", "count": 2}]
    cfg = {"STORYBOARD_SHOT_SECONDS": 5, "ANTHROPIC_API_KEY": "", "OLLAMA_MODEL": "", "LLM_COMFY_ENABLED": True}
    original = llm.generate
    outputs = {
        "good": ("[영상 설명]\n로봇팔과 자율주행차로 학과의 연구를 소개합니다.\n\n[내레이션]\n"
                 "1. MCU가 로봇팔을 정확하게 움직입니다.\n2. 우리 학과의 자율주행차가 스스로 달립니다.", "gemma"),
        "extra_line": ("[영상 설명]\n학과를 소개합니다.\n\n[내레이션]\n1. 하나.\n2. 둘.\n3. 셋.", "gemma"),
        "english": ("[영상 설명]\n학과를 소개합니다.\n\n[내레이션]\n1. MCU moves the robot arm now.\n2. 둘째.", "gemma"),
        "shots": ("[샷 목록]\n1. A new shot with text logo\n\n[영상 설명]\n학과 소개 영상", "gemma"),
    }
    results = {}
    try:
        for name, out in outputs.items():
            llm.generate = lambda system, user, config, _o=out, **kw: _o
            text, source = prompt_updater.update_prompt(doc, content, 2, cfg)
            results[name] = (parse_sections(text), source)
        llm.generate = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("LLM을 부르면 안 됨"))
        no_content_text, no_content_source = prompt_updater.update_prompt(
            doc, [{"topic_category": "audio", "text": "음악이 커요", "count": 1}], 2, cfg)
    finally:
        llm.generate = original
    old = parse_sections(doc)
    good, src = results["good"]
    check("좋은 출력: 영상 설명·내레이션 반영, 샷 목록(길이·시드 표시 포함) 그대로",
          src == "gemma" and "학과의 연구" in good["영상 설명"] and "스스로 달립니다" in good["내레이션"]
          and good["샷 목록"] == old["샷 목록"] and "학과 소개가 부족해요" in good["시청자 반영 사항"])
    check("내레이션 줄 수가 다르면 내레이션은 버리고 설명만", results["extra_line"][0]["내레이션"] == old["내레이션"]
          and results["extra_line"][0]["영상 설명"] == "학과를 소개합니다.")
    check("새 영어 문장이 섞인 내레이션은 버림 (원래 있던 MCU는 허용)", results["english"][0]["내레이션"] == old["내레이션"])
    check("LLM이 샷 목록을 바꿔 보내도 무시", results["shots"][0]["샷 목록"] == old["샷 목록"])
    check("내용 의견이 없으면 LLM을 부르지 않음 (템플릿)", no_content_source == "template"
          and parse_sections(no_content_text)["샷 목록"] == old["샷 목록"])


class _FakeResponse:
    def __init__(self, status_code, body=None, headers=None):
        self.status_code, self._body, self.headers = status_code, body, headers or {}
        self.text = json.dumps(body) if body is not None else ""

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class FakeGoogleHTTP:
    """Google OAuth·YouTube 업로드 흉내. 실제 Google에는 아무것도 보내지 않는다."""

    def __init__(self):
        self.metadata, self.chunks, self.received = None, [], 0
        self.flaky, self.quota = True, False
        self.calls, self.captions = [], None

    def post(self, url, data=None, params=None, headers=None, timeout=None):
        from app.pipeline import youtube_upload as yu
        self.calls.append(("POST", url))
        if url == yu.TOKEN_URL:
            if data.get("grant_type") == "authorization_code":
                ok = data.get("code") == "good-code" and len(data.get("code_verifier", "")) >= 43
                return _FakeResponse(200 if ok else 400, {"access_token": "at-1", "refresh_token": "rt-1",
                                                          "expires_in": 3600} if ok else {"error": "invalid_grant"})
            return _FakeResponse(200, {"access_token": "at-2", "expires_in": 3600})
        if url == yu.UPLOAD_URL:
            if self.quota:
                return _FakeResponse(403, {"error": {"message": "quota", "errors": [{"reason": "quotaExceeded"}]}})
            self.metadata = json.loads(data.decode("utf-8"))
            self.total = int(headers["X-Upload-Content-Length"])
            return _FakeResponse(200, {}, {"Location": "https://upload.fake/s1"})
        if url == yu.REVOKE_URL:
            return _FakeResponse(200, {})
        if url == yu.CAPTIONS_URL:
            self.captions = data
            return _FakeResponse(200, {"id": "cap-1"})
        return _FakeResponse(404, {})

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("GET", url))
        return _FakeResponse(200, {"items": [{"snippet": {"title": "테스트 채널"}}]})

    def put(self, url, headers=None, data=None, timeout=None):
        self.calls.append(("PUT", url))
        rng = headers["Content-Range"]
        if rng.startswith("bytes */"):  # 어디까지 받았나
            return _FakeResponse(308, None, {"Range": f"bytes=0-{self.received - 1}"} if self.received else {})
        start = int(rng.split(" ")[1].split("-")[0])
        if self.flaky and self.chunks:  # 두 번째 조각에서 한 번 서버 오류
            self.flaky = False
            return _FakeResponse(503, {"error": {"message": "backend"}})
        if start != self.received:
            return _FakeResponse(400, {"error": {"message": f"bad offset {start}"}})
        self.chunks.append(len(data))
        self.received += len(data)
        if self.received >= self.total:
            return _FakeResponse(200, {"id": "yt-abc123"})
        return _FakeResponse(308, None, {"Range": f"bytes=0-{self.received - 1}"})


def _youtube_checks(check, tmp):
    from urllib.parse import parse_qs, urlparse

    print("\n── YouTube 업로드 (가짜 Google 서버) ──")
    secrets_file = os.path.join(tmp, "client_secret.json")
    Path(secrets_file).write_text(json.dumps({"web": {"client_id": "cid", "client_secret": "csecret"}}), encoding="utf-8")
    token_file = os.path.join(tmp, "yt_token.json")
    fake = FakeGoogleHTTP()
    app = _app(tmp, "youtube", SIMULATION_MODE=False, YOUTUBE_CLIENT_SECRETS=secrets_file,
               YOUTUBE_TOKEN_FILE=token_file, _YOUTUBE_HTTP=fake, YOUTUBE_UPLOAD_CHUNK_MB=1,
               YOUTUBE_PRIVACY="private")
    from app.pipeline import youtube_upload as yu
    try:
        client = app.test_client()
        sid, rid = _create(client, app)
        client.post(f"/session/{sid}/generate")
        video = os.path.join(tmp, "upload_me.mp4")
        Path(video).write_bytes(os.urandom(int(2.5 * 1024 * 1024)))
        with app.app_context():
            round_obj = db.session.get(VideoRound, rid)
            round_obj.video_path = video
            db.session.commit()
        page = client.get(f"/session/{sid}").get_data(as_text=True)
        check("계정 연결 전: 'YouTube 계정 연결' 버튼, 업로드 시도 없음 (자동 업로드 안 함)",
              "YouTube 계정 연결" in page and not any(u == yu.UPLOAD_URL for _m, u in fake.calls))

        r = client.get(f"/youtube/connect?next=/session/{sid}")
        q = parse_qs(urlparse(r.headers["Location"]).query)
        check("Google 로그인으로 이동 (업로드 권한·오프라인·PKCE)",
              r.status_code == 302 and q["client_id"] == ["cid"] and q["access_type"] == ["offline"]
              and "youtube.upload" in q["scope"][0] and "youtube.force-ssl" in q["scope"][0]
              and q["code_challenge_method"] == ["S256"]
              and q["redirect_uri"][0].endswith("/youtube/callback"))
        bad = client.get("/youtube/callback?state=wrong&code=good-code")
        check("state가 다르면 연결 거부", bad.status_code == 302 and not os.path.exists(token_file))
        client.get(f"/youtube/connect?next=/session/{sid}")  # 위에서 세션 값이 지워졌으므로 다시
        with client.session_transaction() as s:
            state = s["yt_oauth"]["state"]
        r = client.get(f"/youtube/callback?state={state}&code=good-code")
        token = json.loads(Path(token_file).read_text(encoding="utf-8"))
        check("콜백 → 토큰 저장 + 채널 이름", r.status_code == 302 and r.headers["Location"].endswith(f"/session/{sid}")
              and token["refresh_token"] == "rt-1" and token["channel_title"] == "테스트 채널")
        page = client.get(f"/session/{sid}").get_data(as_text=True)
        check("연결 후: 제목·공개 범위·'YouTube에 올리기'", "YouTube에 올리기" in page and "테스트 채널" in page
              and "버스 정류장 (1차)" in page)

        fake.quota = True
        client.post(f"/session/{sid}/youtube-upload", data={"privacy": "unlisted", "title": "버스 정류장 1차"})
        with app.app_context():
            round_obj = db.session.get(VideoRound, rid)
            state1, msg1 = round_obj.status, round_obj.video_job_message
        check("할당량 초과 → ③에 머물고 사유 표시", state1 == "video_ready" and "할당량" in msg1, msg1)

        fake.quota = False
        client.post(f"/session/{sid}/decorate", data={"subtitle_settings": "1", "subtitles": "on"})  # YouTube 자막 켜기
        Path(video).with_name("subtitles.srt").write_text("1\n00:00:00,200 --> 00:00:01,500\n첫 장면입니다.\n",
                                                          encoding="utf-8")
        client.post(f"/session/{sid}/youtube-upload", data={"privacy": "unlisted", "title": "버스 정류장 1차"})
        st = _status(client, rid)
        with app.app_context():
            round_obj = db.session.get(VideoRound, rid)
            vid, approved = round_obj.youtube_video_id, round_obj.video_approved
        size = os.path.getsize(video)
        check("이어 올리기: 1MB 조각으로 빠짐없이, 서버 오류 1번은 재시도 후 이어서",
              fake.received == size and fake.chunks[:2] == [1024 * 1024, 1024 * 1024] and not fake.flaky,
              f"chunks={fake.chunks} received={fake.received}/{size}")
        meta = fake.metadata or {}
        check("메타데이터: 제목·공개 범위·AI 생성 표시",
              meta.get("snippet", {}).get("title") == "버스 정류장 1차"
              and meta.get("status", {}).get("privacyStatus") == "unlisted"
              and meta.get("status", {}).get("containsSyntheticMedia") is True, meta)
        check("업로드 완료 → ④ uploaded + 영상 ID 저장", st["state"] == "uploaded" and vid == "yt-abc123" and approved,
              (st["state"], vid))
        cap = (fake.captions or b"").decode("utf-8", "replace")
        with app.app_context():
            done_msg = db.session.get(VideoRound, rid).video_job_message
        check("영상을 올린 뒤 한국어 자막(.srt)도 그 영상에 올림",
              '"videoId": "yt-abc123"' in cap and '"language": "ko"' in cap and "첫 장면입니다." in cap
              and "자막도 올렸습니다" in done_msg, (cap[:200], done_msg))
        page = client.get(f"/session/{sid}").get_data(as_text=True)
        check("공개 후 YouTube 영상 링크 표시 + (댓글 모으기 전) 확인 단계로 되돌리기 버튼",
              "watch?v=yt-abc123" in page and "확인 단계로 되돌리기" in page)
        client.post(f"/session/{sid}/unpublish")
        with app.app_context():
            back = db.session.get(VideoRound, rid)
            back_state, back_vid, back_msg = back.status, back.youtube_video_id, back.video_job_message
            renders_kept = len(back.shot_renders)
        check("공개 취소 → ③ 확인 (장면은 그대로, 예전 YouTube 영상은 지우지 않고 안내)",
              back_state == "video_ready" and back_vid == "" and "yt-abc123" in back_msg and renders_kept > 0
              and ("DELETE", "https://www.googleapis.com/youtube/v3/videos") not in fake.calls, (back_state, back_msg))
        fake.received, fake.chunks = 0, []  # 가짜 서버: 새 업로드
        client.post(f"/session/{sid}/youtube-upload", data={"privacy": "unlisted", "title": "버스 정류장 1차"})
        with app.app_context():
            again = db.session.get(VideoRound, rid)
            again_state = again.status
            db.session.add(Feedback(comment_text="좋아요", round_id=rid))
            db.session.commit()
        blocked = client.post(f"/session/{sid}/unpublish", follow_redirects=True).get_data(as_text=True)
        with app.app_context():
            still = db.session.get(VideoRound, rid).status
        check("다시 올리기 → ④, 댓글을 모은 뒤에는 되돌리지 않음",
              again_state == "uploaded" and still == "uploaded" and "되돌릴 수 없습니다" in blocked, (again_state, still))
        r = client.post("/youtube/disconnect", data={"next": f"/session/{sid}"})
        check("연결 끊기 → 토큰 삭제 + Google 철회 요청",
              not os.path.exists(token_file) and ("POST", yu.REVOKE_URL) in fake.calls)

        app_sim = _app(tmp, "youtube_sim", SIMULATION_MODE=True)
        with app_sim.app_context():
            sim_client = app_sim.test_client()
            sid2, rid2 = _create(sim_client, app_sim)
            sim_client.post(f"/session/{sid2}/generate")
            try:
                orchestrator.start_youtube_upload(app_sim, db.session.get(VideoRound, rid2))
                blocked = False
            except orchestrator.PipelineError:
                blocked = True
        check("시뮬레이션 모드에서는 YouTube에 올리지 않음", blocked)
    finally:
        shutil.rmtree(os.path.join(tmp, "out_youtube"), ignore_errors=True)


def db_get_none(app, render_id):
    with app.app_context():
        return db.session.get(ShotRender, render_id) is None


def _get_storyboard_json(app, round_id):
    with app.app_context():
        return db.session.get(VideoRound, round_id).storyboard_json


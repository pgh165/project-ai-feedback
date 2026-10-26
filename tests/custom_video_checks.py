"""사용자가 원하는 영상(드라마 전용이 아님) 검증: 문서 형식 · 화면 비율 · 장면 길이 · 버전 갱신."""
from app.models import PipelineSession, VideoRound, db
from app.pipeline.prompt_builder import extract_shots, parse_sections
from app.pipeline.prompt_updater import update_prompt


def run(check, tmp, make_app):
    print("\n── 사용자 지정 영상: 화면 비율 · 장면 길이 · 문서 형식 ──")
    app = make_app(tmp, "custom", STORYBOARD_WIDTH=128, STORYBOARD_HEIGHT=192)
    client = app.test_client()
    client.post("/session/create", data={
        "title": "이어폰 소개", "genre": "제품 소개", "characters": "은색 무선 이어폰",
        "synopsis": "은색 무선 이어폰이 흰 받침대 위에서 천천히 회전하다 케이스가 열린다.",
        "shots": "A silver wireless earbud rotates slowly on a white pedestal\n"
                 "The charging case opens and a small light turns on",
        "aspect_ratio": "16:9", "shot_seconds": "3"})
    with app.app_context():
        session = PipelineSession.query.first()
        rid, sid = session.current_round.id, session.id
        prompt = session.current_round.prompt_text
        aspect, secs = session.aspect_ratio, session.shot_seconds

    sections = parse_sections(prompt)
    check("프롬프트 문서가 영상 중심 섹션명 사용",
          {"프로젝트", "영상 종류", "버전", "등장 요소", "영상 설명", "샷 목록"} <= set(sections)
          and "시리즈" not in sections and sections["버전"] == "1차", list(sections))

    old_doc = "[시리즈]\nX\n\n[이번 화 줄거리]\n설명\n\n[샷 목록]\n1. a shot\n"
    check("예전(드라마용) 섹션명 문서도 읽힘",
          parse_sections(old_doc).get("영상 설명") == "설명" and extract_shots(old_doc) == ["a shot"])

    client.post(f"/session/{sid}/generate")
    with app.app_context():
        board = db.session.get(VideoRound, rid).storyboard_data
    d = board["defaults"]
    check("화면 비율 16:9 → 가로 해상도, 장면 길이 3초 적용",
          aspect == "16:9" and secs == 3.0 and d["width"] > d["height"]
          and all(x["duration"] == 3.0 for x in board["shots"]), f"{d['width']}x{d['height']}")

    cfg = dict(app.config, ANTHROPIC_API_KEY="", OLLAMA_MODEL="", LLM_COMFY_ENABLED=False)
    new_text, source = update_prompt(
        prompt, [{"topic_category": "visual", "text": "조명이 어두워요", "count": 2}], 2, cfg)
    old, new = parse_sections(prompt), parse_sections(new_text)
    check("2차 템플릿 갱신: 영상 설명·샷 목록 유지, 이전 버전 요약·연출 지침에 기록",
          source == "template" and new["영상 설명"] == old["영상 설명"] and new["버전"] == "2차"
          and "1차:" in new["이전 버전 요약"] and "조명이 어두워요" in new["연출 지침"]
          and new["샷 목록"] == old["샷 목록"])

    _enhance_checks(check, app)
    _narration_checks(check, tmp, make_app)
    _import_checks(check, tmp, make_app)
    _cancel_checks(check, tmp, make_app)


def _narration_checks(check, tmp, make_app):
    """내레이션(해설 음성): 문서 형식 · 길이 맞춤 · 캐시 · 러프컷 믹스 · 파이프라인 통합."""
    import os
    import subprocess

    from app.pipeline import narration
    from app.pipeline import storyboard_model as sbm
    from app.pipeline.prompt_builder import build_initial_prompt, extract_narration
    from app.pipeline.video_generator.comfyui import rough_cut
    from tests.fake_comfyui import FakeComfyUI, make_sample_mp4
    from tests.video_checks import WORKFLOWS_DIR

    print("\n── 내레이션(해설 음성) ──")
    cfg = {"LOCAL_VIDEO_MAX_SHOTS": 8, "STORYBOARD_SHOT_SECONDS": 3, "ANTHROPIC_API_KEY": "", "OLLAMA_MODEL": ""}
    text, _src = build_initial_prompt(
        "t", "제품 소개", "", "설명", "shot one\nshot two", "", [], cfg,
        narration_text="1. 첫 장면 해설입니다.\n둘째 장면 해설입니다.")
    parsed = parse_sections(text)
    check("[내레이션] 섹션: 샷 목록 뒤에 번호가 붙어 기록됨",
          list(parsed).index("내레이션") == list(parsed).index("샷 목록") + 1
          and extract_narration(text) == {1: "첫 장면 해설입니다.", 2: "둘째 장면 해설입니다."})
    gap, _ = build_initial_prompt(
        "t", "x", "", "설명", "[2초] shot one\nshot two\nshot three", "", [], cfg,
        narration_text="1. 하나\n3. 셋")
    check("번호를 건너뛴 해설은 그 번호의 샷에 붙음 (해설 없는 샷 허용)",
          extract_narration(gap) == {1: "하나", 3: "셋"})
    gap_board = sbm.build_storyboard(gap, {**cfg, "STORYBOARD_SEED": 1, "STORYBOARD_WIDTH": 576,
                                           "STORYBOARD_HEIGHT": 960, "STORYBOARD_FPS": 24,
                                           "LOCAL_VIDEO_NEGATIVE_PROMPT": ""}, aspect="16:9", shot_seconds=3)
    check("샷 줄의 [2초] 표시가 그 샷의 길이가 되고 묘사에서는 빠짐",
          [s["duration"] for s in gap_board["shots"]] == [2.0, 3.0, 3.0]
          and gap_board["shots"][0]["prompt"] == "shot one")
    seeded, _ = build_initial_prompt(
        "t", "x", "", "설명", "[6초 시드 202] shot one\n[시드 7] shot two\nshot three", "", [], cfg)
    seed_board = sbm.build_storyboard(seeded, {**cfg, "STORYBOARD_SEED": 42, "STORYBOARD_WIDTH": 576,
                                               "STORYBOARD_HEIGHT": 960, "STORYBOARD_FPS": 24,
                                               "LOCAL_VIDEO_NEGATIVE_PROMPT": ""}, aspect="16:9", shot_seconds=3)
    check("샷 줄의 [시드 N] 표시가 그 샷의 시드가 됨 (없으면 기본 시드)",
          [s["seed"] for s in seed_board["shots"]] == [202, 7, 42]
          and [s["duration"] for s in seed_board["shots"]] == [6.0, 3.0, 3.0])
    no_narr, _ = build_initial_prompt("t", "x", "", "설명", "shot one", "", [], cfg)
    check("내레이션을 안 쓰면 섹션 자체가 없음 (기존 문서와 동일)", "내레이션" not in parse_sections(no_narr))
    tricky = "[내레이션]\n2. 둘째만 있음\n12.5kg 짜리 로봇입니다."
    check("번호 없는 줄은 순서대로, '12.5kg' 같은 숫자 시작 문장은 번호로 오인하지 않음",
          extract_narration(tricky) == {2: "둘째만 있음", 3: "12.5kg 짜리 로봇입니다."},
          str(extract_narration(tricky)))

    board = sbm.build_storyboard(text, dict(make_app_defaults(make_app, tmp)))
    edited = text.replace("첫 장면 해설입니다.", "완전히 다른 해설입니다.")
    board2 = sbm.build_storyboard(edited, dict(make_app_defaults(make_app, tmp)))
    check("스토리보드에 장면별 내레이션 포함, 문장을 고쳐도 장면 서명은 같아 영상은 재사용됨",
          board["shots"][0]["narration"] == "첫 장면 해설입니다."
          and board2["shots"][0]["narration"] == "완전히 다른 해설입니다."
          and sbm.shot_signature(board, board["shots"][0]) == sbm.shot_signature(board2, board2["shots"][0]))

    fit = narration.fit_plan(2.0, 3.0, 0.2, 1.4)
    long_ = narration.fit_plan(3.5, 3.0, 0.2, 1.4)
    over = narration.fit_plan(5.0, 3.0, 0.2, 1.4)
    check("길이 맞춤: 들어가면 그대로, 길면 배속, 그래도 넘치면 잘림 표시",
          fit["speedup"] == 1.0 and not fit["truncated"]
          and 1.2 < long_["speedup"] <= 1.4 and not long_["truncated"]
          and over["speedup"] == 1.4 and over["truncated"], f"{fit} {long_} {over}")

    ffmpeg = rough_cut.resolve_ffmpeg({"FFMPEG_PATH": os.getenv("FFMPEG_PATH", "")})
    if not ffmpeg:
        check("내레이션 믹스 검증에 필요한 ffmpeg", False, "ffmpeg 없음")
        return

    # 러프컷 믹스 (TTS와 무관하게 사인파 wav로 검증)
    work = os.path.join(tmp, "narr_mix")
    os.makedirs(work, exist_ok=True)
    clips = [(f"s0{i}", make_sample_mp4(ffmpeg, os.path.join(work, f"c{i}.mp4"), seconds=2, with_audio=True))
             for i in (1, 2)]
    short_wav, long_wav = os.path.join(work, "short.wav"), os.path.join(work, "long.wav")
    for path, secs in ((short_wav, 1.0), (long_wav, 4.0)):
        subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=880",
                        "-t", str(secs), path], check=True)
    report = {}
    out = rough_cut.build(ffmpeg, clips, os.path.join(work, "mix.mp4"), 128, 96, 12,
                          narration={"s01": {"path": short_wav}, "s02": {"path": long_wav}},
                          narration_options={"volume": 1.0, "bg_volume": 0.35, "max_speedup": 1.4, "lead": 0.2},
                          report=report, edit={"transition": "cut"})
    info = rough_cut.probe_media(ffmpeg, out)
    rep = report.get("narration", {})
    check("러프컷 믹스: 영상 길이·오디오 유지, 긴 문장은 배속·잘림 보고",
          info and info["audio"] and abs(info["duration"] - 4.0) < 0.3
          and rep["s01"]["speedup"] == 1.0 and not rep["s01"]["truncated"]
          and rep["s02"]["speedup"] > 1.3 and rep["s02"]["truncated"],
          f"{info} {rep}")

    # 문장 캐시: 같은 문장은 다시 합성하지 않고, 바뀐 문장만 합성
    calls = []
    real_synth, real_avail = narration.synthesize, narration.is_available

    def fake_synth(text, out_path, config):
        calls.append(text)
        os.makedirs(os.path.dirname(str(out_path)), exist_ok=True)
        subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=660",
                        "-t", "1.0", str(out_path)], check=True)

    narration.synthesize = fake_synth
    narration.is_available = lambda config: (True, "fake")
    try:
        vcfg = {"VOICE_ADAPTER": "windows", "VOICE_RATE": 0}
        one = {"shots": [{"id": "s01", "narration": "첫째"}, {"id": "s02", "narration": "둘째"}]}
        two = {"shots": [{"id": "s01", "narration": "첫째"}, {"id": "s02", "narration": "바뀐 둘째"}]}
        wdir = os.path.join(tmp, "narr_cache")
        narration.prepare_clips(vcfg, one, wdir)
        n1 = len(calls)
        narration.prepare_clips(vcfg, one, wdir)
        n2 = len(calls)
        narration.prepare_clips(vcfg, two, wdir)
        n3 = len(calls)
        check("문장 캐시: 같은 문장은 재합성 안 함, 바뀐 장면만 다시 합성",
              (n1, n2, n3) == (2, 2, 3), f"{(n1, n2, n3)}")

        # 파이프라인 통합: 가짜 ComfyUI + 가짜 음성 → 러프컷에 내레이션 적용, 상태 API 반영
        fake = FakeComfyUI(ffmpeg).start()
        try:
            app = make_app(tmp, "narr", VIDEO_GENERATOR_ADAPTER="comfyui", COMFYUI_URL=fake.url,
                           COMFYUI_WORKFLOWS_DIR=WORKFLOWS_DIR, COMFYUI_POLL_INTERVAL_SEC=0.1,
                           COMFYUI_SHOT_TIMEOUT_SEC=30, FFMPEG_PATH=ffmpeg, STORYBOARD_WIDTH=128,
                           STORYBOARD_HEIGHT=192, STORYBOARD_SHOT_SECONDS=2, VOICE_ADAPTER="windows",
                           COMFYUI_STAGED_REVIEW=False,  # .env 값과 무관하게 한 번에 러프컷까지
                           CLIP_TRIM_HEAD_SEC=0, CLIP_TRIM_TAIL_SEC=0)
            client = app.test_client()
            client.post("/session/create", data={
                "title": "해설 영상", "genre": "제품 소개", "synopsis": "로봇 팔 소개",
                "shots": "A white robot arm on a bench\nThe robot arm lifts a red box",
                "narration": "로봇 팔이 작업대 위에 있습니다.\n빨간 상자를 들어 올립니다.",
                "aspect_ratio": "16:9", "shot_seconds": "2"})
            from app.models import PipelineSession
            with app.app_context():
                session = PipelineSession.query.order_by(PipelineSession.id.desc()).first()
                sid, rid = session.id, session.current_round.id
                from app.pipeline import orchestrator as _orch  # 전환과 무관한 시험 — 바로 넘김(컷)으로
                _orch.set_round_edit(session.current_round, app.config, transition="cut")
            before = len(calls)
            client.post(f"/session/{sid}/generate")
            st = client.get(f"/api/rounds/{rid}/status").get_json()
            narr = st["narration"]
            check("내레이션 통합: 생성 후 러프컷에 적용, 상태 API·장면에 문장 노출",
                  st["state"] == "video_ready" and narr["status"] == "ok"
                  and set(narr["shots"]) == {"s01", "s02"} and len(calls) - before == 2
                  and st["shots"][0]["narration"] == "로봇 팔이 작업대 위에 있습니다.",
                  f"{st['state']} {narr.get('status')} {narr.get('detail')}")

            # 음성을 쓸 수 없으면 영상은 정상 완성, 사유만 남김
            narration.is_available = lambda config: (False, "ko-KR 음성이 설치되어 있지 않습니다")
            client.post("/session/create", data={
                "title": "해설 영상2", "genre": "제품 소개", "synopsis": "로봇 팔 소개",
                "shots": "A white robot arm on a bench", "narration": "해설입니다.",
                "aspect_ratio": "16:9", "shot_seconds": "2"})
            with app.app_context():
                session = PipelineSession.query.order_by(PipelineSession.id.desc()).first()
                sid2, rid2 = session.id, session.current_round.id
            client.post(f"/session/{sid2}/generate")
            st2 = client.get(f"/api/rounds/{rid2}/status").get_json()
            check("음성 사용 불가 → 영상은 내레이션 없이 완성(video_ready) + 사유 표시",
                  st2["state"] == "video_ready" and st2["narration"]["status"] == "skipped"
                  and "ko-KR" in st2["narration"]["detail"], str(st2["narration"]))
        finally:
            fake.stop()
    finally:
        narration.synthesize, narration.is_available = real_synth, real_avail

    # 실제 Windows 음성(있을 때만): 한국어 문장이 wav로 합성되는지
    ok, reason = narration.is_available({"VOICE_ADAPTER": "windows", "VOICE_CULTURE": "ko-KR"})
    if ok:
        wav = os.path.join(tmp, "real_tts.wav")
        narration.synthesize("안녕하세요. 이 로봇 팔은 센서로 물체를 인식합니다.", wav,
                             {"VOICE_ADAPTER": "windows", "VOICE_CULTURE": "ko-KR", "VOICE_RATE": 0})
        length = narration.audio_duration(ffmpeg, wav)
        check("실제 Windows 한국어 음성으로 합성", 1.5 < length < 15, f"{reason} · {length:.1f}초")
    else:
        check("실제 Windows 한국어 음성으로 합성 (이 PC에 음성 없음 — 건너뜀)", True, reason)


def make_app_defaults(make_app, tmp):
    """스토리보드 계산에 필요한 설정만 가져온다 (앱 설정과 같은 기본값)."""
    app = make_app(tmp, "defaults")
    return app.config


def _enhance_checks(check, app):
    """Gemma 프롬프트 강화 스위치: 기본은 꺼짐, 켜면 스위치·시드 주입, 결과 문장 추출."""
    from app.pipeline.video_generator.comfyui import workflow

    print("\n── Gemma 프롬프트 강화 옵션 ──")
    template, mapping = workflow.load_template(app.config, "ltx25_t2v")
    params = {"prompt": "A robot arm", "negative": "ugly", "seed": 1234, "width": 960,
              "height": 576, "frames": 73, "fps": 24}
    nodes = workflow.enhance_nodes(template)
    off = workflow.fill(template, mapping, params)
    on = workflow.fill(template, mapping, params, enhance=True)
    switch, gen = nodes["switch"][0], nodes["generate"][0]
    check("프롬프트 강화: 기본(꺼짐)은 mapping의 fixed 값을 따름",
          len(nodes["switch"]) == 1 and len(nodes["generate"]) == 1
          and off[switch]["inputs"]["value"] is False, str(nodes))
    check("프롬프트 강화: 켜면 스위치 ON + 강화 시드 = 장면 시드, 템플릿 원본은 불변",
          on[switch]["inputs"]["value"] is True
          and on[gen]["inputs"]["sampling_mode.seed"] == 1234
          and template[switch]["inputs"]["value"] is False)
    entry = {"outputs": {nodes["preview"][0]: {"text": ["  A sleek white robot arm, ...  "]}}}
    check("프롬프트 강화: history 출력에서 강화된 문장을 꺼냄",
          workflow.enhanced_prompt(on, entry) == "A sleek white robot arm, ..."
          and workflow.enhanced_prompt(on, {"outputs": {}}) == "")
    try:
        workflow.fill({"1": {"class_type": "X", "inputs": {}}}, {
            "prompt": {"node": "1", "input": "a"}, "seed": {"node": "1", "input": "b"},
            "width": {"node": "1", "input": "c"}, "height": {"node": "1", "input": "d"},
            "frames": {"node": "1", "input": "e"}}, params, enhance=True)
        missing_ok = False
    except KeyError:
        missing_ok = True
    check("프롬프트 강화: 노드가 없는 워크플로에서 켜면 명확한 오류", missing_ok)


def _xlsx_bytes(sheets):
    """{시트 이름: [[셀, ...], ...]} → 최소한의 .xlsx 바이트 (인라인 문자열)."""
    import io
    import zipfile
    from xml.sax.saxutils import escape

    def col(i):
        return chr(65 + i)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        wb = "".join(f'<sheet name="{escape(n)}" sheetId="{i}" r:id="rId{i}"/>' for i, n in enumerate(sheets, 1))
        z.writestr("xl/workbook.xml",
                   '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                   f"<sheets>{wb}</sheets></workbook>")
        rels = "".join(f'<Relationship Id="rId{i}" Target="worksheets/sheet{i}.xml"/>' for i in range(1, len(sheets) + 1))
        z.writestr("xl/_rels/workbook.xml.rels",
                   f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rels}</Relationships>')
        for i, rows in enumerate(sheets.values(), 1):
            body = ""
            for r, row in enumerate(rows, 1):
                cells = "".join(f'<c r="{col(c)}{r}" t="inlineStr"><is><t>{escape(str(v))}</t></is></c>'
                                for c, v in enumerate(row) if v != "")
                body += f'<row r="{r}">{cells}</row>'
            z.writestr(f"xl/worksheets/sheet{i}.xml",
                       f'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>{body}</sheetData></worksheet>')
    return buf.getvalue()


def _import_checks(check, tmp, make_app):
    """콘티 엑셀 불러오기: 머리글 이름으로 읽기 · 길이/시드 표시 · 번호 건너뛴 내레이션 · 사이트 라우트."""
    import io

    from app.pipeline import storyboard_import as si
    from app.pipeline.prompt_builder import build_initial_prompt, extract_narration
    from app.pipeline import storyboard_model as sbm

    print("\n── 콘티 엑셀 불러오기 ──")
    style = "Sharp focus, realistic."
    header = ["#", "장면 ID", "길이(초)", "내레이션", "장면 동작 (영문)", "생성기", "입력 사진", "시드", "너비", "높이"]
    rows = [["안내 문장 한 칸짜리 행입니다 " * 3], header,
            [1, "s01", 7, "첫 해설", "A board powers on. " + style, "local", "a.jpg", 201, 960, 544],
            [2, "s02", 2, "", "Use the provided start image as the first frame. A hand moves. " + style, "api", "", "", 960, 544],
            ["", "합계", 9]]
    data = _xlsx_bytes({"콘티": rows, "공통 설정": [["항목", "문구"], ["스타일", style]]})
    result = si.parse_xlsx(data, {"LOCAL_VIDEO_MAX_SHOTS": 8})
    shots = result["shots_text"].split("\n")
    check("콘티 읽기: 머리글 이름으로 장면·길이·시드·스타일을 찾고 합계·안내 행은 건너뜀",
          result["count"] == 2 and shots[0] == "1. [7초 시드 201] A board powers on."
          and shots[1] == "2. [2초] A hand moves." and result["style"] == style
          and result["aspect"] == "16:9", str(result))
    check("내레이션 번호는 장면 번호를 따르고(해설 없는 장면은 건너뜀), 입력 사진·API 지정은 알림으로 남김",
          result["narration"] == "1. 첫 해설" and any("입력 사진" in n for n in result["notes"])
          and any("상용 API" in n for n in result["notes"]), str(result["notes"]))
    cfg = {"LOCAL_VIDEO_MAX_SHOTS": 8, "STORYBOARD_SHOT_SECONDS": 3, "ANTHROPIC_API_KEY": "", "OLLAMA_MODEL": ""}
    text, _ = build_initial_prompt("t", "x", "", result["description"], result["shots_text"], result["style"],
                                   [], cfg, narration_text=result["narration"])
    board = sbm.build_storyboard(text, {**dict(make_app_defaults(make_app, tmp)), "LOCAL_VIDEO_MAX_SHOTS": 8},
                                 aspect="16:9", shot_seconds=3)
    check("불러온 값으로 만든 스토리보드: 장면별 길이·시드·내레이션이 그대로 들어감",
          [s["duration"] for s in board["shots"]] == [7.0, 2.0] and board["shots"][0]["seed"] == 201
          and board["shots"][0]["narration"] == "첫 해설")

    app = make_app(tmp, "import")
    client = app.test_client()
    ok = client.post("/session/import-storyboard", data={"file": (io.BytesIO(data), "콘티.xlsx")},
                     content_type="multipart/form-data")
    bad = client.post("/session/import-storyboard", data={"file": (io.BytesIO(b"not an xlsx"), "x.xlsx")},
                      content_type="multipart/form-data")
    wrong = client.post("/session/import-storyboard", data={"file": (io.BytesIO(b"a,b"), "x.csv")},
                        content_type="multipart/form-data")
    check("사이트 라우트: 올바른 파일은 200, 깨진 파일·다른 형식은 400과 안내 메시지",
          ok.status_code == 200 and ok.get_json()["count"] == 2
          and bad.status_code == 400 and "읽을 수 없습니다" in bad.get_json()["error"]
          and wrong.status_code == 400 and ".xlsx" in wrong.get_json()["error"])
    page = client.get("/session/create").get_data(as_text=True)
    check("새 영상 화면에 콘티 불러오기 입력이 있음", "storyboard-file" in page and "import-storyboard" in page)


def _cancel_checks(check, tmp, make_app):
    """생성 중지: 장면 사이에서 멈춤 · 라운드는 재시도 가능 · 재시도하면 이어서 완성 · 생성 중이 아니면 거절."""
    import time

    from app.models import GenerationJob, ShotRender, db
    from app.pipeline import jobs
    from tests.video_checks import _create, _status

    print("\n── 생성 중지 ──")
    shots = "\n".join(f"shot number {i}" for i in range(1, 5))
    app = make_app(tmp, "cancel", JOBS_SYNC=False, MOCK_DELAY_SEC=0.6)
    client = app.test_client()
    _, rid = _create(client, app, shots)
    idle = client.post(f"/api/rounds/{rid}/cancel")
    check("생성 중이 아닐 때 중지 요청은 409로 거절", idle.status_code == 409)

    client.post(f"/session/{_}/generate")
    deadline = time.time() + 20
    while time.time() < deadline:  # 첫 장면이 시작될 때까지
        with app.app_context():
            if ShotRender.query.filter_by(round_id=rid).count():
                break
        time.sleep(0.1)
    res = client.post(f"/api/rounds/{rid}/cancel")
    check("실행 중 중지 요청: 'stopping' 응답", res.status_code == 200 and res.get_json()["result"] == "stopping",
          res.get_data(as_text=True))
    jobs.wait_idle(app, timeout=30)
    st = _status(client, rid)
    with app.app_context():
        made = ShotRender.query.filter_by(round_id=rid, status="done").count()
        job = GenerationJob.query.filter_by(round_id=rid).order_by(GenerationJob.id.desc()).first()
    check("중지 후 라운드는 generation_failed + 중지 안내, 남은 장면은 만들지 않음",
          st["state"] == "generation_failed" and "중지" in (st.get("error") or "")
          and made < 4, f"{st['state']} made={made}")
    client.post(f"/api/rounds/{rid}/retry")
    jobs.wait_idle(app, timeout=60)
    st2 = _status(client, rid)
    with app.app_context():
        done = ShotRender.query.filter_by(round_id=rid, status="done", selected=True).count()
    check("재시도 결과: 4장면 완성 + video_ready", st2["state"] == "video_ready" and done == 4,
          f"{st2['state']} done={done}")

    # 대기 중이던 작업 취소 (워커 없이 상태만 확인)
    app2 = make_app(tmp, "cancel_q", JOBS_SYNC=False, MOCK_DELAY_SEC=0)
    c2 = app2.test_client()
    sid2, rid2 = _create(c2, app2, shots)
    with app2.app_context():
        from app.models import VideoRound
        from app.pipeline import orchestrator
        r = db.session.get(VideoRound, rid2)
        orchestrator.advance(r, "generating")
        db.session.add(GenerationJob(kind="generate_video", round_id=rid2, payload_json="{}", status="queued"))
        db.session.commit()
    res2 = c2.post(f"/api/rounds/{rid2}/cancel")
    with app2.app_context():
        qjob = GenerationJob.query.filter_by(round_id=rid2).first()
    check("대기 중인 작업은 바로 취소(cancelled)되고 라운드가 재시도 가능 상태가 됨",
          res2.get_json()["result"] == "cancelled" and qjob.status == "failed" and qjob.error == "cancelled"
          and _status(c2, rid2)["state"] == "generation_failed")

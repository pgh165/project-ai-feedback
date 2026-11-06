"""AI 자동 진행 검증: 부족한 것만 묻기(intake) → AI 기획 → 이미지 → AI 점검·다시 뽑기 → 컷 → 점검 → 러프컷 → ③ 확인.

run_check.py가 부른다. 가짜 ComfyUI(tests/fake_comfyui.py)로 영상을 만들고, 글 모델(llm.generate_strong·llm.generate)과
비전 판정(auto_review.ask_vision)은 정해진 답을 돌려주는 함수로 바꿔 끼운다 — 실제 Gemma·LTX는 부르지 않는다.
"""
import io
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from app.models import PipelineSession, ShotRender, VideoRound, db
from app.pipeline import auto_review, face, intake, llm
from tests.fake_comfyui import _PNG_1X1, FakeComfyUI

_DRAFT = """제목: 학과 홍보
영상 종류: 소개·안내
등장 요소: 초록색 회로 기판
[장면]
1. 학생이 회로 기판을 들여다본다
2. 로봇팔이 작은 큐브를 집는다
3. 모형차가 트랙을 달린다
[내레이션]
1. 직접 만들어요
2. 움직여요
3. 달려요
스타일: bright clean studio lighting"""
_REFINED = """1. Bright clean studio, a young Korean student looks at a green circuit board number 1, slow push-in
2. Bright clean studio, a small robot arm picks up a tiny cube number 2 on a white desk, static camera
3. Bright clean studio, a small model car drives along a track number 3 on a white floor, tracking shot"""
_PASS = "SCORE: 8\nTEXT: no\nBROKEN: no\nPROBLEM: none\nFIX: none"
_FAIL = "SCORE: 3\nTEXT: no\nBROKEN: yes\nPROBLEM: the cube is fused\nFIX: a tiny red cube clearly separated from the gripper"


_SEEN = []  # 가짜 LLM이 받은 (system, user) — 제작 원칙이 들어갔는지 본다


def _fake_strong(system, user, config, max_tokens=1200, fallback_config=None, in_job=False):
    _SEEN.append((system, user))
    if "You rewrite scene descriptions" in system:
        return _REFINED, "fake", ""
    if "[장면]" in system:
        n = int(re.search(r"정확히 (\d+)개", system).group(1))
        if n == 3:
            return _DRAFT, "fake", ""
        return ("제목: 사진 영상\n영상 종류: 소개·안내\n등장 요소: 실습 장비\n[장면]\n"
                + "\n".join(f"{i}. 장면 {i}을 보여 준다" for i in range(1, n + 1)) + "\n[내레이션]\n"
                + "\n".join(f"{i}. 해설 {i}" for i in range(1, n + 1)) + "\n스타일: bright clean light"), "fake", ""
    return None, "template", "fake"


def _vision(fail_marker, fail_times, calls):
    """fail_marker가 든 장면 첫 프레임을 처음 fail_times번 떨어뜨린다. 컷 점검(뒤쪽·끝 프레임)은 통과."""
    def ask(image_path, description, config):
        is_clip = Path(image_path).name.startswith("at")  # 컷 점검 프레임 at75.png·at95.png
        calls.append(("clip" if is_clip else "image", description))
        if not is_clip and fail_marker and fail_marker in description:
            seen = sum(1 for k, d in calls if k == "image" and fail_marker in d)
            if fail_times is None or seen <= fail_times:
                return _FAIL
        return _PASS
    return ask


def _app(make_app, tmp, name, fake, ffmpeg, workflows, **extra):
    config = dict(VIDEO_GENERATOR_ADAPTER="comfyui", COMFYUI_URL=fake.url,
                  COMFYUI_WORKFLOWS_DIR=workflows, COMFYUI_POLL_INTERVAL_SEC=0.1, COMFYUI_SHOT_TIMEOUT_SEC=30,
                  FFMPEG_PATH=ffmpeg, STORYBOARD_WIDTH=128, STORYBOARD_HEIGHT=192, STORYBOARD_SHOT_SECONDS=2,
                  LOCAL_VIDEO_MAX_SHOTS=3, COMFYUI_IMAGE_FIRST=True, COMFYUI_STAGED_REVIEW=False,
                  VOICE_ADAPTER="none", BGM_AUTO=False, AUTO_REVIEW_MAX_RETRIES=2,
                  AUTO_REVIEW_STILL_DIFF=0.0001)
    config.update(extra)
    return make_app(tmp, name, **config)


def _start(client, idea="우리 학과 6초 홍보 영상", **form):
    data = {"idea": idea, "genre": "소개·안내", "seconds": "6", "aspect": "9:16", "narration": "on", "music": "off",
            "question_1": "무엇을 보여 줄까요?", "answer_1": "회로 기판과 로봇팔", **form}
    return client.post("/session/auto", data=data, content_type="multipart/form-data")


def _unit_checks(check, ffmpeg, tmp):
    print("\n── Qwen 첫 프레임 색 보정 ──")
    from PIL import Image
    from app.pipeline import tone
    sample = os.path.join(tmp, "tone_sample.png")
    Image.new("RGB", (8, 8), (40, 200, 60)).save(sample)
    def sat(p):
        with Image.open(p) as im:
            return sum(px[1] for px in im.convert("HSV").getdata()) / 64
    before = sat(sample)
    changed = tone.soften(sample, {})
    after = sat(sample)
    same = tone.soften(sample, {"QWEN_TONE_COLOR": 1.0, "QWEN_TONE_CONTRAST": 1.0})
    check("색 보정: 기본(채도 85%·대비 90%)이면 채도가 낮아지고, 1.0이면 그대로",
          changed and after < before and not same, (before, after))
    print("\n── AI에게 맡기기: 부족한 것만 묻기 · 가정 ──")
    check("길이 읽기: '1분 30초' → 90, '30초' → 30, 없으면 None",
          intake.guess_seconds("1분 30초짜리") == 90 and intake.guess_seconds("30초 홍보") == 30
          and intake.guess_seconds("홍보 영상") is None)
    check("화면 비율 읽기: 쇼츠 → 세로, 유튜브 영상 → 가로, 없으면 고른 값",
          intake.guess_aspect("쇼츠로") == "9:16" and intake.guess_aspect("유튜브 영상") == "16:9"
          and intake.guess_aspect("홍보", "1:1") == "1:1")
    a = intake.analyze("우리 학과를 소개하는 30초 영상, 내레이션 없이", {}, aspect="16:9", genres=["소개·안내"], ask_llm=False)
    keys = {x["key"]: x for x in a["assumptions"]}
    check("종류·길이를 설명에서 읽으면 묻지 않고 가정으로, 내레이션 '없이'도 읽음",
          not a["questions"] and keys["genre"]["value"] == "소개·안내" and keys["seconds"]["value"] == "30"
          and keys["seconds"]["from_text"] and keys["narration"]["value"] == "off" and keys["aspect"]["value"] == "16:9", a)
    b = intake.analyze("고양이가 나오는 영상", {}, genres=["홍보·광고", "브이로그"], ask_llm=False)
    check("종류를 알 수 없으면 그것만 질문, 길이는 기본 30초로 가정",
          [q["key"] for q in b["questions"]] == ["genre"]
          and next(x for x in b["assumptions"] if x["key"] == "seconds")["value"] == "30", b)
    qs = intake.parse_questions("질문: 어떤 장면을 꼭 넣을까요?\n보기: 실습실 | 로봇팔 | 졸업 작품\n"
                                "질문: 누가 나오나요?\n보기: 학생 / 교수 / 없음\n질문: 세 번째?\n보기: a | b")
    check("내용 질문은 형식에 맞는 것만 최대 2개, '없음'이면 질문 없음",
          len(qs) == 2 and qs[0]["options"] == ["실습실", "로봇팔", "졸업 작품"] and intake.parse_questions("없음") == [], qs)
    skipped = intake.parse_questions(
        "질문: 영상의 전체적인 분위기는 어떤 느낌이었으면 좋겠나요?\n보기: 활기찬 | 차분한 | 미래적인\n"
        "질문: 영상의 주요 장면 전환 속도는 어떤 것이 적절할까요?\n보기: 빠르게 | 느리게 | 중간\n"
        "질문: 꼭 보여 줄 작품이 있나요?\n보기: 로봇팔 | 모형차 | 회로 기판")
    check("기본값으로 정할 것(분위기·전환 속도)을 물으면 버림 — 실제 Gemma가 물었던 질문",
          [q["question"] for q in skipped] == ["꼭 보여 줄 작품이 있나요?"], skipped)
    check("영상 길이 → 장면 수·길이 (30초 → 6장면×5초, 60초 → 최대 8장면×7.5초)",
          intake.shot_plan(30, {"STORYBOARD_SHOT_SECONDS": 5, "LOCAL_VIDEO_MAX_SHOTS": 8}) == (6, 5.0)
          and intake.shot_plan(60, {"STORYBOARD_SHOT_SECONDS": 5, "LOCAL_VIDEO_MAX_SHOTS": 8}) == (8, 7.5))

    print("\n── 글자 없는 화면 (글자는 편집에서) ──")
    from app.pipeline.text_free import make_textless
    p = make_textless("Bright lab, two students smile in front of glowing computer monitors, slow zoom-out, blue light")
    from app.pipeline import text_free
    check("모니터 → '흐릿한 코드·그래프' 긍정 묘사 + 끝에 깨끗한 표면 (부정문 없음) — 2026-11-06 사용자: 모니터 속 작은 글씨는 괜찮다",
          f"computer monitors {text_free.SCREEN}" in p and p.endswith("clean unbranded plain surfaces")
          and not re.search(r"\b(no|without|not)\b", p), p)
    check("두 번 적용해도 같은 문장 (다시 만들 때 겹치지 않음)", make_textless(p) == p)
    check("간판·책·병도 글자 없는 모습으로",
          all(s in make_textless("a poster, a book and a bottle on a desk")
              for s in ("poster in plain solid colors", "book with blank plain pages", "bottle plain and unlabeled")))
    from app.pipeline import storyboard_model as sbm
    board = {"style": "cool light", "negative": "", "defaults": {"width": 64, "height": 64, "fps": 24, "seed": 1},
             "shots": [{"id": "s01", "prompt": "a student at a laptop", "duration": 2.0}]}
    params = sbm.resolve_params(board, board["shots"][0], {"COMFYUI_SIZE_MULTIPLE": 64, "COMFYUI_WORKFLOW_I2V": "i",
                                                         "COMFYUI_WORKFLOW_T2V": "t"})
    check("생성 파라미터(영상·첫 프레임)에 적용, 장면 서명은 원래 묘사 그대로",
          f"laptop {text_free.SCREEN}" in params["prompt"]
          and f"laptop {text_free.SCREEN}" in params["image_prompt"]
          and text_free.SCREEN not in sbm.shot_signature(board, board["shots"][0])[0])
    verb = make_textless("A Korean student monitors the robot's LiDAR on a laptop screen")
    check("동사로 쓰인 'monitors the …'는 모니터로 보지 않음 (실제 사례: 문장이 'monitors showing … the robot's'로 깨짐)",
          verb.startswith("A Korean student monitors the robot's LiDAR on a laptop screen showing"), verb)

    print("\n── AI 점검: 판정 읽기 · 멈춘 화면 ──")
    got = auto_review.parse("**SCORE:** 4\nTEXT: yes\nBROKEN: no\nPROBLEM: letters on the wall\nFIX: none")
    check("Gemma 답 읽기 (굵은 글씨·none 처리)", got and got["score"] == 4 and got["text"] and not got["broken"]
          and got["fix"] == "", got)
    check("SCORE가 없으면 판정 못 함", auto_review.parse("looks fine") is None)
    orig = auto_review.ask_vision
    try:
        img = Path(tmp) / "one.png"
        img.write_bytes(_PNG_1X1)
        auto_review.ask_vision = lambda *a: "SCORE: 9\nTEXT: yes\nBROKEN: no\nPROBLEM: a sign\nFIX: none"
        r = auto_review.check_image(img, "desc", {"AUTO_REVIEW_MIN_SCORE": 6})
        check("점수가 높아도 글자가 보이면 미달 + 글자를 지우는 고쳐 쓰기 의견",
              not r["passed"] and auto_review.P_TEXT in r["problems"] and r["fix"], r)
        auto_review.ask_vision = lambda *a: "SCORE: 8\nTEXT: yes\nBROKEN: no\nPROBLEM: the focus is soft\nFIX: none"
        r = auto_review.check_image(img, "desc", {"AUTO_REVIEW_MIN_SCORE": 6})
        check("'TEXT: yes'만 있고 문제 문장이 글자를 말하지 않으면 통과 (실제 e2b 오판 사례)", r["passed"], r)
        auto_review.ask_vision = lambda *a: ("SCORE: 8\nTEXT: yes\nBROKEN: no\n"
                                             "PROBLEM: the monitor shows lines of code text\nFIX: none")
        r_scr = auto_review.check_image(img, "desc", {"AUTO_REVIEW_MIN_SCORE": 6})
        auto_review.ask_vision = lambda *a: ("SCORE: 8\nTEXT: yes\nBROKEN: no\n"
                                             "PROBLEM: a big caption with words is written across the image\nFIX: none")
        r_cap = auto_review.check_image(img, "desc", {"AUTO_REVIEW_MIN_SCORE": 6})
        check("모니터 속 코드 글자는 통과, 화면 위 큰 자막 글자는 미달 (2026-11-06 사용자 기준)",
              r_scr["passed"] and not r_cap["passed"] and auto_review.P_TEXT in r_cap["problems"], (r_scr, r_cap))
        auto_review.ask_vision = lambda *a: None
        r = auto_review.check_image(img, "desc", {})
        check("AI가 답하지 못하면 '점검 못 함'으로 통과시켜 사람이 보게 함 (다시 만들지 않음)",
              r["passed"] and not r["checked"] and r["problems"] == [auto_review.P_UNCHECKED], r)
        still = Path(tmp) / "still.mp4"
        subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=gray:s=128x96:r=12",
                        "-t", "1", "-pix_fmt", "yuv420p", str(still)], check=True)
        moving = Path(tmp) / "moving.mp4"
        subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=128x96:r=12",
                        "-t", "1", "-pix_fmt", "yuv420p", str(moving)], check=True)
        auto_review.ask_vision = lambda *a: _PASS
        cfg = {"FFMPEG_PATH": ffmpeg, "AUTO_REVIEW_STILL_DIFF": 1.5, "AUTO_REVIEW_DARK_LEVEL": 18}
        r_still, r_move = auto_review.check_clip(still, "d", cfg), auto_review.check_clip(moving, "d", cfg)
        check("컷 점검: 멈춘 화면은 미달(still — 시드만 바꿔 다시), 움직이는 화면은 통과",
              not r_still["passed"] and r_still["kind"] == "still" and auto_review.P_STILL in r_still["problems"]
              and r_move["passed"], (r_still, r_move))
        check("가장 나은 버전 고르기: 통과 > 점검 못 함 > 미달",
              auto_review.rank({"passed": True, "score": 6}) > auto_review.rank({"passed": True, "score": None})
              > auto_review.rank({"passed": False, "score": 9}))

        print("\n── 카메라 기본 고정 · 줌 점검 (2026-11-06) ──")
        import numpy as np
        rng = np.random.default_rng(0)
        base_img = np.asarray(Image.fromarray((rng.random((18, 32)) * 255).astype(np.uint8)).resize((128, 72), Image.BICUBIC),
                              dtype=np.float32)
        h0, w0 = base_img.shape
        crop = base_img[int(h0 * 0.115):int(h0 * 0.885), int(w0 * 0.115):int(w0 * 0.885)]  # 가운데 77% → 약 1.3배
        zoomed = np.asarray(Image.fromarray(crop.astype(np.uint8)).resize((w0, h0), Image.BILINEAR), dtype=np.float32)
        z_same, z_in, z_out = (auto_review.zoom_factor(base_img, base_img), auto_review.zoom_factor(base_img, zoomed),
                               auto_review.zoom_factor(zoomed, base_img))
        check("줌 재기: 같은 그림 1.0, 1.3배 다가간 그림 ≈1.3, 멀어진 그림 <0.8",
              z_same[0] == 1.0 and 1.2 <= z_in[0] <= 1.4 and z_in[1] > 0.8 and z_out[0] < 0.8, (z_same, z_in, z_out))
        pic = Path(tmp) / "zoom_src.png"
        Image.fromarray((rng.random((48, 64, 3)) * 255).astype(np.uint8)).resize((256, 192), Image.BICUBIC).save(pic)
        zoom_mp4 = Path(tmp) / "zoom.mp4"
        subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-loop", "1", "-i", str(pic), "-vf",
                        "zoompan=z='1+0.03*on':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=128x96:fps=12",
                        "-frames:v", "13", "-pix_fmt", "yuv420p", str(zoom_mp4)], check=True)
        r_zoom = auto_review.check_clip(zoom_mp4, "a robot arm, static shot", cfg)
        r_asked = auto_review.check_clip(zoom_mp4, "a robot arm, slow push-in", cfg)
        check("컷 점검: 고정이어야 할 장면이 다가가면 '카메라가 움직였어요'(still — 시드만 바꿔 다시), "
              "장면 줄이 push-in을 원하면 재지 않음",
              not r_zoom["passed"] and r_zoom["kind"] == "still"
              and any(p.startswith(auto_review.P_CAMERA) for p in r_zoom["problems"])
              and not any(p.startswith(auto_review.P_CAMERA) for p in r_asked["problems"]), (r_zoom, r_asked))
        check("컷 점검: 움직이는 테스트 화면(고정 카메라)은 줌으로 잡지 않음",
              not any(p.startswith(auto_review.P_CAMERA) for p in r_move["problems"]), r_move)

        print("\n── 컷 점검 개선: 끝 프레임 묘사 일치 · 형태 깨짐 기준 (2026-11-06) ──")
        calls = []
        auto_review.ask_vision = lambda *a: calls.append(1) or (
            _PASS if len(calls) % 2 == 1 else
            "SCORE: 1\nTEXT: no\nBROKEN: no\nPROBLEM: the frame is a blank light blue wall\nFIX: the student stays in frame")
        r_end = auto_review.check_clip(moving, "a student at a desk, static shot", cfg)
        check("컷 점검: 뒤쪽(75%)·끝(95%) 두 장을 묘사와 비교 — 끝이 빈 화면이면 '묘사와 달라요' (실제 사례: 빈 벽 컷이 통과해 뽑힘)",
              len(calls) == 2 and not r_end["passed"] and r_end["kind"] == "look"
              and any(p.startswith(auto_review.P_MISMATCH) for p in r_end["problems"])
              and "student stays in frame" in r_end["fix"] and r_end["score"] == 1.0, r_end)
        mism = auto_review.parse("SCORE: 2\nTEXT: no\nBROKEN: yes\nPROBLEM: the robot arm is not holding the cube\n"
                                 "FIX: the gripper holds a tiny cube")
        real = auto_review.parse("SCORE: 3\nTEXT: no\nBROKEN: yes\nPROBLEM: the arm is melting into the laptop\nFIX: none")
        v_m, v_r = auto_review._verdict(mism, 6), auto_review._verdict(real, 6)
        check("형태 깨짐은 문제 문장이 실제 변형(녹음·붙음·일그러짐)을 말할 때만 — '큐브를 안 잡았다'는 묘사 차이로 "
              "(실제 사례: 26B가 묘사 차이도 BROKEN: yes라고 답함)",
              auto_review.P_BROKEN not in v_m[2] and any(p.startswith(auto_review.P_MISMATCH) for p in v_m[2])
              and "gripper holds a tiny cube" in v_m[3] and auto_review.P_BROKEN in v_r[2], (v_m, v_r))
        auto_review.ask_vision = lambda *a: "SCORE: 9\nTEXT: no\nBROKEN: no\nPROBLEM: none\nFIX: none"
        r_nine = auto_review.check_clip(moving, "d", cfg)
        check("통과한 컷도 프레임 점수를 남겨 더 나은 버전을 고를 수 있음", r_nine["passed"] and r_nine["score"] == 9.0, r_nine)

        dark_mp4 = Path(tmp) / "fade_dark.mp4"  # 움직이지만 아주 어두운 화면
        subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=128x96:r=12",
                        "-vf", "eq=brightness=-0.7:contrast=2", "-t", "1", "-pix_fmt", "yuv420p", str(dark_mp4)], check=True)
        frames_at = []
        auto_review.ask_vision = lambda path, *a: frames_at.append(Path(path).name) or _PASS
        r_fade = auto_review.check_clip(dark_mp4, "two students at a bench, the light fades to darkness", cfg)
        frames_at2 = list(frames_at); frames_at.clear()
        r_dark = auto_review.check_clip(dark_mp4, "Dim lab with cool blue light, two students at a bench", cfg)
        check("어두워지며 끝나는 장면(페이드아웃)은 '어두움' 점검을 건너뛰고 앞쪽(25%·50%) 프레임으로 묘사를 봄, "
              "'dim lab'(어두운 실습실)은 페이드가 아님 (실제 사례: 프로젝트 51 s06을 끝이 어둡다고 세 번 미달)",
              r_fade["passed"] and frames_at2 == ["at25.png", "at50.png"]
              and auto_review.P_DARK in r_dark["problems"] and frames_at == ["at75.png", "at95.png"],
              (r_fade, frames_at2, r_dark, frames_at))
        auto_review.ask_vision = lambda *a: _PASS

        from app.pipeline import camera
        from app.pipeline import storyboard_model as sbm_
        board = {"style": "Blue-silver grade, shallow depth-of-field, smooth gimbal motion. Smooth gimbal tracking, "
                          "rack-focus from device to student face, slow pull-back at close.",
                 "negative": "", "defaults": {"width": 640, "height": 384, "fps": 24, "seed": 1},
                 "shots": []}
        plain = {"id": "s01", "prompt": "A small robot drives between orange cones", "duration": 4}
        moving_shot = {"id": "s02", "prompt": "Two students review results, camera slowly pulls back", "duration": 4}
        pcfg = {"COMFYUI_SIZE_MULTIPLE": 64, "COMFYUI_WORKFLOW_I2V": "i2v", "COMFYUI_WORKFLOW_T2V": "t2v"}
        p1, p2 = sbm_.resolve_params(board, plain, pcfg), sbm_.resolve_params(board, moving_shot, pcfg)
        check("카메라 기본 고정: 스타일 칸의 gimbal·rack-focus·pull-back은 모든 장면에서 빠지고, 움직임 없는 장면엔 고정 카메라가 붙음 "
              "(실제 사례: 프로젝트 45 여섯 컷 모두 1.3~1.8배 줌)",
              "gimbal" not in p1["prompt"].lower() and "rack-focus" not in p1["prompt"] and "pull-back" not in p1["prompt"]
              and camera.CAMERA in p1["prompt"] and "Blue-silver grade" in p1["prompt"]
              and camera.CAMERA not in p1["image_prompt"], p1)
        check("장면 줄에 직접 적은 카메라 움직임(pulls back)은 그대로 두고 고정 카메라를 붙이지 않음",
              "camera slowly pulls back" in p2["prompt"] and camera.CAMERA not in p2["prompt"], p2["prompt"])
        check("CAMERA_LOCK_DEFAULT=False면 예전처럼 스타일 그대로",
              "gimbal" in sbm_.resolve_params(board, plain, {**pcfg, "CAMERA_LOCK_DEFAULT": False})["prompt"])
        fade_shot = {"id": "s06", "duration": 3,
                     "prompt": "Two students look at a small computer board, camera slowly pulls back as the light fades to darkness"}
        pf = sbm_.resolve_params({**board, "style": "warm light. Calm, dignified fade to black."}, fade_shot, pcfg)
        check("끝 페이드아웃·어두워짐은 AI 프롬프트에서 뺀다 (편집의 끝 페이드로) — 장면 줄의 카메라 움직임은 남김 "
              "(실제 사례: 프로젝트 51 s06이 조명이 꺼지는 장면이 됨)",
              "fade" not in pf["prompt"].lower() and "darkness" not in pf["prompt"].lower()
              and "camera slowly pulls back" in pf["prompt"] and "fade" not in pf["image_prompt"].lower(), pf["prompt"])

        print("\n── 엔딩 카드 · 끝 페이드아웃 (2026-11-06) ──")
        from tests.fake_comfyui import make_sample_mp4
        from app.pipeline import overlay as ov
        from app.pipeline.video_generator.comfyui import rough_cut as rc
        ec_dir = Path(tmp) / "endcard"
        ec_dir.mkdir(exist_ok=True)
        c1, c2 = ec_dir / "c1.mp4", ec_dir / "c2.mp4"
        make_sample_mp4(ffmpeg, c1, seconds=2, with_audio=True)
        make_sample_mp4(ffmpeg, c2, seconds=2, with_audio=True)
        logo_png = ec_dir / "logo.png"
        Image.new("RGBA", (40, 40), (255, 200, 0, 255)).save(logo_png)
        card = ov.normalize_end_card({"enabled": True, "seconds": 3, "fade_sec": 0.8, "text": "군산대학교\n임베디드소프트웨어학과"})
        rep = {}
        out_ec = rc.build(ffmpeg, [("s01", c1), ("s02", c2)], ec_dir / "rough.mp4", 128, 96, 12, report=rep,
                          edit={"subtitles": True, "transition": "cut"}, subtitles={"s01": "첫 문장", "s02": "군산대학교 임베디드소프트웨어학과"},
                          end_card=card, end_card_font="C:/Windows/Fonts/malgunbd.ttf", end_card_logo=str(logo_png))
        dur_ec = (rc.probe_media(ffmpeg, out_ec) or {}).get("duration") or 0
        tl = rep.get("timeline") or {}
        srt_text = (ec_dir / rc.CAPTIONS_NAME).read_text(encoding="utf-8") if (ec_dir / rc.CAPTIONS_NAME).exists() else ""
        check("엔딩 카드: 장면 뒤에 카드(로고·문구)를 붙여 길이가 늘고, 타임라인에 카드 구간이 따로 있으며 장면 목록에는 없음, "
              "마지막 내레이션(자막)이 카드 위로 옮겨짐",
              out_ec and 6.5 <= dur_ec <= 7.6 and tl.get("end_card") and [s["id"] for s in tl.get("scenes", [])] == ["s01", "s02"]
              and "00:00:04," in srt_text and "군산대학교" in srt_text.split("00:00:04,")[1] and tl["end_card"][0] >= 3.5,
              (dur_ec, tl.get("end_card"), tl.get("scenes"), srt_text[-120:]))
        frame_end = ec_dir / "last_scene_end.png"
        subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-ss", f"{tl['end_card'][0] - 0.05:.2f}", "-i", str(out_ec),
                        "-frames:v", "1", str(frame_end)], check=True)
        with Image.open(frame_end) as im:
            dark = sum(im.convert("L").getdata()) / (im.width * im.height)
        check("끝 페이드: 카드 직전 마지막 장면 끝은 검게 사라짐", dark < 20, dark)
        bj = ov.brand_dumps({"enabled": True, "file": "logo.png"}, card)
        check("로고 설정을 저장해도 엔딩 카드 설정이 남음 (brand_json에 함께)",
              json.loads(bj)["end_card"]["enabled"] and json.loads(bj)["file"] == "logo.png")
        from app.pipeline import scene_refine
        check("장면 다듬기 지시문이 장면마다 카메라 움직임을 붙이라고 하지 않음 (고정이 기본)",
              "End with one short camera move" not in scene_refine._SYSTEM and "static shot" in scene_refine._SYSTEM)
        check("장면 다듬기: 로봇팔은 '한 대'·'either side' 표현 없음, 장비는 '이름: 부품', 부품 예시는 다른 장비(드론)로만 "
              "(실제 사례: 로봇팔 두 대, 장난감 차, 예시를 베껴 모든 영상의 장비가 같아짐)",
              "ONE robot arm" in scene_refine._SYSTEM and "either side" not in scene_refine._SYSTEM
              and "Name the equipment first" in scene_refine._SYSTEM and "quadcopter drone" in scene_refine._SYSTEM
              and "Do not reuse these words" in scene_refine._SYSTEM and "lidar" not in scene_refine._SYSTEM.lower())
    finally:
        auto_review.ask_vision = orig

    print("\n── 큰 모델(26B): GPU 라이브러리 · 비전 · 질문 (2026-11-06) ──")
    from app.pipeline import llm
    srv_dir = Path(tmp) / "ollama_lib"
    (srv_dir / "cuda_v12").mkdir(parents=True, exist_ok=True)
    (srv_dir / "cuda_v13").mkdir(exist_ok=True)
    exe, model_f, proj = srv_dir / "llama-server.exe", Path(tmp) / "model.gguf", Path(tmp) / "proj.gguf"
    for f in (exe, model_f, proj):
        f.write_bytes(b"x")
    big = {"REFINE_LLM_URL": "http://127.0.0.1:9", "REFINE_LLAMA_SERVER": str(exe), "REFINE_LLAMA_MODEL": str(model_f),
           "REFINE_LLAMA_MMPROJ": str(proj), "REFINE_LLAMA_ARGS": "-ngl 99", "COMFYUI_URL": "http://127.0.0.1:9",
           "REFINE_LLM_START_TIMEOUT_SEC": 5}
    launched = {}

    class _Proc:
        returncode = None

        def poll(self):
            return None

    import subprocess as _sp
    from app.pipeline.video_generator.comfyui import comfy_client
    orig_popen, orig_alive, orig_free = _sp.Popen, llm._strong_alive, comfy_client.ComfyClient.free
    orig_only, orig_av = llm.generate_strong_only, auto_review.ask_vision
    try:
        _sp.Popen = lambda cmd, **kw: launched.update(cmd=cmd, env=kw.get("env") or {}) or _Proc()
        llm._strong_alive = lambda url: bool(launched)
        comfy_client.ComfyClient.free = lambda self, *a, **k: None
        llm._start_strong(big, big["REFINE_LLM_URL"])
        path_head = (launched.get("env", {}).get("PATH") or "").split(os.pathsep)[0]
        check("26B 켜기: projector(--mmproj)를 넘기고, llama-server 옆 가장 새 cuda_v* 폴더를 PATH 앞에 둠 "
              "(실제 사례: 못 찾아 'no usable GPU found'로 CPU에서 돎)",
              "--mmproj" in launched.get("cmd", []) and str(proj) in launched.get("cmd", [])
              and path_head == str(srv_dir / "cuda_v13"), (launched.get("cmd"), path_head))
        check("projector가 없으면 비전 아님 → 이미지 질문은 큰 모델로 보내지 않음",
              llm.vision_ready(big) and not llm.vision_ready({**big, "REFINE_LLAMA_MMPROJ": ""})
              and llm.generate_strong_only("s", "u", {**big, "REFINE_LLAMA_MMPROJ": ""}, image_path="x.png")[0] is None)
        seen = {}
        llm.generate_strong_only = (lambda system, user, config, max_tokens=1200, in_job=False, image_path=None, temperature=None:
                                    seen.update(image=image_path, in_job=in_job, temp=temperature) or (_PASS, ""))
        img2 = Path(tmp) / "review26.png"
        Image.new("RGB", (8, 8), (90, 90, 90)).save(img2)
        auto_review.ask_vision = orig_av
        r26 = auto_review.check_image(img2, "a small model car", big)
        check("AI 점검: 비전 설정이 있으면 26B에 이미지를 보내 점검 (작업 안이라 in_job=True)",
              r26["checked"] and r26["passed"] and seen.get("image") == str(img2) and seen.get("in_job")
              and seen.get("temp") == 0.0, (r26, seen))
        llm.generate_strong_only = (lambda system, user, config, max_tokens=1200, in_job=False, image_path=None, temperature=None:
                                    ("질문: 등장인물은 어느 나라 사람인가요?\n보기: 한국인 | 외국인 | 섞어서", "")
                                    if "제작 원칙" in system else ("없음", ""))
        info = intake.analyze("우리 학과 홍보 영상", big, genres=["홍보·광고"])
        rq = [q for q in info["questions"] if q.get("rule")]
        check("원칙 질문·내용 질문도 큰 모델(26B)로 만든다 (e2b는 간단한 일만 — 사용자 결정)",
              len(rq) == 1 and rq[0]["question"].startswith("등장인물은"), info["questions"])
    finally:
        _sp.Popen, llm._strong_alive, comfy_client.ComfyClient.free = orig_popen, orig_alive, orig_free
        llm.generate_strong_only, auto_review.ask_vision = orig_only, orig_av


def _fake_swap(tmp, fail=False):
    script = Path(tmp) / ("swap_fail.py" if fail else "swap_ok.py")
    body = ("import sys\nsys.exit('대상 이미지에서 얼굴을 찾지 못했습니다.')\n" if fail else
            "import shutil, sys\nshutil.copyfile(sys.argv[2], sys.argv[3])\n"
            "open(sys.argv[3] + '.from', 'w').write(sys.argv[1])\n")
    script.write_text(body, encoding="utf-8")
    return str(script)


def run(check, make_app, ffmpeg, workflows):
    tmp = tempfile.mkdtemp(prefix="ai_video_auto_")
    _unit_checks(check, ffmpeg, tmp)
    print("\n── AI 자동 진행: 기획 → 이미지·점검 → 컷·점검 → ③ 확인 ──")
    fake = FakeComfyUI(ffmpeg).start()
    orig_strong, orig_vision = llm.generate_strong, auto_review.ask_vision
    try:
        llm.generate_strong = _fake_strong
        swap = _fake_swap(tmp)
        app = _app(make_app, tmp, "auto", fake, ffmpeg, workflows,
                   FACESWAP_PYTHON=sys.executable, FACESWAP_SCRIPT=swap)
        client = app.test_client()
        page = client.get("/session/new", query_string={"idea": "우리 학과 홍보 영상", "aspect": "16:9"})
        html = page.get_data(as_text=True)
        check("첫 화면 → 'AI에게 맡기기' 화면 (질문·가정·얼굴 사진·직접 기획하기)",
              page.status_code == 200 and 'id="intake-form"' in html and 'name="face"' in html and "직접 기획하기" in html)
        check("첫 화면 입력창은 AI에게 맡기기로 간다", 'action="/session/new"' in client.get("/").get_data(as_text=True))
        res = client.post("/session/new/analyze", json={"idea": "고양이 브이로그 30초", "ask_llm": False})
        check("분석 API: 질문·가정 JSON", res.status_code == 200 and "assumptions" in res.get_json())

        calls = []
        auto_review.ask_vision = _vision("number 2", 1, calls)
        res = _start(client, face=(io.BytesIO(_PNG_1X1), "me.png"))
        with app.app_context():
            session = PipelineSession.query.order_by(PipelineSession.id.desc()).first()
            rnd = session.current_round
            sid, rid = session.id, rnd.id
            renders = ShotRender.query.filter_by(round_id=rid).all()
            by = {}
            for r in renders:
                by.setdefault(r.shot_id, []).append(r)
            chosen = {k: next(r for r in v if r.selected) for k, v in by.items()}
            auto = rnd.auto_data
            prompt, status = rnd.prompt_text, rnd.status
            face_file = session.face_path
            has_video = bool(rnd.video_path) and os.path.exists(rnd.video_path)
            s01_face = chosen["s01"].review.get("face", "")
            s02_face = chosen["s02"].review.get("face", "")
            from_file = Path(chosen["s01"].image_path + "")  # 합성된 첫 프레임
            orig_kept = from_file.with_name(from_file.stem + "_orig.png").exists()
        check("AI에게 맡기기 → 사람 확인 없이 ③ 확인(video_ready)까지, 올릴 영상 있음",
              res.status_code == 302 and status == "video_ready" and has_video, (status, auto.get("log", [])[-3:]))
        check("AI 기획: 장면 다듬기 결과(영어)가 [샷 목록]에, 6초 → 장면 3개, 질문의 답도 설명에",
              "number 1" in prompt and "number 3" in prompt and "회로 기판과 로봇팔" in prompt
              and session.auto_mode and session.shot_seconds == 2.0, prompt[:200])
        draft_users = [u for s, u in _SEEN if "[장면]" in s]
        check("얼굴 사진을 올리면 '주인공이 직접 나오는 장면 1개 이상'을 기획에 요구 (실제 사례: 세 장면 모두 보드만)",
              draft_users and "주인공이 직접 등장" in draft_users[0], draft_users[:1])
        check("첫 프레임 점검에서 떨어진 s02만 다시 뽑음 (s01·s03은 1장)",
              len([r for r in by["s02"] if r.image_path]) >= 2 and len(by["s01"]) == 1 and len(by["s03"]) == 1,
              {k: len(v) for k, v in by.items()})
        draft_sys = [s for s, _u in _SEEN if "[장면]" in s]
        refine_user = [u for s, u in _SEEN if "You rewrite scene descriptions" in s]
        check("원칙 질문에 답하지 않으면 정해 둔 원칙(한국인·초록 PCB 등)은 들어가지 않음",
              draft_sys and "초록색 유리섬유 PCB" not in draft_sys[-1] and "제작 원칙" not in draft_sys[-1]
              and refine_user and "Production rules" not in refine_user[-1])
        check("다시 뽑을 때 AI 점검의 고쳐 쓰기 의견을 장면 의견으로 넘김",
              any("tiny red cube" in n for n in (sbm_shot(app, rid, "s02").get("notes") or [])))
        check("고른 버전은 모두 첫 프레임·컷 점검 통과, 미달 장면 없음",
              all(c.review.get("image", {}).get("passed") and c.review.get("clip", {}).get("passed")
                  for c in chosen.values()) and auto.get("flagged") == [] and auto.get("phase") == "done", auto.get("flagged"))
        check("컷 점검도 장면마다 했음 (뒤쪽·끝 프레임 2장씩)", sum(1 for k, _ in calls if k == "clip") == 6, calls)
        qwen = [wf for wf in fake.submitted if any(n.get("class_type") == "TextEncodeQwenImageEditPlus" for n in wf.values())]
        qwen_prompts = [n["inputs"]["prompt"] for wf in qwen for n in wf.values()
                        if n.get("class_type") == "TextEncodeQwenImageEditPlus" and n["inputs"].get("prompt")]
        check("얼굴 사진: 사람이 나오는 s01 첫 프레임만 Qwen으로 사진 속 사람을 그림 (사진 업로드 + 이미지 1 유지 지시), 합성은 안 함",
              face_file and os.path.exists(face_file) and "그렸" in s01_face and not s02_face and not orig_kept
              and qwen and all("number 1" in p and "image 1" in p for p in qwen_prompts)
              and all(n["inputs"]["image"].startswith("upload_") for wf in qwen for n in wf.values()
                      if n.get("class_type") == "LoadImage"), (s01_face, s02_face, len(qwen)))
        html = client.get(f"/session/{sid}").get_data(as_text=True)
        st = client.get(f"/api/rounds/{rid}/status").get_json()
        check("③ 확인 화면에 AI 점검 결과·가정한 것·직접 고치기 안내",
              "AI 점검 결과" in html and "모든 장면 통과" in html and "AI가 가정한 것" in html and "직접 고치기" in html)
        check("상태 API: auto 요약·장면별 review",
              st["auto"]["phase"] == "done" and all("image" in s["review"] for s in st["shots"]), st["auto"]["phase"])
        check("③ 확인 뒤에는 기존 도구로 장면 다시 만들기 가능 (직접 고치기)",
              client.post(f"/api/rounds/{rid}/clips/regenerate", json={"shot_id": "s03"}).status_code == 200)

        # 끝까지 기준 미달인 장면 → 최대 횟수만 다시 만들고 가장 나은 버전 + 확인 필요 표시
        calls = []
        auto_review.ask_vision = _vision("number 3", None, calls)
        _start(client)
        with app.app_context():
            rnd = PipelineSession.query.order_by(PipelineSession.id.desc()).first().current_round
            s03 = [r for r in ShotRender.query.filter_by(round_id=rnd.id, shot_id="s03") if r.image_path]
            flagged, status2 = rnd.auto_data.get("flagged"), rnd.status
            sid2 = rnd.session_id
        check("끝까지 미달인 s03: 처음 + 2번 = 3장 만들고 멈춤, 영상은 완성하고 '확인 필요'로 표시",
              len(s03) == 3 and flagged == ["s03"] and status2 == "video_ready", (len(s03), flagged, status2))
        html = client.get(f"/session/{sid2}").get_data(as_text=True)
        check("확인 화면에 미달 장면 표시", "확인 필요 1장면" in html and "기준 미달" in html)

        # 컷이 내용 문제(묘사와 다름)로 한 번 미달 → 첫 프레임부터 다시 그리고 그 첫 프레임으로 컷 (2026-11-06)
        clip_calls = []

        def _clip_content_once(image_path, description, config):
            is_clip = Path(image_path).name.startswith("at")
            clip_calls.append(("clip" if is_clip else "image", description))
            if is_clip and "number 2" in description and sum(1 for k, d in clip_calls if k == "clip" and "number 2" in d) <= 2:
                return ("SCORE: 2\nTEXT: no\nBROKEN: no\nPROBLEM: the gripper is empty, no cube on the desk\n"
                        "FIX: a tiny red cube rests on the desk between the open gripper fingers")
            return _PASS
        auto_review.ask_vision = _clip_content_once
        _start(client)
        with app.app_context():
            rnd = PipelineSession.query.order_by(PipelineSession.id.desc()).first().current_round
            s02 = sorted((r for r in ShotRender.query.filter_by(round_id=rnd.id, shot_id="s02")), key=lambda r: r.version)
            pick = next((r for r in s02 if r.selected), None)
            log_text = " ".join(l["text"] for l in rnd.auto_data.get("log", []))
            s02_notes = sbm_shot(app, rnd.id, "s02").get("notes") or []
        check("컷이 '묘사와 달라요'면 첫 프레임부터 다시 그림 → 새 첫 프레임으로 만든 컷이 뽑히고, 26B의 고칠 점이 장면 의견으로 "
              "(실제 사례: 첫 프레임에 큐브가 없어 컷만 다시 만들면 고쳐지지 않음)",
              len([r for r in s02 if r.image_path]) == 2 and pick is not None and pick.version == 2
              and pick.status == "done" and pick.review.get("clip", {}).get("passed")
              and "첫 프레임부터: s02" in log_text and any("tiny red cube" in n for n in s02_notes),
              ([(r.version, r.status, r.selected) for r in s02], log_text[-160:]))

        calls_b = []
        auto_review.ask_vision = lambda *a: calls_b.append(1) or ("SCORE: 6\nTEXT: no\nBROKEN: no\nPROBLEM: small\nFIX: none"
                                                                  if len(calls_b) == 1 else
                                                                  "SCORE: 4\nTEXT: no\nBROKEN: no\nPROBLEM: off\nFIX: a clear cube")
        img_b = Path(tmp) / "border.png"
        from PIL import Image as _Img
        _Img.new("RGB", (8, 8), (120, 120, 120)).save(img_b)
        r_b = auto_review.check_image(img_b, "d", {"AUTO_REVIEW_MIN_SCORE": 6})
        calls_c = []
        auto_review.ask_vision = lambda *a: calls_c.append(1) or _PASS
        auto_review.check_image(img_b, "d", {"AUTO_REVIEW_MIN_SCORE": 6})
        check("점수가 기준 ±1이면 한 번 더 물어 평균 (6, 4 → 5점 미달, 낮은 쪽의 고칠 점), 기준에서 멀면 한 번만",
              len(calls_b) == 2 and not r_b["passed"] and r_b["score"] == 5.0 and "clear cube" in r_b["fix"]
              and len(calls_c) == 1, (r_b, len(calls_b), len(calls_c)))
        auto_review.ask_vision = _vision(None, 0, [])

        # 글 모델을 못 쓰면 ① 기획에 남고 다시 맡기기·직접 기획 안내
        llm.generate_strong = lambda *a, **k: (None, "template", "없음")
        _start(client)
        with app.app_context():
            rnd = PipelineSession.query.order_by(PipelineSession.id.desc()).first().current_round
            state3, phase3, sid3 = rnd.status, rnd.auto_data.get("phase"), rnd.session_id
        html = client.get(f"/session/{sid3}").get_data(as_text=True)
        check("AI 기획 실패 → ① 기획에 남고 'AI에게 다시 맡기기'·기획 탭 안내 (멈추지 않고 사유 표시)",
              state3 == "prompt_built" and phase3 == "plan_failed" and "AI에게 다시 맡기기" in html
              and 'name="synopsis"' in html, (state3, phase3))
        llm.generate_strong = _fake_strong
        auto_review.ask_vision = _vision(None, 0, [])
        client.post(f"/session/{sid3}/auto/plan")
        with app.app_context():
            state4 = db.session.get(PipelineSession, sid3).current_round.status
        check("AI에게 다시 맡기기 → 끝까지 진행", state4 == "video_ready", state4)

        # 제작 원칙: 정해 둔 규칙이 아니라 맡기기 화면의 원칙 질문에 답한 것만 그 프로젝트의 원칙이 된다
        check("원칙 화면(/settings/rules)은 없어짐", client.get("/settings/rules").status_code == 404)
        orig_gen = llm.generate_strong  # 질문은 큰 모델(26B)로 만든다 (2026-11-06)
        llm.generate_strong = lambda system, *a, **k: (
            ("질문: 등장인물은 어떤 사람인가요?\n보기: 한국인 대학생 | 외국인 교수 | 사람 없이\n"
             "질문: 화면의 분위기는?\n보기: 밝게 | 어둡게 | 차분하게") if "제작 원칙" in system else "없음", "fake", "")
        try:
            res = client.post("/session/new/analyze", json={"idea": "우리 학과 홍보 30초"}).get_json()
        finally:
            llm.generate_strong = orig_gen
        rq = [q for q in res["questions"] if q.get("rule")]
        check("원칙 질문은 AI가 설명을 보고 만든다 (분위기처럼 기본값으로 정할 것은 뺌)",
              len(rq) == 1 and rq[0]["question"] == "등장인물은 어떤 사람인가요?", res["questions"])
        _SEEN.clear()
        _start(client, rule_question_1="등장인물은 어떤 사람인가요?", rule_answer_1="고양이")
        draft_sys = [s for s, _u in _SEEN if "[장면]" in s]
        refine_user = [u for s, u in _SEEN if "You rewrite scene descriptions" in s]
        with app.app_context():
            sess = PipelineSession.query.order_by(PipelineSession.id.desc()).first()
            saved = json.loads(sess.intake_json).get("rules")
        check("원칙 답 → 그 프로젝트의 원칙으로 저장되고 AI로 채우기·장면 다듬기에 들어감 (정해 둔 원칙은 없음)",
              saved == ["등장인물은 어떤 사람인가요 → 고양이"] and draft_sys and "고양이" in draft_sys[-1]
              and "초록색" not in draft_sys[-1] and refine_user and "고양이" in refine_user[-1], (saved, refine_user[-1:]))
        _SEEN.clear()
        _start(client)
        draft_sys = [s for s, _u in _SEEN if "[장면]" in s]
        check("원칙 질문에 답하지 않으면 원칙 없이 기획", draft_sys and "제작 원칙" not in draft_sys[-1])

        # 배경음악 파일: 올리면 그 곡을 쓰고(만들지 않음, 음악 모델 표기 없음), 의견 '다른 곡'으로도 안 바뀜
        from app.pipeline import approval, bgm
        _start(client, music="on", music_file=(io.BytesIO(b"fake-audio"), "my_song.mp3"))
        with app.app_context():
            sess = PipelineSession.query.order_by(PipelineSession.id.desc()).first()
            data = json.loads(sess.intake_json)
            music = bgm.load(sess.current_round)
            song = data.get("music_file", "")
            out, used = bgm.ensure_track(app.config, music, 6, tmp)
            kept = json.loads(approval._next_music(sess.current_round, "reroll") or "{}")
        check("배경음악 파일 → 그 프로젝트의 곡으로 저장, 만들지 않고 처음부터 씀, 음악 모델 표기 대상 아님",
              song.endswith("music.mp3") and os.path.exists(song) and music["uploaded"] == song
              and str(out) == song and used["offset"] == 0.0 and not bgm.is_generated(used)
              and any("올린 곡" in line for line in data.get("shown", [])), (song, music))
        check("올린 곡은 '다른 곡' 의견으로 바뀌지 않음", kept.get("uploaded") == song, kept)
        bad = _start(client, music_file=(io.BytesIO(b"x"), "song.exe"))
        check("음악이 아닌 파일은 거절", bad.status_code == 302 and "/session/new" in bad.headers.get("Location", ""))

        # 물건 사진 여러 장: 장면 묘사에 영어 이름이 나오면 그 사진을 이미지 2로, 사람 장면이면 얼굴이 이미지 1
        def strong_rpi(system, user, config, max_tokens=1200, fallback_config=None, in_job=False):
            text, src, reason = _fake_strong(system, user, config, max_tokens, fallback_config, in_job)
            if "You rewrite scene descriptions" in system:
                text = text.replace("a green circuit board number 1", "a Raspberry Pi 5 board number 1")
            return text, src, reason
        llm.generate_strong = strong_rpi
        auto_review.ask_vision = _vision(None, 0, [])
        _SEEN.clear()
        sub0 = len(fake.submitted)
        _start(client, face=(io.BytesIO(_PNG_1X1), "me.png"),
               ref_file=[(io.BytesIO(_PNG_1X1), "rpi.png"), (io.BytesIO(_PNG_1X1), "lab.png")],
               ref_name=["Raspberry Pi 5", "Lab bench"], ref_kind=["object", "place"])
        llm.generate_strong = _fake_strong
        with app.app_context():
            sess = PipelineSession.query.order_by(PipelineSession.id.desc()).first()
            saved_refs = json.loads(sess.refs_json)
            notes = {r.shot_id: r.review.get("face", "") for r in ShotRender.query.filter_by(round_id=sess.current_round.id)
                     if r.selected}
        images = [wf for wf in fake.submitted[sub0:] if any(n.get("class_type") == "SaveImage" for n in wf.values())]
        qwen = [wf for wf in images if any(n.get("class_type") == "TextEncodeQwenImageEditPlus" for n in wf.values())]
        enc = [n for n in qwen[0].values() if n.get("class_type") == "TextEncodeQwenImageEditPlus"] if qwen else []
        refine_user = [u for s, u in _SEEN if "You rewrite scene descriptions" in s]
        check("참조 사진 2장 저장(이름·종류), 장면 다듬기에 영어 이름을 그대로 쓰라고 알림",
              [(r["name"], r["kind"], r["en"]) for r in saved_refs] == [("Raspberry Pi 5", "object", "Raspberry Pi 5"),
                                                                       ("Lab bench", "place", "Lab bench")]
              and refine_user and '"Raspberry Pi 5"' in refine_user[-1], saved_refs)
        check("묘사에 'Raspberry Pi 5'가 나오는 사람 장면(s01)만 Qwen: 얼굴=이미지 1, 보드 사진=이미지 2 (긍정·부정 모두), 나머지는 Z-Image",
              len(images) == 3 and len(qwen) == 1 and all(e["inputs"].get("image2") for e in enc)
              and "image 2: keep the same shape" in enc[0]["inputs"]["prompt"] and "person from image 1" in enc[0]["inputs"]["prompt"]
              and "얼굴 사진, Raspberry Pi 5" in notes.get("s01", "") and not notes.get("s02"), (len(images), len(qwen), notes))
        from app.pipeline import ref_photos
        refs3 = [{"path": __file__, "en": n, "name": n} for n in ("Robot Arm", "STM32 board", "Raspberry Pi 5")]
        got = ref_photos.pick("A ROBOT ARM next to an stm32-board and a Raspberry Pi 5", refs3, __file__, True)
        orig_gen = llm.generate
        try:
            llm.generate = lambda *a, **k: ("Raspberry Pi 5 on a wooden desk", "fake")
            en1 = ref_photos.english_name("라즈베리파이 5", app.config)
            llm.generate = lambda *a, **k: ("STM32 보드 -> STM32 board.", "fake")
            en2 = ref_photos.english_name("STM32 보드", app.config)
        finally:
            llm.generate = orig_gen
        check("사진 이름 번역은 짧은 이름만 (실제 사례: 'Raspberry Pi 5 on a wooden desk' → 'Raspberry Pi 5'), 영어 이름은 그대로",
              en1 == "Raspberry Pi 5" and en2 == "STM32 board" and ref_photos.english_name("ESP32", app.config) == "ESP32",
              (en1, en2))
        from app.pipeline import draft_fill
        kept = draft_fill.parse("[장면]\n1. 학생이 STM32 보드에 선을 꽂는다\n2. 로봇팔이 큐브를 집는다\n3. 모형차가 달린다\n"
                                "[내레이션]\n1. 하나\n2. 둘\n3. 셋\n스타일: 밝게", 3, 20, ["소개·안내"])
        check("숫자가 붙은 부품 이름(STM32)이 든 장면을 영어로 보고 버리지 않음 (실제 사례: 장면 3개 → 2개)",
              len(kept.get("shots", "").splitlines()) == 3, kept)
        pcb = draft_fill.parse("[장면]\n1. 로봇팔이 큐브를 집는다\n2. 초록색 PCB 위 LED가 켜진다\n3. 모형차가 달린다\n"
                               "[내레이션]\n1. 하나\n2. Embedded software\n3. 셋\n스타일: 밝게", 3, 20, ["소개·안내"])
        check("영어 약어(PCB)가 든 장면도 버리지 않고, 버린 내레이션은 번호를 지켜 다른 장면에 당겨 붙지 않음 (실제 사례: 2026-11-06 장면 6개 → 4개)",
              len(pcb.get("shots", "").splitlines()) == 3 and pcb.get("narration") == "1. 하나\n3. 셋", pcb)
        place = {"path": __file__, "en": "our lab room", "name": "실습실", "kind": "place"}
        got2 = ref_photos.pick("A robot arm in our lab room", [refs3[0], place], __file__, True)
        check("얼굴 먼저, 물건은 장면당 하나(실제 사례: 두 개를 넣으면 하나만 그림), 장소는 함께, 이름은 대소문자·기호 무시, 일부 단어만 나오면 안 맞춤",
              [p["label"] for p in got] == ["person", "Robot Arm"]
              and [p["label"] for p in got2] == ["person", "Robot Arm", "our lab room"]
              and not ref_photos.matches("a raspberry board", refs3[2]))
        check("참조 사진으로 그릴 때 화면은 흐릿한 코드·그래프, 큰 제목·자막은 없게 (2026-11-06 사용자 기준)",
              "small out-of-focus code" in ref_photos.instruction(got) and "no big titles or captions" in ref_photos.instruction(got))

        # 얼굴 사진 여러 명: 사람 장면마다 무작위로, 두 사람 장면은 서로 다른 두 얼굴 (2026-11-06 사용자: 팀원 세 명 얼굴로 랜덤하게)
        from PIL import Image
        face_dir = Path(tmp) / "faces3"
        face_dir.mkdir(exist_ok=True)
        people = []
        for k in range(3):
            fp = face_dir / f"p{k}.png"
            Image.new("RGB", (8, 8), (k * 60, 80, 80)).save(fp)
            people.append({"path": str(fp), "name": f"팀원{k}", "kind": "person"})
        one = [ref_photos.pick("a Korean male student in a grey hoodie sits at a desk", people, None, True, seed=f"s{k}:1")
               for k in range(12)]
        two = ref_photos.pick("two Korean male students look at a monitor", people, None, True, seed="s03:1")
        same = ref_photos.pick("a Korean male student in a grey hoodie sits at a desk", people, None, True, seed="s0:1")
        check("얼굴 여러 명: 사람 장면마다 시드로 무작위 한 명(장면마다 다른 사람이 나옴, 같은 시드는 같은 사람), "
              "두 사람 장면은 서로 다른 두 얼굴 + '첫 번째·두 번째 사람' 지시, 사람 없는 장면엔 얼굴 없음",
              all(len(p) == 1 for p in one) and len({p[0]["path"] for p in one}) >= 2
              and same[0]["path"] == one[0][0]["path"]
              and len(two) == 2 and two[0]["path"] != two[1]["path"]
              and "The first person is the person from image 1" in ref_photos.instruction(two)
              and "The second person is the person from image 2" in ref_photos.instruction(two)
              and ref_photos.pick("one robot arm picks a cube", people, None, False, seed="x") == []
              and "얼굴 사진 3명" in ref_photos.planning_note(people) and ref_photos.refine_note(people) == "",
              ([p[0]["path"][-6:] for p in one], [p["path"][-6:] for p in two]))

        # 사진으로 영상 만들기: 사진 하나 = 장면 하나, 사진이 첫 프레임 (새로 그리지 않음)
        from app.pipeline import photo_scenes
        orig_ask = photo_scenes.ask
        asked = []
        photo_scenes.ask = lambda p, cap, cfg: (asked.append(cap) or
                                                f"A real green circuit board photo {len(asked)} on a lab desk, an LED blinks, slow push-in")
        auto_review.ask_vision = _vision(None, 0, [])
        _SEEN.clear()
        sub0 = len(fake.submitted)
        try:
            _start(client, seconds="10",
                   photo_file=[(io.BytesIO(_PNG_1X1), "a.png"), (io.BytesIO(_PNG_1X1), "b.png")],
                   photo_caption=["LED가 켜진다", ""])
        finally:
            photo_scenes.ask = orig_ask
        with app.app_context():
            sess = PipelineSession.query.order_by(PipelineSession.id.desc()).first()
            rnd = sess.current_round
            board = rnd.storyboard_data
            photos_saved = json.loads(sess.photos_json)
            chosen = [r for r in ShotRender.query.filter_by(round_id=rnd.id) if r.selected]
            status_p, prompt_p = rnd.status, rnd.prompt_text
        new_wfs = fake.submitted[sub0:]
        zimage = [wf for wf in new_wfs if any(n.get("class_type") == "SaveImage" for n in wf.values())]
        videos = [wf for wf in new_wfs if any(n.get("class_type") == "SaveVideo" for n in wf.values())]
        check("사진 2장 → 장면 2개, 사진마다 Gemma가 (적어 둔 동작과 함께) 장면 묘사, 장면당 5초(10초÷2)",
              len(board["shots"]) == 2 and asked == ["LED가 켜진다", ""] and "photo 1" in prompt_p and "photo 2" in prompt_p
              and all(s["duration"] == 5.0 for s in board["shots"]), (len(board["shots"]), asked))
        check("사진이 그 장면의 첫 프레임 (그림 모델을 부르지 않음, 첫 프레임 점검 건너뜀), 컷은 사진을 올려 I2V, ③ 확인까지",
              status_p == "video_ready" and not zimage and len(videos) == 2
              and [s["image"] for s in board["shots"]] == [p["path"] for p in photos_saved]
              and all(s.get("photo") for s in board["shots"])
              and all(r.provider != "previous" and "image" not in r.review for r in chosen)
              and all(r.review.get("clip", {}).get("passed") for r in chosen), (status_p, len(zimage), len(videos)))
        check("사진 장면은 내레이션도 장면 수만큼", prompt_p.count("해설 ") == 2, prompt_p[-300:])
        check("사진 장면은 카메라 고정 — 모델이 쓴 확대·이동은 빼고 고정 카메라를 붙임 (실제 사례: 3~4배 확대로 글자 뭉개짐)",
              "push-in" not in prompt_p.split("[샷 목록]")[1].split("[")[0] and prompt_p.count(photo_scenes.CAMERA) == 2
              and photo_scenes.steady_camera("A green board, an LED blinks. The camera slowly zooms in on the chip.")
              == f"A green board, an LED blinks, {photo_scenes.CAMERA}", prompt_p[:400])
        html = client.get("/session/new", query_string={"idea": "학과 홍보"}).get_data(as_text=True)
        check("맡기기 화면에 '장면 사진으로 만들기' 칸", "장면 사진으로 만들기" in html and "photo_file" in html)

        # Qwen을 쓸 수 없으면(워크플로·모델 없음) 예전처럼 inswapper로 합성
        app2 = _app(make_app, tmp, "auto_noqwen", fake, ffmpeg, workflows, FACESWAP_PYTHON=sys.executable,
                    FACESWAP_SCRIPT=swap, COMFYUI_WORKFLOW_FACE="no_such_workflow")
        llm.generate_strong = _fake_strong
        auto_review.ask_vision = _vision(None, 0, [])
        _start(app2.test_client(), face=(io.BytesIO(_PNG_1X1), "me.png"))
        with app2.app_context():
            rnd = PipelineSession.query.order_by(PipelineSession.id.desc()).first().current_round
            note1 = next(r for r in ShotRender.query.filter_by(round_id=rnd.id, shot_id="s01") if r.selected).review.get("face", "")
        check("Qwen을 쓸 수 없으면 inswapper 합성으로 대신함", "합성" in note1, note1)

        ok, msg = face.apply(dict(app.config, FACESWAP_SCRIPT=_fake_swap(tmp, fail=True)), face_file,
                             str(Path(tmp) / "one.png"))
        check("얼굴을 못 찾으면 합성하지 않고 이유만 남김 (장면은 그대로 진행)", not ok and "얼굴" in msg, msg)
        check("사람 장면만 합성 대상", face.wanted("a young Korean student smiles") and not face.wanted("a robot arm"))
    finally:
        llm.generate_strong, auto_review.ask_vision = orig_strong, orig_vision
        fake.stop()
    reference_checks(check, make_app, ffmpeg, workflows)


def reference_checks(check, make_app, ffmpeg, workflows):
    """참조 영상으로 움직임 따라 하기 (유니언 컨트롤) — 장면에 붙이기·떼기, 워크플로·서명, 결과 파일 고르기."""
    from app.pipeline.video_generator.comfyui.comfy_client import ComfyClient
    from tests.fake_comfyui import make_sample_mp4

    print("\n── 참조 영상으로 움직임 따라 하기 (유니언 컨트롤) ──")
    picked = ComfyClient.output_files({"outputs": {
        "50": {"images": [{"filename": "ref.mp4", "type": "input"}]},
        "39": {"images": [{"filename": "LTX_union_00001_.mp4", "type": "output", "subfolder": "video"}]}}})
    check("결과 파일은 type=output만 (LoadVideo 미리보기로 참조 영상을 돌려받던 버그)",
          [f["filename"] for f in picked] == ["LTX_union_00001_.mp4"], picked)
    tmp = tempfile.mkdtemp(prefix="ai_video_ref_")
    fake = FakeComfyUI(ffmpeg).start()
    try:
        app = _app(make_app, tmp, "ref", fake, ffmpeg, workflows, COMFYUI_STAGED_REVIEW=True)
        client = app.test_client()
        client.post("/session/create", data={"title": "로봇팔", "synopsis": "로봇팔이 큐브를 옮긴다",
                                             "shots": "A white robot arm moves a small cube on a desk\n"
                                                      "A small model car drives on a track"})
        with app.app_context():
            session = PipelineSession.query.order_by(PipelineSession.id.desc()).first()
            sid, rid = session.id, session.current_round.id
        client.post(f"/session/{sid}/generate")
        client.post(f"/api/rounds/{rid}/images/approve")
        ref = make_sample_mp4(ffmpeg, Path(tmp) / "phone.mp4", seconds=1)
        url = f"/api/rounds/{rid}/shots/s01/reference"
        bad = client.post(url, data={"video": (io.BytesIO(b"x"), "a.txt")}, content_type="multipart/form-data")
        sub0 = len(fake.submitted)
        res = client.post(url, data={"video": (open(ref, "rb"), "phone.mp4")}, content_type="multipart/form-data")
        wf = fake.submitted[-1] if len(fake.submitted) > sub0 else {}
        loads = [n["inputs"].get("file") for n in wf.values() if n.get("class_type") == "LoadVideo"]
        lora = [n["inputs"].get("lora_name") for n in wf.values() if n.get("class_type") == "LTXICLoRALoaderModelOnly"]
        bypass = [n["inputs"].get("bypass") for n in wf.values() if n.get("class_type") == "LTXVImgToVideoInplace"]
        st = client.get(f"/api/rounds/{rid}/status").get_json()
        s01 = next(s for s in st["shots"] if s["id"] == "s01")
        check("영상이 아닌 파일은 400", bad.status_code == 400)
        check("컷 확인에서 참조 영상 붙이기 → 그 장면만 유니언 워크플로로 새 버전 (참조 영상 업로드, 첫 프레임 우회)",
              res.status_code == 200 and res.get_json()["regenerating"] and len(fake.submitted) - sub0 == 1
              and loads and loads[0].startswith("upload_") and "union-control" in (lora[0] if lora else "")
              and bypass == [True] and st["state"] == "clips_ready" and len(s01["versions"]) == 2
              and s01["reference"]["name"] == "phone.mp4", (res.get_json(), loads, lora, bypass, st["state"]))
        with app.app_context():
            board = db.session.get(VideoRound, rid).storyboard_data
        stored = board["shots"][0]["reference"]["video"]
        check("참조 영상은 장면 폴더에 내용 해시 이름으로 저장, 장면 서명에 들어감",
              os.path.exists(stored) and "reference_" in stored
              and from_sig(board, 0) != from_sig({**board, "shots": [{**board["shots"][0], "reference": None}]}, 0))
        same = client.post(url, data={"video": (open(ref, "rb"), "again.mp4")}, content_type="multipart/form-data")
        check("같은 영상을 다시 올리면 다시 만들지 않음", same.get_json()["changed"] is False)
        sub0 = len(fake.submitted)
        res = client.post(url, data={"clear": "1"}, content_type="multipart/form-data")
        wf = fake.submitted[-1] if len(fake.submitted) > sub0 else {}
        check("참조 영상 떼기 → 일반 I2V로 다시 만듦",
              res.get_json()["regenerating"] and not any(n.get("class_type") == "LoadVideo" for n in wf.values()),
              res.get_json())
        html = client.get(f"/session/{sid}").get_data(as_text=True)
        check("장면 화면에 '참조 영상으로 움직임 따라 하기' 패널", "참조 영상으로 움직임 따라 하기" in html and "reference-form" in html)
    finally:
        fake.stop()


def from_sig(board, i):
    from app.pipeline import storyboard_model as sbm
    shot = {k: v for k, v in board["shots"][i].items() if v is not None}
    return sbm.shot_signature(board, shot)


def sbm_shot(app, round_id, shot_id):
    with app.app_context():
        board = db.session.get(VideoRound, round_id).storyboard_data
    return next(s for s in board["shots"] if s["id"] == shot_id)

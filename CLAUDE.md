# CLAUDE.md

Claude Code가 이 저장소에서 일할 때 **반드시 기억해야 할 것**만 모은 문서다.
- 프로젝트 소개·화면·기능·설치 방법 → `README.md`
- 진행 상황·실험 결과·날짜별 결정 기록·남은 일 → `PROGRESS.md` (작업을 마치면 여기에 기록을 남긴다)
- 영상 파이프라인 통합 설계서 → `VIDEO_PIPELINE_INTEGRATION.md`

## 0. 한 줄 요약

국립군산대학교 임베디드소프트웨어학과 캡스톤. **사용자가 원하는 영상을 로컬 AI(ComfyUI + LTX-2.5)로 장면 단위로 만들고,
YouTube 시청자 댓글에서 건설적 피드백을 골라 묶어, 제작자가 승인한 의견만 다음 버전에 반영하는** Flask 웹 앱.
시연 소재는 학과 홍보영상이다. 핵심 원칙: **외부 계정 없이 동작한다** (YouTube는 키가 있을 때만 선택적으로). 상용 영상 API(Runway)는 2026-11-05에 뺐다.

용어: 프로젝트 = `PipelineSession`, 버전(N차) = `VideoRound`, 장면 결과 1건 = `ShotRender`, 작업 큐 = `GenerationJob`.

## 1. 절대 규칙

- **이 폴더(`D:\capstone\project_ai_feedback`)가 원본이다.** 수정은 여기서만 한다. 쓰지 않는 옛 파일은 `_archive/`(git 제외)로 옮긴다.
- **ComfyUI 설치 폴더·모델·`extra_model_paths.yaml`은 사용자 승인 없이 건드리지 않는다** (`D:\capstone\ComfyUI`, `D:\capstone\Comfy-Desktop`).
  승인받아 추가한 것: `custom_nodes/ComfyUI-LTXVideo`, LoRA `ltx-2.3-22b-ic-lora-motion-track-control-ref0.5` (변경 전 패키지 목록 `D:\capstone\tools\comfy-backup`),
  LoRA `ltx-2.3-22b-ic-lora-union-control-ref0.5` (654MB, 2026-11-03), Qwen-Image-Edit-2511 일체(2026-11-03 승인:
  `diffusion_models/qwen_image_edit_2511_fp8_e4m3fn_scaled_lightning_comfyui_4steps_v1.0` 20.4GB, `text_encoders/qwen_2.5_vl_7b_fp8_scaled` 9.4GB, `vae/qwen_image_vae`).
- 워크플로 출력 받기: `LoadVideo` 같은 입력 노드도 history `outputs`에 미리보기(type=input)를 남긴다 — 결과는 `type == "output"`인 파일만 받는다.
  패키지는 **ComfyUI를 끈 뒤** 기존 버전을 constraints로 고정해 설치한다 — 켠 채로 uv 설치하면 파일 잠금으로 tokenizers 등이 깨진다.
- `.env`, API 키, `instance/youtube_token.json`, 자격증명은 커밋하지 않는다. GitHub 저장소(`pgh165/project-ai-feedback`)는 **공개**다:
  사진(`promo_draft/assets/`, png/jpg)·엑셀·영상·오디오·가상환경·`_archive/`는 올리지 않는다.
- 팀원 개인정보(학번·연락처·이메일)는 코드·문서 어디에도 넣지 않는다.
- 모델명·임계값·경로는 하드코딩하지 않고 `config.py` + `.env`로만 주입한다 (promo_draft는 `config.yaml`).
- **상태 전이는 `orchestrator.advance(round, to_state)` 한 곳에서만.** 라우트·작업에서 `round.status`를 직접 대입하지 않는다.
- **AI가 화면에 글자를 만들지 않게 한다 — 글자·제목·로고는 후처리(편집)로 넣는다** (사용자 결정, 2026-11-03).
  `text_free.make_textless`가 `resolve_params`에서 모든 생성 프롬프트의 간판·책·라벨 등을 '글자 없는 모습'으로 긍정 묘사한다.
  **모니터는 '흐릿한 코드·그래프'까지 허용**(2026-11-06 사용자: 모니터 속 글씨는 뭉개져 괜찮고, 화면 위에 자막처럼 크게 뜨는 글자가 문제)
  (부정 프롬프트는 무효). 기획 지시문(draft_fill·scene_refine)도 글자 나올 물건을 장면 중심에 두지 않게 한다. 장면 서명은 원래 묘사로 계산.
- **영상·이미지 모델 프롬프트에 한국어·로고·화면 글자를 넣지 않는다.** 이미지 모델이 화면에 글자로 그려 버린다.
  로고는 `overlay.py`로 합성, 자막은 `.srt` → YouTube 자막 트랙. 번역에 실패하면 만들지 않고 안내한다(한국어를 몰래 넣지 않음).
- YouTube 업로드는 ③에서 제작자가 'YouTube에 올리기'를 눌러야만 한다(`containsSyntheticMedia=true`).

## 2. 작업 방식

- 가상환경: 앱 `.\venv\Scripts\python.exe`, TTS는 `venv_tts2`(edge-tts·Supertonic). 실행은 이 폴더에서 `.\venv\Scripts\python run.py` (http://127.0.0.1:5000).
- `run.py`는 `debug=False` — **코드를 고친 뒤에는 Flask 서버를 다시 켜야** 반영된다. 끄기 전에 `generation_job`에 queued/running 작업이 없는지 본다.
- **검증은 pytest가 아니라 `.\venv\Scripts\python run_check.py` 하나**다. 임시 DB + `JOBS_SYNC=True` + mock 어댑터 + 시뮬레이션으로 2차까지 돌리고
  `tests/video_checks.py`(가짜 ComfyUI `tests/fake_comfyui.py`)·`tests/custom_video_checks.py`를 부른다. 약 270개, 몇 분.
  끝에 "모든 검증 통과" 또는 "실패 N건"(종료 코드 1). 실제 `instance/pipeline.db`는 건드리지 않는다. 러프컷 검증은 실제 ffmpeg(`FFMPEG_PATH`)를 쓴다.
- 검증 하나만 돌리는 옵션은 없다. 새 기능 검증은 `check(label, condition, detail)`를 부르는 `_xxx_checks(client, app)` 함수에 추가한다.
  외부 의존(LLM·TTS 등)은 모듈 함수를 잠시 바꿔 끼우고 `finally`에서 되돌린다.
- **최적화 방향(사용자 결정, 2026-11-03)**: 사람의 확인을 줄이고 AI가 대부분 처리한다. 새 기능은 기존 코드를 먼저 재활용하고, 필요할 때만 새로 만든다.
- 화면 문구는 쉬운 한국어로 쓰고, 긴 설명은 화면에 쓰지 않고 `_ui.tip(text)`(! 아이콘 툴팁)로 둔다.
- ComfyUI 서버 http://127.0.0.1:8188. 영상 생성 중에는 GPU를 쓰는 다른 프로그램을 끈다(RTX 5070 Ti 16GB, VRAM 부족 시 극도로 느려짐).
  생성 전 시스템 RAM 여유를 확인한다(부족해서 작업이 중단된 적 있음).
- 큰 작업을 마치면 `PROGRESS.md`의 진행 기록·남은 일을 갱신한다.

## 3. 사용자 선호 (영상 제작)

> 아래는 **이 홍보영상을 만들 때** Claude가 지킬 선호다. 앱에는 정해 둔 원칙을 넣지 않는다(2026-11-05 사용자 결정):
> 앱의 제작 원칙은 프로젝트마다 'AI에게 맡기기' 화면에서 AI가 만든 원칙 질문에 사용자가 답한 것이다(`plan_rules.py`).
> 화면 글자 금지만은 묻지 않고 항상 적용한다(`text_free.py`).

- 학과 소개·홍보영상은 **캠퍼스·건물·걷는 학생 같은 학교 풍경이 아니라, 학과에서 배우고 만드는 것**을 보여 준다.
  "한눈에 알아볼 수 있는" 소재만: 회로 보드, 로봇팔, 자율주행 모형차. 흰 배경·고른 조명, **한 컷 한 동작**.
  스마트홈·IoT 방·야경 책상·캠퍼스·코드 모니터 장면은 쓰지 않는다.
- 등장인물은 **한국인**으로 지정한다. 기판은 **초록색 유리섬유 PCB**처럼 보이게.
- 화면 속 글자·로고는 생성하지 않고 편집에서 합성한다.
- 마음에 안 드는 컷은 통째로 새로 만들길 원한다. **시드 2~3개를 뽑아 비교하고 추천**을 준다.
- 결과 확인은 프레임 4장으로 하되, 움직임은 사용자가 직접 재생해 봐야 한다고 알린다.
- 이미 고른 컷은 다시 만들지 않는다 — 새 컷 전에 `PROGRESS.md`의 콘티·선택 컷을 먼저 본다.

## 4. LTX-2.5 생성 요령 (확인된 한계)

- distilled, **CFG=1이라 부정 프롬프트가 효과 없다.** 원하는 것을 구체적으로 쓴다. "빈 운전석" 같은 부정 표현은 못 따른다 → "틴팅 창문, 무인 로보택시"처럼 긍정 묘사.
- 회로기판·모터·납땜 손동작·화면 글자/코드는 T2V가 못 만든다 → **실물 사진 I2V**, 카메라 거의 고정, 움직임은 LED 점등·모터 회전 정도.
- 첫 프레임에 없는 물체를 줌아웃·전환으로 만들어 넣는 건 대체로 실패한다. 한 클립에 동작 하나.
- 정지 이미지 I2V는 "카메라 쪽으로 달려와 커지는" 전진 동작을 못 만든다(차가 같은 크기로 머물거나 제자리 회전). 전진 장면은 **T2V**로 시드를 바꿔 뽑는다.
- I2V는 회로(LED 점등·푸시인)·로봇팔(집어 올리기/옮기기)에는 잘 된다.
- 해상도는 64 배수(`size_multiple`), 프레임은 8n+1. 1280x704·121프레임 ≈ 160초/컷, 1920x1088 4~5초 ≈ 220~270초, VRAM ≈ 15.6GB.
- 얼굴 I2V: 표정이 커질수록 얼굴이 달라진다. "smile"은 과장되므로 "거의 무표정, 입꼬리만 아주 조금" + 짧게.
  inswapper 얼굴 합성은 흐려지고 안경테·앞머리를 망치며 비상업 라이선스라 **앱에서 뺐다**(2026-11-09, 코드는 `_archive/unused_code/face_inswapper.py`).
- Motion Control IC-LoRA: 경로대로 움직이지만 대상 모양이 변하거나 늘어날 수 있다. 2D 계산이라 옆에서 본 팔에만 맞는다. 집어 올리기는 시드에 따라 물체가 안 따라와서 `takes` 여러 개를 뽑는다.

## 5. 핵심 설계 (코드 고칠 때 지켜야 할 것)

### AI 자동 진행 (기본 흐름, 2026-11-03~)
사람은 **처음 설명과 마지막 ③ 확인만** 한다. 첫 화면 → `/session/new`(질문·가정) → `POST /session/auto` → `auto_plan` 작업 → `generate_video {"auto": true}`.
- `intake.py`: 종류·길이·비율·내레이션·음악은 규칙으로 읽고 못 읽으면 기본값을 "이렇게 가정했어요"로. 종류를 모를 때만 그 칸을 질문으로. 내용 질문은 26B가 최대 2개(못 쓰면 e2b) — 못 쓰면 질문 없이 진행(멈추지 않는 것이 우선).
  제작 원칙 질문(`rule_questions`, 최대 2개, `key=rN`·`rule: true`)도 26B가 설명을 보고 만든다(두 질문에 서버 한 번). 폼 `rule_question_N`/`rule_answer_N` → `plan_rules.from_answers`
  → `intake_json["rules"]` → `plan_rules.for_session` → `draft_fill.fill`·`scene_refine.refine`의 `form["rules"]`. 답하지 않으면 원칙 없음. 전역 원칙 파일·화면은 없다.
- 배경음악 파일: 맡기기 화면 `music_file`(또는 ③ 꾸미기 `music_upload`) → `music_json["uploaded"]`. 올린 곡이 있으면 `bgm.ensure_track`이 만들지 않고 처음부터 쓰고(offset 0),
  AI 생성 곡 표기(`bgm.is_generated`·`bgm.credit`)를 붙이지 않으며, '다른 곡' 의견으로 바꾸지 않는다. 없을 때만 AI가 만든다(`MUSIC_ENGINE`, 기본 Stable Audio).
- `auto_pipeline.run_auto_plan`: `draft_fill.fill` → `scene_refine.refine` → `build_initial_prompt` → `ko_en` (전부 `in_job=True`로 큰 모델 사용 가능). 실패하면 ① 기획에 남기고 `auto_json.phase=plan_failed` + 'AI에게 다시 맡기기'.
- `auto_pipeline.run_auto`: `run_staged(..., hold=True)`로 images_ready·clips_ready에서 **멈추지 않고** 이미지 → `auto_review` 점검 → 미달만 `_reroll_shot`(fix 문장을 장면 의견으로) → 컷 → 점검 → `_pick_best` → `finalize_generation`. 재시도 최대 `AUTO_REVIEW_MAX_RETRIES`(2). 끝까지 미달이면 가장 나은 버전 + `auto_json.flagged`.
- `auto_review.py`: 이미지 = 26B 비전(projector 있으면, 점검 한 번에 서버 한 번) → 못 쓰면 e2b (SCORE/TEXT/BROKEN/FIX), 컷 = ffmpeg 프레임 차이(멈춤)·밝기(어두움)·**첫·끝 프레임 줌 배율**(가장자리 띠, 장면 줄이 카메라 움직임을 원하지 않을 때) + **뒤쪽 75%·끝 95% 두 장을 묘사 일치 점수로**. '형태 깨짐'은 PROBLEM 문장에 실제 변형 단어가 있을 때만(26B는 묘사 차이도 BROKEN이라 답한다). **AI가 답하지 못하면 통과(점검 못 함)로 두고 다시 만들지 않는다.** 결과는 `ShotRender.review_json`.
- 멈춘 컷(kind=still)·줌은 의견 없이 시드만 바꾸고, 생김새 문제(look)는 fix 문장을 의견으로 넘긴다. **컷이 '묘사와 달라요'면 첫 프레임부터 다시 그린다**(`_redraw_first_frames`) — 첫 프레임에 없는 것은 컷에서 생기지 않는다.
  26B 점검은 temperature 0, 점수가 기준 ±1이면 한 번 더 물어 평균(`ask_scored`).
- **참고 사진 (인물·물건·장소, 한 칸, 2026-11-07 사용자: 원본·데모 모두 Qwen을 쓰지 않는다)** — 맡기기 화면 `ref_file`·`ref_name`·`ref_kind`(person/object/place, 최대 6장)
  → `PipelineSession.refs_json`. 예전 단일 `face` 필드(`face_path`)도 받기는 한다(인물 1명으로 취급).
  기본 `REF_PHOTO_MODE=reference`: 자동 기획 때 26B 비전을 한 번 켜서 사진마다 생김새를 영어로 읽고(`face.describe`·`ref_photos.describe`, 못 쓰면 e2b, `ref["desc"]`),
  기획 뒤 `auto_pipeline._refs_into_shots`가 장면 줄마다 `ref_photos.pick`(사람 장면 → 인물 사진 중 시드로 무작위, 두 사람이면 서로 다른 두 명 /
  묘사에 영어 이름(en)이 나오면 그 물건·장소, 물건은 장면당 하나, 최대 3장)으로 고른 사진의 생김새를 `ref_photos.describe_line`으로 붙인다
  ('; the main person is …' / '; the Raspberry Pi 5 is …' / '; the place is …'). 첫 프레임은 Z-Image가 그린다 — `_ref_draw`는 아무것도 안 한다.
  51번처럼 장면 줄마다 생김새를 적어야 장면이 바뀌어도 같은 사람·물건으로 그려진다. 영어 이름은 자동 기획 때 정하고 `scene_refine`에 "그대로 쓰라"(`refine_note`).
- `REF_PHOTO_MODE=draw`(예전 방식, 기본 아님): 고른 사진으로 첫 프레임을 **Qwen-Image-Edit-2511**로 그린다
  (`orchestrator._ref_draw` → `params["refs"]` → `ComfyUIAdapter._first_frame`, 워크플로 `qwen_face_t2i` + 이미지 2·3 노드는 코드가 붙임, 프롬프트 끝에 `ref_photos.instruction`).
  Qwen 그림은 색이 진해 `tone.soften`(채도 85%·대비 90%, `QWEN_TONE_*`)으로 낮춘다. Qwen을 못 쓰면(`face_problem`) 사진 없이 그린다.
- 장면 사진(사진 → 영상, `photo_scenes.py`, `PipelineSession.photos_json`): 사진 하나 = 장면 하나(올린 순서), **사진 자체가 첫 프레임**.
  `ensure_storyboard`가 `photo_scenes.attach`로 `shot["image"]`·`shot["photo"]`를 붙이고 `run_staged`가 그대로 복사(provider `photo`).
  장면 묘사는 Gemma가 사진을 보고(사용자가 적은 동작 반영) 쓰며 장면 다듬기는 건너뛴다. 자동 진행의 첫 프레임 점검·다시 그리기도 건너뛴다.
- 참조 영상(유니언 컨트롤, `reference_video.py`): `shot["reference"]`가 있으면 `ltx25_union`으로 그 영상의 Canny 윤곽선을 따라 만든다. 기본은 첫 프레임 우회(`bypass_image`).
  우선순위 끝 프레임 > 참조 영상 > 움직임 경로. API `POST /api/rounds/<id>/shots/<sid>/reference`(multipart video / clear), 단계 규칙은 움직임 경로와 같다.
- 기존 단계별 확인 도구(다시 뽑기·움직임 지정·끝 프레임)는 **'직접 고치기'로 남긴다** — 자동 프로젝트도 ③에서 쓸 수 있다. '직접 기획하기'(`/session/create`)는 예전 흐름 그대로.
- 자동 모드 판단은 `PipelineSession.auto_mode` 하나. `start_generation`·`retry_failed`가 이걸 보고 `{"auto": true}`로 보낸다.

### 프롬프트 문서
- `[섹션명]` 헤더 텍스트, 버전 관리·diff 대상. 섹션: 프로젝트 / 영상 종류 / 버전 / 등장 요소 / 이전 버전 요약 / 영상 설명 / 샷 목록 / 내레이션(선택) / 영상 스타일 / 시청자 반영 사항 / 연출 지침 (+ 불러온 영상이면 자막).
- 영상 모델에는 **[샷 목록] 각 줄(영어) + [영상 스타일]만** 전달된다. 샷 1개 = 클립 1개.
- 샷 줄 앞 `[6초]`·`[6초 시드 202]`는 그 샷의 길이·시드(`prompt_builder._SHOT_MARK`, 모델에는 안 감).
- [내레이션]은 샷 번호와 1:1 한국어 해설. **장면 서명에 안 들어가므로** 해설만 고치면 음성만 다시 합성된다.
- **프롬프트 문서가 원본, 스토리보드는 파생 스냅샷**(`storyboard_model.build_storyboard()` → `VideoRound.storyboard_json`). 편집 설정 `storyboard["edit"]`도 서명에 안 들어간다.
- 한국어 입력: 저장 시 `ko_en.py`가 [샷 목록]·[영상 스타일]의 한글 줄만 영어로 바꾸고, 번역 캐시(`TextTranslation`, engine=`user`)로 화면에는 원문을 보인다.
  `ko_en.needs_translation`은 번호·`[N초]`·따옴표 안을 빼고 판단한다 — 안 빼면 이미 영어인 줄을 저장마다 다시 번역하다 실패했다.

### 다음 버전 반영
- [영상 설명]·[샷 목록]은 유지. 승인된 content/story → [시청자 반영 사항], 나머지 → [연출 지침].
- **장면을 다시 만드는 건 반영 위치(`ClusterResult.apply_target`, `feedback_target.py`)가 scene·motion인 의견뿐.** audio·edit·content는 장면 재사용.
  - scene: 첫 프레임 이미지부터 다시(시드 변경). motion: 이전 첫 프레임 유지, 컷만.
  - audio·edit: 편집 설정(`edit_plan.py`)으로 러프컷 합성에만 반영 (`suggest_changes` → `VideoRound.next_edit_json` → 다음 `edit_json` → `rough_cut.build(edit=…)`).
  - content: LLM이 [영상 설명]·[내레이션]만 다시 씀(`prompt_updater.refine_sections`). **[샷 목록]은 LLM이 바꾸지 않는다** — 바뀌면 지적 안 된 장면까지 다시 만든다.
    내레이션은 같은 번호·한국어·장면 길이 안·새 영어 단어 없음 검사를 통과할 때만 쓴다.
- 부분 재생성(`orchestrator.plan_reuse`): 프롬프트·스타일·길이·해상도가 같고 지적 안 된 장면은 `ShotRender(status=reused, reused_from_id)`.
- 지적 장면 계산(`assign_affected_shots`): 댓글 타임스탬프 → 장면. 없으면 technical/subtitle/audio는 전 장면, 그 외 LLM 추정 → 실패 시 전 장면.
- 장면 의견 고쳐 쓰기: 한국어 `notes` → `feedback_target.revise_shot_prompt`가 **장면 묘사 자체를 고친** 영어 `prompt_fixed`로 원래 묘사를 대체(`final_prompt`).
  꼬리에 붙이면 작은 모델이 문제를 그대로 묘사했다("사진이 없어" → "empty frame"). 지시문: 고친 상태만, 실수 묘사 금지, **최소 수정**.
  인물 생김새(`kept_phrases`: 옷·머리·안경)가 빠지면 한 번 더 묻고 그래도 빠지면 되살린다. `prompt`(원래 묘사)는 그대로라 서명은 안 바뀐다. 실패하거나 영어 의견이면 `notes_en`을 꼬리에.
- 영상 묘사(`prompt_fixed`, 동작 순서)와 첫 프레임 묘사(`image_prompt_fixed`, 동작 시작 전)를 따로 만든다 — 하나로 쓰면 이미지 모델이 동작 중간을 그렸다.
- 생김새 의견으로 컷을 다시 만들면 첫 프레임부터 다시 뽑아 `images_ready`에서 멈춘다(같은 이미지로는 생김새가 안 바뀜).
- 길이 요청('3초로', '2초 더', '두 배', '늘려줘', '짧게')은 `feedback_target.length_request`로 [N초]에 남긴다(최대 `SHOT_MAX_SECONDS`), 길이만 말한 문장은 묘사에 붙이지 않는다(`is_length_only`).
  양을 말하지 않으면 내레이션을 합성해 재서(`orchestrator._narration_fit_seconds`) 1배속으로 들어가는 길이로 맞춘다.
- 모호한 의견(`vague_reason`)이면 다시 만들기 API가 422로 방향을 묻고, '그래도 진행'은 `force`.

### 상태 머신
```
① prompt_built → ② generating (images_ready / clips_ready / generation_failed)
→ ③ video_ready → ④ uploaded → ⑤ feedback_collected → ⑥ feedback_classified
→ ⑦ clustering → ⑧ pending_review → ⑨ prompt_updated → 다음 버전 ①
```
- 같은 단계 반복(댓글 추가·재분류·재군집화)은 자기 전이로 허용. 허용 안 된 전이는 `InvalidTransition`.
- **사람의 결정은 공개·반영 두 번**(2026-11-08 사용자, 발표 자료와 같게): 공개 뒤 화면이 1분마다 `POST /api/rounds/<id>/auto-collect`로
  댓글을 모으고(묶음이 생기기 전 단계에서만), 댓글을 넣으면 분류 → 묶기까지 자동(`orchestrator.process_feedback`), 묶음은 **반대 의견이 없으면 '반영'으로
  미리 골라 둔다**(`approval.save_clusters`, 반대 의견 있는 묶음만 '결정 전'). 사람은 검토 화면에서 빼고 싶은 것만 끄고 '반영하고 다음 버전 만들기' 한 번.
  `collect`는 새 댓글이 없으면 상태를 바꾸지 않는다(공개 직후 빈 수집에도 '공개 취소'가 그대로 됨). '지금 모으기'·'의견 추가'·'장면 의견 추가'도 같은 흐름.
  분류·묶은 뒤(feedback_classified·pending_review)에도 댓글을 더 넣을 수 있고(→ feedback_collected 전이), 넣으면 처음부터 다시 묶어 검토 결정이 초기화된다.
  새 댓글 없이 다시 모으면 아무것도 바꾸지 않는다. '판정 바꾸기'도 pending_review에서 가능하고 바꾸면 다시 묶는다. `/filter`·`/cluster` 라우트와 `/api/rounds/<id>/advance`는 단계별로 남아 있다.
- 단계별 확인(`COMFYUI_STAGED_REVIEW=true`): 이미지 확인 → 컷 확인 → **사람이 '러프컷 만들기'를 눌러야** 합성. 러프컷은 자동으로 만들지 않는다.
  다시 뽑으면 시드 +101, 새 `ShotRender` 버전이 쌓인다(이전 이미지 `discarded`, 이전 컷은 고를 수 있음).
- 화면 단계: ② 제작 = 장면별(이미지 → 컷), ③ 확인 = 러프컷. 서버 상태는 `generating`이어도 러프컷 작업(`payload.stage == "rough"`)이면 ③에 표시(`compute_steps`). 단계 계산은 `routes/session._stage_of`.
- `select`는 선택만 바로 저장하고 ③이면 러프컷을 작업 큐로 다시 합성한다(요청 안에서 합성하면 화면이 멈췄다).
- `cancel`: 대기 작업 취소, 실행 중 장면은 ComfyUI `/interrupt` → `generation_failed`. 만든 장면은 남는다.
- 제작 중 기획 변경: `orchestrator.plan_diff`가 (묘사, 길이)로 짝지어 고친·새 장면만 다시 만든다. 스타일이 바뀌면 전체, 내레이션만 바뀌면 음성만. 저장 전 `preview=1`로 확인.

### 영상 생성
- **카메라는 기본 고정**(`camera.py`): [영상 스타일]은 모든 장면에 붙으므로 카메라 움직임을 지우고, 장면 줄에 움직임이 없으면 고정 카메라를 붙인다. 장면 다듬기도 'static shot'이 기본.
- 기본 어댑터 `comfyui`(LTX-2.5). ComfyUI가 꺼져 있으면 mock으로 폴백하고 화면에 "mock으로 대체됨". `local`(diffusers Wan)·`mock`은 설정으로만 선택.
- 이미지 먼저(`COMFYUI_IMAGE_FIRST`): Z-Image Turbo로 첫 프레임 → LTX I2V. `COMFYUI_FREE_BETWEEN_SHOTS=true` 권장.
- 워크플로는 `promo_draft/workflows/`(Export(API) JSON + `mapping.yaml`) 한 곳에서 관리하고 앱은 읽기만 한다:
  `ltx25_t2v`, `ltx25_i2v`, `ltx25_flf2v`(끝 프레임 고정), `ltx25_motion`(움직임 경로), `zimage_t2i`. 이름·매핑을 바꾸면 promo_draft CLI도 확인한다.
- 끝 프레임 고정(`regenerate_clip_to_frame`): **같은 컷에서 뽑은 화면만** 끝으로 쓴다(Z-Image로 따로 그리면 구도가 달라 겹쳐 보였다). 끝 프레임 고정이 움직임 경로보다 우선.
- **자동 움직임 경로**(`auto_motion.py`, `AUTO_MOTION`, 2026-11-06): 컷 단계 시작(`run_staged` clips)에서 장면마다 26B 비전에 첫 프레임+묘사를 보여
  ① 종류(move/pick/none)·방향·물체/집게/고정할 것의 **이름**, ② 그 이름들의 상자를 **이름 붙은 칸 {x_min,y_min,x_max,y_max}(0~1000)**로 묻는다
  (한 번에 묻거나 숫자 목록으로 받으면 [y,x] 순서가 섞이고 JSON이 깨졌다). move = 물체 위 격자를 **같은 높이로** 평행 이동(위로 휘면 떠오름),
  pick = 손가락 끝(손가락별 상자 아래 가운데)·집게 몸통·물체만 내려가기→닫기→들어 올리기, **팔 관절은 찍지 않는다**(관절 없이도 팔 모양 유지, 26B 관절 위치는 부정확).
  사람이 준 경로·참조 영상·끝 프레임 다시 만들기는 건드리지 않고, 자동 경로는 `motion["auto"]` 표시·첫 프레임이 바뀌면 다시 계산. 못 찾거나 공간이 없으면 경로 없이 진행.
- 움직임 경로(`motion_tracks.py`): `shot["motion"]`에 저장, 그린 첫 프레임 파일에 묶임(`is_active`) — 첫 프레임을 다시 뽑으면 경로 없이 만들고 `motion.stale`로 알린다.
  경로 계산은 서버(`build_preset`, 순·역기구학)만 한다(점을 손으로 옮기면 팔이 늘어났다). 노드·LoRA가 없으면 일반 I2V로 몰래 넘어가지 않고 409로 설치 안내(`motion_problem`).
  문장 → 동작(`motion_plan.py`): LLM은 좌표가 아니라 관절 각도(`joints`)·`grasp`·`unsupported`만 답한다. 관절 위치는 사람이 찍는다.
- 산출물: `instance/videos/session_<id>/round_<n>/<shot_id>/v<k>.mp4`(+`v<k>.png`), `rough_cut.mp4`, `subtitles.srt`, `bgm_*.flac`.

### 러프컷·소리·꾸미기
- 러프컷: 선택된 컷을 해상도·fps로 정규화, 앞뒤 잘라 냄(`CLIP_TRIM_HEAD/TAIL_SEC`), 디졸브 0.4초 기본, 오디오 없는 조각엔 무음 트랙.
  보이는 몫 = 컷 길이 − 잘라 냄 − 디졸브 겹침(마지막 제외), 말할 수 있는 몫은 여기서 `NARRATION_LEAD_SEC`를 뺀 것(3초 컷이면 2.2초 남짓).
- 소리 = 장면 소리(LTX, `scene_sound`로 끔) + 내레이션(TTS: edge-tts → Supertonic-3) + 배경음악 한 곡(기본 Stable Audio 3 Medium, `MUSIC_ENGINE`, `bgm.py`·`music.py`). MiniMax는 다른 소리가 섞여, LTX는 피아노 여러 대처럼 들려 기본에서 뺐다(2026-11-05 사용자 청취).
  배경음악은 `MUSIC_MIN_SECONDS`(60) 이상으로 요청해 `bgm.steady_offset`이 고른 구간을 골라 쓴다(짧게 만들면 출렁임). 곡 설명은 영어로. 같은 곡이 다음 버전에도 이어지고 의견("music": on/reroll/off)일 때만 바뀐다. YouTube 설명에 엔진별 표기(`bgm.credit`).
- 자막은 영상에 굽지 않는다. `.srt`를 만들어 업로드 뒤 `captions.insert`(`youtube.force-ssl`). 자막 실패해도 영상 업로드는 성공으로 둔다.
- 로고: PNG·JPG·WEBP·`.ai`·`.pdf`(`vector_to_png`). 로고 시각 계산은 `overlay.logo_range` 한 곳.
- **엔딩 카드·끝 페이드아웃**(brand_json['end_card'], `rough_cut._end_card_clip`): 마지막 장면을 검게 사라지게 한 뒤 배경색 + 로고 + 문구 카드. 어두워짐·페이드아웃은 편집 효과라 AI 프롬프트에서 뺀다(`camera.strip_fades`) — AI에게 시키면 조명이 꺼지는 장면이 됐다. 타임라인(`VideoRound.timeline_json`)은 **실제 합성에 쓴 값**을 그린다.
- 장면별 효과음(Stable Audio)·Qwen3-TTS는 품질이 나빠 쓰지 않는다(코드는 `_archive/unused_code/`).

### LLM 사용
- 글 다듬기 폴백: Claude API → Ollama → ComfyUI Gemma(e2b) → 템플릿.
- 장면 다듬기·AI로 채우기·문장→동작·**맡기기 질문(내용·원칙)·AI 점검**은 큰 모델(Gemma4 26B A4B): **llama-server로 필요할 때 켜고 끈다**(켜기 전 ComfyUI `/free`). e2b는 번역처럼 단순한 일만(2026-11-06 사용자 결정).
  llama-server는 옆 폴더 `cuda_v13`(GPU 라이브러리)을 PATH에 넣어 켠다 — 없으면 'no usable GPU found'로 CPU에서 돈다. 이미지 입력은 `REFINE_LLAMA_MMPROJ`(Ollama blobs의 projector), 요청에 `enable_thinking: False`.
  영상 작업이 돌거나 기다리는 중이면 켜지 않고 e2b로 대신하며 화면에 알린다. 영상 작업은 `llm.strong_model_lock`으로 기다린다.
  Ollama는 Gemma4 준비를 못 알아채 실패하므로 Ollama blobs를 llama-server로 직접 띄운다.
- 번역·글 생성 모델은 Gemma4 e2b(`gemma4_e2b_it_int8_convrot`, `clip_type=ltxv`). `gemma4-12b-with-proj`는 LTX 투영층이 있어 글쓰기용이 아니고, 12B급은 너무 느리다.

### 피드백 처리
- 분류: `EmbeddingClassifier`(prototype 코사인, 인증 불필요)가 기본 → NLI(mDeBERTa, HF 게이트라 기본 아님) → 규칙. factory에서 임계값 specificity×0.75, validity×0.70.
- 군집화 임베딩 대상은 `improvement_suggestion`이 아니라 **`comment_text` 원문**(요약문은 교차 언어 품질이 나빴다).
- 토픽별 그룹 → 그룹 내 12개 이상 HDBSCAN, 미만 Agglomerative(cosine, average). 카테고리별 임계값 technical 0.26 … content 0.44. HDBSCAN 노이즈는 가장 가까운 중심으로 배정.
- 서브클러스터링은 count > 15이고 각 조각이 2개 이상일 때만.
- 우선순위 `P = 0.35·freq + 0.25·quality + 0.20·severity + 0.12·urgency + 0.08·validity`, 재발 ×(1+0.10×min(n,3)), 상반 ×0.60.
- diff는 `splitlines()` + `"\n".join(unified_diff(..., lineterm=""))` (헤더 뭉침 방지).
- 코드 주석 `[오픈소스]` / `[팀 직접 구현]` 구분을 유지한다(평가 항목).

## 6. 알려진 함정

1. **작업 중 스토리보드 고치기**: 작업 큐가 장면을 만드는 동안 `storyboard_json`을 고치는 요청(내레이션 저장 등)은 사라질 수 있다. 고친 뒤 DB에 실제로 들어갔는지 확인한다.
2. **ComfyUI 통신**: 작업은 `client_id` 없이 보낸다(붙이면 ComfyUI 창에 진행률이 안 보임). 완료는 `/history` 폴링. `upload`·`download`는 시간 초과·연결 오류만 3번까지 재시도.
3. **DB**: `instance/pipeline.db`. 새 컬럼은 `app/__init__.py`의 `_migrate_existing_tables()`(ALTER TABLE)에 추가. 컬럼 제거·타입 변경은 DB 삭제 후 재생성.
4. `/api/clusters`는 현재 라운드 기준 — `prompt_updated` 뒤 검토 화면에 클러스터가 안 보이는 건 정상.
5. `versions.html`에서 v1에는 rollback 버튼이 없다(정상).
6. 작업 큐 워커는 첫 `enqueue` 때 지연 시작. 재시작 시 `running` 작업은 `failed(interrupted)`로 바꾸고 재시도 가능 상태로 돌린다.

## 7. 데모 재생 (시연 촬영용)

`PipelineSession.demo_json = {"rounds": {"1": {"replay_round_id": id, "youtube_video_id": "..."}, ...}}`이면 '제작 시작'이 장면을 만들지 않고
`orchestrator.replay_demo`로 준비된 결과를 복사해 ③으로 가고, 업로드는 그 영상 ID에 연결한다. 화면에서 켜는 곳은 없다.
**데모 앱은 실제 앱과 분리** (2026-11-06): `run_demo.py`가 `DEMO_MODE=true` + 작업 폴더 `instance_demo`(DB·영상·토큰·분류기 따로)로 5001번에 띄운다.
`demo_setup.py`가 실제 DB에서 데모에 필요한 프로젝트만 복사(영상도 복사, 경로 바꿈), `demo_reset.py`(기본 `team`)가 데모 프로젝트를 ① 기획으로 되돌리고
분류기 파일을 기준 상태(`*.npz.base`)로 되돌린다 — 분류기는 승인 때 학습(`approval.update_prototype`)해서 안 되돌리면 다음 촬영의 묶음이 달라졌다. `old` = 실제 앱의 27번.
team: 1차 = '[데모 소스] … 수정 전'(51번 수정 전 컷), 2차 = 51번(데모 DB에서 '[데모 소스]' 제목, 데모 화면 목록·숫자에서 숨김).
**대화형 시작 연출 `/demo`** (`app/routes/demo.py`, 데모 앱에서만): 데모 앱의 `/`·`/session/new`는 데모 페이지로 이어진다. 첫 화면·질문 화면 템플릿 복사본(`templates/demo/`)에서
설명(F2 자동 입력)·준비된 질문에 답하면 데모 프로젝트에 맡긴 내용을 적고 `auto_plan` 작업 행(running, 큐가 집지 않음)을 만들어 실제 프로젝트 화면이
'AI 기획 중'을 보이게 한 뒤, 스레드가 단계 문구를 흘리고 `start_generation`(데모 재생)을 부른다. 시작할 때 분류기 캐시를 비워 기준 파일을 다시 읽는다.
촬영 안내는 `D:\capstone\demo_kit\촬영_순서.md`.

## 8. 주요 파일 위치

```
app/models.py                      DB 모델
app/routes/session.py              화면·API 전부 / devtest.py 점검 페이지(/dev/pipeline-test)
app/pipeline/orchestrator.py       상태 머신(advance)·생성 흐름·부분 재생성·기획 변경·데모 재생
app/pipeline/auto_pipeline.py      AI 자동 진행 (기획 → 생성·점검·다시 만들기 → 러프컷)
app/pipeline/auto_review.py · intake.py · face.py · ref_photos.py   AI 점검 / 부족한 것만 묻기 / 참고 사진(생김새 읽기·장면 줄에 넣기)
tests/auto_checks.py               AI 자동 진행 검증 (가짜 LLM·비전·참고 사진, draw 모드의 Qwen 그리기)
app/pipeline/jobs.py               GPU 작업 큐 (워커 1개)
app/pipeline/feedback_target.py    반영 위치·길이 요청·장면 묘사 고쳐 쓰기
app/pipeline/edit_plan.py          편집 설정 + 의견 → 편집 제안
app/pipeline/video_generator/      어댑터(comfyui/local/mock), comfyui/rough_cut.py 러프컷
app/pipeline/motion_tracks.py · motion_plan.py   움직임 경로 / 문장 → 관절 각도
app/pipeline/llm.py                LLM 폴백 체인, generate_strong
app/pipeline/classifier/ · clusterer/   6축 분류 / 군집화·우선순위
app/templates/session/detail.html  프로젝트 상세(단계 탭) + static/js/round_view.js(상태 줄)
config.py                          모든 설정 (.env 우선) — 전체 변수 목록은 여기와 .env.example
promo_draft/                       홍보영상 초안 CLI (run.py, storyboard_*.yaml, outputs/), workflows/ 공용
```

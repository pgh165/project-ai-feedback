# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# AI 영상 피드백 자동 개선 시스템

## 프로젝트 개요

**사용자가 원하는 영상을 만들고, 시청자 피드백으로 거듭 다듬는 파이프라인.**
사용자가 "만들고 싶은 영상"(제품 소개·홍보·뮤직비디오·단편 등 종류 무관)을 설명하면
로컬 영상 생성 AI(ComfyUI + LTX-2.5)가 장면 단위로 만들고, 시청자 댓글에서 건설적 피드백을
자동으로 추출·군집화한 뒤, 제작자가 승인한 의견을 다음 버전에 반영한다.
프로젝트 = `PipelineSession`(화면 비율·장면 길이 포함), 버전(N차) = `VideoRound`.
외부 계정 없이 동작하는 것이 핵심 설계 원칙 — 영상 생성도 로컬 GPU에서 실행한다.

### 프롬프트 문서 형식
프롬프트는 `[섹션명]` 헤더로 나뉜 텍스트이며 버전 관리·diff 대상이다.
섹션: 프로젝트 / 영상 종류 / 버전 / 등장 요소 / 이전 버전 요약 / 영상 설명 / 샷 목록 /
내레이션(선택) / 영상 스타일 / 시청자 반영 사항 / 연출 지침.
[내레이션]은 샷 번호와 1:1인 한국어 해설 문장이며, TTS(`pipeline/narration/`)로 합성해 러프컷 위에 얹는다.
문장은 영상 장면의 서명에 들어가지 않으므로 해설만 고치면 영상은 재사용되고 음성만 다시 합성된다.
샷 줄 앞에 `[6초]`·`[6초 시드 202]`를 쓰면 그 샷만 길이·시드를 따로 지정한다(`prompt_builder._SHOT_MARK`, 모델에는 전달되지 않음).
새 영상 화면과 점검 페이지의 "콘티 엑셀(.xlsx) 불러오기"(`POST /session/import-storyboard`, `pipeline/storyboard_import.py`)는
머리글 이름(`장면 ID`·`길이(초)`·`내레이션`·`장면 동작 (영문)`/`최종 프롬프트 (자동)`·`시드` …)으로 콘티를 읽어 폼을 채운다. 저장은 폼 제출 때 한다.
(예전 드라마용 이름 — 시리즈·장르·회차·등장인물·지금까지의 줄거리·이번 화 줄거리 — 도 읽을 때 자동 변환된다.)

### 완성 영상 불러오기
**완성 영상 불러오기** (`/session/import`, `pipeline/video_import.py`): 이미 만든 영상(+선택 .srt)을 올리면 컷 경계를 찾아
제안하고(앞뒤 0.5초 평균 화면 비교 + 주변 대비 봉우리 → 디졸브·검은 화면 페이드도 잡음, 하드 컷은 프레임 단위로 맞춤),
사용자가 합치기/나누기로 고친 뒤 확정하면 ③ 확인(`video_ready`) 상태의 1차 버전을 만든다(`import_video` 작업).
장면 클립 `<sid>/v1.mp4`, 첫 프레임 `v1.png`(컷 다시 만들기 I2V 시작 이미지), 원본은 `source.<ext>`(= 올릴 영상).
장면 영어 묘사는 ComfyUI Gemma(`comfy_text.py`, 이미지 입력)가 장면 가운데 프레임을 보고 [샷 목록]에 `[4.04초] …`로 채운다.
.srt 문장은 **영상에 소리 트랙이 없으면 [내레이션]**(TTS로 읽고 자막도 켬, 불러오기 끝에 러프컷을 새로 합성)으로,
**소리가 있으면 [자막]** 섹션(`shot["subtitle"]`, 화면 자막만)으로 프롬프트 문서에 넣는다 — 문서에 있어야 다음 버전에도 이어진다.

### 한국어 입력 · AI로 채우기
영상 모델에는 **[샷 목록]의 각 줄(영어) + [영상 스타일]만** 전달된다 (샷 1개 = 장면 길이만큼의 클립).
**한국어로 입력해도 된다**: 프로젝트를 만들거나 프롬프트를 저장할 때 `pipeline/ko_en.py`가 [샷 목록]·[영상 스타일]에서
한글이 든 줄만 영어로 바꾼다(줄 앞 `[6초]` 표시·번호·내레이션은 그대로, 장면은 한 번에 묶어 번역 → 어긋난 줄만 한 줄씩).
번역할 줄인지는 `ko_en.needs_translation`이 번호·`[N초]` 표시·따옴표 안(화면 글자 인용)을 빼고 본다 — 빼지 않으면 '초'나
`text reads "학과"` 때문에 이미 영어인 줄까지 저장할 때마다 다시 번역하려다 실패했다.
번역 캐시(`TextTranslation`, engine=`user`)에 '영어 → 사용자가 쓴 한국어'를 넣어 화면에는 원문이 보인다. 번역에 실패하면 만들지 않고 안내한다(한국어를 몰래 넣지 않음).
**AI로 채우기** (`pipeline/draft_fill.py`, `POST /session/draft/fill`): 세부사항 화면에서 비어 있는 칸만 한국어 초안(제목·종류·등장 요소·장면·내레이션·스타일)으로 채운다.
적어 둔 칸은 '정해진 값'으로만 LLM에 주고 덮어쓰지 않는다. 장면 수는 설명의 'N초' ÷ 장면 길이(없으면 6, 최대 LOCAL_VIDEO_MAX_SHOTS).
작은 모델이 붙이는 '(지시문) 문장'·괄호는 정리한다. 번역은 `ko_en.engine_config`의 모델(KO_EN_COMFY_MODEL, 기본 Gemma4 e2b)을 쓴다.
**장면 다듬기** (`pipeline/scene_refine.py`, `POST /session/draft/refine`): 기획 화면 장면 목록 아래 버튼. 적어 둔 장면 줄(한국어·영어)을
이미지+영상 모델이 잘 따르는 영어 한 줄로 고쳐 쓴다 — 맨 앞 분위기·장소·시간, 보이는 것만(추상어 빼기), 막연한 물건은 생김새로, 색은 빛의 색으로,
렌즈·구도, 카메라 움직임은 끝에, 부정문 금지, 장면끼리 조명·대상 표현 통일. `[6초]` 표시는 그대로 두고, 고치기 전·후를 보여 준 뒤 받아들일 때만 칸을 바꾼다(저장하지 않음).
장면 다듬기·AI로 채우기는 `llm.generate_strong`: **큰 모델(Gemma4 26B A4B)을 llama-server로 필요할 때 켜고 다 쓰면 끈다**(`REFINE_*`, 켜기 전 ComfyUI `/free`).
영상 작업이 돌거나 기다리는 중이면 켜지 않고 e2b로 대신하며 화면에 이유를 알린다. 영상 작업(`jobs._GPU_KINDS`)은 `llm.strong_model_lock`으로 큰 모델이 끝날 때까지 기다린다.
Ollama 0.40.9는 Gemma4를 띄우고도 준비됨을 못 알아채 5분 뒤 실패해서(2026-10-23) Ollama가 받은 모델 파일(blobs)을 llama-server로 직접 띄운다.

### 피드백 반영 위치 · 편집 설정
다음 버전은 같은 영상을 다듬는 것이라 [영상 설명]·[샷 목록]은 유지되고, 승인된 content/story 피드백은
[시청자 반영 사항]으로, 나머지는 [연출 지침]으로 들어간다.
**장면을 다시 만드는 것은 반영 위치(`ClusterResult.apply_target`, `pipeline/feedback_target.py`)가 화면(scene)·움직임(motion)인 의견뿐**이다.
소리(audio)·편집(edit: 전환·속도·순서·자막)·내용(content) 의견은 장면을 그대로 재사용한다.
소리·편집 의견은 **편집 설정**(`pipeline/edit_plan.py`)으로 올릴 영상(러프컷) 합성에만 반영한다: 전환(cut/fade/dissolve)·배경 소리/내레이션 크기·빠르기·
내레이션 자막·장면별 길이(keep)·재생 속도·순서. 의견 문장 → 키워드 규칙으로 제안(`suggest_changes`) → 검토 화면에서 고침(`VideoRound.next_edit_json`)
→ 반영 시 다음 버전 `edit_json` → 스토리보드 `storyboard["edit"]`(장면 서명에 안 들어감) → `rough_cut.build(edit=…)`. `shot_ranges`도 편집된 시각을 따른다.

### 올릴 영상 꾸미기 · 소리
**올릴 영상 꾸미기** (`pipeline/overlay.py`): 로고(프로젝트 설정 `PipelineSession.brand_json`, 파일은 `instance/assets/session_<id>/`.
PNG·JPG·WEBP 또는 Illustrator **.ai(PDF 호환)·.pdf** — `vector_to_png`(pypdfium2+Pillow)가 투명 배경 PNG로 바꾸고 원본은 `logo_source.*`로 둔다)를 러프컷 위에 얹는다.
**자막은 영상에 굽지 않는다** — 편집 설정 `subtitles`가 켜져 있으면 내레이션 문장을 합성 리포트의 말 시작·길이(또는 타임라인 `subtitle_times`)에 맞춰
올릴 영상 옆 `subtitles.srt`(`rough_cut.CAPTIONS_NAME`, `overlay.write_srt`)로 만들고, YouTube 업로드 뒤 `youtube_upload.upload_captions`
(captions.insert, `youtube.force-ssl` 권한)로 한국어 자막 트랙을 붙인다. 자막이 실패해도 영상 업로드는 성공으로 두고 사유를 남긴다.
그 권한 전에 연결한 계정은 다시 연결해야 한다(`status()["captions"]`). 직접 올릴 때를 위해 `/round/<id>/captions.srt`로 받을 수 있다.
자막 크기·위치는 YouTube에서 보는 사람이 정하므로 의견 → 편집 제안에서도 다루지 않는다.
로고·글자는 절대 영상 모델 프롬프트에 넣지 않는다. ③에서 "저장하고 올릴 영상 다시 만들기"는 `rebuild_upload_video`(러프컷만 재합성, 장면 재생성 없음).
**소리** = 장면 소리(LTX) + 내레이션(TTS) + **배경음악 한 곡**(`pipeline/bgm.py`, MiniMax Music 3).
장면 소리는 편집 설정 `scene_sound`(기본 켬)로 끌 수 있다 — 끄면 새로 만든 컷 파일에서 소리를 빼고(`orchestrator._mute_if_needed`,
`rough_cut.strip_audio`) 러프컷에도 무음 트랙을 쓴다. 소리 없는 완성 영상을 불러오면 자동으로 꺼서, 다시 만든 장면에만 LTX 효과음이 생기지 않게 한다.
로고 칸은 실제 합성(`overlay.logo_filter`)과 같은 비율로 첫·마지막 장면 위에 미리보기를 그린다(슬라이더·저장 전 파일 미리보기).
위치는 네 모서리 또는 `custom` — 미리보기를 누르거나 끌면 로고 가운데를 화면 비율 `x`·`y`(0~1)로 저장하고, 합성은 그 자리에 두되 화면 밖으로는 나가지 않게 자른다.
보이는 때: 전체 / 처음 `first_sec`초 / 마지막(`last_sec`초, 0이면 마지막 장면 내내) / `range`(`start_sec`~`end_sec`, end 0이면 끝까지) — 화면에는 고른 모드의 칸만 보인다.
**타임라인** (`session/_timeline.html`, ③ 올릴 영상 아래): `rough_cut.build`가 합성 때 실제로 쓴 시간표(장면·자막 조각·로고 구간)를
`report["timeline"]` → `VideoRound.timeline_json`에 남기고, 화면은 그걸 그려 영상 재생 위치와 맞춘다. 자막(장면별 한 막대)·로고 막대를 끌어 옮기거나
양 끝을 끌어 길이를 바꾸면, 저장 시 자막은 편집 설정 `subtitle_times {sid: [시작, 끝]}`(영상 기준 초 — 다음 버전으로는 넘기지 않음),
로고는 `range` 모드로 들어간다. 영상에는 다시 만들 때 반영된다(합성 전 미리보기 아님). 로고 시각 계산은 `overlay.logo_range` 한 곳.
브라우저는 벡터를 못 그리므로 저장 전 .ai·.pdf는 `POST /session/<id>/logo/preview`가 PNG로 바꿔 돌려준다(저장하지 않음) — 그래야 저장 전에도 끌어 놓을 수 있다. 장면별 효과음(예전 `ambience.py`, Stable Audio)은 어색해서 쓰지 않는다 — 코드는 `_archive/unused_code/`로 옮김.
배경음악 설정은 `VideoRound.music_json`(곡 설명·시드·크기·파일). 곡 설명은 LLM(Gemma)이 [영상 종류]·[영상 설명]·[영상 스타일]로 제안(없으면 종류별 템플릿),
러프컷 합성 때 곡이 없으면 만들고 같은 설명·시드면 다시 쓴다. 곡은 `MUSIC_MIN_SECONDS`(60) 이상으로 요청해(모델이 길이를 정해 실제론 40초 안팎, 약 3분)
`bgm.steady_offset`이 소리 크기가 가장 고른 구간(도입·끝맺음·끊긴 곳 피함)을 골라 `offset`부터 섞는다 — 영상 길이(20초)로 만들면
모델이 도입·본곡·끝맺음을 욱여넣어 갑자기 조용해지거나 커졌다. 곡 설명은 영어로 적어야 잘 따른다. 내레이션이 나오면 `sidechaincompress`로 낮춘다.
다음 버전에 같은 곡이 이어지고, 의견(`edit_plan` "music": on/reroll/off)일 때만 바꾼다. YouTube 설명에 'MiniMax-Music3' 표기를 붙인다.
**배경음악·음성 탭**: 배경음악 설정과 내레이션 목소리(`pipeline/voice.py`, 프로젝트 공통 `PipelineSession.voice_json` — 켜기/끄기·Qwen3-TTS 화자·말투 지시문)와
이 버전의 내레이션 크기·빠르기(`edit_plan` narration_gain/tempo)를 고친다. 목소리를 바꾸면 TTS 캐시 키가 달라져 음성만 다시 합성된다.
말투는 화면에 한국어 이름, 모델에는 영어 지시문. 한국어 모국어 화자는 sohee뿐(방언 화자 eric·dylan은 뺌). "미리 듣기"는 첫 내레이션 한 문장만 합성(`/session/<id>/voice/preview`, 30초~1분).

### 프롬프트 다듬기 · 장면 다시 만들기
**프롬프트 다듬기**: 다음 버전은 템플릿 갱신이 기본이고, 내용 의견이 있으면 LLM(Claude → Ollama → ComfyUI Gemma, `llm.py`)이
[영상 설명]·[내레이션]만 다시 쓴다(`prompt_updater.refine_sections`). [샷 목록]은 LLM이 바꾸지 않는다 — 샷 줄이 바뀌면 지적되지 않은 장면까지 다시 만든다.
내레이션은 같은 번호·한국어·장면 길이 안·새 영어 단어 없음 검사를 통과할 때만 쓴다. 반영 위치는 키워드+카테고리로
자동 제안되고 검토 화면에서 바꿀 수 있다. 움직임 의견만 있는 장면은 이전 버전 첫 프레임을 그대로 쓰고 컷만 다시 만든다.
지적된 장면은 시드가 바뀌고, 의견(`notes`, 한국어·표시용)은 `ensure_english_notes`가 **장면 묘사 자체를 고친 모습으로 다시 쓴**
`prompt_fixed`(영어, `feedback_target.revise_shot_prompt`)로 바꿔 원래 묘사 대신 쓴다(`final_prompt`). 꼬리에 붙이면 작은 모델이
지적된 문제를 그대로 묘사하고("사진이 없어" → "empty frame") 원래 묘사가 남아 Z-Image가 따르지 않았다 — 지시문에 '고친 상태만, 실수는 묘사 금지'와 예시를 둔다.
`prompt`(원래 묘사)는 그대로라 장면 서명은 안 바뀐다. 고쳐 쓰기에 실패하거나 의견이 영어면 예전처럼 `notes_en`을 꼬리에 붙인다.
한국어 원문을 넣으면 이미지 모델이 화면에 글자로 그려 버리므로 절대 넣지 않는다.

## 기술 스택

- Python 3.11, Flask 3.x, SQLAlchemy (SQLite)
- `sentence-transformers` (로컬 임베딩·분류), `sklearn` (군집화), `hdbscan` (밀도 기반 군집화), `transformers` (NLI 폴백)
- Bootstrap 5 + Chart.js (UI)

## 파이프라인 상태 머신

```
① prompt_built          ← 프롬프트(스토리보드) 생성/갱신 완료
② generating            ← GPU 작업 큐에 장면 제출, 진행 중 (진행률·장면 n/N)
   images_ready         ← (단계별 확인) 첫 프레임 이미지 확인 대기 → generating
   clips_ready          ← (단계별 확인) 컷 영상 확인 대기 → '러프컷 만들기' → generating → ③
   generation_failed    ← 일부 장면 실패 → 실패한 장면만 재시도 (②에 빨간 표시)
③ video_ready           ← 모든 장면 + 러프컷 완료. REQUIRE_VIDEO_APPROVAL=true면 사람이 확인할 때까지 대기
                           (품질 부족 장면은 상용 API로 재생성 → ②로 되돌아갔다가 ③ 복귀)
④ uploaded              ← 영상 업로드 완료
⑤ feedback_collected    ← 댓글 수집 완료 (YouTube API 또는 시뮬레이션)
⑥ feedback_classified   ← 피드백 6축 분류 + 타임스탬프 추출
⑦ clustering            ← 클러스터링 진행 중 (작업 큐에서 비동기 실행)
⑧ pending_review        ← 클러스터링 완료, 사용자 승인 대기
⑨ prompt_updated        ← 승인 적용, 다음 라운드(①) 생성
```

- **단계별 확인 생성** (`COMFYUI_STAGED_REVIEW=true`, comfyui 어댑터): ②가 세 번 멈춰 사람이 확인한다.
  `generating → images_ready`(첫 프레임 이미지 확인, 장면별 "다시 뽑기"+피드백 문장) → `generating → clips_ready`(컷 영상 확인, 장면별 "다시 만들기")
  → 사용자가 "러프컷 만들기"를 눌러야 `generating → video_ready`. 러프컷은 자동으로 만들지 않는다. images_ready·clips_ready는 번호 없는 상태로 ② 위에 "waiting"으로 표시된다.
  API: `POST /api/rounds/<id>/{images/approve, images/regenerate, clips/regenerate, rough-cut}`, 이미지는 `GET /shot/<render_id>/image`.
  장면 1개는 `ShotRender` 한 줄이 `running → image_ready → image_approved → running → done`으로 진행하고, 다시 뽑으면 새 버전 줄이 생긴다(이전 이미지는 `discarded`, 이전 컷은 `done`으로 남아 고를 수 있음).
  다시 뽑을 때 시드가 +101 되고 피드백 문장은 장면 `notes`로 프롬프트에 덧붙는다. 문장이 길이 요청('3초로', '2초 더', '두 배',
  '늘려줘', '짧게')이면 `feedback_target.length_request`로 장면 길이를 바꾸고 [샷 목록]에 [N초]로 남긴다(최대 SHOT_MAX_SECONDS).
  길이만 말한 문장은 장면 묘사에 붙이지 않는다(`is_length_only`).
  **컷 다시 만들기 의견이 생김새(색·물건·배경 등, `feedback_target.suggest` == scene)면 첫 프레임 이미지부터 다시 뽑아 `images_ready`에서 멈추고**,
  사람이 이미지를 승인해야 그 장면의 컷만 만든다(움직임·길이 의견은 같은 이미지로 컷만) — 컷은 첫 프레임을 움직이기만 해서 같은 이미지로는 생김새 의견이 반영될 수 없었다.
  의견으로 고쳐 쓸 때 영상 묘사(`prompt_fixed`, 동작을 순서대로)와 첫 프레임 묘사(`image_prompt_fixed`, 동작 시작 전)를 따로 만들어
  이미지 모델에는 첫 프레임 묘사를 넣는다(`final_prompt(first_frame=True)`) — 하나로 쓰면 이미지 모델이 동작 중간(이미 든 모습)을 그렸다.
  컷 버전 지우기: `POST /api/shots/<render_id>/delete`(`orchestrator.delete_render`) — 공개 전에만, 장면마다 한 버전은 남기고, 다른 버전과 공유하지 않는 파일만 지운다.
  **이 순간을 끝으로 다시 만들기**: `POST /api/shots/<render_id>/end-frame {"at": 초}`(`orchestrator.regenerate_clip_to_frame`) — 컷에서 고른 순간을 ffmpeg로 뽑아
  (`v<k>_end_<ms>.png`, `ShotRender.end_image_path`) 그 컷의 첫 프레임 = 0번, 뽑은 화면 = 마지막(-1) 프레임에 강도 0.7로 고정하고 장면 길이 그대로 새 버전을 만든다
  (`COMFYUI_WORKFLOW_FLF` = `ltx25_flf2v`, 공식 템플릿 video_ltx2_5_flf2v를 API로 옮긴 것, 업스케일 없이 최종 해상도로 한 번에 ≈ 2분 반).
  **장면 의견 지우기**: `POST /api/rounds/<id>/notes/delete {shot_id, index}`(`orchestrator.remove_shot_note`) — 의견은 다시 만들 때마다 전부 다시 반영되므로
  지난 의견을 지울 수 있다(이미 만든 컷은 그대로, 다음 생성부터). 고쳐 쓰기는 **최소 수정**(바꿀 부분만, 나머지 낱말은 그대로)이고,
  원래 묘사의 인물 생김새(`feedback_target.kept_phrases`: 옷·머리·안경)가 빠지면 한 번 더 묻고 그래도 빠지면 사람 낱말 뒤에 되살린다(의견이 옷·머리 얘기면 안 지킴).
  **모호한 의견**(`vague_reason`: 무엇으로 바꿀지 없는 '바꿔', 짧은 '이상해')이면 다시 만들기 API가 422로 방향을 묻고, 화면에서 '그래도 진행'하면 `force`로 보낸다.
  요청한 동작 뒤에 엉뚱한 동작이 붙을 때(사람이 물건을 받아 감, 카메라가 사람을 벗어남) 쓴다. 끝 이미지를 Z-Image로 따로 그리면 구도가 달라 두 그림이 겹쳐 보여서
  (2026-10-23 시험) 같은 컷에서 뽑은 화면만 끝으로 쓴다.
  '늘려줘'·'나레이션이 빨라'·'내레이션에 맞춰'처럼 얼마나를 말하지 않으면 내레이션을 합성해 재고(`orchestrator._narration_fit_seconds`)
  1배속으로 들어가는 길이로 맞춘다: 말 + 말 시작 지연 + 디졸브 겹침(마지막 장면 제외) + 앞뒤 잘라 냄. 이미 들어가면 예전처럼 1.5배. 끄면 예전처럼 한 번에 러프컷까지 만든다.
- **화면상 단계 구분**: ② 제작 = 장면별 영상 만들기(이미지 → 컷), ③ 확인 = 올릴 영상(러프컷) 만들기·확인·공개.
  서버 상태는 그대로(`generating`)지만 러프컷 작업(`payload.stage == "rough"`)이 돌거나 러프컷 합성이 실패하면
  `compute_steps`가 ③에 표시한다(`rough_cut_in_progress`, `_move_rough_cut_to_confirm`). 상세 화면은 ②에서 러프컷을 숨기고
  `session/_scene_viewer.html`(번호 버튼으로 한 장면씩)만 보여 주며, ③ 이후에는 올릴 영상 아래에 장면별 영상을 둔다.
  컷 다시 만들기는 `video_ready`(업로드 전)에서도 되며 `clips_ready`로 돌아가 올릴 영상을 다시 만든다.
- 상태 전이는 **`pipeline/orchestrator.py`의 `advance(round, to_state)` 한 곳에서만** 수행한다. 허용되지 않은 전이는 `InvalidTransition`.
  라우트·작업에서 `round.status`를 직접 대입하지 않는다.
- 같은 단계에서의 반복(댓글 추가, 재분류, 재클러스터링)은 자기 자신으로의 전이로 허용된다.
- 상태 API: `GET /api/rounds/<id>/status` — 9단계(`done/active/waiting/failed/pending`), 장면별 상태·비용, 상용 API 예산.
- 제어 API: `POST /api/rounds/<id>/{retry,advance,escalate,approve-video,cancel}`, `POST /api/shots/<render_id>/select`.
  `select`(`orchestrator.select_render`)는 선택만 바로 저장하고, ③(`video_ready`)이면 올릴 영상은 작업 큐 러프컷 작업으로 다시 합성한다
  (예전엔 요청 안에서 통째로 합성해 고를 때마다 몇 초~몇 분 멈췄다). 합성 중에도 고를 수 있다 — 기다리는 합성이 있으면 더 걸지 않고, 돌고 있으면 끝난 뒤 한 번 더.
  `cancel`은 생성 중지: 대기 작업은 즉시 취소, 실행 중인 장면은 ComfyUI `/interrupt`로 끊고 장면 사이에서 멈춘 뒤 `generation_failed`(재시도 가능)로 둔다. 만들어진 장면은 남는다.
- 화면: `templates/session/detail.html` = 왼쪽 **단계 탭**(① 기획 ② 제작 ③ 확인 ④ 공개 ⑤ 의견 — 모든 버전이 같은 탭, 지난 버전은 읽기 전용)
  + 위쪽 상태 한 줄(`static/js/round_view.js`: 진행 중 2초 폴링·상태가 바뀌면 새로 고침·생성 중지·유료 API 재생성).
  ① 기획은 처음 입력한 칸을 다시 보여 주고 고칠 수 있다(`POST /session/<id>/plan`, 안 고친 장면 줄은 원래 영어 유지).
  제작 중(이미지·컷 확인, 확인 단계, 실패)에도 고칠 수 있다 — `orchestrator.plan_diff`가 장면을 (묘사, 길이)로 짝지어
  고친·새 장면만 이미지부터 다시 만들고(`apply_plan_change`), 짝지은 장면은 결과(ShotRender)를 새 id로 옮긴다(끼워 넣어도 안 다시 만듦).
  스타일이 바뀌면 전체, 내레이션만 바뀌면 음성만. 저장 전에 `preview=1`로 다시 만들 장면을 보여 주고 확인받는다.
  ② 제작은 이미지 → 컷, '다음: 확인 단계로'가 러프컷을 시작한다. ③ 확인은 러프컷 + 장면별 내레이션 결과 + 배경음악 듣기 + 꾸미기·소리 설정.
  ③ 확인의 내레이션 칸은 제작 뒤에도 고칠 수 있다(`POST /session/<id>/narration`, images_ready~video_ready) — [내레이션]·스토리보드만 바꾸고
  장면은 그대로, '저장하고 다시 만들기'면 음성만 다시 합성한다. 긴 설명은 화면에 쓰지 않고 `_ui.tip(text)`(! 아이콘, 마우스를 올리면 설명)로 둔다.
  단계 계산은 `routes/session._stage_of`. 예전 9단계 스테퍼(`pipeline_stepper.js`)는 개발용 점검 페이지에만 남아 있다.

## 모델 정보 (하드코딩 금지 — 전부 .env / config.py로 주입)

| 역할 | 모델 | 라이선스 | 크기 | 인증 |
|------|------|---------|------|------|
| 피드백 분류 (임베딩 기반, **기본**) | `paraphrase-multilingual-MiniLM-L12-v2` | Apache 2.0 | ~118 MB | 불필요 |
| 문장 임베딩 (클러스터링) | `paraphrase-multilingual-mpnet-base-v2` | Apache 2.0 | ~278 MB | 불필요 |
| 임베딩 폴백 | `paraphrase-multilingual-MiniLM-L12-v2` | Apache 2.0 | ~118 MB | 불필요 |
| 피드백 분류 (NLI, 2순위 폴백) | `MoritzLaurer/mDeBERTa-v3-base-zeroshot-v2.0` | MIT | ~280 MB | **게이트됨** |
| 영상 생성 (로컬 GPU, **주 경로**) | LTX-2.5 (ComfyUI 워크플로 `ltx25_t2v`/`ltx25_i2v`, `LTX_MODEL_VARIANT`) | — | GGUF Q4_K_M | 불필요 |
| 첫 프레임 이미지 | Z-Image Turbo (`zimage_t2i`: `z_image_turbo_bf16`, `qwen_3_4b`, `ae`) | — | — | 불필요 |
| 글(번역·AI로 채우기·장면 묘사·다듬기) | Gemma4 e2b (`gemma4_e2b_it_int8_convrot`, ComfyUI `TextGenerate`) | — | — | 불필요 |
| 배경음악 | MiniMax Music 3 (`MUSIC_DIT`/`MUSIC_TEXT_ENCODER`/`MUSIC_VAE`, ComfyUI) | — | — | 불필요 |
| 내레이션 TTS | edge-tts(기본, 인터넷) → Supertonic-3(로컬) / Qwen3-TTS(선택) | — | — | 불필요 |
| 영상 생성 (`local` 어댑터, 예전) | `Wan-AI/Wan2.1-T2V-1.3B-Diffusers` | Apache 2.0 | ~29 GB | 불필요 |

> **중요**: mDeBERTa는 HuggingFace 인증이 필요해 기본 분류기로 사용하지 않는다.
> 기본 분류기는 `EmbeddingClassifier` (prototype 코사인 유사도 방식, 인증 불필요).

ComfyUI 모델(LTX·Z-Image·Gemma·MiniMax)은 ComfyUI 쪽에 설치돼 있고 워크플로 JSON·설정이 이름만 가리킨다.
분류·임베딩 모델은 로컬 CPU에서 실행(GPU 불필요). 영상 생성만 NVIDIA GPU 필요
(RTX 5070 Ti 16GB 기준, `enable_model_cpu_offload`). HuggingFace 최초 실행 시 자동 다운로드.
영상 생성 중에는 GPU를 쓰는 다른 프로그램(게임 등)을 꺼야 한다 — VRAM이 부족하면 극도로 느려진다.

## 오픈소스 vs 팀 직접 구현 구분

### 오픈소스 활용 (주석 `[오픈소스]`)
- `sentence_transformers.SentenceTransformer` — 문장 임베딩 + 분류기 원형 비교
- `sklearn.cluster.AgglomerativeClustering` — 계층적 군집화 (metric=cosine, linkage=average)
- `hdbscan.HDBSCAN` — 밀도 기반 군집화 (설치 시 우선 사용, 없으면 AgglomerativeClustering 폴백)
- `transformers.pipeline("zero-shot-classification")` — NLI 추론 (2순위 폴백)
- `difflib.unified_diff` — 프롬프트 diff 생성

### 팀 직접 설계·구현 (주석 `[팀 직접 구현]`)
- **prototype 기반 다국어 분류기** (`EmbeddingClassifier`): 원형 문장 코사인 유사도로 건설적/비건설적 판별
- **6축 분류 판단 로직**: 구체성(specificity) + 타당성(validity) + 심각도(severity) + 긴급도(urgency) + 품질(quality) + 멀티레이블 토픽
- **동적 프로토타입 업데이트** (`update_prototype()`): 승인된 피드백으로 지수이동평균 온라인 학습
- **멀티레이블 토픽 태깅** (`_detect_topics_multi()`): 단일 댓글 → 최대 3개 카테고리 동시 감지
- **2단계 클러스터링**: 주제 태깅(tagger) → 태그 내 계층적 군집화(hierarchical)
- **카테고리별 적응형 임계값**: technical=0.26 ~ content=0.44 (고정값 탈피)
- **대형 클러스터 자동 서브클러스터링**: count > 15 시 더 세밀하게 분할
- **품질 가중 대표 텍스트 선택**: 중심근접도 50% + 품질 30% + 길이 20% 결합
- **클러스터 응집도 점수** (`cohesion_score`): 평균 쌍별 코사인 유사도 기반 신뢰도 지표
- **클러스터 추출 요약** (`cluster_summary`): 단어 빈도 기반 핵심 문장 자동 생성
- **5축 우선순위 공식**: `P = α·freq + β·quality + γ·severity + δ·urgency + ε·validity`
- **재발 이슈 부스트**: 이전 라운드 동일 이슈 재등장 시 우선순위 10%/라운드 자동 상향
- 상반된 피드백 감지: 키워드 쌍 매칭 (_CONTRADICTION_PAIRS)
- 클러스터 승인 워크플로: pending → approved / rejected / deferred
- 프롬프트 버전 관리 + 롤백 (PromptVersion, unified_diff 저장)
- 재발(reoccurrence) 추적: 이전 라운드 동일 주제 재등장 감지
- 전역 기술 설정 승격: technical scope 클러스터 → GlobalTechnicalSetting

## 주요 파일 구조

```
app/
  __init__.py              # create_app(), _migrate_existing_tables() — SQLite ALTER TABLE
  models.py                # DB 모델:
                           #   PipelineSession, VideoRound, Feedback,
                           #   ClusterResult, PromptVersion, GlobalTechnicalSetting,
                           #   PromptChange, Metric
  pipeline/
    classifier/
      base.py              # ClassificationResult (6축 필드), BaseClassifier, TOPIC_CATEGORIES
                           #   신규 필드: severity_score, urgency_score, quality_score,
                           #             topic_categories_multi, needs_review
      embedding_classifier.py  # [팀 직접 구현] v2 prototype 코사인 유사도 분류기
                               #   _PROTOTYPES: 카테고리별 원형 문장 (한국어+영어)
                               #   _SPECIFICITY_SIGNALS: 구체성 regex 패턴
                               #   _VALIDITY_SIGNALS: 타당성 regex 패턴
                               #   _SEVERITY_SIGNALS: 심각도 regex 패턴 (v2 신규)
                               #   _URGENCY_SIGNALS: 긴급도 regex 패턴 (v2 신규)
                               #   _TOPIC_KEYWORDS: 주제 분류 키워드 (확장됨)
                               #   update_prototype(): 동적 온라인 학습 (v2 신규)
                               #   _detect_topics_multi(): 멀티레이블 토픽 감지 (v2 신규)
                               #   _compute_quality_score(): 품질 점수 산정 (v2 신규)
                               #   _detect_lang(): 일본어·중국어·혼합어 지원 (v2 개선)
                               #   _extract_suggestion(): 점수 기반 제안 추출 (v2 개선)
      zeroshot.py          # NLI zero-shot 분류기 (2순위 폴백, mDeBERTa)
      rule_based.py        # 키워드 규칙 기반 폴백 분류기 (3순위, v2 신규 필드 반환)
      factory.py           # create_classifier() — EmbeddingClassifier 우선, 자동 폴백
                           #   임계값 조정: specificity×0.75, validity×0.70 (EmbeddingClassifier용)
      __init__.py
    clusterer/
      embedder.py          # get_embeddings() — 다국어 문장 임베딩, 모듈 레벨 캐시
      tagger.py            # group_by_topic() — topic_category 기준 그룹화
      hierarchical.py      # cluster_group() — v2 업그레이드
                           #   _CATEGORY_THRESHOLDS: 카테고리별 적응형 거리 임계값
                           #   _cluster_embeddings(): HDBSCAN 우선 / AgglomerativeClustering 폴백
                           #   _select_representative(): 품질 가중 대표 텍스트 선택
                           #   _compute_cohesion(): 클러스터 응집도 점수
                           #   _generate_cluster_summary(): 단어 빈도 기반 추출 요약
                           #   대형 클러스터 자동 서브클러스터링 (>15개)
                           #   _CONTRADICTION_PAIRS: 상반된 피드백 감지
      ranker.py            # assign_priorities() — 5축 공식 (v2)
                           #   P = α·freq + β·quality + γ·severity + δ·urgency + ε·validity
                           #   기본값: α=0.35, β=0.25, γ=0.20, δ=0.12, ε=0.08
      __init__.py          # run_clustering() — 전체 파이프라인 오케스트레이터
                           #   신규 파라미터: severity_scores, urgency_scores, quality_scores
    video_generator/
      base.py              # BaseVideoGeneratorAdapter, VideoGenerationResult, ShotResult
      mock_adapter.py      # 시뮬레이션용 mock — 영상 파일 없음 (MOCK_DELAY_SEC로 진행 표시 확인)
      local_adapter.py     # 로컬 GPU 영상 생성 (diffusers, 전체를 한 번에 생성 — 장면 단위 미지원)
      comfyui_adapter.py   # [팀 직접 구현] 로컬 ComfyUI + LTX-2.5, 장면 단위 생성
      runway_adapter.py    # [팀 직접 구현] 상용 API(Runway) — 품질 부족 장면 재생성 전용
      comfyui/             # promo_draft 모듈 이식: comfy_client · workflow(mapping.yaml 주입) · rough_cut(ffmpeg)
      factory.py           # create_adapter / resolve_local_adapter(mock 폴백) /
                           #   create_commercial_adapter / create_adapter_for_shot
      comfyui/rough_cut.py # 러프컷 합성(ffmpeg): 앞뒤 잘라 냄·전환·내레이션 트랙(장면 길이에 맞춰 배속)·로고·배경음악 + 자막 .srt
    narration/             # 내레이션 TTS (edge/supertonic은 venv_tts2 작업자, qwen은 venv_tts) + fit_plan(장면에 맞추기)
    feedback_target.py     # 의견 → 반영 위치(scene/motion/audio/edit/content), 길이 요청 읽기, 장면 묘사 고쳐 쓰기
    edit_plan.py           # 편집 설정(전환·소리 크기·빠르기·자막·장면 길이·순서) + 의견 → 편집 제안
    overlay.py · bgm.py · music.py · voice.py  # 로고·자막(.srt) / 배경음악 설정·곡 생성(ComfyUI) / 목소리 설정
    video_import.py · storyboard_import.py      # 완성 영상 불러오기(컷 찾기) / 콘티 엑셀 불러오기
    comfy_text.py · translate.py                # ComfyUI Gemma 글 생성(이미지 입력 가능) / 화면용 영→한 번역 캐시
    orchestrator.py        # [팀 직접 구현] 상태 머신(advance), 9단계 계산, 영상 생성 흐름,
                           #   부분 재생성 계획(plan_reuse), 상용 에스컬레이션, 분류·클러스터링 작업
    jobs.py                # [팀 직접 구현] GPU 작업 큐 — GenerationJob 테이블 + 워커 스레드 1개
    storyboard_model.py    # [팀 직접 구현] 스토리보드 스키마·시간 계산·타임스탬프→장면 매핑
    quality_gate.py        # [팀 직접 구현] 상용 재생성 비용·예산 검사, 재발 시 'API 권장' 배지
    classifier/timestamp.py  # [팀 직접 구현] 댓글 → 영상 시각(초) 추출
    approval.py            # save_clusters() — 재발 이슈 우선순위 부스트 (v2)
                           #   apply_approved_clusters(), rollback_to_version(),
                           #   check_reoccurrence(), preview_prompt_change()
    llm.py                 # 텍스트 LLM 폴백 체인: Claude API → Ollama(로컬) → ComfyUI Gemma → 템플릿
                           #   generate_strong(): 큰 모델(llama-server)을 필요할 때 켜고 끔 → e2b
    prompt_builder.py      # 1화 프롬프트 문서 구성, 섹션 파싱/렌더링, 샷 추출
    prompt_updater.py      # 승인 피드백 → 다음 화 프롬프트 (LLM 없으면 템플릿)
    youtube_client.py      # 댓글 수집 (SIMULATION_MODE=true 시 시뮬레이션, false면 YouTube Data API)
    ko_en.py               # [팀 직접 구현] 한국어 장면·스타일 → 영어(영상 모델용), 화면에는 한국어 원문
    draft_fill.py          # [팀 직접 구현] AI로 채우기 — 비어 있는 칸만 한국어 초안
    scene_refine.py        # [팀 직접 구현] 장면 다듬기 — 장면 줄을 영상 모델용 영어 묘사로 (큰 모델 → e2b)
    voice.py               # [팀 직접 구현] 내레이션 목소리·말투(프로젝트 공통) → TTS 설정 덮어쓰기
    youtube_upload.py      # [팀 직접 구현] YouTube 업로드 — OAuth(PKCE, 토큰은 instance/youtube_token.json) + 이어 올리기.
                           #   ③에서 제작자가 'YouTube에 올리기'를 눌러야만 작업 큐(youtube_upload)로 올리고 ④로 넘어간다.
                           #   containsSyntheticMedia=true(AI 생성 표시). 연결 안 됐으면 직접 올린 영상 ID 입력으로 대체
    metrics.py             # calculate_metrics() — 반영률, 시간절약 지표
  routes/
    session.py             # 모든 세션 라우트 (v2: filter/cluster 라우트 신규 필드 전달)
  templates/
    base.html
    _app_shell.html        # 왼쪽 대시보드(새 영상·내 프로젝트·할 일·불러오기·YouTube·곧 추가·GPU 상태 /api/system) + 본문.
                           #   내 프로젝트·세부사항·불러오기가 쓴다(첫 화면은 대시보드 없음). 프로젝트 안은 session/_project_base.html. 좁은 화면에서는 숨김
    home.html              # 첫 화면(/): 가운데 입력창 + 비율·첨부(콘티 엑셀/완성 영상) → GET /session/create?idea=…
    index.html             # 내 프로젝트 목록 (/projects, endpoint 이름은 session.index 그대로). ?view=todo = 할 일(TODO_STATUSES)
    session/
      _plan_form.html      # ① 기획 입력 화면(기본 정보·장면 구성·소리와 스타일·AI로 채우기·콘티 엑셀) — 새 프로젝트와 기획 탭이 같이 쓴다.
                           #   pf_mode: new / edit(제작 전) / producing(제작 중: 비율·기본 길이 잠금, 저장 전 다시 만들 장면 미리보기)
      create.html          # 새 프로젝트의 ① 기획 단계: 첫 화면 문장으로 제목·종류 추천(_suggest_from_idea) + AI로 채우기.
                           #   "기획 저장" → 프로젝트 기획 탭(#plan), "저장하고 제작 시작" → 바로 생성 시작(#produce)
      _project_base.html   # 프로젝트 화면 공통 틀: 왼쪽 사이드 탭(_sidebar.html) + 본문
      _sidebar.html        # 버전(N차) · 이 버전 기능 탭(#main/#deco/#prompt/#comments ↔ detail의 data-panel) · 검토·버전 기록·리포트
      detail.html
      review.html
      versions.html
      version_diff.html
      report.html
  static/
    css/style.css
    js/app.js
config.py                  # 모든 설정값 — .env 값 우선
                           #   v2 신규: PRIORITY_GAMMA, PRIORITY_DELTA, PRIORITY_EPSILON
run.py                     # Flask 앱 진입점
run_cluster.py             # 수동 실행용 스크립트
run_check.py               # 전체 라우트·API 기능 검증 스크립트
requirements.txt           # 기본 의존성 (CPU만으로 동작)
requirements-video.txt     # 로컬 영상 생성용: GPU torch(cu130) + diffusers
instance/
  pipeline.db              # SQLite DB (자동 생성)
```

## 환경변수 필수 목록

| 변수 | 권장값 | 설명 |
|------|--------|------|
| `SECRET_KEY` | 랜덤 문자열 | Flask 세션 키 |
| `ANTHROPIC_API_KEY` | (없어도 동작) | Claude API — 없으면 템플릿 폴백 |
| `CLASSIFIER_MODEL` | `paraphrase-multilingual-MiniLM-L12-v2` | 분류기 모델 |
| `EMBEDDING_MODEL` | `paraphrase-multilingual-mpnet-base-v2` | 클러스터링 임베딩 모델 |
| `EMBEDDING_FALLBACK_MODEL` | `paraphrase-multilingual-MiniLM-L12-v2` | 임베딩 폴백 |
| `SPECIFICITY_THRESHOLD` | `0.45` | 구체성 임계값 (factory에서 ×0.75 적용) |
| `VALIDITY_THRESHOLD` | `0.40` | 타당성 임계값 (factory에서 ×0.70 적용) |
| `CLUSTERING_DISTANCE_THRESHOLD` | `0.40` | 군집화 기본 코사인 거리 임계값 (카테고리별 오버라이드됨) |
| `PRIORITY_ALPHA` | `0.35` | 빈도 가중치 |
| `PRIORITY_BETA` | `0.25` | 품질 가중치 |
| `PRIORITY_GAMMA` | `0.20` | 심각도 가중치 |
| `PRIORITY_DELTA` | `0.12` | 긴급도 가중치 |
| `PRIORITY_EPSILON` | `0.08` | 타당성 가중치 |
| `SIMULATION_MODE` | `true` | YouTube/영상 시뮬레이션 여부 |
| `MANUAL_TIME_PER_FEEDBACK_MINUTES` | `3` | 수동 처리 시간 추정 (지표 계산용) |
| `VIDEO_GENERATOR_ADAPTER` | `comfyui` | `comfyui`(로컬 ComfyUI+LTX-2.5) / `local`(diffusers) / `mock`(스토리보드만) |
| `COMFYUI_URL` | `http://127.0.0.1:8188` | ComfyUI 서버 |
| `COMFYUI_WORKFLOWS_DIR` | `<프로젝트>/promo_draft/workflows` | Export (API) JSON + `mapping.yaml` (수정하지 않고 읽기만) |
| `COMFYUI_WORKFLOW_T2V` / `I2V` | `ltx25_t2v` / `ltx25_i2v` | 사용할 워크플로 이름 |
| `COMFYUI_TEST_MODE` | `false` | true면 `COMFYUI_TEST_*` 해상도·프레임으로 빠르게 시험 |
| `COMFYUI_SIZE_MULTIPLE` | `64` | 해상도 보정 배수 |
| `COMFYUI_PROMPT_ENHANCE` | `false` | true면 워크플로의 Gemma 프롬프트 강화(`TextGenerateLTX2Prompt`)를 켠다. 강화된 문장은 `ShotRender.enhanced_prompt`에 기록. 점검 페이지 체크박스로 임시 전환 가능 |
| `COMFYUI_IMAGE_FIRST` | `false` (`.env`에서 `true`) | true면 입력 이미지가 없는 장면을 **Z-Image Turbo로 첫 프레임을 먼저 만든 뒤 I2V**로 움직인다. T2V의 구도·움직임은 좋지만 대상 품질이 떨어지는 문제 대응. 이미지는 `v<k>.png`로 영상 옆에 남고 `ShotRender.image_path`에 기록. 장면당 이미지 1회 + 영상 1회라 시간이 늘어난다 |
| `COMFYUI_STAGED_REVIEW` | `false` (`.env`에서 `true`) | true면 이미지 → (확인·다시 뽑기) → 컷 → (확인·다시 뽑기) → 사람이 러프컷을 눌러야 합성. 이미지 먼저를 포함한다. comfyui 어댑터에서만 동작하고, ComfyUI가 꺼져 있으면 예전 방식(mock 폴백)으로 돌아간다 |
| `COMFYUI_WORKFLOW_T2I` | `zimage_t2i` | 이미지 먼저에 쓰는 워크플로 (`promo_draft/workflows/zimage_t2i.json` + mapping.yaml). Turbo는 cfg 1·8스텝이라 부정 프롬프트 미사용. 모델: `z_image_turbo_bf16`, `qwen_3_4b`, `ae` |
| `COMFYUI_SHOT_TIMEOUT_SEC` / `POLL_INTERVAL_SEC` | `900` / `2` | 장면당 최대 대기 / 폴링 간격 |
| `STORYBOARD_SHOT_SECONDS` / `WIDTH` / `HEIGHT` / `FPS` | `5` / `576` / `960` / `24` | 장면 기본값 (세로 9:16) |
| `LTX_MODEL_VARIANT` | `ltx25-gguf-q4km` | 기록용 모델 식별자 (실제 모델은 워크플로 JSON이 결정) |
| `FFMPEG_PATH` | (PATH → imageio-ffmpeg) | 러프컷 합성 |
| `PARTIAL_REGEN_ENABLED` | `true` | 장면 단위 부분 재생성 |
| `REQUIRE_VIDEO_APPROVAL` | `true` | ③에서 사람이 확인한 뒤 업로드 |
| `COMMERCIAL_VIDEO_ADAPTER` | `runway` | 품질 부족 장면용 상용 어댑터 |
| `RUNWAY_API_KEY` | (없어도 동작) | 없으면 상용 재생성 버튼 비활성화 |
| `RUNWAY_API_BASE` / `API_VERSION` / `MODEL` / `T2V_MODEL` / `RATIO` | 코드 참고 | 공급자 변경에 코드 수정이 필요 없도록 전부 설정 |
| `COMMERCIAL_COST_PER_SEC_USD` | `0.12` | 예상 비용 (결제 전 공식 가격 확인 필요) |
| `COMMERCIAL_BUDGET_USD` | `30` | 세션당 상용 API 예산 상한 |
| `VOICE_ADAPTER` / `VOICE_FALLBACK` | `edge` / `supertonic` | 내레이션 TTS: `edge`(Microsoft 신경망 음성 SunHi·InJoon·Hyunsu, 계정 불필요·**인터넷 필요**) / `supertonic`(Supertonic-3, 로컬 CPU·오프라인) / `qwen`(Qwen3-TTS — 한국어가 어색해 기본에서 뺌) / `windows` / `none`. 기본 엔진이 실패하면 FALLBACK으로 다시 합성. edge·supertonic은 `venv_tts2`(`TTS2_PYTHON`)의 `narration/tts2_worker.py`로 돈다 |
| `EDGE_TTS_VOICE` / `SUPERTONIC_VOICE` | `ko-KR-SunHiNeural` / `F1` | 엔진별 기본 목소리. 프로젝트마다 ③ 확인 탭에서 바꿀 수 있다(`voice.py`, "엔진:목소리") |
| `CLIP_TRIM_HEAD_SEC` / `CLIP_TRIM_TAIL_SEC` | `0.15` / `0.1` | 러프컷에서 AI가 만든 컷의 시작 멈칫·끝 느려짐을 잘라 낸다(불러온 원본 장면은 그대로). 전환 기본값은 디졸브 0.4초(`edit_plan.DEFAULTS`) — 따로 만든 컷을 바로 붙이면 어색했다 |
| `BGM_AUTO` | `true` | 배경음악을 정하지 않은 버전은 ③ 확인(러프컷)에서 곡 설명을 제안받아 곡을 만들어 깐다. 끄면 그 값이 저장된다 |
| `VOICE_CULTURE` / `VOICE_NAME` / `VOICE_RATE` | `ko-KR` / (비움) / `0` | 음성 언어·이름·속도(-10~10) |
| `NARRATION_VOLUME` / `NARRATION_BG_VOLUME` | `1.0` / `0.35` | 해설 음량 / 해설 아래 효과음 비율 |
| `NARRATION_MAX_SPEEDUP` / `NARRATION_LEAD_SEC` | `1.4` / `0.2` | 장면보다 긴 문장을 이 배속까지 빠르게 / 장면 시작 후 말 시작 지연 |
| `LLM_COMFY_ENABLED` / `LLM_COMFY_MODEL` | `true` / (비우면 `TRANSLATE_COMFY_MODEL`) | 글 다듬기 LLM의 마지막 단계로 ComfyUI Gemma 사용. 12B는 한 번에 수 분이 걸려 쓰지 않는다 (e2b ≈ 10~25초) |
| `KO_EN_COMFY_MODEL` / `KO_EN_COMFY_CLIP_TYPE` | (비우면 Gemma4 e2b / `ltxv`) | 한국어 → 영어 번역·AI로 채우기 모델. 2026-10 비교: e2b 빠르고 정확(기본), `qwen3.5_2b_bf16`(type `flux2`) 번역이 가장 꼼꼼한 대안, `gemma4-12b-with-proj`는 글이 `SSSS…`로 깨짐(LTX 투영층이 들어 있어 ComfyUI가 LTX 텍스트 인코더로 읽고 입력을 1024토큰으로 채움 — 글쓰기용이 아님), `gemma_3_12B fp4`는 오역이 있어 모델 파일을 지움, `qwen_3_4b`는 한국어 생성이 깨짐 |
| `REFINE_LLM_URL` / `REFINE_LLM_LABEL` | (비움) / `gemma4-26b` | 장면 다듬기·AI로 채우기용 큰 모델 서버(llama-server, OpenAI 호환). 비우면 e2b로. 예: `http://127.0.0.1:8099` |
| `REFINE_LLAMA_SERVER` / `REFINE_LLAMA_MODEL` / `REFINE_LLAMA_ARGS` | (비움) / (비움) / `-ngl 99 --n-cpu-moe 12 -c 4096 --jinja` | llama-server.exe(Ollama 설치 폴더 `lib/ollama`)와 .gguf(Ollama blobs). 주면 필요할 때 켜고 끈다. 26B는 18GB라 전문가 일부를 CPU에(`--n-cpu-moe`) — 켜기 15~20초, 다듬기 20~30초 |
| `TRANSLATE_ENABLED` | `true` | 영어 장면 묘사를 화면에서 한글 번역 먼저 + 영어 원문 아래로 표시 (`pipeline/translate.py`, 캐시 `TextTranslation`). 영상 모델에는 영어 그대로 |
| `TRANSLATE_COMFY_MODEL` / `TRANSLATE_COMFY_CLIP_TYPE` | `gemma4_e2b_it_int8_convrot.safetensors` / `ltxv` | ComfyUI `TextGenerate` 노드로 번역할 모델 (이미 받아 둔 Gemma). ComfyUI가 꺼져 있으면 llm.py 폴백 → 영어만 표시. Gemma3 12B fp4는 오역이 있어 지웠다 |
| `TRANSLATE_TIMEOUT_SEC` | `900` | 영상 생성 중이면 ComfyUI 큐에서 장면 사이에 끼어 실행되므로 넉넉히 |
| `MOCK_DELAY_SEC` | `0` | mock 장면당 인위적 지연 (스테퍼 확인용) |
| `JOBS_SYNC` | `false` | true면 작업 큐 없이 요청 안에서 실행 (run_check용) |
| `LOCAL_VIDEO_MODEL` | `Wan-AI/Wan2.1-T2V-1.3B-Diffusers` | diffusers text-to-video 모델 ID |
| `LOCAL_VIDEO_WIDTH` / `HEIGHT` | `480` / `832` | 세로 9:16 |
| `LOCAL_VIDEO_FRAMES` / `FPS` | `81` / `16` | 샷당 약 5초 |
| `LOCAL_VIDEO_STEPS` | `30` | 샘플링 스텝 (낮추면 빠르고 품질 저하) |
| `LOCAL_VIDEO_MAX_SHOTS` | `4` | 한 화에 생성할 최대 샷 수 |
| `OLLAMA_MODEL` | (없어도 동작) | 로컬 LLM 모델명 — 설정 시 줄거리·샷 자동 작성 |
| `YOUTUBE_API_KEY` | (없어도 동작) | `SIMULATION_MODE=false`일 때 댓글 수집용 |
| `YOUTUBE_CLIENT_SECRETS` | (없어도 동작) | YouTube 업로드용 OAuth 클라이언트 JSON 경로 (리디렉션 URI `http://127.0.0.1:5000/youtube/callback`). 없으면 영상 ID 직접 입력 |
| `YOUTUBE_PRIVACY` / `CATEGORY_ID` / `UPLOAD_CHUNK_MB` / `TOKEN_FILE` | `private` / `22` / `8` / (instance) | 업로드 공개 범위 기본값 · 카테고리 · 조각 크기 · 토큰 위치 |

## 실행 방법

```powershell
# 최초 실행 (Windows PowerShell 기준, Python 3.11)
py -3.11 -m venv venv
.\venv\Scripts\pip install -r requirements.txt
.\venv\Scripts\pip install -r requirements-video.txt   # 로컬 영상 생성 시

# 앱 실행
.\venv\Scripts\python run.py

# 전체 기능 검증
.\venv\Scripts\python run_check.py

# 수동 분류+클러스터링
.\venv\Scripts\python run_cluster.py
```

### 검증 (테스트)
- pytest는 쓰지 않는다. **`run_check.py` 하나가 전체 검증**이다: 임시 DB + `JOBS_SYNC=True` + mock 어댑터 + 시뮬레이션으로
  2차 버전까지 돌리고, 끝에 `tests/video_checks.py`(가짜 ComfyUI `tests/fake_comfyui.py`)·`tests/custom_video_checks.py`를 부른다.
  실제 `instance/pipeline.db`는 건드리지 않는다. 약 270개 검증, 몇 분 걸린다. 끝에 "모든 검증 통과" 또는 "실패 N건: …"(종료 코드 1).
- 검증 한 개만 돌리는 옵션은 없다. 검증은 `check(label, condition, detail)`를 부르는 `_xxx_checks(client, app)` 함수로 나뉘어 있고
  `main()`이 차례로 부른다. 새 기능의 검증은 이 함수들에 추가한다. 외부 의존(LLM·TTS 등)은 모듈 함수를 잠시 바꿔 끼우고 `finally`에서 되돌린다.
- 러프컷·컷 경계 검증은 실제 ffmpeg(`.env`의 `FFMPEG_PATH`)를 쓴다.
- `run.py`는 `debug=False`(자동 재시작 없음) — **코드를 고친 뒤에는 Flask 서버를 다시 켜야** 반영된다. 끄기 전에 `generation_job`에 queued/running 작업이 없는지 본다.

## 클러스터링 핵심 설계 결정

### 임베딩 대상: comment_text 사용 (improvement_suggestion 아님)
```python
texts = [f.comment_text for f in constructive]
```
`improvement_suggestion`은 짧게 요약된 텍스트라 의미가 손실됨.
특히 한국어·영어 혼재 피드백의 교차 언어 클러스터링 품질이 크게 저하됨.

### 카테고리별 적응형 임계값 (v2)
```python
_CATEGORY_THRESHOLDS = {
    "technical": 0.26,  # 기술 이슈는 매우 구체적 → 좁게
    "subtitle":  0.30,
    "story":     0.34,  # 서로 다른 전개 요청이 한 클러스터로 뭉치지 않게 좁힘 (원래 0.48)
    "visual":    0.36,
    "audio":     0.38,
    "pacing":    0.42,
    "content":   0.44,
}
```
HDBSCAN은 그룹 내 댓글이 12개 이상일 때만 사용하고, 그보다 작으면 AgglomerativeClustering을 쓴다.

### 5축 우선순위 공식 (v2)
```python
P = 0.35·freq + 0.25·quality + 0.20·severity + 0.12·urgency + 0.08·validity
# 재발 부스트: prev_count > 0이면 × (1 + 0.10 × min(prev_count, 3))
# 상반 페널티: contradicted_by_key ≠ None이면 × 0.60
```
v1(`α=0.60·freq + β=0.40·validity`)에서 빈도 의존도를 낮추고 품질·심각도·긴급도를 반영.

### unified_diff 생성 방식 (approval.py)
```python
# splitlines() + "\n".join() — 헤더 라인 뭉침 방지
diff = difflib.unified_diff(orig_lines, upd_lines, fromfile="...", tofile="...", lineterm="")
return "\n".join(diff)
```

## 영상 생성 어댑터

| 어댑터 | 상태 | 조건 |
|--------|------|------|
| `mock` | 완성 | 항상 동작, 스토리보드 텍스트 반환 |
| `comfyui` | 구현됨 (가짜 ComfyUI로 검증, **실제 LTX-2.5 생성은 ComfyUI 실행 후 확인 필요**) | 로컬 ComfyUI 서버 + `promo_draft/workflows`의 LTX-2.5 워크플로. 장면 단위 생성, 작업 큐에서 실행. 서버가 꺼져 있으면 mock으로 폴백(화면에 "mock으로 대체됨") |
| `local` | 구현됨 (GPU 실생성 검증 필요) | diffusers Wan 2.x. 장면 단위 미지원 — 전체를 한 번에 생성 |
| `runway` | 구현됨 (**실제 API 호출은 검증하지 않음**, 테스트 더블로만 검증) | 상용. 키가 있을 때만, 사용자 비용 확인·예산 검사를 통과한 장면만 호출 |

산출물: `<VIDEO_OUTPUT_DIR 또는 instance/videos>/session_<id>/round_<n>/<shot_id>/v<k>.mp4`, 러프컷은 `round_<n>/rough_cut.mp4`.
`COMFYUI_IMAGE_FIRST=true`면 같은 폴더에 첫 프레임 `v<k>.png`도 남는다. 피드백으로 장면을 다시 만들 때는 시드가 바뀌어 이미지부터 다시 뽑는다.
이미지 먼저는 `comfyui` 어댑터 안에서만 동작한다 (`ComfyUIAdapter._first_frame`). 이미지 모델을 내려야 하므로 `COMFYUI_FREE_BETWEEN_SHOTS=true`를 권장한다.

로컬 영상의 한계: 음성·자막 없음(무성 클립), 샷 간 인물 일관성은 샷 묘사에 외모를 반복해 적는 것에 의존.

## 보안 규칙

- `.env`, API 키, 자격증명 파일은 절대 코드/커밋에 포함하지 않는다.
- 모델명, 임계값 등 설정은 전부 `config.py` + `.env`에서만 관리한다.
- 팀원 개인정보(학번, 연락처, 이메일)는 코드·문서 어디에도 포함하지 않는다.

## DB 스키마 — v2 신규 컬럼

### Feedback 테이블
| 컬럼 | 타입 | 설명 |
|------|------|------|
| `severity_score` | FLOAT | 심각도 (0~1) |
| `urgency_score` | FLOAT | 긴급도 (0~1) |
| `quality_score` | FLOAT | 피드백 품질 종합 (0~1) |
| `topic_categories_json` | TEXT | 멀티레이블 토픽 JSON `[(category, score), ...]` |
| `needs_review` | BOOLEAN | 분류 신뢰도 낮음 — 수동 검토 권장 |

### ClusterResult 테이블
| 컬럼 | 타입 | 설명 |
|------|------|------|
| `avg_severity_score` | FLOAT | 클러스터 내 평균 심각도 |
| `avg_urgency_score` | FLOAT | 클러스터 내 평균 긴급도 |
| `avg_quality_score` | FLOAT | 클러스터 내 평균 품질 |
| `cohesion_score` | FLOAT | 클러스터 응집도 (쌍별 코사인 유사도 평균) |
| `cluster_summary` | TEXT | 단어 빈도 기반 추출 요약 문장 |

## DB 마이그레이션 정책

- `db.create_all()` — 신규 테이블 자동 생성
- `_migrate_existing_tables()` (app/__init__.py) — 기존 테이블에 신규 컬럼 추가 (SQLite ALTER TABLE)
- 컬럼 제거/타입 변경 필요 시 → `instance/pipeline.db` 삭제 후 재생성

## 폴백 체인

```
피드백 분류:  EmbeddingClassifier (prototype 코사인 유사도, 인증 불필요)
              → ZeroShotClassifier (NLI, mDeBERTa, HuggingFace 인증 필요)
              → RuleBasedClassifier (키워드 규칙, 완전 오프라인)

클러스터링:  HDBSCAN (hdbscan 패키지 설치 시 자동 활성화)
              → AgglomerativeClustering (sklearn, 항상 동작)

임베딩:       paraphrase-multilingual-mpnet-base-v2 (~278MB)
              → paraphrase-multilingual-MiniLM-L12-v2 (~118MB)
              → TF-IDF 벡터화 (sklearn, 다운로드 없음)

영상 생성(장면별):
  generator=local → comfyui (LTX-2.5, 로컬 ComfyUI 응답 + 워크플로 매핑 있을 때) → mock
  generator=api   → 상용 API (COMMERCIAL_VIDEO_ADAPTER, 키 있을 때) → 재생성 불가 표시 (mock으로 몰래 대체하지 않음)
  VIDEO_GENERATOR_ADAPTER=mock/local 은 설정으로 선택 (자동 폴백 아님)

프롬프트 생성: Claude API (ANTHROPIC_API_KEY 있을 때)
               → Ollama 로컬 LLM (OLLAMA_MODEL 있을 때)
               → ComfyUI Gemma (LLM_COMFY_ENABLED, ComfyUI가 켜져 있을 때)
               → 템플릿 기반 (다음 버전은 [영상 설명]·[샷 목록]·[내레이션] 유지, 고칠 내용은 제작자가 직접)

댓글:         YouTube Data API (SIMULATION_MODE=false, YOUTUBE_API_KEY)
              / 시뮬레이션 (SIMULATION_MODE=true, 기본값)
              + 상세 화면에서 댓글 직접 입력 (어느 모드에서나 가능)
```

## 알려진 설계 주의사항

1. **EmbeddingClassifier 임계값**: `factory.py`에서 `specificity×0.75`, `validity×0.70`으로 낮춰 적용.
   EmbeddingClassifier는 규칙 기반 점수를 쓰므로 NLI보다 임계값을 낮게 설정해야 한다.

2. **클러스터 API (`/api/clusters`)**: 현재 라운드 기준으로 조회.
   라운드가 `prompt_updated`로 완료되면 다음 라운드가 생성되므로 검토 화면에 클러스터가 보이지 않는 것은 정상이다.

3. **버전 rollback 버튼**: `versions.html`에서 첫 번째 버전(v1)에는 rollback 버튼이 표시되지 않는다 (`{% if not loop.first %}`).

4. **DB 경로**: Flask 앱은 `instance/pipeline.db`를 사용한다 (`sqlite:///pipeline.db` → instance 폴더 기준).
   `run_cluster.py`도 동일한 app context를 사용하므로 같은 DB를 바라본다.

5. **HDBSCAN 노이즈 재배정**: HDBSCAN이 노이즈(-1)로 분류한 포인트는 가장 가까운 클러스터 중심으로 강제 배정한다.
   모두 노이즈인 경우 단일 클러스터로 처리한다.

6. **서브클러스터링 조건**: count > 15이고 분할 후 각 서브클러스터가 2개 이상 멤버를 가질 때만 분할한다.
   조건 미충족 시 원본 클러스터를 그대로 유지한다.

7. **작업 중 스토리보드 고치기**: 작업 큐가 장면을 만드는 동안(예: 컷 다시 만들기) 내레이션 저장처럼 `storyboard_json`을 고치는 요청이 들어가면
   반영되지 않을 수 있다(2026-10-22 s05 내레이션 저장이 사라진 사례). 고친 뒤 DB의 `storyboard_json`에 실제로 들어갔는지 확인한다.

8. **러프컷에서 장면이 짧아지는 몫**: 컷 파일 길이 − 앞뒤 잘라 냄(`CLIP_TRIM_HEAD/TAIL_SEC`) − 디졸브 겹침(마지막 장면 제외)이
   화면에 보이는 몫이고, 여기서 `NARRATION_LEAD_SEC`를 뺀 만큼만 말할 수 있다. 3초 컷이면 말할 시간은 2.2초 남짓.

9. **ComfyUI 통신** (`comfy_client.py`): 작업을 `client_id` 없이 보낸다(`show_in_ui`) — 붙이면 ComfyUI가 스텝 진행률을 그 클라이언트에만 보내
   ComfyUI 창에 진행이 안 보였다. 완료는 `/history` 폴링으로 확인한다. 컷이 끝난 직후 ComfyUI가 몇 분씩 응답하지 않아 결과 받기·다음 이미지 올리기가
   300초를 넘긴 적이 있어(2026-10-23) `upload`·`download`는 시간 초과·연결 오류만 3번까지 다시 시도한다(NVIDIA 드라이버 업데이트 뒤엔 재현되지 않음).

## 영상 파이프라인 설계 결정 (VIDEO_PIPELINE_INTEGRATION.md)

1. **프롬프트 문서가 원본, 스토리보드는 파생 스냅샷.** 텍스트 프롬프트(`[샷 목록]`·`[영상 스타일]`)는
   그대로 두고, 영상 생성 시점에 `storyboard_model.build_storyboard()`가 장면 JSON을 만들어
   `VideoRound.storyboard_json`에 저장한다. 라운드 간 "어느 장면이 바뀌었나" 비교와 타임스탬프→장면 매핑은 이 스냅샷을 쓴다.
2. **부분 재생성** (`orchestrator.plan_reuse`): 이전 라운드와 프롬프트·스타일·길이·해상도가 같고, 승인된 클러스터가 지적하지
   않은 장면은 `ShotRender(status=reused, reused_from_id=…)`로 이전 파일을 연결한다. 지적된 장면은 시드를 바꿔 다시 뽑고
   피드백 요지를 `notes`로 프롬프트에 덧붙인다 (`STORYBOARD_APPEND_NOTES`).
3. **지적 장면 계산** (`assign_affected_shots`): 멤버 댓글의 타임스탬프 → 장면. 타임스탬프가 없으면
   technical/subtitle/audio는 전 장면, 그 외는 LLM 추정 → 실패 시 전 장면.
4. **상용 API는 사람이 누를 때만 호출한다.** 서버가 비용을 다시 계산해 확인 금액과 다르면 409, 예산 초과도 409.
   이후 라운드에서 같은 장면을 로컬로 다시 만들 때 자동으로 API를 쓰지 않고 "API 권장" 배지만 띄운다 (자동 과금 방지).
5. **작업 큐**: 워커 스레드는 첫 `enqueue` 때 지연 시작한다(개발 서버 리로더의 부모 프로세스에서 뜨지 않음).
   재시작 시 `running` 작업은 `failed(interrupted)`로 바꾸고 라운드는 재시도 가능 상태로 돌린다.
6. **러프컷**: 선택된 버전을 스토리보드 해상도·fps로 정규화해 이어 붙이고, 오디오가 없는 조각에는 무음 트랙을 넣는다.

### DB 추가 (영상 파이프라인)

| 테이블 | 추가 |
|---|---|
| `VideoRound` | `storyboard_json`, `generation_progress`, `generation_detail`, `generation_error`, `adapter_used`, `video_approved` |
| `Feedback` | `timestamp_sec` |
| `ClusterResult` | `affected_shots_json` |
| `Metric` | `shots_total/regenerated/reused`, `regen_saved_seconds`, `api_shots`, `api_cost_usd` |
| `ShotRender` (신규) | 장면 결과 1건 (버전·상태·generator·provider·비용·품질 사유·selected·경로·재현용 파라미터·재사용 원본) |
| `GenerationJob` (신규) | 작업 큐 (kind·round·payload·status·error·시각) |

## 워크스페이스 구성 (통합 후)

```
project_ai_feedback/
  app/ config.py run.py run_check.py ...   # Flask 피드백 시스템 (이 문서의 대상)
  tests/                                   # fake_comfyui.py, video_checks.py (run_check.py가 호출)
  instance/                                # SQLite DB, 기본 영상 저장 위치
  promo_draft/                             # ComfyUI 초안 생성기 CLI + workflows/(Export API JSON, mapping.yaml)
  VIDEO_PIPELINE_INTEGRATION.md            # 영상 파이프라인 통합 설계서
```

- **이 폴더가 원본이다.** 이후 수정은 여기서만 한다 (통합 전 사본 `D:\capstone\ai_video`·`D:\capstone\ai`는 지워졌다).
- `COMFYUI_WORKFLOWS_DIR` 기본값은 `promo_draft/workflows` — 워크플로를 한 곳에서만 관리한다.
- 쓰지 않는 예전 파일은 `_archive/`에 모아 둔다(코드가 참조하지 않음, 지워도 동작에 영향 없음).
- 가상환경은 프로젝트 안의 `venv`를 쓴다:
  `.\venv\Scripts\python.exe run.py` (워크스페이스 폴더에서 실행). Qwen3-TTS·번역 작업자는 `venv_tts`,
  edge-tts·Supertonic은 `venv_tts2` (`py -3.11 -m venv venv_tts2; .\venv_tts2\Scripts\pip install edge-tts supertonic`).
- ComfyUI 설치 폴더·모델·`extra_model_paths.yaml`은 건드리지 않는다 (`D:\capstone\ComfyUI`, `Comfy-Desktop`).

## 데모 재생 (시연 영상 촬영용)

`PipelineSession.demo_json = {"rounds": {"1": {"replay_round_id": id, "youtube_video_id": "..."}, "2": {...}}}`인 프로젝트는
그 차수에서 '제작 시작'을 누르면 장면을 만들지 않고 `orchestrator.replay_demo`가 replay 버전의 결과(선택된 ShotRender·올릴 영상·
자막 .srt·편집/음악/내레이션/타임라인 설정)를 복사해 바로 ③ 확인으로 가고, 'YouTube에 올리기'는 업로드 없이 그 영상 ID에 연결한다
(`start_youtube_upload`의 preset). 제작은 작업 큐에서 '장면 n/N 만드는 중'을 `DEMO_REPLAY_SEC_PER_SHOT`(기본 1.5)초씩 보여 준 뒤 끝난다(`run_demo_replay`). 프롬프트는 이 버전 것을 두고 장면 구성(샷 목록·내레이션·자막)만 바꿔 '장면 구성 갱신'으로 남긴다.
예전 형식 `{"replay_round_id": id}`는 2차부터 전부. 화면에서 켜는 곳은 없다.
데모 프로젝트 27번(2026-10-22): 1차 = 24번이 처음 올린 영상(round 25, G95N4KGwnPA, 실제 댓글 7개), 2차 = 26번 final_nosub(round 28, wMahJiUlwcw).
**리허설 뒤 처음으로 되돌리기: `.\venv\Scripts\python demo_reset.py`** (① 기획 상태, 장면은 기획 화면에 한국어로 보이게 번역 캐시에 넣음).

## 파이프라인 점검 페이지 (`/dev/pipeline-test`)

로컬 개발용. `[TEST]` 세션을 만들어 영상 생성부터 2화 부분 재생성까지 단계별로 실행하고,
결과를 "기대 동작" 기준으로 자동 판정한다 (상태 일관성, 클립·러프컷 규격, 어댑터 폴백, 자동 업로드 금지,
타임스탬프→장면 매핑, 부분 재생성 재사용 등). 상용 API는 호출하지 않는다. `DEV_TEST_PAGE=false`로 끈다.

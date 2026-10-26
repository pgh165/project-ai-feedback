# AI 영상 피드백 자동 개선 시스템

> 사용자가 원하는 영상을 로컬 AI로 만들고, 시청자 댓글로 거듭 다듬는 파이프라인

제품 소개·홍보·뮤직비디오·단편 등 **만들고 싶은 영상**을 설명하면 로컬 영상 생성 AI(ComfyUI + LTX-2.5)가
장면 단위로 영상을 만듭니다. 내레이션(TTS)·배경음악·로고를 얹어 YouTube에 올린 뒤, 시청자 댓글에서
**건설적인 피드백만 자동으로 골라 묶고**, 제작자가 승인한 의견을 다음 버전에 반영합니다.
지적받은 장면만 다시 만들고, 소리·편집 의견은 장면을 다시 만들지 않고 올릴 영상 합성에만 반영합니다.

**핵심 설계 원칙: 외부 계정 없이 동작한다.** 영상·이미지·음악 생성, 분류·임베딩, 번역, 음성 합성이 모두 로컬에서 실행되며,
상용 API(Claude, Runway)와 YouTube 연동은 키·계정이 있을 때만 선택적으로 사용합니다.

---

## 목차

1. [전체 흐름](#1-전체-흐름)
2. [화면 구성](#2-화면-구성)
3. [주요 기능](#3-주요-기능)
4. [기술 스택과 모델](#4-기술-스택과-모델)
5. [설치와 실행](#5-설치와-실행)
6. [프로젝트 구조](#6-프로젝트-구조)
7. [파이프라인 상태 머신](#7-파이프라인-상태-머신)
8. [피드백 처리 알고리즘](#8-피드백-처리-알고리즘)
9. [영상 생성 파이프라인](#9-영상-생성-파이프라인)
10. [프롬프트 문서 형식](#10-프롬프트-문서-형식)
11. [폴백 체인](#11-폴백-체인)
12. [주요 설정](#12-주요-설정)
13. [오픈소스 vs 팀 직접 구현](#13-오픈소스-vs-팀-직접-구현)
14. [검증](#14-검증)
15. [시연용 데모 재생](#15-시연용-데모-재생)
16. [알려진 한계](#16-알려진-한계)

---

## 1. 전체 흐름

```
 ┌────────────────┐   ┌───────────────────────┐   ┌──────────────────┐   ┌──────────────────┐
 │ ① 기획          │ → │ ② 제작                 │ → │ ③ 확인            │ → │ ④ 공개            │
 │ 설명·장면·내레이션│   │ 첫 프레임 이미지 → 확인 │   │ 러프컷 + 내레이션  │   │ YouTube 업로드    │
 │ (콘티 엑셀/완성  │   │ → 컷 영상 → 확인       │   │ + 배경음악 + 로고  │   │ + 한국어 자막 트랙 │
 │  영상 불러오기)  │   │ (Z-Image + LTX-2.5)   │   │ (타임라인 편집)    │   │                  │
 └────────────────┘   └───────────────────────┘   └──────────────────┘   └────────┬─────────┘
         ▲                                                                          │
         │                                                                          ▼
 ┌───────┴─────────┐   ┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐
 │ 다음 버전(N+1차) │ ← │ 제작자 승인        │ ← │ 군집화·우선순위    │ ← │ ⑤ 의견            │
 │ 지적 장면만 재생성│   │ (반영 위치 지정)   │   │                  │   │ 댓글 수집·6축 분류 │
 │ 편집·소리는 재합성│   └──────────────────┘   └──────────────────┘   └──────────────────┘
 └─────────────────┘
```

### 시스템 블록도

```mermaid
flowchart TB
    user(["제작자 (브라우저)"]) --> app

    subgraph app["Flask 웹 앱"]
        direction LR
        routes["routes/session.py<br/>화면 · API"] --> orch["orchestrator.py<br/>상태 머신 ①~⑨"] --> jobs["jobs.py<br/>GPU 작업 큐"]
        orch <--> db[("SQLite<br/>pipeline.db")]
    end

    subgraph s1["① 기획"]
        direction LR
        in1["설명 · 장면 · 내레이션<br/>콘티 엑셀 · 완성 영상"] --> koen["ko_en · draft_fill<br/>한국어→영어 · AI로 채우기<br/>(Gemma)"] --> prompt["prompt_builder<br/>프롬프트 문서"]
    end

    subgraph s2["② 제작"]
        direction LR
        sb["storyboard_model<br/>스토리보드"] --> img["첫 프레임 이미지<br/>(Z-Image Turbo)"] --> clip["컷 영상<br/>(LTX-2.5 I2V)"]
        fb["feedback_target<br/>장면 피드백 · 길이 요청"] -.-> img
    end

    subgraph s3["③ 확인"]
        direction LR
        rc["rough_cut (ffmpeg)<br/>컷 잇기 · 디졸브"] --> mix["내레이션 (edge-tts)<br/>배경음악 (MiniMax Music)<br/>로고 · 타임라인"] --> out["올릴 영상 .mp4<br/>+ 자막 .srt"]
    end

    subgraph s4["④ 공개"]
        direction LR
        up["youtube_upload<br/>영상 + 한국어 자막 트랙"] --> yt(["YouTube"])
        viewer(["시청자"]) -- 댓글 --> yt
    end

    subgraph s5["⑤ 의견"]
        direction LR
        col["youtube_client<br/>댓글 수집"] --> cls["classifier<br/>6축 분류"] --> clu["clusterer<br/>군집화 · 우선순위"] --> appr["approval<br/>제작자 승인 · 반영 위치"]
    end

    subgraph nx["다음 버전 (N+1차)"]
        direction LR
        r1["화면 · 움직임 의견<br/>→ 지적 장면만 다시 만듦"]
        r2["소리 · 편집 의견<br/>→ 편집 설정 (장면 재사용)"]
        r3["내용 의견<br/>→ 프롬프트 갱신 (LLM)"]
    end

    app --> s1 --> s2 --> s3 --> s4 --> s5 --> nx
    nx == "반복" ==> s1
```

- **프로젝트** = `PipelineSession` (화면 비율·장면 길이·로고·목소리)
- **버전(N차)** = `VideoRound` (프롬프트 문서·스토리보드 스냅샷·편집/음악 설정·상태)
- **장면 결과 1건** = `ShotRender` (이미지·컷 버전, 다시 만들면 새 버전이 쌓이고 고를 수 있음)

## 2. 화면 구성

| 화면 | 주소 | 내용 |
|------|------|------|
| 첫 화면 | `/` | 가운데 입력창에 만들고 싶은 영상을 적고 화면 비율·첨부(콘티 엑셀/완성 영상)를 고름 |
| 새 영상 (① 기획) | `/session/create` | 제목·종류 추천, 장면 구성·내레이션·스타일 입력, **AI로 채우기**, 콘티 엑셀 불러오기 |
| 내 프로젝트 | `/projects` | 프로젝트 목록, `?view=todo` = 할 일(사람 확인이 필요한 단계) |
| 프로젝트 상세 | `/session/<id>` | 왼쪽 **단계 탭** ① 기획 ② 제작 ③ 확인 ④ 공개 ⑤ 의견 + 위쪽 상태 줄(진행률·생성 중지) |
| 의견 검토 | `/session/<id>/review` | 클러스터 승인·거절·보류, 반영 위치, 다음 버전 편집 설정 |
| 버전 기록·리포트 | `/session/<id>/versions`, `/report` | 프롬프트 diff·롤백, 반영률·절약 시간·재사용 장면 |
| 완성 영상 불러오기 | `/session/import` | 컷 경계 자동 감지 → 합치기/나누기 → 1차 버전으로 등록 |
| 점검 페이지 (개발용) | `/dev/pipeline-test` | 영상 생성~2차 부분 재생성까지 단계별 실행·자동 판정 |

모든 버전이 같은 단계 탭을 쓰고, 지난 버전은 읽기 전용입니다. 진행 중에는 상태 줄이 2초마다 갱신됩니다.

## 3. 주요 기능

### 기획
- **텍스트로 새 영상 만들기** — 영상 설명·장면 목록·내레이션·스타일을 입력. **한국어로 적어도 된다**:
  저장할 때 장면·스타일 줄만 영어로 바꿔 영상 모델에 주고(`ko_en.py`), 화면에는 적은 한국어 그대로 보여 줌.
  번역에 실패하면 만들지 않고 안내(한국어를 몰래 모델에 넣지 않음)
- **AI로 채우기** (`draft_fill.py`) — 비어 있는 칸만 한국어 초안(제목·종류·등장 요소·장면·내레이션·스타일)으로 채움.
  설명의 'N초'로 장면 수를 정하고, 적어 둔 칸은 덮어쓰지 않음
- **콘티 엑셀(.xlsx) 불러오기** — `장면 ID`·`길이(초)`·`내레이션`·`장면 동작 (영문)`·`시드` 등 머리글 이름으로 콘티를 읽어 폼을 채움
- **완성 영상 불러오기** — 이미 만든 영상(+선택 .srt)을 올리면 컷 경계를 자동으로 찾아 제안(디졸브·페이드도 감지,
  하드 컷은 프레임 단위). 각 장면의 영어 묘사는 Gemma가 장면 가운데 프레임을 보고 작성
- **장면 다듬기** (`scene_refine.py`) — 적어 둔 장면 줄을 이미지·영상 모델이 잘 따르는 영어 한 줄로 고쳐 씀
  (분위기·장소 먼저, 보이는 것만, 카메라 움직임은 끝에). 고치기 전·후를 보여 주고 받아들일 때만 바꿈.
  큰 모델(Gemma4 26B, llama-server)을 필요할 때만 켜고 끄며, 영상 작업 중이면 e2b로 대신함
- **제작 중에도 기획 고치기** — 장면을 (묘사, 길이)로 짝지어 고친·새 장면만 다시 만들고, 저장 전에 다시 만들 장면을 미리 보여 줌

### 제작 (② 장면별 영상)
- **단계별 확인 생성** — 첫 프레임 이미지 확인(피드백 문장과 함께 "다시 뽑기") → 컷 영상 확인("다시 만들기") → 사람이 눌러야 러프컷
- **장면별 피드백** — "더 밝게", "카메라 기준 오른쪽으로 코너 돌게" 같은 문장을 장면 묘사 자체를 고친 영어 프롬프트로 바꿔 다시 만듦.
  **길이 요청**("3초로", "2초 더", "두 배", "늘려줘", "나레이션이 빨라")은 장면 길이를 바꾸고, 얼마나를 말하지 않으면
  **내레이션이 1배속으로 들어가는 길이**를 재서 맞춤(앞뒤 잘라 냄·디졸브 겹침·말 시작 지연 포함)
- **이 순간을 끝으로 다시 만들기** — 컷에서 고른 순간을 마지막 프레임으로 고정해 같은 길이로 다시 만듦
  (LTX-2.5 FLF2V). 요청한 동작 뒤에 엉뚱한 동작이 붙을 때 씀
- **의견 관리** — 무엇으로 바꿀지 없는 모호한 의견은 방향을 먼저 묻고, 지난 장면 의견은 지울 수 있음
- **생성 중지·재시도** — 대기 작업은 취소, 실행 중 장면은 ComfyUI `/interrupt`. 만들어진 장면은 남고 실패한 장면만 다시
- **상용 API 재생성** — 품질이 부족한 장면만, 사람이 비용을 확인하고 누를 때만 Runway로 (예산 상한 검사)

### 확인 (③ 올릴 영상)
- **러프컷 합성** — 선택된 컷을 정규화해 디졸브(기본 0.4초)로 잇고, AI 컷의 시작 멈칫·끝 느려짐을 조금 잘라 냄
- **내레이션** — 장면별 한국어 해설을 TTS로 합성해 장면 길이에 맞춤(최대 1.4배속, 그래도 넘치면 안내).
  프로젝트마다 목소리 선택·미리 듣기, 이 버전의 크기·빠르기. 해설만 고치면 음성만 다시 합성
- **배경음악** — 영상 전체에 연주곡 한 곡(MiniMax Music 3, 로컬). 곡 설명은 Gemma가 제안하고 고칠 수 있음(영어가 잘 따름).
  곡을 60초 이상으로 만들어 **소리 크기가 가장 고른 구간**을 골라 씀(짧게 만들면 도입·끝맺음이 욱여넣어져 출렁였다).
  내레이션이 나오면 자동으로 작아지고, 다음 버전에도 같은 곡이 이어짐("분위기가 안 맞아요" 같은 의견일 때만 바뀜)
- **로고** — PNG·JPG·WEBP 또는 Illustrator `.ai`(PDF 호환)·`.pdf`(투명 PNG로 변환). 첫·마지막 장면 위 미리보기에서
  **누르거나 끌어서 위치 지정**(저장 전 벡터 파일도), 크기·불투명도, 보이는 때(전체/처음 N초/끝 N초/타임라인 구간)
- **타임라인** — 올릴 영상 아래에 장면·자막·로고 막대를 영상 시각에 맞춰 그림. 막대를 끌어 옮기거나 양 끝을 끌어
  자막·로고가 나오는 때를 정하고, 영상 재생 위치와 함께 움직임
- **자막은 YouTube 자막으로** — 영상에 굽지 않고 내레이션 시각에 맞춘 `.srt`를 만들어 업로드 때 한국어 자막 트랙으로 올림.
  직접 올릴 때를 위해 `.srt` 내려받기 제공
- **장면 소리 끄기** — 영상 모델이 만든 효과음을 빼고 내레이션·배경음악만

### 공개·의견 (④·⑤)
- **YouTube 업로드** — Google 계정 연결(OAuth PKCE) 후 제작자가 눌러야만 업로드(이어 올리기, 공개 범위, AI 생성 표시) + 자막 트랙.
  연결 없이 직접 올린 영상 ID를 이어 줄 수도 있음
- **공개 취소** — 댓글을 모으기 전이면 ③ 확인으로 되돌려 올릴 영상만 고쳐 다시 올림(장면은 그대로, 예전 영상은 지우지 않음)
- **댓글 수집** — YouTube Data API 또는 시뮬레이션, 화면에서 직접 입력·장면별 의견도 가능
- **6축 분류** — 구체성·타당성·심각도·긴급도·품질·멀티레이블 토픽, 댓글 속 `1:23` 같은 시각 → 장면 매핑
- **2단계 군집화** — 토픽별 그룹 → 그룹 내 HDBSCAN / 계층적 군집화, **5축 우선순위** + 재발 이슈 부스트 + 상반 의견 감지
- **승인 워크플로** — 클러스터별 승인 / 거절 / 보류, 반영 위치(화면·움직임·편집·소리·내용) 지정
- **다음 버전** — 화면·움직임 의견이 지적한 장면만 다시 만들고 나머지는 재사용. 소리·편집 의견은 **편집 설정**
  (전환·소리 크기·빠르기·장면 길이·속도·순서·자막·배경음악)으로만 반영. 내용 의견은 LLM이 [영상 설명]·[내레이션]만 다시 씀
- **프롬프트 버전 관리·리포트** — 버전별 diff, 롤백, 반영률·수동 처리 대비 절약 시간·재사용 장면 수·상용 API 비용

## 4. 기술 스택과 모델

| 영역 | 사용 기술 |
|------|-----------|
| 백엔드 | Python 3.11, Flask 3.x, SQLAlchemy (SQLite) |
| ML | sentence-transformers, scikit-learn, hdbscan, transformers |
| 영상·이미지·음악·글 | ComfyUI (LTX-2.5, Z-Image Turbo, Gemma, MiniMax Music 3), ffmpeg |
| 음성 | edge-tts, Supertonic-3, (선택) Qwen3-TTS, Windows SAPI |
| UI | Bootstrap 5, Chart.js |

| 역할 | 모델 | 라이선스 | 실행 위치 |
|------|------|---------|-----------|
| 피드백 분류 (기본) | `paraphrase-multilingual-MiniLM-L12-v2` | Apache 2.0 | CPU |
| 클러스터링 임베딩 | `paraphrase-multilingual-mpnet-base-v2` | Apache 2.0 | CPU |
| 분류 폴백 (NLI) | `MoritzLaurer/mDeBERTa-v3-base-zeroshot-v2.0` | MIT (게이트됨) | CPU |
| 영상 생성 | LTX-2.5 (T2V / I2V, ComfyUI, GGUF Q4_K_M) | — | GPU |
| 첫 프레임 이미지 | Z-Image Turbo | — | GPU |
| 번역·AI로 채우기·장면 묘사·글 다듬기 | Gemma4 e2b (ComfyUI `TextGenerate`) | — | GPU |
| 장면 다듬기·AI로 채우기 (큰 모델, 선택) | Gemma4 26B A4B (llama-server, 필요할 때만 켬) | — | GPU+CPU |
| 배경음악 | MiniMax Music 3 | MiniMax-Music3 Community License (YouTube 설명에 표기) | GPU |
| 내레이션 | edge-tts(기본, 인터넷 필요) → Supertonic-3(로컬 CPU) | — | CPU |
| 영상 생성 (`local` 어댑터) | `Wan-AI/Wan2.1-T2V-1.3B-Diffusers` | Apache 2.0 | GPU |

> 기준 GPU: RTX 5070 Ti 16GB. 영상 생성 중에는 GPU를 쓰는 다른 프로그램을 꺼야 합니다.
> ComfyUI 모델·설치 폴더는 이 저장소 밖에 있고, 워크플로 JSON·설정은 이름만 가리킵니다.

## 5. 설치와 실행

```powershell
# 가상환경 (Windows PowerShell, Python 3.11)
py -3.11 -m venv venv
.\venv\Scripts\pip install -r requirements.txt
.\venv\Scripts\pip install -r requirements-video.txt   # local(diffusers) 어댑터를 쓸 때만

# 내레이션 엔진 (edge-tts · Supertonic) — 별도 가상환경
py -3.11 -m venv venv_tts2
.\venv_tts2\Scripts\pip install edge-tts supertonic

# 설정
copy .env.example .env    # 값 채우기 (키가 없어도 동작)

# 앱 실행 → http://127.0.0.1:5000
.\venv\Scripts\python run.py
```

- 실제 영상·이미지·음악·번역에는 ComfyUI 서버(`COMFYUI_URL`, 기본 `http://127.0.0.1:8188`)가 필요합니다.
  꺼져 있으면 영상은 mock 어댑터로 대체되고 화면에 "mock으로 대체됨"이 표시됩니다.
- 러프컷·컷 감지는 ffmpeg를 씁니다(`FFMPEG_PATH` → PATH → imageio-ffmpeg 순으로 찾음).
- `run.py`는 자동 재시작이 없습니다. **코드를 고친 뒤에는 서버를 다시 켜야** 반영됩니다.
- Qwen3-TTS·번역 작업자를 쓰려면 `venv_tts` 가상환경이 필요합니다(선택).

## 6. 프로젝트 구조

```
project_ai_feedback/
├─ app/
│  ├─ __init__.py              create_app(), 기존 테이블에 새 컬럼 추가(SQLite ALTER TABLE)
│  ├─ models.py                DB 모델
│  ├─ pipeline/
│  │  ├─ classifier/           6축 분류기 (임베딩 → NLI → 규칙 폴백), 타임스탬프 추출
│  │  ├─ clusterer/            임베딩, 토픽 태깅, 계층적 군집화, 우선순위
│  │  ├─ video_generator/      어댑터: comfyui / local / mock / runway
│  │  │  └─ comfyui/           ComfyUI 클라이언트, 워크플로 주입, rough_cut.py(러프컷·내레이션 트랙·로고·음악·자막 .srt)
│  │  ├─ narration/            내레이션 TTS (edge/Supertonic/Qwen 작업자, SAPI) + 장면 길이에 맞추기
│  │  ├─ orchestrator.py       상태 머신, 단계 계산, 생성 흐름, 부분 재생성, 기획 변경, 데모 재생
│  │  ├─ jobs.py               GPU 작업 큐 (워커 스레드 1개)
│  │  ├─ storyboard_model.py   스토리보드 스키마, 타임스탬프 → 장면 매핑
│  │  ├─ feedback_target.py    의견의 반영 위치, 길이 요청 읽기, 장면 묘사 고쳐 쓰기
│  │  ├─ edit_plan.py          편집 설정 + 의견 → 편집 제안
│  │  ├─ overlay.py            로고 합성, 자막 .srt, 로고 시각 계산
│  │  ├─ bgm.py music.py       배경음악 설정·곡 생성(ComfyUI)·고른 구간 고르기
│  │  ├─ voice.py              내레이션 목소리·말투 (프로젝트 공통)
│  │  ├─ ko_en.py draft_fill.py scene_refine.py   한국어 입력 → 영어, AI로 채우기, 장면 다듬기
│  │  ├─ comfy_text.py translate.py   Gemma 글 생성(이미지 입력), 화면용 영→한 번역 캐시
│  │  ├─ storyboard_import.py video_import.py   콘티 엑셀 / 완성 영상 불러오기
│  │  ├─ approval.py           클러스터 저장·승인 적용·롤백·재발 추적
│  │  ├─ prompt_builder.py prompt_updater.py   프롬프트 문서 구성·파싱 / 다음 버전 프롬프트
│  │  ├─ llm.py                Claude → Ollama → ComfyUI Gemma → 템플릿
│  │  ├─ quality_gate.py       상용 재생성 비용·예산 검사
│  │  ├─ youtube_client.py youtube_upload.py   댓글 수집 / 업로드·자막(OAuth + 이어 올리기)
│  │  └─ metrics.py            반영률·시간 절약 지표
│  ├─ routes/                  session.py(화면·API), devtest.py(점검 페이지)
│  ├─ templates/               home, index, _app_shell, session/{detail, create, import, _plan_form, _scene_viewer, _timeline, review, …}
│  └─ static/                  css/style.css, js/round_view.js(상태 줄), devtest.js·pipeline_stepper.js(점검 페이지)
├─ tests/                      fake_comfyui.py, video_checks.py, custom_video_checks.py (run_check.py가 호출)
├─ promo_draft/                학과 홍보영상 초안 CLI (run.py + storyboard_*.yaml, 결과는 outputs/)
│  └─ workflows/               ComfyUI Export(API) 워크플로 JSON + mapping.yaml — 앱도 여기서 읽음
│                              (ltx25_t2v · ltx25_i2v · ltx25_flf2v · zimage_t2i)
├─ instance/                   pipeline.db, 영상·로고·토큰 (git 제외)
├─ docs/                       feedback_loop.html (피드백 루프 설명 그림)
├─ _archive/                   쓰지 않는 예전 파일 보관 (git 제외, 지워도 동작에 영향 없음)
├─ config.py                   모든 설정 (.env 우선)
├─ run.py                      앱 실행
├─ run_check.py                전체 검증
├─ run_cluster.py              수동 분류·군집화
├─ demo_reset.py               데모 프로젝트를 처음으로 되돌림
├─ requirements.txt            기본 의존성 (CPU만으로 동작)
├─ requirements-video.txt      local(diffusers) 어댑터용 GPU 의존성
├─ CLAUDE.md                   개발 지침 (상세 설계)
└─ VIDEO_PIPELINE_INTEGRATION.md   영상 파이프라인 통합 설계서
```

가상환경: `venv`(앱), `venv_tts2`(edge-tts·Supertonic), `venv_tts`(Qwen3-TTS·번역 작업자, 선택).

### 주요 DB 테이블

| 테이블 | 역할 |
|--------|------|
| `PipelineSession` | 프로젝트 (화면 비율·장면 길이·로고 `brand_json`·목소리 `voice_json`·데모 `demo_json`) |
| `VideoRound` | 버전(N차): 상태·프롬프트·스토리보드 스냅샷·편집/음악/내레이션 설정·타임라인·YouTube ID |
| `ShotRender` | 장면 결과 1건 (버전·상태·시드·이미지/영상 경로·비용·재사용 원본·선택 여부) |
| `GenerationJob` | GPU 작업 큐 |
| `Feedback` | 댓글 + 6축 점수 + 타임스탬프 |
| `ClusterResult` | 클러스터 (우선순위·응집도·요약·지적 장면·반영 위치·승인 상태) |
| `PromptVersion` / `PromptChange` | 프롬프트 버전과 diff |
| `GlobalTechnicalSetting` | 전역 기술 설정으로 승격된 의견 |
| `Metric` | 반영률·재사용 장면·상용 비용 지표 |
| `TextTranslation` | 번역 캐시 (영어 장면 묘사 ↔ 한국어, 사용자가 쓴 한국어 원문 포함) |

## 7. 파이프라인 상태 머신

```
① prompt_built          프롬프트(스토리보드) 생성/갱신 완료
② generating            GPU 작업 큐에서 생성 중 (진행률 n/N)
     ├ images_ready     첫 프레임 이미지 확인 대기   ┐ 단계별 확인 생성
     ├ clips_ready      컷 영상 확인 대기            ┘ (COMFYUI_STAGED_REVIEW)
     └ generation_failed  실패 장면만 재시도
③ video_ready           올릴 영상(러프컷) 완료, 사람 확인 대기
④ uploaded              공개 (댓글 수집 전이면 ③으로 되돌릴 수 있음)
⑤ feedback_collected    댓글 수집 완료
⑥ feedback_classified   6축 분류 + 타임스탬프 추출
⑦ clustering            군집화 진행 중 (비동기)
⑧ pending_review        제작자 승인 대기
⑨ prompt_updated        승인 적용 → 다음 버전 ① 생성
```

- 상태 전이는 `orchestrator.advance(round, to_state)` **한 곳에서만** 수행하며, 허용되지 않은 전이는 `InvalidTransition`.
- 같은 단계의 반복(댓글 추가·재분류·재군집화)은 자기 자신으로의 전이로 허용.
- 화면의 단계 탭: ② 제작 = 장면별 영상(이미지 → 컷), ③ 확인 = 올릴 영상 합성·확인. 러프컷 합성 중에는 ③에 진행을 표시.
- 상태 API `GET /api/rounds/<id>/status`, 제어 API `POST /api/rounds/<id>/{retry,advance,escalate,approve-video,cancel,…}`.

## 8. 피드백 처리 알고리즘

### 6축 분류 (`EmbeddingClassifier`)
원형(prototype) 문장과의 코사인 유사도로 건설적/비건설적을 판별하고, 정규식 신호로 점수를 매깁니다.

| 축 | 방식 |
|----|------|
| 구체성 (specificity) | 정규식 신호 + 임베딩 |
| 타당성 (validity) | 정규식 신호 |
| 심각도 (severity) | 정규식 신호 |
| 긴급도 (urgency) | 정규식 신호 |
| 품질 (quality) | 위 점수 종합 |
| 토픽 | 멀티레이블, 댓글 하나에 최대 3개 카테고리 |

승인된 피드백으로 원형을 지수이동평균 갱신하는 **온라인 학습**(`update_prototype()`)을 지원합니다.

### 2단계 군집화
1. **토픽 태깅** — `topic_category` 기준으로 그룹화
2. **그룹 내 군집화** — 댓글 12개 이상이면 HDBSCAN, 미만이면 AgglomerativeClustering (cosine, average)

카테고리별 적응형 거리 임계값:

| technical | subtitle | story | visual | audio | pacing | content |
|-----------|----------|-------|--------|-------|--------|---------|
| 0.26 | 0.30 | 0.34 | 0.36 | 0.38 | 0.42 | 0.44 |

- 임베딩 대상은 요약문이 아니라 **댓글 원문**(교차 언어 품질 유지)
- 15개 초과 클러스터는 자동 서브클러스터링
- 대표 텍스트 = 중심 근접도 50% + 품질 30% + 길이 20%
- 응집도 점수(쌍별 코사인 유사도 평균), 단어 빈도 기반 요약 문장

### 5축 우선순위
```
P = 0.35·빈도 + 0.25·품질 + 0.20·심각도 + 0.12·긴급도 + 0.08·타당성
재발 부스트:  × (1 + 0.10 × min(이전 등장 횟수, 3))
상반 페널티:  × 0.60   (상반된 의견이 함께 있을 때)
```

### 반영 위치 (`feedback_target.py`)
| 위치 | 의미 | 장면 처리 |
|------|------|-----------|
| `scene` 화면 | 구도·대상·색감 | 첫 프레임 이미지부터 다시 (시드 변경) |
| `motion` 움직임 | 카메라·동작 | 이전 첫 프레임 유지, 컷만 다시 |
| `edit` 편집 | 전환·속도·순서·자막 | 재사용 → 편집 설정으로 올릴 영상만 다시 합성 |
| `audio` 소리 | 음악·음성·내레이션 | 재사용 → 편집 설정으로 올릴 영상만 다시 합성 |
| `content` 내용 | 설명 부족·장면 추가 | 재사용 → [시청자 반영 사항], LLM이 [영상 설명]·[내레이션] 다듬기 |

키워드 + 카테고리로 자동 제안되고 검토 화면에서 바꿀 수 있습니다.
지적된 장면은 한국어 의견을 **장면 묘사 자체를 고친 영어 문장**(`prompt_fixed`)으로 바꿔 원래 묘사 대신 씁니다
(고친 상태만 묘사하고 실수는 묘사하지 않음). 한국어 원문은 이미지 모델이 화면에 글자로 그려 버리므로 절대 넣지 않습니다.

## 9. 영상 생성 파이프라인

### 어댑터

| 어댑터 | 설명 |
|--------|------|
| `comfyui` | 로컬 ComfyUI + LTX-2.5, 장면 단위 생성 (기본) |
| `local` | diffusers Wan 2.x, 전체를 한 번에 생성 |
| `mock` | 스토리보드만, 영상 파일 없음 (폴백·시험용) |
| `runway` | 상용, 품질 부족 장면 재생성 전용 |

### 장면 생성 과정 (이미지 먼저 + 단계별 확인)
1. Z-Image Turbo로 장면 첫 프레임 이미지 생성 → 사람이 확인, 필요하면 피드백 문장과 함께 "다시 뽑기"(시드 +101)
2. 승인된 이미지를 LTX-2.5 I2V로 움직여 컷 생성 → 확인, "다시 만들기"(이전 컷은 버전으로 남아 고를 수 있음)
3. "다음: 확인 단계로"를 누르면 선택된 컷을 정규화·다듬어 디졸브로 잇고, 내레이션·배경음악·로고를 얹고 자막 `.srt`를 만듦

### 설계 결정
- **프롬프트 문서가 원본, 스토리보드는 파생 스냅샷** — 생성 시점에 장면 JSON을 만들어 `VideoRound.storyboard_json`에 저장
- **부분 재생성** — 이전 버전과 프롬프트·스타일·길이·해상도가 같고 지적되지 않은 장면은 파일을 재사용
- **지적 장면 계산** — 댓글 타임스탬프 → 장면. 없으면 technical/subtitle/audio는 전 장면, 그 외는 LLM 추정
- **편집 설정은 장면 서명에 들어가지 않음** — 소리·편집·자막·로고·음악을 바꿔도 장면은 다시 만들지 않고 올릴 영상만 다시 합성
- **타임라인 = 실제 합성 값** — 러프컷을 만들 때 쓴 장면·자막·로고 시각을 `VideoRound.timeline_json`에 남겨 그대로 그림
- **상용 API는 사람이 누를 때만** — 서버가 비용을 다시 계산해 확인 금액과 다르거나 예산을 넘으면 409. 자동 과금 없음
- **작업 큐** — 워커 스레드는 첫 작업 때 지연 시작, 재시작 시 실행 중이던 작업은 재시도 가능 상태로 복구

산출물: `instance/videos/session_<id>/round_<n>/<shot_id>/v<k>.mp4` (+ 첫 프레임 `v<k>.png`),
올릴 영상 `round_<n>/rough_cut.mp4`, 자막 `round_<n>/subtitles.srt`, 배경음악 `round_<n>/bgm_*.flac`

## 10. 프롬프트 문서 형식

`[섹션명]` 헤더로 나뉜 텍스트이며 버전 관리·diff 대상입니다.

```
[프로젝트]        [영상 종류]       [버전]          [등장 요소]
[이전 버전 요약]  [영상 설명]       [샷 목록]       [내레이션]
[영상 스타일]     [시청자 반영 사항] [연출 지침]
```

- 영상 모델에는 **[샷 목록]의 각 줄(영어) + [영상 스타일]만** 전달 (샷 1개 = 클립 1개)
- 샷 줄 앞에 `[6초]`, `[6초 시드 202]`를 쓰면 그 샷만 길이·시드 지정 (모델에는 전달되지 않음)
- [내레이션]은 샷 번호와 1:1인 한국어 해설 — 장면 서명에 포함되지 않아 해설만 고치면 음성만 다시 합성
- 다음 버전에서도 [영상 설명]·[샷 목록]은 유지되고, 승인된 의견은 [시청자 반영 사항]·[연출 지침]으로 들어감
- 로고·글자는 절대 영상 모델 프롬프트에 넣지 않음 (로고는 합성, 자막은 YouTube 자막 트랙)

## 11. 폴백 체인

```
피드백 분류   EmbeddingClassifier → ZeroShotClassifier(NLI) → RuleBasedClassifier
군집화        HDBSCAN → AgglomerativeClustering
임베딩        mpnet-base-v2 → MiniLM-L12-v2 → TF-IDF
영상 생성     comfyui(LTX-2.5) → mock          (상용은 키 있을 때만, mock으로 몰래 대체하지 않음)
글 다듬기     Claude API → Ollama(로컬) → ComfyUI Gemma(로컬) → 템플릿
장면 다듬기   Gemma4 26B(llama-server) → Gemma4 e2b(ComfyUI)
번역          ComfyUI Gemma → llm.py → 영어만 표시
내레이션      edge-tts → Supertonic-3 (VOICE_ADAPTER / VOICE_FALLBACK)
배경음악      MiniMax Music 3 (ComfyUI) → 음악 없이 합성하고 사유 표시
댓글          YouTube Data API / 시뮬레이션 / 화면 직접 입력
```

## 12. 주요 설정

모델명·임계값·경로는 코드에 하드코딩하지 않고 모두 `config.py` + `.env`로 주입합니다. 전체 목록은 `config.py`와 `CLAUDE.md` 참고.

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `VIDEO_GENERATOR_ADAPTER` | `comfyui` | `comfyui` / `local` / `mock` |
| `COMFYUI_URL` | `http://127.0.0.1:8188` | ComfyUI 서버 |
| `COMFYUI_IMAGE_FIRST` | `false` | 첫 프레임 이미지 먼저 생성 후 I2V (권장 `true`) |
| `COMFYUI_STAGED_REVIEW` | `false` | 이미지 → 컷 → 러프컷 단계별 확인 (권장 `true`) |
| `STORYBOARD_SHOT_SECONDS` / `WIDTH` / `HEIGHT` / `FPS` | `5` / `576` / `960` / `24` | 장면 기본값 (프로젝트의 화면 비율에 맞게 배치) |
| `SHOT_MAX_SECONDS` | `8` | 의견으로 장면을 늘릴 때 최대 길이 |
| `CLIP_TRIM_HEAD_SEC` / `CLIP_TRIM_TAIL_SEC` | `0.15` / `0.1` | 러프컷에서 AI 컷 앞뒤를 잘라 내는 길이 |
| `VOICE_ADAPTER` / `VOICE_FALLBACK` | `edge` / `supertonic` | 내레이션 TTS 엔진 / 실패 시 대신 쓸 엔진 |
| `NARRATION_MAX_SPEEDUP` | `1.4` | 장면보다 긴 문장을 이만큼까지 빠르게 |
| `BGM_AUTO` / `MUSIC_MIN_SECONDS` | `true` / `60` | 배경음악 자동 / 곡을 이 길이 이상으로 만들어 고른 구간을 씀 |
| `TRANSLATE_ENABLED` | `true` | 장면 묘사 한국어 번역 표시 |
| `REFINE_LLAMA_SERVER` / `REFINE_LLAMA_MODEL` | (비움) | 장면 다듬기용 큰 모델 서버·모델 파일. 비우면 Gemma4 e2b로 |
| `SIMULATION_MODE` | `true` | 댓글 시뮬레이션 (`false`면 YouTube Data API) |
| `REQUIRE_VIDEO_APPROVAL` | `true` | 업로드 전 사람 확인 |
| `COMMERCIAL_BUDGET_USD` | `30` | 세션당 상용 API 예산 상한 |
| `ANTHROPIC_API_KEY` / `RUNWAY_API_KEY` / `YOUTUBE_API_KEY` | (없어도 동작) | 선택 기능용 키 |
| `YOUTUBE_CLIENT_SECRETS` / `YOUTUBE_PRIVACY` | (없어도 동작) / `private` | 업로드용 OAuth 클라이언트 JSON 경로 / 공개 범위 기본값 |
| `DEMO_REPLAY_SEC_PER_SHOT` | `1.5` | 데모 재생에서 장면마다 '만드는 중'을 보여 주는 시간 |

> `.env`·API 키·`instance/youtube_token.json`은 절대 커밋하지 않습니다.

## 13. 오픈소스 vs 팀 직접 구현

| 오픈소스 활용 | 팀 직접 설계·구현 |
|---------------|-------------------|
| `SentenceTransformer` 문장 임베딩 | prototype 기반 다국어 분류기, 6축 판단 로직 |
| `AgglomerativeClustering` 계층적 군집화 | 동적 프로토타입 업데이트(온라인 학습) |
| `HDBSCAN` 밀도 기반 군집화 | 멀티레이블 토픽 태깅, 2단계 클러스터링 |
| `transformers` zero-shot NLI | 카테고리별 적응형 임계값, 대형 클러스터 서브클러스터링 |
| `difflib.unified_diff` 프롬프트 diff | 품질 가중 대표 텍스트, 응집도 점수, 추출 요약 |
| ComfyUI · LTX-2.5 · Z-Image · Gemma · MiniMax Music · llama.cpp | 5축 우선순위 + 재발 부스트 + 상반 의견 감지 |
| ffmpeg (xfade·overlay·sidechaincompress) | 상태 머신·작업 큐·부분 재생성·반영 위치 판단·편집 설정 |
| edge-tts · Supertonic | 내레이션 길이 맞추기, 의견 → 장면 길이, 배경음악 고른 구간 고르기 |
| pypdfium2 · Pillow | 완성 영상 컷 감지, 콘티 엑셀 불러오기, 타임라인, YouTube 자막 업로드, 상용 API 비용 게이트 |

## 14. 검증

```powershell
.\venv\Scripts\python run_check.py
```

- pytest가 아니라 **`run_check.py` 하나가 전체 검증**입니다. 임시 DB + 작업 큐 동기 실행 + mock 어댑터 + 시뮬레이션으로
  2차 버전까지 돌리고, 가짜 ComfyUI(`tests/fake_comfyui.py`)·가짜 Google 서버로 영상·업로드 흐름을 확인합니다.
  실제 ffmpeg로 러프컷·로고·자막·배경음악을 합성해 픽셀·소리 크기까지 봅니다. 약 270개 항목, 몇 분 걸립니다.
- 끝에 "모든 검증 통과" 또는 "실패 N건: …"(종료 코드 1)을 출력합니다. 실제 `instance/pipeline.db`는 건드리지 않습니다.
- `/dev/pipeline-test` — `[TEST]` 세션을 만들어 영상 생성부터 2차 부분 재생성까지 단계별로 실행하고 기대 동작을 자동 판정
  (상용 API는 호출하지 않음).

## 15. 시연용 데모 재생

시연 영상을 찍을 때 장면 생성(GPU로 수십 분)과 업로드를 기다리지 않도록, 프로젝트에 **준비된 결과**를 지정할 수 있습니다
(`PipelineSession.demo_json`). 그 차수에서 '제작 시작'을 누르면 "장면 n/N 만드는 중"을 몇 초 보여 준 뒤 준비된 버전의
장면·올릴 영상·자막·소리 설정을 그대로 쓰고, 'YouTube에 올리기'는 이미 올려 둔 영상(실제 댓글이 달린)에 연결합니다.
댓글 수집·분류·군집화·승인·반영은 실제로 처리합니다.

```powershell
.\venv\Scripts\python demo_reset.py      # 데모 프로젝트를 ① 기획 단계로 되돌림 (리허설 뒤)
```

## 16. 알려진 한계

- 장면 간 인물·대상 일관성은 샷 묘사에 외모를 반복해 적는 것에 의존
- 배경음악은 모델이 곡 구조를 정하므로 고른 구간을 골라도 작아지는 곳이 남을 수 있음 — "다른 곡으로 바꾸기"로 다시 뽑음
- Runway 어댑터는 테스트 더블로만 검증했고 실제 API 호출은 검증하지 않음
- Google 감사를 받지 않은 프로젝트로 올린 영상은 YouTube가 비공개로 고정할 수 있음. 자막 업로드에는 `youtube.force-ssl` 권한이 필요해
  그 전에 연결한 계정은 다시 연결해야 함
- 컬럼 제거·타입 변경 시에는 `instance/pipeline.db`를 삭제하고 다시 만들어야 함

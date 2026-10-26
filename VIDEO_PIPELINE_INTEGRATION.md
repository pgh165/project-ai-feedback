# VIDEO_PIPELINE_INTEGRATION.md — 로컬 영상 생성 + 피드백 루프 통합 설계

> 대상: `CLAUDE.md`의 **AI 영상 피드백 자동 개선 시스템**(Flask)에
> `README.md`의 **ComfyUI 초안 생성기**를 영상 생성 단계로 접목한다.
> 결과적으로 한 라운드가 아래 9단계 파이프라인으로 끝까지 자동 진행되고, 화면에 진행 상황이 실시간으로 표시된다.
>
> **생성 전략**: 기본은 **로컬 LTX-2.5 (ComfyUI)**. 생성 결과의 품질이 부족한 장면만 **상용 API로 다시 생성**한다.
>
> 이 문서는 Claude Code에 그대로 넘겨 단계별로 구현시키는 것을 전제로 작성했다. 기존 `CLAUDE.md`의 규칙(하드코딩 금지, 폴백 체인, `[오픈소스]`/`[팀 직접 구현]` 주석, 보안 규칙)은 전부 그대로 유지한다.

---

## 1. 목표

```
① 프롬프트 → ② 생성 중 → ③ 영상 완성 → ④ 업로드 → ⑤ 피드백 수집
→ ⑥ 분류 → ⑦ 클러스터링 → ⑧ 검토 대기 → ⑨ 반영 완료 → (다음 라운드 ①)
```

1. `VIDEO_GENERATOR_ADAPTER=comfyui`로 설정하면 mock 대신 **로컬 ComfyUI + LTX-2.5로 실제 영상**을 만든다.
1-1. 품질이 부족한 장면은 사용자가 선택하면 **상용 API(기본 runway)로 그 장면만 다시 생성**한다. 로컬 결과와 API 결과를 한 영상 안에 섞어 쓸 수 있다 (6-1절).
2. 영상 생성(수 분 소요)은 **백그라운드 작업**으로 돌리고, 웹 요청은 즉시 반환한다.
3. 라운드 상세 화면 상단에 **9단계 스테퍼**(첨부 이미지 형태)를 두고, 상태 API를 폴링해 실시간 갱신한다.
4. 프롬프트를 단일 문자열이 아닌 **장면 단위 스토리보드**로 관리해, 피드백이 지적한 장면만 다시 생성할 수 있게 한다.
5. ComfyUI가 꺼져 있거나 실패하면 기존처럼 **mock으로 폴백**해 파이프라인이 멈추지 않는다.

## 2. 반드시 지킬 규칙

1. 기존 기능(mock / runway 어댑터, 분류·클러스터링·승인·롤백)을 깨지 않는다. 작업 후 `run_check.py`가 모두 통과해야 한다.
2. ComfyUI 설치 폴더, 커스텀 노드, 모델 파일, `extra_model_paths.yaml`은 수정·삭제하지 않는다.
3. 워크플로 노드 그래프를 지어내지 않는다. `promo_draft/workflows/`의 **Export (API) JSON + `mapping.yaml`**만 템플릿으로 쓴다.
4. 서버 주소, 경로, 해상도, 프레임 수 등은 전부 `config.py` + `.env`로 주입한다.
5. GPU 작업은 **동시에 하나만** 실행한다 (16GB VRAM).
6. 1GB 이상 다운로드나 새 패키지 설치가 필요하면 먼저 사용자에게 묻는다. 설치는 D드라이브 정책(`env.ps1`)을 따른다.
7. 새로 만든 코드에는 기존 규칙대로 `[오픈소스]` / `[팀 직접 구현]` 주석을 단다.

## 3. 전체 구조

```
┌──────────────── Flask 앱 (피드백 시스템) ────────────────┐
│ routes/session.py ── 상태 API ──► templates/…/detail.html │
│        │                             (9단계 스테퍼, JS 폴링) │
│        ▼                                                  │
│ pipeline/orchestrator.py  ← 상태 전이 단일 진입점 (신규)    │
│        │                                                  │
│        ├─► video_generator/factory.py                     │
│        │       ├─ mock_adapter / runway_adapter (기존)     │
│        │       └─ comfyui_adapter.py (신규)                │
│        │              └─ comfyui/ (promo_draft 모듈 이식)   │
│        │                    comfy_client · storyboard ·    │
│        │                    rough_cut                      │
│        ├─► youtube_client (업로드·댓글, 시뮬레이션 가능)    │
│        ├─► classifier / clusterer / approval (기존)        │
│        └─► prompt_updater → 스토리보드 갱신                │
│                                                           │
│ pipeline/jobs.py  ← GPU 작업 큐 + 워커 스레드 1개 (신규)   │
└───────────────────────────┬───────────────────────────────┘
                            │ HTTP (/prompt, /history, /view, /upload/image, /free)
                            ▼
                ComfyUI (http://127.0.0.1:8188, 별도 실행)
                LTX-2.5 증류 (16GB용 양자화, 3-1절)

        품질 부족 장면만 ──► 상용 API 어댑터 (runway, 이후 kling·veo 추가 가능)
```

### 3-1. LTX-2.5 로컬 구동 조건 (RTX 5070 Ti 16GB / RAM 48GB)
- 공식 체크포인트는 그대로 16GB에 들어가지 않는다. 공식 NVFP4 증류 트랜스포머(약 18.7GB)와 INT8 Gemma 4 인코더(약 15.4GB)를 합치면 약 34GB다.
- 아래 두 구성 중 하나를 쓴다. 어느 쪽이든 **ComfyUI에서 먼저 직접 돌려 보고 정상 동작하는 워크플로를 Export (API)로 내보낸다.**
  1. **GGUF 구성(16GB 권장 출발점)**: 커뮤니티 GGUF 증류 트랜스포머 `Q4_K_M`(약 15.7GB) 또는 `Q3_K_M`(약 12.9GB) + Gemma 4 인코더 GGUF `Q5_K_M`(약 9.5GB). ComfyUI-GGUF 커스텀 노드가 필요하다. 비공식 양자화라 품질 손실이 있을 수 있다.
  2. **공식 NVFP4 + CPU 오프로딩**: Blackwell(50번대)에서 NVFP4 가속을 받지만, 16GB에선 오프로딩 때문에 크게 느려진다. 시스템 RAM 48GB가 오프로딩 여유를 준다.
- 공식 가중치는 Hugging Face 로그인·약관 동의가 필요하다. 연 매출 1,000만 달러 미만은 LTX-2.x Community License로 무료 사용 가능.
- 증류 모델은 8스텝 고정, CFG=1. 프레임 수는 8n+1, 너비·높이는 32의 배수(이 도구는 `size_multiple`=64로 더 엄격하게 보정).
- LTX-2.5의 자동 길이(duration head)는 **쓰지 않는다**. 피드백 타임스탬프 → 장면 매핑을 위해 장면 길이를 스토리보드에서 명시적으로 정한다.
- VRAM 수치는 커뮤니티 보고 기준이라 실제로 돌려 보고 해상도·프레임 상한을 정한다 (14절 4단계).

### promo_draft 코드 재사용 방식
- **이식(권장)**: `promo_draft/src/`의 `comfy_client.py`, `storyboard.py`, `rough_cut.py`를 `app/pipeline/video_generator/comfyui/`로 복사해 패키지로 쓴다. 두 프로젝트의 가상환경이 달라도 문제없고, Flask 앱 안에서 진행률 콜백을 직접 받을 수 있다.
- 워크플로 JSON과 `mapping.yaml`은 **복사하지 않고** `promo_draft/workflows/`를 경로로 공유한다 (`COMFYUI_WORKFLOWS_DIR`). 워크플로를 한 곳에서만 관리하기 위해서다.
- promo_draft CLI(`run.py`)는 그대로 둔다. 단독 초안 작업용으로 계속 쓴다.

## 4. 파이프라인 상태 머신 (7 → 9 상태)

이미지의 9단계와 1:1로 맞추기 위해 **진행 중 상태 2개(`generating`, `clustering`)**와 실패 상태 1개를 추가한다.

| 단계 | 화면 라벨 | 상태값 | 성격 | 진입 조건 |
|---|---|---|---|---|
| ① | 프롬프트 | `prompt_built` | 완료 | 스토리보드(프롬프트) 생성/갱신 완료 |
| ② | 생성 중 | `generating` **(신규)** | 진행 중 | GPU 작업 큐에 장면 제출 |
| ③ | 영상 완성 | `video_ready` | 완료 / 확인 대기 | 모든 장면 생성 + 러프컷 합성 완료. `REQUIRE_VIDEO_APPROVAL=true`면 사람이 확인(또는 상용 API 재생성 요청)할 때까지 대기 |
| ④ | 업로드 | `uploaded` | 완료 | YouTube 업로드(또는 시뮬레이션) 완료 |
| ⑤ | 피드백 수집 | `feedback_collected` | 완료 | 댓글 수집 완료 |
| ⑥ | 분류 | `feedback_classified` | 완료 | 6축 분류 완료 |
| ⑦ | 클러스터링 | `clustering` **(신규)** | 진행 중 | 클러스터링 실행 중 |
| ⑧ | 검토 대기 | `pending_review` | 대기 | 클러스터 저장, 사용자 승인 대기 |
| ⑨ | 반영 완료 | `prompt_updated` | 완료 | 승인 반영, 다음 라운드 생성 |
| — | (② 위에 빨간 표시) | `generation_failed` **(신규)** | 실패 | 일부/전체 장면 실패, 재시도 대기 |

```
prompt_built ─► generating ─► video_ready ─► uploaded ─► feedback_collected
                   │  ▲  ▲          │
                   ▼  │  └──────────┘ (품질 부족 장면 상용 API 재생성)
           generation_failed
             (재시도)

feedback_collected ─► feedback_classified ─► clustering ─► pending_review ─► prompt_updated
```

- 상태 전이는 **`pipeline/orchestrator.py`의 `advance(round_id, to_state)` 한 곳에서만** 수행하고, 허용되지 않은 전이는 예외를 낸다. (지금처럼 라우트마다 상태를 직접 바꾸지 않는다.)
- 상태 컬럼은 문자열이라 마이그레이션 없이 새 값을 쓸 수 있다. 기존 세션의 라운드는 그대로 표시된다.
- `generation_failed`에서는 **실패한 장면만** 재시도한다. 성공한 장면은 다시 만들지 않는다.

## 5. 프롬프트 → 스토리보드

영상은 장면 여러 개로 구성되므로, `PromptVersion`의 내용을 **스토리보드 JSON**으로 확장한다. 기존 텍스트 프롬프트는 `style` 필드로 흡수해 하위 호환을 유지한다.

```json
{
  "project": "dept-promo",
  "style": "Cinematic, realistic, warm lab lighting, consistent color grade",
  "negative": "text, letters, watermark, logo, distorted hands, extra fingers, blurry, flickering",
  "defaults": {"workflow": "ltx25_i2v", "width": 960, "height": 576, "fps": 24, "seed": 42,
               "generator": "local"},
  "shots": [
    {"id": "s01", "duration": 5, "workflow": "ltx25_t2v",
     "prompt": "A dark screen slowly comes alive as glowing circuit traces spread outward..."},
    {"id": "s03", "duration": 6, "workflow": "ltx25_i2v", "image": "assets/photos/lab_board.jpg",
     "prompt": "Close-up of a green microcontroller board, LEDs light up one by one...",
     "generator": "api"}
  ]
}
```
- `generator`: `local`(ComfyUI LTX-2.5) / `api`(상용). 장면별로 지정하며, 기본값은 `defaults.generator`. 품질 부족으로 API 재생성한 장면은 이후 라운드에서도 `api`로 유지한다 (사용자가 되돌릴 수 있음).

- **시작·끝 시간**은 `duration` 누적으로 계산한다 (저장하지 않음). 피드백 타임스탬프 → 장면 매핑에 쓴다.
- 생성 시 각 장면의 최종 프롬프트 = `shot.prompt + ", " + style`.
- 프레임 수는 `duration × fps`를 8n+1로 올림, 해상도는 `size_multiple`(config, 기본 64)의 배수로 보정한다. (promo_draft의 `storyboard.py` 검증 로직 재사용)
- `prompt_builder.py`: 세션 생성 시 Claude API로 스토리보드 JSON을 만든다. API 키가 없으면 **사용자가 올린 `storyboard.yaml`** 또는 템플릿 스토리보드로 폴백한다.
- `prompt_updater.py`: 승인된 클러스터를 받아 **영향받는 장면의 `prompt`만** 수정한 새 스토리보드를 만든다. diff는 기존처럼 `unified_diff`로 저장하되, JSON을 정렬·들여쓰기한 텍스트 기준으로 만든다 (장면별 변경이 한눈에 보이도록).

### 피드백 카테고리 → 반영 위치

| 클러스터 카테고리 | 반영 위치 | 예시 |
|---|---|---|
| `visual` | 해당 장면 `prompt` (조명·색감·구도) | "2:10쯤 너무 어두워요" → s07에 brighter lighting |
| `pacing` | 해당 장면 `duration` 또는 장면 분할 | "중반 지루함" → s05~s06 길이 단축 |
| `content` / `story` | 장면 `prompt` 또는 장면 추가·삭제 | "로봇 장면 더 보고 싶어요" |
| `audio` | LTX 오디오 프롬프트 구절 / 후처리 볼륨 | "배경음 커요" → 러프컷 합성 시 음량 조정 |
| `subtitle` | **영상 생성이 아닌 ffmpeg 후처리** | 자막 위치·크기 |
| `technical` | `GlobalTechnicalSetting` → 스토리보드 `defaults` | 해상도, fps, 끊김 |

## 6. ComfyUI 어댑터

### `video_generator/comfyui_adapter.py` `[팀 직접 구현]`
`BaseVideoGeneratorAdapter`를 구현한다.

```python
class ComfyUIAdapter(BaseVideoGeneratorAdapter):
    def is_available(self) -> bool: ...          # GET /system_stats 응답 확인
    def generate(self, round_id, storyboard, shot_ids=None, progress_cb=None) -> VideoGenerationResult: ...
    def regenerate_shots(self, round_id, shot_ids, progress_cb=None) -> VideoGenerationResult: ...
```

`generate()` 흐름:
1. 스토리보드 검증·보정 (해상도, 8n+1, 입력 파일 존재)
2. 장면마다: 입력 이미지·제어 영상 업로드(`/upload/image`) → 템플릿 + `mapping.yaml`로 값 주입 → `/prompt` 제출 → `/history/{id}` 폴링 → `/view`로 결과 다운로드
3. 장면 결과를 `outputs/session_<id>/round_<n>/<shot_id>/v<k>.mp4`에 저장하고 `ShotRender` 행 기록
4. 모든 장면 성공 시 러프컷(`rough_cut.mp4`) 합성 → `VideoRound.video_path`에 저장
5. 장면 간 필요하면 `/free`로 VRAM 정리
6. 진행률: `(완료 장면 + 현재 장면 진행 비율) / 전체 장면`을 `progress_cb`로 전달

### `factory.py` 변경
```
VIDEO_GENERATOR_ADAPTER=comfyui → ComfyUIAdapter (generator=local 장면)
  ├─ is_available() False (서버 꺼짐)  → mock 폴백 + 경고 로그 + 화면에 "mock으로 대체됨" 배지
  └─ True → 사용

COMMERCIAL_VIDEO_ADAPTER=runway → RunwayAdapter (generator=api 장면)
  └─ API 키 없음 → 상용 재생성 버튼 비활성화 + 안내 문구 (mock으로 몰래 대체하지 않음)
```
- `create_adapter_for_shot(shot)`: 장면의 `generator` 값으로 로컬/상용 어댑터를 고른다.
- 상용 어댑터는 기존 `runway_adapter.py`를 장면 단위 호출(`generate_shot()`)이 가능하도록 확장한다. `kling`·`veo`는 같은 인터페이스로 나중에 추가할 수 있게만 열어두고 이번 범위에서는 구현하지 않는다.

### 폴백 체인 (CLAUDE.md에 추가)
```
영상 생성(장면별):
  generator=local → comfyui (LTX-2.5, 로컬 ComfyUI 응답 시) → mock
  generator=api   → 상용 API (COMMERCIAL_VIDEO_ADAPTER, 키 있을 때) → 재생성 불가 표시 (자동 폴백 없음)
```

## 6-1. 품질 부족 장면 → 상용 API 재생성 `[팀 직접 구현]`

로컬 LTX-2.5로 먼저 만들고, **사람이 보고 품질이 부족하다고 판단한 장면만** 상용 API로 다시 만든다. 비용이 드는 작업이라 자동으로 API를 호출하지 않는다.

### 흐름
1. ③ 영상 완성 후 `REQUIRE_VIDEO_APPROVAL=true`(기본)이면 바로 업로드하지 않고 ③을 **확인 대기**(주황)로 표시한다.
2. 사용자는 장면별 썸네일·플레이어를 보고 품질이 부족한 장면을 체크한다. 체크 사유(형태 붕괴, 손·얼굴 뭉개짐, 깜빡임, 프롬프트 불일치, 기타)를 고른다.
3. "선택 장면 상용 API로 재생성"을 누르면 **예상 비용**(장면 길이 × `COMMERCIAL_COST_PER_SEC_USD`)과 이번 세션 누적 비용·예산 잔액을 보여주고 확인을 받는다.
4. 확인하면 라운드를 ② `generating`으로 되돌려 해당 장면만 상용 API로 생성한다. 나머지 장면은 로컬 결과를 그대로 쓴다(`reused`).
5. 완료 후 러프컷을 다시 합성하고 ③으로 돌아온다. 문제없으면 "업로드 진행"으로 ④로 넘어간다.

### 규칙
- **예산 상한**: `COMMERCIAL_BUDGET_USD`를 넘는 요청은 제출하지 않는다. 비용은 `ShotRender.cost_usd`에 기록하고 세션 단위로 합산한다.
- 상용 API 결과는 로컬 결과와 해상도·fps가 다를 수 있으므로, 러프컷 합성 시 스토리보드 `defaults` 해상도·fps로 맞춘다(ffmpeg 스케일·fps 변환).
- 같은 장면의 로컬 버전과 API 버전은 모두 남기고, 화면에서 어느 버전을 쓸지 고를 수 있다.
- 체크 사유는 `ShotRender.quality_flags_json`에 저장해, 발표 자료용으로 "로컬 대비 API 전환 비율·사유"를 집계한다 (`metrics.py`).

### 피드백과의 연결 (선택 기능)
- 같은 장면에 `visual`·`technical` 카테고리 클러스터가 **2라운드 연속 재발**하면(기존 재발 추적 로직 사용), 그 장면에 "상용 API 재생성 권장" 배지를 띄운다. 실제 호출은 여전히 사용자가 누를 때만 한다.

## 7. 백그라운드 작업 (GPU 큐) `[팀 직접 구현]`

Celery·Redis 없이 **SQLite 작업 테이블 + 워커 스레드 1개**로 구현한다 (외부 의존성 추가 없음).

- `pipeline/jobs.py`
  - `enqueue(kind, round_id, payload)` → `GenerationJob` 행 생성 (`queued`)
  - 워커 스레드가 `queued` 작업을 하나씩 꺼내 실행 → `running` → `done` / `failed`
  - 작업 종류: `generate_video`, `regenerate_shots`, `cluster` (클러스터링도 수십 초 걸리므로 같은 큐에서 비동기 처리)
- 워커는 `create_app()`에서 한 번만 시작한다. Flask 디버그 리로더로 두 번 뜨지 않도록 `WERKZEUG_RUN_MAIN` 체크를 넣는다.
- 앱 재시작 시 `running`으로 남은 작업은 `failed(interrupted)`로 바꾸고, 화면에서 재시도할 수 있게 한다.
- 작업 실행 중에는 `app.app_context()` 안에서 DB에 접근한다.

## 8. DB 변경

### `VideoRound` 신규 컬럼 (`_migrate_existing_tables()`에 추가)
| 컬럼 | 타입 | 설명 |
|---|---|---|
| `storyboard_json` | TEXT | 이 라운드 생성에 쓴 스토리보드 스냅샷 |
| `video_path` | TEXT | 러프컷 경로 |
| `generation_progress` | FLOAT | 0~1, 스테퍼 ② 진행률 |
| `generation_detail` | TEXT | "장면 3/10 생성 중" 같은 표시 문구 |
| `generation_error` | TEXT | 실패 사유 |
| `adapter_used` | TEXT | `comfyui` / `mixed` / `runway` / `mock` (폴백·혼합 여부 표시용) |
| `video_approved` | BOOLEAN | ③ 확인 완료 여부 (`REQUIRE_VIDEO_APPROVAL`용) |

### 신규 테이블 `ShotRender`
| 컬럼 | 타입 | 설명 |
|---|---|---|
| `id` | INTEGER PK | |
| `round_id` | FK → VideoRound | |
| `shot_id` | TEXT | `s01` … |
| `version` | INTEGER | 같은 장면 재생성 시 증가 |
| `status` | TEXT | `queued` / `running` / `done` / `failed` / `reused` |
| `generator` | TEXT | `local` / `api` |
| `provider` | TEXT | `comfyui-ltx25` / `runway` / `mock` … |
| `cost_usd` | FLOAT | 상용 API 비용 (로컬은 0) |
| `quality_flags_json` | TEXT | 품질 부족 체크 사유 목록 |
| `selected` | BOOLEAN | 러프컷에 쓸 버전 |
| `workflow` / `prompt` / `seed` | TEXT / TEXT / INTEGER | 재현용 |
| `width` / `height` / `frames` / `fps` | INTEGER | |
| `path` | TEXT | 결과 mp4 |
| `elapsed_sec` / `peak_vram_mb` | FLOAT | 지표·발표용 |
| `reused_from_id` | FK → ShotRender, nullable | 이전 라운드 결과를 재사용한 경우 |

### 신규 테이블 `GenerationJob`
`id, kind, round_id, payload_json, status, error, created_at, started_at, finished_at`

## 9. 장면 단위 부분 재생성 `[팀 직접 구현]` (핵심 차별점)

피드백이 지적한 장면만 다시 만들고, 나머지는 이전 라운드 결과를 재사용한다.

1. **타임스탬프 추출**: 댓글에서 `2:10`, `2분 10초`, `1:05~1:20` 같은 표현을 정규식으로 뽑아 `Feedback.timestamp_sec`(신규 컬럼, nullable)에 저장한다. 분류기의 구체성 신호와 같은 위치(`embedding_classifier.py`)에 둔다.
2. **장면 매핑**: 스토리보드의 누적 시작·끝 시간으로 `timestamp_sec → shot_id`를 찾는다. 클러스터 단위로 `affected_shots`(멤버 피드백 장면들의 집합)를 계산해 `ClusterResult.affected_shots_json`(신규)에 저장한다.
3. **타임스탬프가 없는 클러스터**: `technical`·전체 톤 피드백은 전 장면 재생성, 그 외는 `prompt_updater`가 Claude API로 해당 장면을 추정하게 하고, 실패하면 전 장면 재생성.
4. **다음 라운드 생성 시**: 스토리보드 diff에서 바뀐 장면만 `regenerate_shots`로 큐에 넣고, 바뀌지 않은 장면은 `ShotRender(status=reused, reused_from_id=…)`로 이전 파일을 연결한다.
5. **지표 추가** (`metrics.py`): 라운드별 재생성 장면 비율, 전체 재생성 대비 절감 시간(이전 라운드 장면 생성 시간 합 기준).

## 10. 상태 API

`GET /api/rounds/<round_id>/status`

```json
{
  "round": 1,
  "state": "generating",
  "step": 2,
  "steps": [
    {"no": 1, "key": "prompt_built",        "label": "프롬프트",   "status": "done"},
    {"no": 2, "key": "generating",          "label": "생성 중",    "status": "active",
     "progress": 0.42, "detail": "장면 4/10 생성 중"},
    {"no": 3, "key": "video_ready",         "label": "영상 완성",  "status": "pending"},
    {"no": 4, "key": "uploaded",            "label": "업로드",     "status": "pending"},
    {"no": 5, "key": "feedback_collected",  "label": "피드백 수집", "status": "pending"},
    {"no": 6, "key": "feedback_classified", "label": "분류",       "status": "pending"},
    {"no": 7, "key": "clustering",          "label": "클러스터링", "status": "pending"},
    {"no": 8, "key": "pending_review",      "label": "검토 대기",  "status": "pending"},
    {"no": 9, "key": "prompt_updated",      "label": "반영 완료",  "status": "pending"}
  ],
  "adapter_used": "comfyui",
  "shots": [{"id": "s01", "status": "done"}, {"id": "s04", "status": "running"}],
  "error": null
}
```

- `status` 값: `done` / `active` / `waiting`(③ 영상 확인 대기, ⑧ 사용자 승인 대기) / `failed` / `pending`
- `shots` 항목에 `generator`, `provider`, `cost_usd`, `recommend_api`(재발 배지) 필드를 포함하고, 응답 최상위에 `commercial_spent_usd`, `commercial_budget_usd`를 넣는다.
- 계산 규칙: 현재 단계보다 앞은 `done`, 현재 단계는 성격에 따라 `active`·`waiting`·`failed`, 뒤는 `pending`.
- `POST /api/rounds/<round_id>/retry` — `generation_failed`에서 실패 장면만 재시도
- `POST /api/rounds/<round_id>/advance` — 수동 진행 버튼(시뮬레이션 모드에서 업로드·수집 단계를 넘길 때)
- `POST /api/rounds/<round_id>/escalate` — 본문 `{"shot_ids": ["s03"], "flags": {"s03": ["hands"]}, "confirm_cost_usd": 0.72}` → 선택 장면 상용 API 재생성. 서버가 다시 계산한 비용과 `confirm_cost_usd`가 다르거나 예산 초과면 409 반환
- `POST /api/rounds/<round_id>/approve-video` — ③ 확인 완료, ④ 업로드로 진행
- `POST /api/shots/<render_id>/select` — 러프컷에 쓸 버전 선택(로컬/API)

## 11. UI — 9단계 스테퍼 (첨부 이미지 기준)

위치: `templates/session/detail.html` 상단, 라운드 카드 헤더 `⚡ 라운드 N — 파이프라인`.
구현: Bootstrap 5 + 바닐라 JS (`static/js/pipeline_stepper.js` 신규), 2초 간격 폴링. `done`/`pending`만 남거나 `waiting`이면 10초로 늦추고, `prompt_updated`면 중지.

| 상태 | 원 | 라벨 | 추가 표시 |
|---|---|---|---|
| `done` | 보라색 채움 + ✓ | 밝은 보라 굵게 | 클릭 시 결과 보기 |
| `active` | 보라 테두리 + 회전 링 애니메이션, 번호 | 흰색 | 원 아래 진행률 바 + `detail` 문구 |
| `waiting` | 주황 테두리 + 번호 | 주황 | ③: "영상 확인" 패널 열기 / ⑧: "검토하기" 버튼 → `review.html` |
| `failed` | 빨강 채움 + `!` | 빨강 | 오류 툴팁 + "실패 장면 재시도" 버튼 |
| `pending` | 진회색 채움 + 회색 번호 | 회색 | — |

- 단계 사이 `›` 화살표. 완료된 구간 화살표는 보라색.
- 좁은 화면에서는 가로 스크롤(`overflow-x: auto`), 원 크기 축소.
- 완료 단계 클릭 시 패널:
  - ① 스토리보드 장면 목록 (이전 버전과 diff 링크)
  - ③ 러프컷 `<video>` 플레이어 + 장면별 썸네일·버전 선택
    - 장면 카드마다 `로컬`/`API` 배지, 품질 부족 체크박스와 사유 선택, "권장" 배지(재발)
    - 하단: "선택 장면 상용 API로 재생성 (예상 $x.xx · 남은 예산 $y.yy)" / "업로드 진행" 버튼
  - ⑤ 수집 댓글 수, ⑥ 건설적 비율, ⑦ 클러스터 수·응집도, ⑨ 반영된 변경 요약
- `adapter_used`가 `mock`이면 ② 아래에 "mock으로 대체됨" 배지 표시.
- 색상은 기존 `style.css` 다크 테마 변수로 정의하고 하드코딩하지 않는다.

## 12. 설정 (`config.py` / `.env`)

| 변수 | 권장값 | 설명 |
|---|---|---|
| `VIDEO_GENERATOR_ADAPTER` | `comfyui` | `mock` / `runway` / `comfyui` |
| `COMFYUI_URL` | `http://127.0.0.1:8188` | ComfyUI 서버 |
| `COMFYUI_WORKFLOWS_DIR` | `D:\capstone\ai\promo_draft\workflows` | Export (API) JSON 위치 (공유) |
| `COMFYUI_MAPPING_PATH` | `…\workflows\mapping.yaml` | 노드 매핑 |
| `VIDEO_OUTPUT_DIR` | `D:\capstone\ai\feedback_outputs` | 라운드별 영상 저장 (D드라이브) |
| `FFMPEG_PATH` | `D:\capstone\ai\tools\ffmpeg\bin\ffmpeg.exe` | 러프컷·자막 후처리 |
| `COMFYUI_POLL_INTERVAL_SEC` | `2` | 히스토리 폴링 간격 |
| `COMFYUI_SHOT_TIMEOUT_SEC` | `900` | 장면당 최대 대기 |
| `COMFYUI_SIZE_MULTIPLE` | `64` | 해상도 보정 배수 (promo_draft와 동일) |
| `COMFYUI_TEST_MODE` | `false` | `true`면 512x320·49프레임으로 빠르게 시험 |
| `COMFYUI_FREE_BETWEEN_SHOTS` | `true` | 장면 사이 `/free` 호출 |
| `PARTIAL_REGEN_ENABLED` | `true` | 장면 단위 부분 재생성 |
| `LTX_MODEL_VARIANT` | `ltx25-gguf-q4km` | 기록용 모델 식별자 (`ShotRender.provider`, meta.json). 실제 모델 선택은 워크플로 JSON이 결정 |
| `REQUIRE_VIDEO_APPROVAL` | `true` | ③에서 사람이 확인한 뒤 업로드 |
| `COMMERCIAL_VIDEO_ADAPTER` | `runway` | 품질 부족 장면용 상용 어댑터 |
| `RUNWAY_API_KEY` | (없어도 동작) | 없으면 상용 재생성 버튼 비활성화 |
| `COMMERCIAL_COST_PER_SEC_USD` | `0.12` | 예상 비용 계산용 (2026년 기준 Runway Gen-4.5 공개 단가, 결제 전 공식 가격 확인 필요) |
| `COMMERCIAL_BUDGET_USD` | `30` | 세션당 상용 API 예산 상한 |

## 13. 파일 변경 목록

```
app/
  models.py                       # VideoRound 컬럼, ShotRender, GenerationJob, Feedback.timestamp_sec,
                                  # ClusterResult.affected_shots_json
  __init__.py                     # 마이그레이션 추가, 워커 스레드 시작
  pipeline/
    orchestrator.py               # 신규 — 상태 전이·단계 계산·라운드 실행 흐름
    jobs.py                       # 신규 — GPU 작업 큐 + 워커
    storyboard_model.py           # 신규 — 스토리보드 스키마, 시간 계산, 장면 매핑
    video_generator/
      comfyui_adapter.py          # 신규
      comfyui/                    # 신규 — promo_draft 모듈 이식
        comfy_client.py
        storyboard.py
        rough_cut.py
      runway_adapter.py           # 장면 단위 generate_shot() 추가, 비용 기록
      factory.py                  # comfyui 분기 + 폴백, create_adapter_for_shot()
    quality_gate.py               # 신규 — 품질 부족 장면 선택·비용 계산·예산 검사·재발 권장 배지
    classifier/embedding_classifier.py   # 타임스탬프 추출 추가
    clusterer/__init__.py         # affected_shots 계산
    prompt_builder.py             # 스토리보드 JSON 생성
    prompt_updater.py             # 장면 단위 갱신
    approval.py                   # 스토리보드 diff·롤백 호환
    metrics.py                    # 부분 재생성 지표
  routes/session.py               # 상태·재시도·진행 API, 영상 파일 서빙
  templates/session/detail.html   # 스테퍼 + 결과 패널
  static/js/pipeline_stepper.js   # 신규
  static/css/style.css            # 스테퍼 스타일
config.py                         # 12절 변수
run_check.py                      # 신규 검증 항목
tests/fake_comfyui.py             # 신규 — 테스트용 가짜 ComfyUI 서버
CLAUDE.md                         # 상태 머신·폴백·어댑터 표·환경변수 갱신
```

## 14. 구현 순서 (Claude Code 작업 단계)

각 단계가 끝나면 결과를 보고하고 다음 단계로 넘어가기 전에 확인을 받는다.

**0단계 — 점검**
- 기존 `run_check.py` 실행해 현재 통과 상태 기록
- ComfyUI `/system_stats` 응답, `promo_draft/workflows/`의 JSON·`mapping.yaml` 존재 확인
- 워크플로가 **LTX-2.5용**(`ltx25_t2v.json`, `ltx25_i2v.json` 등)인지 확인. LTX-2.3용만 있으면 사용자에게 LTX-2.5 워크플로를 ComfyUI에서 돌려 보고 Export (API)로 다시 내보내 달라고 요청하고 멈춤. promo_draft의 `mapping.yaml`도 새 노드 ID로 갱신이 필요함
- 모델 파일 다운로드는 직접 하지 않는다 (사용자가 3-1절 구성 중 하나로 준비)
- 문제가 있으면 보고하고 멈춤

**1단계 — 상태 머신·DB**
- 9상태 정의, `orchestrator.advance()`, 단계 계산 함수
- 모델·마이그레이션 추가, 기존 라우트의 상태 직접 변경을 `advance()`로 교체
- mock 어댑터로 한 라운드가 9단계를 모두 지나는지 확인

**2단계 — 작업 큐**
- `jobs.py` + 워커, `generate_video`·`cluster` 작업을 mock으로 비동기 실행
- 재시작 시 중단 작업 처리

**3단계 — 상태 API + 스테퍼 UI**
- `/api/rounds/<id>/status`, 스테퍼 JS·CSS, 완료 단계 패널
- mock에 인위적 지연(`MOCK_DELAY_SEC`)을 넣어 진행 표시 확인

**4단계 — ComfyUI 어댑터**
- promo_draft 모듈 이식, `ComfyUIAdapter`, factory 폴백
- `tests/fake_comfyui.py`로 먼저 통과시킨 뒤, `COMFYUI_TEST_MODE=true`로 실제 ComfyUI(LTX-2.5)에서 장면 1개 생성
- 이어서 기본 해상도(960x576, 121프레임)로 1개 생성해 OOM 여부와 소요 시간 확인. OOM이면 해상도·프레임 상한을 낮춘 권장값을 제안
- 결과 경로, 소요 시간, VRAM 사용량 보고 후 멈춤

**4-1단계 — 상용 API 품질 에스컬레이션**
- `REQUIRE_VIDEO_APPROVAL` 흐름(③ 확인 대기), `quality_gate.py`, `/escalate`·`/approve-video`·`/select` API
- `runway_adapter`의 장면 단위 생성, 비용 기록, 예산 상한, 러프컷 해상도·fps 정규화
- API 키 없이 테스트할 수 있도록 상용 어댑터를 흉내 내는 테스트 더블을 쓴다. **실제 상용 API 호출은 사용자가 명시적으로 지시할 때만** 한다

**5단계 — 스토리보드화**
- `PromptVersion`에 스토리보드 저장, builder·updater·diff·롤백 대응
- 카테고리별 반영 위치(5절 표) 적용

**6단계 — 부분 재생성**
- 타임스탬프 추출 → 장면 매핑 → 바뀐 장면만 재생성 + 재사용 연결
- 지표 추가

**7단계 — 검증·문서**
- `run_check.py`에 추가: 9단계 전이, 잘못된 전이 거부, ComfyUI 꺼짐 시 mock 폴백, 실패 장면만 재시도, 부분 재생성 시 재사용 장면 수, 상태 API 형식, 상용 재생성 시 선택 장면만 API로 생성·나머지 재사용, 예산 초과 시 409, API 키 없을 때 버튼 비활성화
- `CLAUDE.md` 갱신 (상태 머신, 폴백 체인, 어댑터 표, 환경변수, 신규 테이블)

## 15. 완료 기준

- `VIDEO_GENERATOR_ADAPTER=comfyui` + ComfyUI(LTX-2.5) 실행 상태에서 세션을 만들면, 화면 스테퍼가 ①→② 진행률→③으로 바뀌고 러프컷이 재생된다.
- ③에서 장면 하나를 품질 부족으로 체크해 상용 API 재생성을 요청하면, 비용 확인 후 그 장면만 API 버전으로 바뀌고 러프컷이 다시 합성된다. 화면에 장면별 `로컬`/`API` 배지와 누적 비용이 보인다.
- ComfyUI를 끈 상태에서는 같은 흐름이 mock으로 끝까지 진행되고 "mock으로 대체됨"이 표시된다.
- 시뮬레이션 댓글로 ④~⑧까지 진행되고, 승인하면 ⑨ 후 라운드 2가 ①부터 시작된다.
- 라운드 2에서 타임스탬프가 있는 피드백의 장면만 다시 생성되고, 나머지는 `reused`로 표시된다.
- `run_check.py` 전 항목 통과.

## 16. 알려진 제약

- 화면 속 글자(자막·로고·인식 박스)는 생성 모델이 깨뜨리므로 ffmpeg 후처리로만 넣는다.
- 장면 경계에서 스타일이 튈 수 있다. 필요하면 다음 장면 생성 시 앞 장면 마지막 프레임을 첫 프레임 조건으로 넣는 옵션(`ltx25_flf` 워크플로)을 후속 작업으로 둔다.
- LTX-2.5는 16GB에 공식 구성이 들어가지 않아 GGUF 양자화나 오프로딩을 써야 하고, 장면당 수 분이 걸린다. 시연은 `COMFYUI_TEST_MODE` 또는 미리 생성해 둔 라운드로 진행하는 것을 권장한다.
- GGUF 양자화 품질이 기대에 못 미치면 같은 워크플로 구조로 LTX-2.3 FP8(16GB 공식 구동 사례 다수)로 내릴 수 있다. 워크플로 JSON만 바꾸면 되고 코드 변경은 없다.
- 로컬과 상용 API 장면은 색감·질감이 다를 수 있다. 스토리보드 `style`을 양쪽에 똑같이 넣고, 필요하면 편집 단계에서 색 보정으로 맞춘다.
- 상용 API 가격은 자주 바뀐다. `COMMERCIAL_COST_PER_SEC_USD`는 예상치이며 실제 청구액은 공급자 대시보드로 확인한다.
- 워커 스레드 방식은 단일 프로세스 개발 서버 기준이다. 여러 프로세스로 배포할 경우 작업 큐 구조를 다시 검토해야 한다.

# 작업: 학과 홍보영상 초안 생성기 (로컬 LTX-2.3 / Wan2.2, ComfyUI API 기반)

## 배경
- 학과 홍보영상(약 60초, 1920x1080 가로)의 **초안**을 로컬에서 만든다. 최종본은 나중에 상용 API로 다시 뽑을 예정이라, 여기서는 빠른 반복이 목표다.
- 환경: Windows 11, PowerShell, RTX 5070 Ti (16GB VRAM, Blackwell), 시스템 RAM 48GB
- ComfyUI는 로컬(기본 http://127.0.0.1:8188)에서 따로 실행한다. 이 도구는 ComfyUI의 HTTP API만 호출한다.

## 반드시 지킬 규칙
1. ComfyUI 설치 폴더, 커스텀 노드, 모델 파일은 절대 수정·삭제하지 않는다.
2. 1GB 이상 다운로드(모델 등)는 하기 전에 나에게 먼저 물어본다.
3. 워크플로 노드 그래프를 직접 지어내지 않는다. 내가 ComfyUI에서 "Export (API)"로 저장한 JSON을 `workflows/` 폴더에 넣어주면 그걸 템플릿으로 쓴다. 파일이 없으면 만들지 말고 나에게 요청한다.
4. 모델명, 경로, 서버 주소, 기본 해상도 등 설정값은 하드코딩하지 않고 `config.yaml`과 `.env`에서 읽는다.
5. GPU 작업은 한 번에 하나씩 큐에 넣는다. 장면이 끝날 때마다 필요하면 `/free` 엔드포인트로 메모리를 비운다.

## 설치 위치: 모든 것을 D드라이브에
C드라이브에는 아무것도 설치하거나 캐시를 쌓지 않는다. 기준 폴더는 `D:\ai`로 한다 (config.yaml에서 바꿀 수 있게).

### 폴더 구성
```
D:\ai\
  promo_draft\        # 이 프로젝트 코드
  venvs\promo_draft\  # 이 프로젝트 가상환경
  python\             # uv로 받는 Python 인터프리터
  cache\pip\          # pip 캐시
  cache\uv\           # uv 캐시
  cache\hf\           # Hugging Face 모델·데이터 캐시
  cache\torch\        # torch hub 캐시
  tmp\                # 임시 파일
  tools\ffmpeg\       # ffmpeg 포터블
```

### 규칙
1. 가상환경은 `D:\ai\venvs\promo_draft`에 만든다. Python이 필요하면 `uv`로 받아 `D:\ai\python`에 둔다 (`UV_PYTHON_INSTALL_DIR`). 시스템 전역(C드라이브)에 Python이나 패키지를 설치하지 않는다.
2. 패키지 설치 전에 아래 환경변수가 현재 세션에 설정돼 있는지 확인하고, 없으면 세션에만 설정한 뒤 진행한다.
   ```powershell
   $env:PIP_CACHE_DIR = "D:\ai\cache\pip"
   $env:UV_CACHE_DIR = "D:\ai\cache\uv"
   $env:UV_PYTHON_INSTALL_DIR = "D:\ai\python"
   $env:HF_HOME = "D:\ai\cache\hf"
   $env:TORCH_HOME = "D:\ai\cache\torch"
   $env:TEMP = "D:\ai\tmp"
   $env:TMP = "D:\ai\tmp"
   ```
3. 이 설정을 영구 적용(`setx` 또는 사용자 환경변수 변경)하는 것은 나에게 먼저 물어본 다음에만 한다. 대신 위 내용을 담은 `D:\ai\promo_draft\env.ps1`을 만들어 두고, README에 "작업 전에 `. .\env.ps1` 실행"이라고 안내한다.
4. ffmpeg는 포터블 버전을 `D:\ai\tools\ffmpeg`에 두고, config.yaml에 실행 파일 경로를 적어 그걸 쓴다. PATH를 영구 변경하지 않는다.
5. ComfyUI도 D드라이브에 있다고 가정한다. 경로는 config.yaml의 `comfyui_dir`로 받고, 모르면 나에게 묻는다. 모델 폴더를 따로 쓰고 싶으면 ComfyUI의 `extra_model_paths.yaml`로 연결하는 방법을 README에 안내만 하고, 직접 수정하지는 않는다.
6. 설치가 끝나면 `pip cache dir`, `python -c "import sys; print(sys.prefix)"`, HF 캐시 경로를 출력해서 전부 D드라이브인지 확인하고 결과를 보고한다.

## 0단계: 환경 점검 (코드 작성 전에 먼저 실행하고 결과를 보고)
- `nvidia-smi`로 GPU와 드라이버 확인
- ComfyUI가 쓰는 Python에서 `torch.__version__`, `torch.version.cuda`, `torch.cuda.is_available()` 확인 (CUDA 12.8 이상이어야 함)
- ComfyUI 서버 응답 확인 (`/system_stats`)
- `ffmpeg` 설치 여부 확인 (없으면 `D:\ai\tools\ffmpeg`에 포터블 설치를 제안)
- D드라이브 남은 용량 확인 (모델과 출력 영상 포함 최소 100GB 여유 권장, 부족하면 보고하고 멈춤)
- 문제가 있으면 해결 방법만 제안하고 멈춘다.

## 1단계: 워크플로 템플릿 연결
- `workflows/`에 있는 API 형식 JSON을 읽고, 각 워크플로에서 바꿔야 할 입력 노드를 찾아 `workflows/mapping.yaml`로 정리한다.
  - 대상 필드: 긍정/부정 프롬프트, 시드, 너비, 높이, 프레임 수, fps, 첫 프레임 이미지, 마지막 프레임 이미지, 제어 영상(포즈/깊이)
- 노드 ID를 확신할 수 없으면 후보를 보여주고 나에게 확인받는다.
- 예상 워크플로 종류 (내가 넣어주는 것만 지원하면 됨):
  - `ltx23_t2v.json`, `ltx23_i2v.json`, `ltx23_flf.json` (첫·마지막 프레임), `ltx23_iclora_control.json` (포즈·깊이 제어)
  - `wan22_5b_ti2v.json` (선택)

## 2단계: 도구 구현
### 입력: `storyboard.yaml`
장면별로 다음 필드를 받는다.
```yaml
project: dept-promo-draft
defaults:
  workflow: ltx23_i2v
  width: 960
  height: 544
  frames: 121
  fps: 24
  seed: 42
shots:
  - id: s01
    workflow: ltx23_t2v
    prompt: "Wide shot of a modern university electronics lab at dusk, rows of workbenches with oscilloscopes, slow dolly in, soft warm lighting"
  - id: s02
    workflow: ltx23_i2v
    image: assets/photos/lab_board.jpg
    prompt: "Close-up of an embedded development board, status LEDs blinking, shallow depth of field, slow push in"
  - id: s03
    workflow: ltx23_iclora_control
    control_video: assets/blender/s03_pose.mp4
    image: assets/ref/student_ref.png
    prompt: "A student walks across the lab carrying a circuit board, natural lighting"
```

### 기능
1. **검증**: 너비·높이는 32의 배수, 프레임 수는 8n+1인지 확인한다. 아니면 가장 가까운 유효값으로 보정하고 경고를 출력한다. 입력 파일 경로가 실제로 있는지도 확인한다.
2. **업로드**: 이미지·제어 영상은 ComfyUI `/upload/image`로 올린다.
3. **실행**: 템플릿에 값을 채워 `/prompt`로 제출하고, `/history/{prompt_id}`를 폴링(또는 `/ws` 웹소켓)해서 완료를 기다린다. 진행률을 터미널에 표시한다.
4. **결과 저장**: `/view`로 결과물을 받아 `outputs/<project>/<shot_id>/v<번호>.mp4`로 저장한다. 같은 장면을 다시 돌리면 버전 번호를 올리고 이전 결과를 지우지 않는다.
5. **메타데이터 기록**: 장면·버전마다 `meta.json`에 워크플로, 프롬프트, 시드, 해상도, 프레임 수, 소요 시간, 최대 VRAM 사용량(`nvidia-smi` 샘플링)을 저장한다.
6. **부분 재실행**: `--shots s02,s05`처럼 특정 장면만 다시 돌릴 수 있게 한다. `--seed-sweep 3`이면 시드를 바꿔 3개 버전을 만든다.
7. **러프컷**: `--rough-cut` 옵션이면 장면마다 최신(또는 `selected: v2`로 지정한) 버전을 ffmpeg로 이어 붙여 `outputs/<project>/rough_cut.mp4`를 만든다. 화면 왼쪽 위에 장면 ID를 작게 표시하는 옵션(`--label`)도 넣는다.
8. **오류 처리**: OOM이나 ComfyUI 오류가 나면 해당 장면만 실패로 기록하고 다음 장면으로 넘어간다. 마지막에 성공·실패 요약표를 출력한다.

### 구조
```
D:\ai\promo_draft\
  config.yaml          # ComfyUI 주소·경로, ffmpeg 경로, 기본값, 출력 경로
  .env.example
  env.ps1              # D드라이브 캐시·임시 경로 환경변수 (세션용)
  storyboard.yaml
  workflows/           # 내가 넣는 API JSON + mapping.yaml
  assets/              # photos/, blender/, ref/
  outputs/
  src/
    comfy_client.py    # 업로드, 제출, 폴링, 다운로드, free
    storyboard.py      # 로드·검증·보정
    runner.py          # 장면 실행, 버전 관리, 메타데이터
    rough_cut.py       # ffmpeg 이어붙이기
  run.py               # CLI 진입점
  README.md            # 사용법 (한국어)
```

## 3단계: 테스트
- 먼저 `--dry-run`으로 ComfyUI에 보낼 JSON만 출력해서 내가 확인하게 한다.
- 그다음 장면 1개를 512x320, 49프레임으로 낮춰 실제 생성 테스트를 한다.
- 성공하면 결과 경로, 소요 시간, VRAM 사용량을 보고하고 멈춘다. 전체 장면 실행은 내가 지시할 때만 한다.

## 완료 기준
- `. .\env.ps1` 실행 후 `python run.py storyboard.yaml --shots s01` 한 줄로 장면 하나가 생성되고 `outputs/`에 영상과 meta.json이 생긴다.
- `python run.py storyboard.yaml --rough-cut`으로 러프컷 한 파일이 나온다.
- 가상환경, 패키지, 캐시, 임시 파일이 모두 D드라이브에 있다.
- README에 워크플로 내보내기 방법(ComfyUI 메뉴 → Export (API))과 실행 예시가 있다.

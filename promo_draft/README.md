# 학과 홍보영상 초안 생성기 (ComfyUI API)

로컬 ComfyUI(LTX-2.5 / Z-Image Turbo)의 HTTP API만 호출해 `storyboard.yaml`의 장면을 순서대로 생성하고, 버전 관리·메타데이터 기록·러프컷까지 만든다. 최종본은 나중에 상용 API로 다시 뽑는 것을 전제로 한 **초안용** 도구다.

## 준비
```powershell
cd D:\capstone\project_ai_feedback\promo_draft
. .\env.ps1                      # 작업 전에 실행 (D드라이브 캐시/임시 경로 D:\capstone\cache, 이 세션에만 적용)
..\venv\Scripts\Activate.ps1      # 웹 앱과 같은 가상환경 (yaml·requests·tqdm·dotenv 포함)
```
- ffmpeg `D:\capstone\tools\ffmpeg\ffmpeg-9.0.2-essentials_build\bin\ffmpeg.exe` (PATH는 바꾸지 않고 `config.yaml`의 경로를 사용)
- `workflows/`는 웹 앱도 읽기 전용으로 같이 쓴다. 워크플로 이름·매핑을 바꿀 때는 앱(`COMFYUI_WORKFLOW_*`)도 함께 확인한다.
- 서버 주소 등 설정은 `config.yaml`, 오버라이드는 `.env` (`.env.example` 복사). 코드에 하드코딩하지 않는다.
- ComfyUI는 따로 실행해 둔다 (기본 http://127.0.0.1:8188).

## 워크플로 내보내기 (필수)
이 도구는 노드 그래프를 만들지 않는다. 직접 만든/검증한 워크플로를 템플릿으로 쓴다.
1. ComfyUI에서 워크플로를 열고 정상 동작을 확인한다.
2. 메뉴 → **Export (API)** (Workflow → Export (API))로 저장한다. (일반 Save가 아님)
3. 파일을 `workflows/`에 넣는다. 지금 쓰는 것: `ltx25_t2v.json`, `ltx25_i2v.json`, `ltx25_flf2v.json`(첫·마지막 프레임 고정), `zimage_t2i.json`(첫 프레임 이미지). `video_ltx2_5_*.json`·`image_z_image_turbo.json`은 원본 템플릿이다.
4. `python run.py --scan-workflows`로 프롬프트/시드/해상도 등 후보 노드를 확인하고, `workflows/mapping.example.yaml`을 참고해 `workflows/mapping.yaml`을 작성한다.

## 실행 예시
현재 콘티: `storyboard_ad30.yaml`(30초 광고, 최신) · `storyboard_intro30_v5.yaml` · `storyboard_60s.yaml`, 러프컷 전용 `storyboard_rough30.yaml`.
엑셀 콘티(`dept_promo_ad_storyboard.xlsx` 등)는 `tools/xlsx_to_storyboard.py`로 yaml로 바꾼다. 예전 콘티는 `../_archive/storyboards_old/`.

```powershell
python run.py storyboard.yaml --dry-run --shots s01      # 보낼 JSON만 출력
python run.py storyboard.yaml --test --shots s01         # 512x320, 49프레임 시험 생성
python run.py storyboard.yaml --shots s01                # 장면 1개 생성
python run.py storyboard.yaml --shots s02,s05 --seed-sweep 3   # 시드 바꿔 3개 버전
python run.py storyboard.yaml --rough-cut --label        # 러프컷 (장면 ID 표시)
```
- 결과: `outputs/<project>/<shot_id>/v<번호>.mp4` + `v<번호>.meta.json` (이전 버전은 지우지 않음)
- 검증: 너비·높이는 `size_multiple`(기본 64)의 배수, 프레임 수는 8n+1로 자동 보정(경고 출력). 입력 파일 존재 확인.
- 장면이 실패(OOM 등)해도 다음 장면으로 넘어가고 마지막에 요약표를 출력한다.
- 러프컷은 장면별 최신 버전(또는 storyboard의 `selected: v2`)을 이어 붙인다.

## 모델 경로 (ComfyUI)
모델은 `D:\capstone\Comfy-Desktop\ComfyUI-Shared\models` 아래에 둔다 (Comfy Desktop이 자동 생성한 `extra_model_paths` 사용 중).
다른 드라이브/폴더의 모델을 쓰고 싶으면 ComfyUI의 `extra_model_paths.yaml`에 경로를 추가하면 된다 (이 도구는 해당 파일을 수정하지 않는다).

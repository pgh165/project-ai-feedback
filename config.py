"""모든 설정값 — .env 값 우선. 모델명·임계값 하드코딩 금지."""
import os

from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))


def _bool(name, default):
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-change-me")
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL", "sqlite:///pipeline.db")
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # LLM (프롬프트 생성/업데이트) — Claude API → Ollama(로컬) → 템플릿 순 폴백
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
    ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-5-5")
    OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "")

    # 분류·임베딩 모델 (로컬 CPU)
    CLASSIFIER_MODEL = os.getenv("CLASSIFIER_MODEL", "paraphrase-multilingual-MiniLM-L12-v2")
    NLI_MODEL = os.getenv("NLI_MODEL", "MoritzLaurer/mDeBERTa-v3-base-zeroshot-v2.0")
    EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "paraphrase-multilingual-mpnet-base-v2")
    EMBEDDING_FALLBACK_MODEL = os.getenv(
        "EMBEDDING_FALLBACK_MODEL", "paraphrase-multilingual-MiniLM-L12-v2"
    )

    SPECIFICITY_THRESHOLD = float(os.getenv("SPECIFICITY_THRESHOLD", "0.45"))
    VALIDITY_THRESHOLD = float(os.getenv("VALIDITY_THRESHOLD", "0.40"))
    CLUSTERING_DISTANCE_THRESHOLD = float(os.getenv("CLUSTERING_DISTANCE_THRESHOLD", "0.40"))

    PRIORITY_ALPHA = float(os.getenv("PRIORITY_ALPHA", "0.35"))
    PRIORITY_BETA = float(os.getenv("PRIORITY_BETA", "0.25"))
    PRIORITY_GAMMA = float(os.getenv("PRIORITY_GAMMA", "0.20"))
    PRIORITY_DELTA = float(os.getenv("PRIORITY_DELTA", "0.12"))
    PRIORITY_EPSILON = float(os.getenv("PRIORITY_EPSILON", "0.08"))

    SIMULATION_MODE = _bool("SIMULATION_MODE", True)
    YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "")
    # YouTube 업로드 (pipeline/youtube_upload.py) — OAuth 클라이언트 JSON이 있을 때만 켜진다
    YOUTUBE_CLIENT_SECRETS = os.getenv("YOUTUBE_CLIENT_SECRETS", "")  # Google Cloud에서 받은 OAuth 클라이언트 JSON 경로
    YOUTUBE_TOKEN_FILE = os.getenv("YOUTUBE_TOKEN_FILE", "")          # 비우면 instance/youtube_token.json
    YOUTUBE_PRIVACY = os.getenv("YOUTUBE_PRIVACY", "private")         # private / unlisted / public (화면에서 바꿀 수 있음)
    YOUTUBE_CATEGORY_ID = os.getenv("YOUTUBE_CATEGORY_ID", "22")      # 22 = 인물/블로그
    YOUTUBE_UPLOAD_CHUNK_MB = int(os.getenv("YOUTUBE_UPLOAD_CHUNK_MB", "8"))
    MANUAL_TIME_PER_FEEDBACK_MINUTES = float(os.getenv("MANUAL_TIME_PER_FEEDBACK_MINUTES", "3"))

    # 영상 생성: mock / local(diffusers) / comfyui(로컬 ComfyUI + LTX-2.5)
    VIDEO_GENERATOR_ADAPTER = os.getenv("VIDEO_GENERATOR_ADAPTER", "mock")
    LOCAL_VIDEO_MODEL = os.getenv("LOCAL_VIDEO_MODEL", "Wan-AI/Wan2.1-T2V-1.3B-Diffusers")
    LOCAL_VIDEO_WIDTH = int(os.getenv("LOCAL_VIDEO_WIDTH", "480"))
    LOCAL_VIDEO_HEIGHT = int(os.getenv("LOCAL_VIDEO_HEIGHT", "832"))
    LOCAL_VIDEO_FRAMES = int(os.getenv("LOCAL_VIDEO_FRAMES", "81"))
    LOCAL_VIDEO_FPS = int(os.getenv("LOCAL_VIDEO_FPS", "16"))
    LOCAL_VIDEO_STEPS = int(os.getenv("LOCAL_VIDEO_STEPS", "30"))
    LOCAL_VIDEO_GUIDANCE = float(os.getenv("LOCAL_VIDEO_GUIDANCE", "5.0"))
    LOCAL_VIDEO_MAX_SHOTS = int(os.getenv("LOCAL_VIDEO_MAX_SHOTS", "8"))
    LOCAL_VIDEO_NEGATIVE_PROMPT = os.getenv(
        "LOCAL_VIDEO_NEGATIVE_PROMPT",
        "blurry, low quality, distorted face, deformed hands, extra fingers, text, "
        "subtitles, watermark, logo, static image, jpeg artifacts, overexposed",
    )

    # ── ComfyUI (VIDEO_GENERATOR_ADAPTER=comfyui) ──
    COMFYUI_URL = os.getenv("COMFYUI_URL", "http://127.0.0.1:8188")
    # 워크플로 JSON·mapping.yaml은 promo_draft와 공유한다 (워크스페이스의 promo_draft/workflows)
    COMFYUI_WORKFLOWS_DIR = os.getenv(
        "COMFYUI_WORKFLOWS_DIR", os.path.join(BASE_DIR, "promo_draft", "workflows"))
    COMFYUI_MAPPING_PATH = os.getenv("COMFYUI_MAPPING_PATH", "")  # 비우면 <workflows>/mapping.yaml
    COMFYUI_WORKFLOW_T2V = os.getenv("COMFYUI_WORKFLOW_T2V", "ltx25_t2v")
    COMFYUI_WORKFLOW_I2V = os.getenv("COMFYUI_WORKFLOW_I2V", "ltx25_i2v")
    # 이미지 먼저: 이미지가 없는 장면은 Z-Image로 첫 프레임을 만든 뒤 I2V로 움직인다 (T2V의 뭉개진 대상 보완).
    # 켜면 장면마다 이미지 1장 + I2V 1회. 이미지는 영상 옆에 v<k>.png로 남는다.
    COMFYUI_IMAGE_FIRST = _bool("COMFYUI_IMAGE_FIRST", False)
    COMFYUI_WORKFLOW_T2I = os.getenv("COMFYUI_WORKFLOW_T2I", "zimage_t2i")
    # 처음·끝 프레임 (컷 확인 '이 순간을 끝으로 다시 만들기'). 공식 템플릿 video_ltx2_5_flf2v를 API 형식으로 옮긴 것
    COMFYUI_WORKFLOW_FLF = os.getenv("COMFYUI_WORKFLOW_FLF", "ltx25_flf2v")
    # 움직임 경로 (Motion Control IC-LoRA). 경로가 있는 장면만 쓴다. ComfyUI-LTXVideo 노드와 LoRA가 필요하다
    COMFYUI_WORKFLOW_MOTION = os.getenv("COMFYUI_WORKFLOW_MOTION", "ltx25_motion")
    # 참조 영상으로 움직임 따라 하기 (LTX IC-LoRA Union Control, pipeline/reference_video.py)
    COMFYUI_WORKFLOW_UNION = os.getenv("COMFYUI_WORKFLOW_UNION", "ltx25_union")
    # 얼굴 사진으로 인물 그리기 (Qwen-Image-Edit-2511 Lightning 4단계). REF_PHOTO_MODE=draw일 때만, 모델이 없으면 사진 없이 그린다
    COMFYUI_WORKFLOW_FACE = os.getenv("COMFYUI_WORKFLOW_FACE", "qwen_face_t2i")
    # Qwen 첫 프레임 색 보정 (pipeline/tone.py): 색이 진해 채도·대비를 낮춘다. 1.0이면 보정 안 함 (2026-11-05 사용자가 고른 값)
    QWEN_TONE_COLOR = float(os.getenv("QWEN_TONE_COLOR", "0.85"))
    QWEN_TONE_CONTRAST = float(os.getenv("QWEN_TONE_CONTRAST", "0.9"))
    # 단계별 확인: 이미지 → (확인·다시 뽑기) → 컷 영상 → (확인·다시 뽑기) → 사람이 누르면 러프컷.
    # 꺼져 있으면 예전처럼 한 번에 러프컷까지 만든다. comfyui 어댑터에서만 동작한다.
    COMFYUI_STAGED_REVIEW = _bool("COMFYUI_STAGED_REVIEW", False)
    # AI 자동 진행 (pipeline/auto_pipeline.py · auto_review.py): 사람 대신 AI가 이미지·컷을 점검하고 미달 장면을 다시 만든다.
    # 사람은 마지막에 올릴 영상만 한 번 확인한다. 기존 단계별 확인 화면은 '직접 고치기'로 남는다.
    # 글자는 편집에서 넣는다: 모델 프롬프트의 모니터·간판·책 등을 '글자 없는 모습'으로 고쳐 보낸다 (pipeline/text_free.py)
    TEXTLESS_PROMPTS = _bool("TEXTLESS_PROMPTS", True)
    AUTO_REVIEW_MAX_RETRIES =int(os.getenv("AUTO_REVIEW_MAX_RETRIES", "2"))  # 장면마다 다시 만드는 최대 횟수
    AUTO_REVIEW_MIN_SCORE = float(os.getenv("AUTO_REVIEW_MIN_SCORE", "6"))  # 묘사 일치 점수(0~10) 통과 기준
    AUTO_REVIEW_CLIP_MIN_SCORE = float(os.getenv("AUTO_REVIEW_CLIP_MIN_SCORE", os.getenv("AUTO_REVIEW_MIN_SCORE", "6")))  # 컷 뒤쪽·끝 프레임
    AUTO_REVIEW_TEMPERATURE = float(os.getenv("AUTO_REVIEW_TEMPERATURE", "0"))  # 26B 점검 — 같은 그림이면 같은 답이 나오게
    AUTO_REVIEW_RECHECK_MARGIN = float(os.getenv("AUTO_REVIEW_RECHECK_MARGIN", "1"))  # 점수가 기준 ±이 안이면 한 번 더 물어 평균
    AUTO_REVIEW_MODEL = os.getenv("AUTO_REVIEW_MODEL", "")  # 비우면 TRANSLATE_COMFY_MODEL (Gemma4 e2b, 이미지 입력)
    AUTO_REVIEW_STILL_DIFF = float(os.getenv("AUTO_REVIEW_STILL_DIFF", "1.5"))  # 프레임 차이가 이보다 작으면 '멈춘 화면'
    AUTO_REVIEW_DARK_LEVEL = float(os.getenv("AUTO_REVIEW_DARK_LEVEL", "18"))  # 평균 밝기(0~255)가 이보다 낮으면 '어두운 화면'
    # 카메라 줌(첫·끝 프레임 배율)이 이 범위를 벗어나면 '카메라가 다가가거나 멀어졌어요' — 장면 줄이 카메라 움직임을 원할 때는 재지 않는다.
    # 일치도가 MIN_MATCH보다 낮으면(화면이 통째로 바뀜) 잰 배율을 믿지 않는다. 2026-11-06 실측: 고정 컷 1.0~1.04, 분명한 줌 1.24~1.38, 물체가 다가오며 살짝 다가간 컷 1.12~1.16(넘어감)
    AUTO_REVIEW_ZOOM_IN = float(os.getenv("AUTO_REVIEW_ZOOM_IN", "1.18"))
    AUTO_REVIEW_ZOOM_OUT = float(os.getenv("AUTO_REVIEW_ZOOM_OUT", "0.85"))
    AUTO_REVIEW_ZOOM_MIN_MATCH = float(os.getenv("AUTO_REVIEW_ZOOM_MIN_MATCH", "0.5"))
    END_CARD_FONT = os.getenv("END_CARD_FONT", "C:/Windows/Fonts/malgunbd.ttf")  # 엔딩 카드 문구 글꼴 (한글)
    CAMERA_LOCK_DEFAULT = _bool("CAMERA_LOCK_DEFAULT", True)  # 장면 줄에 카메라 움직임이 없으면 고정 카메라를 붙인다 (camera.py)
    # 자동 움직임 경로 (auto_motion.py) — 차 이동·집어 올리기처럼 방향·접촉이 필요한 장면에 26B가 찾은 위치로 Motion Control 경로를 붙인다.
    # 26B 비전(REFINE_LLAMA_MMPROJ)과 ComfyUI-LTXVideo·움직임 LoRA가 있을 때만 동작, 없으면 그냥 I2V
    AUTO_MOTION = _bool("AUTO_MOTION", True)
    COMFYUI_POLL_INTERVAL_SEC = float(os.getenv("COMFYUI_POLL_INTERVAL_SEC", "2"))
    COMFYUI_SHOT_TIMEOUT_SEC = int(os.getenv("COMFYUI_SHOT_TIMEOUT_SEC", "900"))
    COMFYUI_SIZE_MULTIPLE = int(os.getenv("COMFYUI_SIZE_MULTIPLE", "64"))
    COMFYUI_TEST_MODE = _bool("COMFYUI_TEST_MODE", False)
    COMFYUI_TEST_WIDTH = int(os.getenv("COMFYUI_TEST_WIDTH", "320"))
    COMFYUI_TEST_HEIGHT = int(os.getenv("COMFYUI_TEST_HEIGHT", "512"))
    COMFYUI_TEST_FRAMES = int(os.getenv("COMFYUI_TEST_FRAMES", "49"))
    COMFYUI_FREE_BETWEEN_SHOTS = _bool("COMFYUI_FREE_BETWEEN_SHOTS", True)
    # 워크플로의 Gemma 프롬프트 강화(TextGenerateLTX2Prompt)를 켠다. 장면 묘사를 영상 모델에 맞게 풍부하게
    # 다듬는다. 기본 꺼짐 — 켜면 같은 시드라도 강화 결과가 달라질 수 있어 재현성이 떨어진다.
    COMFYUI_PROMPT_ENHANCE = _bool("COMFYUI_PROMPT_ENHANCE", False)
    COMFYUI_AVAILABLE_TIMEOUT_SEC = float(os.getenv("COMFYUI_AVAILABLE_TIMEOUT_SEC", "3"))
    LTX_MODEL_VARIANT = os.getenv("LTX_MODEL_VARIANT", "ltx25-gguf-q4km")  # 기록용

    # 스토리보드 기본값 (프롬프트 문서 → 장면 목록 변환 시 사용)
    STORYBOARD_SHOT_SECONDS = float(os.getenv("STORYBOARD_SHOT_SECONDS", "5"))
    STORYBOARD_WIDTH = int(os.getenv("STORYBOARD_WIDTH", "576"))    # 세로 9:16 숏폼
    STORYBOARD_HEIGHT = int(os.getenv("STORYBOARD_HEIGHT", "960"))
    STORYBOARD_FPS = int(os.getenv("STORYBOARD_FPS", "24"))
    STORYBOARD_SEED = int(os.getenv("STORYBOARD_SEED", "42"))
    STORYBOARD_APPEND_NOTES = _bool("STORYBOARD_APPEND_NOTES", True)  # 피드백 요지를 재생성 장면 프롬프트에 덧붙임

    # 내레이션(해설 음성) — 프롬프트 문서의 [내레이션] 섹션을 TTS로 합성해 러프컷 위에 얹는다
    # 배경음악 — ComfyUI 로컬 음악 모델 (pipeline/music.py). 모델 파일은 ComfyUI 모델 폴더에 있어야 한다.
    # 기본 stable_audio (2026-11-05 비교에서 사용자가 고름). minimax는 곡에 다른 소리가 섞여 기본에서 뺐다
    MUSIC_ENGINE = os.getenv("MUSIC_ENGINE", "stable_audio")
    STABLE_AUDIO_CHECKPOINT = os.getenv("STABLE_AUDIO_CHECKPOINT", "stable_audio_3_medium.safetensors")
    STABLE_AUDIO_TEXT_ENCODER = os.getenv("STABLE_AUDIO_TEXT_ENCODER", "t5gemma_b_b_ul2.safetensors")
    STABLE_AUDIO_STEPS = int(os.getenv("STABLE_AUDIO_STEPS", "8"))
    MUSIC_DIT = os.getenv("MUSIC_DIT", "minimax_music3_dit_fp16.safetensors")
    MUSIC_TEXT_ENCODER = os.getenv("MUSIC_TEXT_ENCODER", "minimax_music3_text_encoder_pruned_bf16.safetensors")
    MUSIC_VAE = os.getenv("MUSIC_VAE", "minimax_music3_dav.safetensors")
    MUSIC_STEPS = int(os.getenv("MUSIC_STEPS", "30"))
    # 곡은 이 길이 이상으로 만들고 그중 소리 크기가 고른 구간을 영상 길이만큼 골라 쓴다 (bgm.steady_offset).
    # 20초 남짓으로 만들면 모델이 도입·본곡·끝맺음을 욱여넣어 갑자기 조용해지거나 커져 어색했다
    MUSIC_MIN_SECONDS = float(os.getenv("MUSIC_MIN_SECONDS", "60"))
    # 데모 재생(PipelineSession.demo_json)에서 '만드는 중'을 장면마다 보여 주는 시간(초) — 시연 영상용
    DEMO_REPLAY_SEC_PER_SHOT = float(os.getenv("DEMO_REPLAY_SEC_PER_SHOT", "1.5"))
    MUSIC_CFG = float(os.getenv("MUSIC_CFG", "1.7"))
    MUSIC_TIMEOUT_SEC = int(os.getenv("MUSIC_TIMEOUT_SEC", "3600"))
    # 영어 장면 묘사를 한국어로 번역해 먼저 보여 준다 (영어 원문은 아래에 덧붙임).
    # ComfyUI의 TextGenerate 노드 + 이미 받아 둔 Gemma 모델을 쓴다 → 꺼져 있으면 LLM 폴백(llm.py) → 영어만 표시
    TRANSLATE_ENABLED = os.getenv("TRANSLATE_ENABLED", "true").lower() == "true"
    TRANSLATE_COMFY_MODEL = os.getenv("TRANSLATE_COMFY_MODEL", "gemma4_e2b_it_int8_convrot.safetensors")
    TRANSLATE_COMFY_CLIP_TYPE = os.getenv("TRANSLATE_COMFY_CLIP_TYPE", "ltxv")
    TRANSLATE_TIMEOUT_SEC = int(os.getenv("TRANSLATE_TIMEOUT_SEC", "900"))  # 영상 생성 중이면 큐에서 기다린다
    # edge(Microsoft 신경망 음성, 계정 불필요·인터넷 필요) / supertonic(로컬 CPU) / windows / none
    VOICE_ADAPTER = os.getenv("VOICE_ADAPTER", "edge")
    # 러프컷: AI가 만든 컷의 시작 멈칫·끝 느려짐을 잘라 낸다 (불러온 원본 장면은 그대로). 0이면 자르지 않음
    SHOT_MAX_SECONDS = float(os.getenv("SHOT_MAX_SECONDS", "8"))  # 의견으로 장면 길이를 늘릴 때 최대 길이
    CLIP_TRIM_HEAD_SEC = float(os.getenv("CLIP_TRIM_HEAD_SEC", "0.15"))
    CLIP_TRIM_TAIL_SEC = float(os.getenv("CLIP_TRIM_TAIL_SEC", "0.1"))
    BGM_AUTO = _bool("BGM_AUTO", True)  # 배경음악을 정하지 않은 버전은 확인 단계(러프컷)에서 곡을 만들어 깐다
    VOICE_FALLBACK = os.getenv("VOICE_FALLBACK", "supertonic")  # 기본 엔진이 실패하면(오프라인 등) 이 엔진으로
    TTS2_PYTHON = os.getenv("TTS2_PYTHON", "")  # 비우면 <프로젝트>/venv_tts2 (edge-tts·supertonic)
    TTS2_TIMEOUT_SEC = int(os.getenv("TTS2_TIMEOUT_SEC", "300"))
    EDGE_TTS_VOICE = os.getenv("EDGE_TTS_VOICE", "ko-KR-SunHiNeural")  # SunHi(여) / InJoon(남) / HyunsuMultilingual(남)
    SUPERTONIC_VOICE = os.getenv("SUPERTONIC_VOICE", "F1")  # F1~F5 / M1~M5
    # 맡기기 화면의 참고 사진(인물·물건·장소): reference = 26B가 사진마다 생김새를 읽어 장면 묘사에 글로 넣고 Z-Image로 그림
    # (Qwen 안 씀), draw = 예전 방식(Qwen-Image-Edit가 사진대로 첫 프레임을 그림).
    # 2026-11-07 사용자: 원본·데모 모두 Qwen을 쓰지 않는다 → reference
    REF_PHOTO_MODE = os.getenv("REF_PHOTO_MODE", "reference")
    VOICE_CULTURE = os.getenv("VOICE_CULTURE", "ko-KR")    # 이 언어의 설치된 음성을 고른다
    VOICE_NAME = os.getenv("VOICE_NAME", "")               # 비우면 VOICE_CULTURE의 첫 음성
    VOICE_RATE = int(os.getenv("VOICE_RATE", "0"))         # -10(느림) ~ 10(빠름)
    NARRATION_VOLUME = float(os.getenv("NARRATION_VOLUME", "1.0"))
    NARRATION_BG_VOLUME = float(os.getenv("NARRATION_BG_VOLUME", "0.35"))  # 내레이션 아래 효과음 비율
    NARRATION_MAX_SPEEDUP = float(os.getenv("NARRATION_MAX_SPEEDUP", "1.4"))  # 장면보다 길면 이만큼까지 빠르게
    NARRATION_LEAD_SEC = float(os.getenv("NARRATION_LEAD_SEC", "0.2"))  # 장면 시작 후 말을 시작하는 지연
    # 글 다듬기 LLM의 마지막 단계: ComfyUI의 Gemma (llm.py). Claude·Ollama가 없을 때 쓴다
    # 한국어 장면·스타일 → 영어 (pipeline/ko_en.py)와 AI로 채우기(pipeline/draft_fill.py)에 쓰는 ComfyUI 모델.
    # 비우면 LLM_COMFY_MODEL → TRANSLATE_COMFY_MODEL(Gemma4 e2b). 2026-10 비교: e2b가 빠르고 정확(기본),
    # qwen3.5_2b_bf16(type flux2)은 번역이 가장 꼼꼼한 대안. gemma4-12b-with-proj는 ComfyUI가 LTX 텍스트 인코더로 읽어
    # 입력을 1024토큰으로 채우므로(lt.ltxav_gemma4_tokenizer) 글을 못 쓴다(SSSS…). gemma_3_12B fp4는 오역이 있어 지웠다.
    KO_EN_COMFY_MODEL = os.getenv("KO_EN_COMFY_MODEL", "")
    KO_EN_COMFY_CLIP_TYPE = os.getenv("KO_EN_COMFY_CLIP_TYPE", "")  # 비우면 TRANSLATE_COMFY_CLIP_TYPE (Qwen3.5는 flux2)
    LLM_COMFY_ENABLED = _bool("LLM_COMFY_ENABLED", True)
    LLM_COMFY_MODEL = os.getenv("LLM_COMFY_MODEL", "")  # 비우면 TRANSLATE_COMFY_MODEL
    LLM_COMFY_MAX_TOKENS = int(os.getenv("LLM_COMFY_MAX_TOKENS", "1500"))
    LLM_COMFY_TIMEOUT_SEC = int(os.getenv("LLM_COMFY_TIMEOUT_SEC", "900"))
    # 장면 다듬기·AI로 채우기용 큰 모델 (llm.generate_strong). 번역은 e2b 그대로.
    # 2026-10-23 비교: Gemma4 26B A4B가 규칙(앞에 분위기·빛의 색·렌즈·추상어 빼기)을 e2b보다 훨씬 잘 따랐다.
    # Ollama 0.40.9는 Gemma4를 띄우고도 준비됨을 못 알아채(5분 뒤 실패) llama-server(OpenAI 호환)를 직접 쓴다.
    # REFINE_LLAMA_SERVER·MODEL을 주면 필요할 때 켜고 다 쓰면 끈다(VRAM 반납). 비우면 REFINE_LLM_URL에 떠 있는 서버만 쓴다.
    REFINE_LLM_URL = os.getenv("REFINE_LLM_URL", "")  # 예: http://127.0.0.1:8099 — 비우면 큰 모델 없이 e2b로
    REFINE_LLM_LABEL = os.getenv("REFINE_LLM_LABEL", "gemma4-26b")
    REFINE_LLAMA_SERVER = os.getenv("REFINE_LLAMA_SERVER", "")  # llama-server.exe 경로 (Ollama 설치 폴더 lib/ollama)
    REFINE_LLAMA_MODEL = os.getenv("REFINE_LLAMA_MODEL", "")    # .gguf (Ollama blobs의 모델 파일)
    REFINE_LLAMA_ARGS = os.getenv("REFINE_LLAMA_ARGS", "-ngl 99 --n-cpu-moe 12 -c 4096 --jinja")
    # 이미지 입력(비전) 파일 — Ollama blobs의 projector 층. 주면 AI 점검·원칙 질문 등도 26B가 이미지를 보고 한다
    REFINE_LLAMA_MMPROJ = os.getenv("REFINE_LLAMA_MMPROJ", "")
    # llama-server의 GPU 라이브러리 폴더 — 비우면 llama-server 옆의 cuda_v13·cuda_v12를 찾는다.
    # 2026-11-06: 이 폴더를 못 찾으면 'no usable GPU found'로 CPU에서 돌았다
    REFINE_LLAMA_LIB_DIR = os.getenv("REFINE_LLAMA_LIB_DIR", "")
    REFINE_LLM_START_TIMEOUT_SEC = int(os.getenv("REFINE_LLM_START_TIMEOUT_SEC", "180"))
    REFINE_LLM_TIMEOUT_SEC = int(os.getenv("REFINE_LLM_TIMEOUT_SEC", "300"))

    # 산출물 / 후처리
    VIDEO_OUTPUT_DIR = os.getenv("VIDEO_OUTPUT_DIR", "")  # 비우면 instance/videos
    FFMPEG_PATH = os.getenv("FFMPEG_PATH", "")  # 비우면 PATH → imageio-ffmpeg 순으로 탐색
    ROUGH_CUT_LABEL = _bool("ROUGH_CUT_LABEL", False)

    # 파이프라인 동작
    PARTIAL_REGEN_ENABLED = _bool("PARTIAL_REGEN_ENABLED", True)
    REQUIRE_VIDEO_APPROVAL = _bool("REQUIRE_VIDEO_APPROVAL", True)
    MOCK_DELAY_SEC = float(os.getenv("MOCK_DELAY_SEC", "0"))  # mock 장면당 인위적 지연(진행 표시 확인용)
    JOBS_SYNC = _bool("JOBS_SYNC", False)  # true면 작업 큐 없이 요청 안에서 바로 실행(테스트용)


    # 개발용 파이프라인 점검 페이지 (/dev/pipeline-test) — 로컬 전용, 배포 시 false
    DEV_TEST_PAGE = _bool("DEV_TEST_PAGE", True)
    DEMO_MODE = _bool("DEMO_MODE", False)  # 시연 촬영용 데모 앱 — run_demo.py가 켠다 (따로 된 작업 폴더 instance_demo, 포트 5001)

    # 화면에 보이는 서비스 이름
    SERVICE_NAME = os.getenv("SERVICE_NAME", "AI 영상 피드백 자동 개선 시스템")

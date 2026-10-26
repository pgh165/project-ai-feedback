import json
from datetime import datetime

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

# 파이프라인 9단계 (화면 스테퍼의 ①~⑨와 1:1). generating·clustering은 진행 중 상태.
ROUND_STATUSES = [
    "prompt_built",
    "generating",
    "video_ready",
    "uploaded",
    "feedback_collected",
    "feedback_classified",
    "clustering",
    "pending_review",
    "prompt_updated",
]
# 단계 번호에는 없지만 유효한 상태: ② 위에 빨간 표시로 보인다
FAILED_STATUS = "generation_failed"
# 단계별 확인 생성(COMFYUI_STAGED_REVIEW): 사람이 이미지·컷을 확인하는 동안의 대기 상태. 역시 ② 단계에 표시된다.
IMAGES_READY = "images_ready"   # 첫 프레임 이미지 확인 대기 (마음에 안 드는 이미지는 다시 뽑는다)
CLIPS_READY = "clips_ready"     # 컷(장면 영상) 확인 대기 — 러프컷은 아직 만들지 않았다
REVIEW_STATUSES = [IMAGES_READY, CLIPS_READY]
ALL_STATUSES = ROUND_STATUSES + [FAILED_STATUS] + REVIEW_STATUSES

# 이 카테고리의 클러스터는 scope="technical"(전 장면이 지적된 것으로 계산)이 된다
TECHNICAL_SCOPE_CATEGORIES = {"technical", "subtitle", "audio"}

# 전역 기술 설정으로 승격되는 것은 이 중 범용 기술 결함(끊김·화질·싱크)뿐이다.
# 자막·음향은 영상마다 구성과 취향이 달라 모든 새 프로젝트에 붙이면 안 된다.
GLOBAL_PROMOTION_CATEGORIES = {"technical"}


class PipelineSession(db.Model):
    """사용자가 만들고 싶은 영상 프로젝트 1개 (피드백으로 버전을 거듭 다듬는다)."""

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    genre = db.Column(db.String(100), default="")
    characters = db.Column(db.Text, default="")
    style = db.Column(db.Text, default="")
    aspect_ratio = db.Column(db.String(10), default="9:16")  # 9:16 / 16:9 / 1:1
    shot_seconds = db.Column(db.Float, default=0.0)  # 장면 1개 길이(초). 0이면 설정 기본값
    brand_json = db.Column(db.Text, default="")  # 로고 설정 (pipeline/overlay.py) — 버전이 바뀌어도 유지
    # 데모 재생 (orchestrator.demo_plan): {"rounds": {"1": {"replay_round_id": id, "youtube_video_id": "..."}, "2": {...}}}
    # 그 차수에서 '제작 시작'을 누르면 만들지 않고 replay_round_id 버전의 결과를 바로 쓰고, 'YouTube에 올리기'는 이미 올린 영상에 연결한다
    demo_json = db.Column(db.Text, default="")
    voice_json = db.Column(db.Text, default="")  # 내레이션 목소리·말투 (pipeline/voice.py) — 모든 버전 공통
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    rounds = db.relationship(
        "VideoRound", backref="session", order_by="VideoRound.round_number",
        cascade="all, delete-orphan",
    )
    versions = db.relationship(
        "PromptVersion", backref="session", order_by="PromptVersion.version_number",
        cascade="all, delete-orphan",
    )

    @property
    def current_round(self):
        return self.rounds[-1] if self.rounds else None


class VideoRound(db.Model):
    """영상 버전(N차) 1개 = 파이프라인 1라운드."""

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey("pipeline_session.id"), nullable=False)
    round_number = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(30), default="prompt_built")
    prompt_text = db.Column(db.Text, default="")
    storyboard = db.Column(db.Text, default="")
    video_path = db.Column(db.String(500), default="")
    youtube_video_id = db.Column(db.String(50), default="")
    video_job_status = db.Column(db.String(20), default="idle")  # idle/running/failed
    video_job_message = db.Column(db.Text, default="")
    processing_seconds = db.Column(db.Float, default=0.0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # 영상 파이프라인 (VIDEO_PIPELINE_INTEGRATION.md 8절)
    storyboard_json = db.Column(db.Text, default="")      # 이 라운드 생성에 쓴 스토리보드 스냅샷
    generation_progress = db.Column(db.Float, default=0.0)  # 0~1
    generation_detail = db.Column(db.Text, default="")
    generation_error = db.Column(db.Text, default="")
    adapter_used = db.Column(db.String(20), default="")   # comfyui/mixed/runway/mock/local
    narration_json = db.Column(db.Text, default="")  # 내레이션 합성 결과 {"status","detail","shots":{id:{...}}}
    timeline_json = db.Column(db.Text, default="")   # 올릴 영상의 시간표 {"total","scenes","cues","logo"} — ③ 타임라인 편집기
    # 편집 설정 (pipeline/edit_plan.py) — 장면을 다시 만들지 않고 러프컷 합성에만 쓰는 값
    edit_json = db.Column(db.Text, default="")       # 이 버전의 러프컷에 쓰는 설정 (이전 버전 검토에서 정해짐)
    next_edit_json = db.Column(db.Text, default="")  # 이 버전 검토 화면에서 제작자가 직접 고친 다음 버전 설정
    edit_note = db.Column(db.Text, default="")       # 편집 설정을 다 적용하지 못한 사유 (예: 자막 글꼴 없음)
    music_json = db.Column(db.Text, default="")      # 배경음악 설정·곡 파일 (pipeline/bgm.py) — 다음 버전에 이어진다
    video_approved =db.Column(db.Boolean, default=False)

    feedbacks = db.relationship("Feedback", backref="round", cascade="all, delete-orphan")
    clusters = db.relationship(
        "ClusterResult", backref="round", order_by="ClusterResult.rank",
        cascade="all, delete-orphan",
    )
    shot_renders = db.relationship(
        "ShotRender", backref="round", order_by="ShotRender.id", cascade="all, delete-orphan",
        foreign_keys="ShotRender.round_id",
    )

    @property
    def status_index(self):
        if self.status == FAILED_STATUS or self.status in REVIEW_STATUSES:
            return ROUND_STATUSES.index("generating")
        return ROUND_STATUSES.index(self.status) if self.status in ROUND_STATUSES else 0

    @property
    def storyboard_data(self):
        try:
            return json.loads(self.storyboard_json) if self.storyboard_json else None
        except ValueError:
            return None


class Feedback(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    round_id = db.Column(db.Integer, db.ForeignKey("video_round.id"), nullable=False)
    cluster_id = db.Column(db.Integer, db.ForeignKey("cluster_result.id"), nullable=True)
    author = db.Column(db.String(100), default="")
    source = db.Column(db.String(20), default="simulation")  # simulation/youtube/manual
    comment_text = db.Column(db.Text, nullable=False)
    language = db.Column(db.String(10), default="")
    is_constructive = db.Column(db.Boolean, nullable=True)  # None = 미분류
    specificity_score = db.Column(db.Float, default=0.0)
    validity_score = db.Column(db.Float, default=0.0)
    severity_score = db.Column(db.Float, default=0.0)
    urgency_score = db.Column(db.Float, default=0.0)
    quality_score = db.Column(db.Float, default=0.0)
    topic_category = db.Column(db.String(30), default="")
    topic_categories_json = db.Column(db.Text, default="[]")
    improvement_suggestion = db.Column(db.Text, default="")
    needs_review = db.Column(db.Boolean, default=False)
    classifier_name = db.Column(db.String(30), default="")
    timestamp_sec = db.Column(db.Float, nullable=True)  # 댓글에서 추출한 영상 시각(초)

    @property
    def topic_categories_multi(self):
        try:
            return json.loads(self.topic_categories_json or "[]")
        except ValueError:
            return []


class ClusterResult(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    round_id = db.Column(db.Integer, db.ForeignKey("video_round.id"), nullable=False)
    cluster_key = db.Column(db.String(40), nullable=False)
    topic_category = db.Column(db.String(30), default="")
    scope = db.Column(db.String(20), default="creative")  # creative/technical
    representative_text = db.Column(db.Text, default="")
    cluster_summary = db.Column(db.Text, default="")
    feedback_count = db.Column(db.Integer, default=0)
    avg_validity_score = db.Column(db.Float, default=0.0)
    avg_severity_score = db.Column(db.Float, default=0.0)
    avg_urgency_score = db.Column(db.Float, default=0.0)
    avg_quality_score = db.Column(db.Float, default=0.0)
    cohesion_score = db.Column(db.Float, default=0.0)
    priority_score = db.Column(db.Float, default=0.0)
    rank = db.Column(db.Integer, default=0)
    contradicted_by_key = db.Column(db.String(40), nullable=True)
    reoccurrence_count = db.Column(db.Integer, default=0)
    status = db.Column(db.String(20), default="pending")  # pending/approved/rejected/deferred
    affected_shots_json = db.Column(db.Text, default="[]")  # 이 클러스터가 지적한 장면 ID 목록
    # 반영 위치(scene/motion/edit/audio/content) — 제작자가 고른 값. 비어 있으면 feedback_target.suggest()
    apply_target = db.Column(db.String(20), nullable=True)

    feedbacks = db.relationship("Feedback", backref="cluster")

    @property
    def affected_shots(self):
        try:
            return json.loads(self.affected_shots_json or "[]")
        except ValueError:
            return []

    @property
    def promotes_to_global(self):
        """승인되면 전역 기술 설정이 되는가. 범용 기술 결함이고, 특정 영상의 한 시점(타임스탬프)을
        가리키는 댓글이 없어야 한다 — 타임스탬프가 있으면 그 영상 이야기라 다른 프로젝트에 붙이지 않는다."""
        return (self.scope == "technical" and self.topic_category in GLOBAL_PROMOTION_CATEGORIES
                and not any(f.timestamp_sec is not None for f in self.feedbacks))

    def to_dict(self):
        return {
            "id": self.id,
            "cluster_key": self.cluster_key,
            "topic_category": self.topic_category,
            "scope": self.scope,
            "representative_text": self.representative_text,
            "cluster_summary": self.cluster_summary,
            "feedback_count": self.feedback_count,
            "avg_validity_score": self.avg_validity_score,
            "avg_severity_score": self.avg_severity_score,
            "avg_urgency_score": self.avg_urgency_score,
            "avg_quality_score": self.avg_quality_score,
            "cohesion_score": self.cohesion_score,
            "priority_score": self.priority_score,
            "rank": self.rank,
            "contradicted_by_key": self.contradicted_by_key,
            "reoccurrence_count": self.reoccurrence_count,
            "status": self.status,
            "affected_shots": self.affected_shots,
            "apply_target": self.target,
        }

    @property
    def target(self):
        """반영 위치 — 장면을 다시 만드는 의견(scene/motion)인지 아닌지 (feedback_target.py)."""
        from .pipeline.feedback_target import target_of
        return target_of(self)


class PromptVersion(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey("pipeline_session.id"), nullable=False)
    round_id = db.Column(db.Integer, db.ForeignKey("video_round.id"), nullable=True)
    version_number = db.Column(db.Integer, nullable=False)
    content = db.Column(db.Text, default="")
    diff_text = db.Column(db.Text, default="")
    note = db.Column(db.String(300), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    changes = db.relationship("PromptChange", backref="version", cascade="all, delete-orphan")


class PromptChange(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    version_id = db.Column(db.Integer, db.ForeignKey("prompt_version.id"), nullable=False)
    cluster_id = db.Column(db.Integer, db.ForeignKey("cluster_result.id"), nullable=True)
    description = db.Column(db.Text, default="")


class GlobalTechnicalSetting(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    category = db.Column(db.String(30), default="")
    instruction = db.Column(db.Text, nullable=False)
    source_cluster_id = db.Column(db.Integer, db.ForeignKey("cluster_result.id"), nullable=True)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Metric(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    round_id = db.Column(db.Integer, db.ForeignKey("video_round.id"), nullable=False)
    total_feedback = db.Column(db.Integer, default=0)
    constructive_count = db.Column(db.Integer, default=0)
    cluster_count = db.Column(db.Integer, default=0)
    approved_count = db.Column(db.Integer, default=0)
    reflected_feedback_count = db.Column(db.Integer, default=0)
    reflection_rate = db.Column(db.Float, default=0.0)
    time_saved_minutes = db.Column(db.Float, default=0.0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    # 부분 재생성 · 상용 API 지표 (metrics.py)
    shots_total = db.Column(db.Integer, default=0)
    shots_regenerated = db.Column(db.Integer, default=0)
    shots_reused = db.Column(db.Integer, default=0)
    regen_saved_seconds = db.Column(db.Float, default=0.0)
    api_shots = db.Column(db.Integer, default=0)
    api_cost_usd = db.Column(db.Float, default=0.0)


class ShotRender(db.Model):
    """장면 1개의 생성 결과 1건. 같은 장면을 다시 만들면 version이 올라간다."""

    id = db.Column(db.Integer, primary_key=True)
    round_id = db.Column(db.Integer, db.ForeignKey("video_round.id"), nullable=False)
    shot_id = db.Column(db.String(20), nullable=False)
    version = db.Column(db.Integer, default=1)
    status = db.Column(db.String(20), default="queued")  # queued/running/done/failed/reused
    generator = db.Column(db.String(10), default="local")  # local/api
    provider = db.Column(db.String(40), default="")        # comfyui-ltx25-gguf-q4km / runway / mock
    cost_usd = db.Column(db.Float, default=0.0)
    quality_flags_json = db.Column(db.Text, default="[]")
    selected = db.Column(db.Boolean, default=False)         # 러프컷에 쓸 버전
    workflow = db.Column(db.String(60), default="")
    prompt = db.Column(db.Text, default="")
    enhanced_prompt = db.Column(db.Text, default="")  # Gemma 프롬프트 강화 결과(켰을 때만)
    seed = db.Column(db.Integer, default=0)
    width = db.Column(db.Integer, default=0)
    height = db.Column(db.Integer, default=0)
    frames = db.Column(db.Integer, default=0)
    fps = db.Column(db.Integer, default=0)
    duration = db.Column(db.Float, default=0.0)
    path = db.Column(db.String(500), default="")
    image_path = db.Column(db.String(500), default="")  # '이미지 먼저'로 만든 첫 프레임(png)
    end_image_path = db.Column(db.String(500), default="")  # '이 순간을 끝으로' — 끝 프레임으로 고정한 화면(png)
    error = db.Column(db.Text, default="")
    elapsed_sec = db.Column(db.Float, default=0.0)
    peak_vram_mb = db.Column(db.Float, default=0.0)
    reused_from_id = db.Column(db.Integer, db.ForeignKey("shot_render.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def quality_flags(self):
        try:
            return json.loads(self.quality_flags_json or "[]")
        except ValueError:
            return []


class GenerationJob(db.Model):
    """GPU·클러스터링 작업 큐 (SQLite + 워커 스레드 1개)."""

    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(30), nullable=False)  # generate_video/regenerate_shots/cluster
    round_id = db.Column(db.Integer, db.ForeignKey("video_round.id"), nullable=False)
    payload_json = db.Column(db.Text, default="{}")
    status = db.Column(db.String(20), default="queued")  # queued/running/done/failed
    error = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    started_at = db.Column(db.DateTime, nullable=True)
    finished_at = db.Column(db.DateTime, nullable=True)


class TextTranslation(db.Model):
    """영어 장면 묘사 → 한국어 번역 캐시. 같은 문장은 한 번만 번역한다 (원문 해시로 찾는다)."""

    id = db.Column(db.Integer, primary_key=True)
    source_hash = db.Column(db.String(40), unique=True, nullable=False, index=True)
    source_text = db.Column(db.Text, nullable=False)
    ko_text = db.Column(db.Text, default="")
    engine = db.Column(db.String(60), default="")  # comfyui:<모델 파일> / claude / ollama
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

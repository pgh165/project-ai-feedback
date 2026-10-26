"""[팀 직접 구현] 클러스터 승인 워크플로 · 프롬프트 버전 관리 · 재발 추적."""
import difflib

from ..models import (
    TECHNICAL_SCOPE_CATEGORIES, ClusterResult, GlobalTechnicalSetting, PipelineSession,
    PromptChange, PromptVersion, VideoRound, db,
)
from .clusterer.embedder import get_embeddings
from .clusterer.ranker import rerank
from .prompt_updater import update_prompt

_REOCCURRENCE_SIMILARITY = 0.70
_REOCCURRENCE_BOOST = 0.10
_REOCCURRENCE_MAX_ROUNDS = 3


def make_diff(original, updated, fromfile="이전", tofile="변경"):
    # splitlines() + "\n".join() — 헤더 라인 뭉침 방지
    diff = difflib.unified_diff(
        original.splitlines(), updated.splitlines(), fromfile=fromfile, tofile=tofile, lineterm="")
    return "\n".join(diff)


def check_reoccurrence(round_obj, clusters, config):
    """이전 라운드에 같은 주제의 클러스터가 몇 번 등장했는지 계산해 clusters에 기록."""
    previous = (
        ClusterResult.query.join(VideoRound)
        .filter(VideoRound.session_id == round_obj.session_id,
                VideoRound.round_number < round_obj.round_number)
        .all()
    )
    for c in clusters:
        c["reoccurrence_count"] = 0
    if not previous or not clusters:
        return clusters

    texts = [c["representative_text"] for c in clusters] + [p.representative_text for p in previous]
    emb = get_embeddings(texts, config["EMBEDDING_MODEL"], config["EMBEDDING_FALLBACK_MODEL"])
    new_emb, prev_emb = emb[: len(clusters)], emb[len(clusters):]
    sims = new_emb @ prev_emb.T
    for i, c in enumerate(clusters):
        rounds = {
            p.round_id for j, p in enumerate(previous)
            if p.topic_category == c["topic_category"] and sims[i, j] >= _REOCCURRENCE_SIMILARITY
        }
        c["reoccurrence_count"] = len(rounds)
    return clusters


def save_clusters(round_obj, clusters, feedbacks, config):
    """클러스터 결과 저장. 재발 이슈는 라운드당 10% (최대 3라운드) 우선순위 부스트."""
    for f in round_obj.feedbacks:
        f.cluster_id = None
    ClusterResult.query.filter_by(round_id=round_obj.id).delete()
    db.session.flush()

    check_reoccurrence(round_obj, clusters, config)
    for c in clusters:
        prev = c["reoccurrence_count"]
        if prev > 0:
            c["priority_score"] = round(
                c["priority_score"] * (1 + _REOCCURRENCE_BOOST * min(prev, _REOCCURRENCE_MAX_ROUNDS)), 4)
    rerank(clusters)

    for c in clusters:
        row = ClusterResult(
            round_id=round_obj.id,
            cluster_key=c["cluster_key"],
            topic_category=c["topic_category"],
            scope="technical" if c["topic_category"] in TECHNICAL_SCOPE_CATEGORIES else "creative",
            representative_text=c["representative_text"],
            cluster_summary=c["cluster_summary"],
            feedback_count=c["feedback_count"],
            avg_validity_score=c["avg_validity_score"],
            avg_severity_score=c["avg_severity_score"],
            avg_urgency_score=c["avg_urgency_score"],
            avg_quality_score=c["avg_quality_score"],
            cohesion_score=c["cohesion_score"],
            priority_score=c["priority_score"],
            rank=c["rank"],
            contradicted_by_key=c["contradicted_by_key"],
            reoccurrence_count=c["reoccurrence_count"],
        )
        db.session.add(row)
        db.session.flush()
        for idx in c["member_indices"]:
            feedbacks[idx].cluster_id = row.id
    return clusters


def _cluster_payload(clusters):
    return [{"topic_category": c.topic_category, "text": c.representative_text,
             "count": c.feedback_count} for c in clusters]


def next_version_number(session_id):
    last = (PromptVersion.query.filter_by(session_id=session_id)
            .order_by(PromptVersion.version_number.desc()).first())
    return (last.version_number if last else 0) + 1


def add_version(session_id, round_id, previous_text, new_text, note):
    version = PromptVersion(
        session_id=session_id, round_id=round_id,
        version_number=next_version_number(session_id), content=new_text,
        diff_text=make_diff(previous_text, new_text), note=note)
    db.session.add(version)
    db.session.flush()
    return version


def preview_prompt_change(round_obj, config):
    """승인된 클러스터를 반영했을 때의 diff 미리보기 (저장하지 않음, 템플릿 방식)."""
    approved = [c for c in round_obj.clusters if c.status == "approved"]
    if not approved:
        return ""
    new_text, _ = update_prompt(  # 미리보기는 LLM을 부르지 않는다 (느리고, 실제 반영 때 다시 쓴다)
        round_obj.prompt_text, _cluster_payload(approved), round_obj.round_number + 1, config, allow_llm=False)
    return make_diff(round_obj.prompt_text, new_text)


def apply_approved_clusters(round_obj, config, classifier=None):
    """승인된 클러스터를 반영해 다음 버전(라운드)을 만든다. (next_round, source) 반환."""
    approved = [c for c in round_obj.clusters if c.status == "approved"]
    if not approved:
        raise ValueError("승인된 클러스터가 없습니다.")

    next_number = round_obj.round_number + 1
    new_text, source = update_prompt(
        round_obj.prompt_text, _cluster_payload(approved), next_number, config)

    version = add_version(
        round_obj.session_id, round_obj.id, round_obj.prompt_text, new_text,
        f"{round_obj.round_number}차 피드백 {len(approved)}건 반영 ({source})")
    for c in approved:
        db.session.add(PromptChange(
            version_id=version.id, cluster_id=c.id,
            description=f"[{c.topic_category}] {c.representative_text} ({c.feedback_count}명)"))

        # 전역 기술 설정 승격: 범용 기술 결함 클러스터 → 이후 새 프로젝트에도 적용 (조건은 ClusterResult.promotes_to_global)
        if c.promotes_to_global:
            exists = GlobalTechnicalSetting.query.filter_by(
                instruction=c.representative_text).first()
            if not exists:
                db.session.add(GlobalTechnicalSetting(
                    category=c.topic_category, instruction=c.representative_text,
                    source_cluster_id=c.id))

        # 동적 프로토타입 업데이트: 승인된 피드백으로 분류기 온라인 학습
        if classifier is not None:
            for f in c.feedbacks:
                classifier.update_prototype("constructive", f.comment_text)
                classifier.update_prototype(c.topic_category, f.comment_text)

    from .orchestrator import advance  # 순환 import 회피
    from . import edit_plan

    # 편집·소리 의견 → 다음 버전 러프컷의 편집 설정 (장면은 그대로 재사용된다)
    plan, _manual = edit_plan.next_plan(round_obj)
    music = _next_music(round_obj, plan.pop("music", ""))  # 배경음악 바꾸기는 한 번만 적용하고 지운다
    plan.pop("subtitle_times", None)  # 타임라인 자막 시간은 이 버전 영상의 시각 — 장면 길이가 바뀌는 다음 버전엔 맞지 않는다
    plan = edit_plan.normalize(plan)
    advance(round_obj, "prompt_updated", commit=False)
    next_round = VideoRound(
        session_id=round_obj.session_id, round_number=next_number,
        status="prompt_built", prompt_text=new_text,
        edit_json="" if edit_plan.is_default(plan) else edit_plan.dumps(plan), music_json=music)
    db.session.add(next_round)
    db.session.flush()
    return next_round, source


def _next_music(round_obj, change):
    """다음 버전의 배경음악 설정 (JSON). 기본은 같은 곡 그대로 — on/off/reroll 의견이 있을 때만 바꾼다."""
    import json
    import random

    from . import bgm

    settings = bgm.load(round_obj)
    if change == "on":
        settings["enabled"] = True
    elif change == "off":
        settings["enabled"] = False
    elif change == "reroll":  # 다른 곡: 시드를 바꾸고 곡 설명은 다시 제안받는다
        settings.update(enabled=True, seed=random.randint(1, 2**31 - 1), file="", key="", seconds=0.0, caption="")
    if not (settings["enabled"] or settings["caption"] or settings["file"]):
        return ""
    return json.dumps(settings, ensure_ascii=False)


def rollback_to_version(session_id, version_id):
    """지정 버전의 내용으로 현재 라운드 프롬프트를 되돌리고 새 버전으로 기록."""
    session = db.session.get(PipelineSession, session_id)
    target = PromptVersion.query.filter_by(id=version_id, session_id=session_id).first()
    if target is None:
        raise ValueError("버전을 찾을 수 없습니다.")
    current = session.current_round
    version = add_version(
        session_id, current.id, current.prompt_text, target.content,
        f"v{target.version_number}으로 롤백")
    current.prompt_text = target.content
    return version

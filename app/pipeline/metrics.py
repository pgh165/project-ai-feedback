"""라운드별 지표 — 반영률, 시간 절약."""
from ..models import Metric, ShotRender, db


def calculate_metrics(round_obj, config):
    feedbacks = round_obj.feedbacks
    constructive = [f for f in feedbacks if f.is_constructive]
    approved = [c for c in round_obj.clusters if c.status == "approved"]
    reflected = sum(c.feedback_count for c in approved)

    manual_minutes = len(feedbacks) * config["MANUAL_TIME_PER_FEEDBACK_MINUTES"]
    auto_minutes = (round_obj.processing_seconds or 0.0) / 60.0

    metric = Metric.query.filter_by(round_id=round_obj.id).first() or Metric(round_id=round_obj.id)
    metric.total_feedback = len(feedbacks)
    metric.constructive_count = len(constructive)
    metric.cluster_count = len(round_obj.clusters)
    metric.approved_count = len(approved)
    metric.reflected_feedback_count = reflected
    metric.reflection_rate = round(reflected / len(constructive), 3) if constructive else 0.0
    metric.time_saved_minutes = round(max(manual_minutes - auto_minutes, 0.0), 1)

    # 부분 재생성: 선택된 결과 중 재사용 vs 새로 생성, 전체 재생성 대비 절감 시간
    selected = [r for r in round_obj.shot_renders if r.selected]
    reused = [r for r in selected if r.status == "reused"]
    metric.shots_total = len(selected)
    metric.shots_reused = len(reused)
    metric.shots_regenerated = len(selected) - len(reused)
    metric.regen_saved_seconds = round(sum(
        (db.session.get(ShotRender, r.reused_from_id).elapsed_sec or 0.0)
        for r in reused if r.reused_from_id), 1)
    # 상용 API 전환: 장면 수·누적 비용 (이 라운드의 모든 api 버전)
    api_renders = [r for r in round_obj.shot_renders if r.generator == "api" and r.status == "done"]
    metric.api_shots = len({r.shot_id for r in api_renders})
    metric.api_cost_usd = round(sum(r.cost_usd for r in api_renders), 4)
    db.session.add(metric)
    return metric

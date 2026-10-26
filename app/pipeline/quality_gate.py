"""[팀 직접 구현] 품질 부족 장면 → 상용 API 재생성의 안전장치.

- 예상 비용 계산, 세션 누적 비용, 예산 상한 검사
- 서버가 비용을 다시 계산해 사용자가 확인한 금액과 다르면 거부 (409)
- 같은 장면에 visual·technical 클러스터가 2라운드 연속 재발하면 "API 권장" 배지
상용 API는 비용이 드는 작업이라 이 모듈을 통과한 요청만 어댑터로 전달된다.
"""
from ..models import ClusterResult, ShotRender, VideoRound, db

QUALITY_REASONS = {
    "shape": "형태 붕괴",
    "face_hands": "손·얼굴 뭉개짐",
    "flicker": "깜빡임",
    "prompt_mismatch": "프롬프트 불일치",
    "other": "기타",
}
_RECOMMEND_CATEGORIES = {"visual", "technical"}
_COST_TOLERANCE_USD = 0.01


class EscalationError(Exception):
    """상용 재생성 요청 거부. http_status는 라우트가 그대로 응답한다."""

    def __init__(self, code, message, http_status=409, **extra):
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.extra = extra


def estimate_cost(duration_sec, config):
    return round(float(duration_sec) * config["COMMERCIAL_COST_PER_SEC_USD"], 4)


def session_spent(session_id):
    rows = (db.session.query(db.func.coalesce(db.func.sum(ShotRender.cost_usd), 0.0))
            .join(VideoRound, VideoRound.id == ShotRender.round_id)
            .filter(VideoRound.session_id == session_id, ShotRender.status == "done").scalar())
    return round(float(rows or 0.0), 4)


def budget_summary(session_id, config):
    spent = session_spent(session_id)
    budget = float(config["COMMERCIAL_BUDGET_USD"])
    return {"spent_usd": spent, "budget_usd": budget, "remaining_usd": round(budget - spent, 4),
            "cost_per_sec_usd": config["COMMERCIAL_COST_PER_SEC_USD"]}


def validate_escalation(round_obj, storyboard, shot_ids, confirm_cost_usd, config,
                        commercial_ready):
    """통과하면 서버가 계산한 비용을 반환, 아니면 EscalationError."""
    if not commercial_ready:
        raise EscalationError("commercial_unavailable",
                              "상용 API 키가 없어 재생성할 수 없습니다.", 409)
    if round_obj.status != "video_ready":
        raise EscalationError("bad_state", f"현재 단계({round_obj.status})에서는 재생성할 수 없습니다.", 409)
    if not shot_ids:
        raise EscalationError("no_shots", "재생성할 장면을 선택하세요.", 400)
    known = {s["id"]: s for s in storyboard["shots"]}
    unknown = [s for s in shot_ids if s not in known]
    if unknown:
        raise EscalationError("unknown_shot", f"없는 장면: {', '.join(unknown)}", 400)

    cost = round(sum(estimate_cost(known[s]["duration"], config) for s in shot_ids), 4)
    if confirm_cost_usd is None or abs(float(confirm_cost_usd) - cost) > _COST_TOLERANCE_USD:
        raise EscalationError("cost_mismatch", "예상 비용이 달라졌습니다. 다시 확인하세요.", 409,
                              server_cost_usd=cost)
    budget = budget_summary(round_obj.session_id, config)
    if cost > budget["remaining_usd"] + 1e-9:
        raise EscalationError("budget_exceeded", "세션 예산을 초과합니다.", 409,
                              server_cost_usd=cost, remaining_usd=budget["remaining_usd"])
    return cost


def recommend_api_shots(round_obj):
    """visual·technical 클러스터가 같은 장면에서 2라운드 연속 재발했는지 → 장면 ID 집합."""
    prior = (VideoRound.query
             .filter(VideoRound.session_id == round_obj.session_id,
                     VideoRound.round_number < round_obj.round_number)
             .order_by(VideoRound.round_number.desc()).limit(2).all())
    if len(prior) < 2:
        return set()

    def hit(r, require_reoccurrence):
        shots = set()
        for c in ClusterResult.query.filter_by(round_id=r.id).all():
            if c.topic_category not in _RECOMMEND_CATEGORIES:
                continue
            if require_reoccurrence and c.reoccurrence_count < 1:
                continue
            shots |= set(c.affected_shots)
        return shots

    return hit(prior[0], True) & hit(prior[1], False)

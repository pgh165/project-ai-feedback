"""[팀 직접 구현] 5축 우선순위 공식 (v2).

P = α·freq + β·quality + γ·severity + δ·urgency + ε·validity
상반 페널티: contradicted_by_key가 있으면 × 0.60
"""
_CONTRADICTION_PENALTY = 0.60


def assign_priorities(clusters, alpha, beta, gamma, delta, epsilon):
    if not clusters:
        return clusters
    max_count = max(c["feedback_count"] for c in clusters)
    for c in clusters:
        score = (
            alpha * (c["feedback_count"] / max_count)
            + beta * c["avg_quality_score"]
            + gamma * c["avg_severity_score"]
            + delta * c["avg_urgency_score"]
            + epsilon * c["avg_validity_score"]
        )
        if c.get("contradicted_by_key"):
            score *= _CONTRADICTION_PENALTY
        c["priority_score"] = round(score, 4)
    return rerank(clusters)


def rerank(clusters):
    clusters.sort(key=lambda c: c["priority_score"], reverse=True)
    for rank, c in enumerate(clusters, start=1):
        c["rank"] = rank
    return clusters

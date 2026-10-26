"""클러스터링 파이프라인 오케스트레이터: 임베딩 → 주제 태깅 → 태그 내 군집화 → 우선순위."""
import numpy as np

from .embedder import get_embeddings
from .hierarchical import cluster_group, mark_contradictions
from .ranker import assign_priorities
from .tagger import group_by_topic


def run_clustering(texts, topic_categories, validity_scores, severity_scores,
                   urgency_scores, quality_scores, config):
    """건설적 피드백 목록 → 우선순위가 매겨진 클러스터 dict 목록.

    texts는 comment_text 원문이어야 한다 (요약된 improvement_suggestion은 의미 손실).
    """
    if not texts:
        return []
    embeddings = get_embeddings(
        texts, config["EMBEDDING_MODEL"], config["EMBEDDING_FALLBACK_MODEL"])

    clusters = []
    for category, indices in group_by_topic(topic_categories).items():
        clusters.extend(cluster_group(
            category, indices, texts, embeddings, quality_scores,
            config["CLUSTERING_DISTANCE_THRESHOLD"]))

    for c in clusters:
        idx = c["member_indices"]
        c["feedback_count"] = len(idx)
        c["avg_validity_score"] = round(float(np.mean([validity_scores[i] for i in idx])), 3)
        c["avg_severity_score"] = round(float(np.mean([severity_scores[i] for i in idx])), 3)
        c["avg_urgency_score"] = round(float(np.mean([urgency_scores[i] for i in idx])), 3)
        c["avg_quality_score"] = round(float(np.mean([quality_scores[i] for i in idx])), 3)

    mark_contradictions(clusters)
    return assign_priorities(
        clusters, config["PRIORITY_ALPHA"], config["PRIORITY_BETA"], config["PRIORITY_GAMMA"],
        config["PRIORITY_DELTA"], config["PRIORITY_EPSILON"])

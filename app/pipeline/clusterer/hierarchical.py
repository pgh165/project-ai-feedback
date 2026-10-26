"""[팀 직접 구현] 2단계 클러스터링의 2단계 — 태그 내 군집화 (v2).

군집화 알고리즘 자체(HDBSCAN, AgglomerativeClustering)는 [오픈소스].
"""
import re
from collections import Counter

import numpy as np
from sklearn.cluster import AgglomerativeClustering

try:
    import hdbscan  # [오픈소스] 설치 시 우선 사용

    _HAS_HDBSCAN = True
except ImportError:
    _HAS_HDBSCAN = False

# 카테고리별 적응형 코사인 거리 임계값 (기술 이슈는 좁게, 표현이 다양한 주제는 넓게)
# story는 시청자 참여형 드라마에서 "어떤 전개를 원하는가"가 요청마다 갈려야 하므로
# 넓게(0.48) 잡으면 서로 다른 전개 요청이 한 클러스터로 뭉친다 → 0.34로 좁힘.
_CATEGORY_THRESHOLDS = {
    "technical": 0.26,
    "subtitle": 0.30,
    "story": 0.34,
    "visual": 0.36,
    "audio": 0.38,
    "pacing": 0.42,
    "content": 0.44,
}
_SUBCLUSTER_MIN_COUNT = 15
_SUBCLUSTER_THRESHOLD_SCALE = 0.75
# 밀도 추정이 의미 있으려면 표본이 필요 — 이보다 작은 그룹은 계층적 군집화 사용
_HDBSCAN_MIN_POINTS = 12

# 상반된 피드백 감지용 키워드 쌍 (pole A, pole B)
_CONTRADICTION_PAIRS = [
    (r"빠르|빨라|빨리|급하|급해|too fast|rushed|quick", r"느리|느려|지루|늘어|too slow|drag|boring"),
    (r"길어|길다|너무 긴|too long", r"짧아|짧다|too short"),
    (r"시끄|소리가 커|음악이 커|너무 커|too loud", r"안 들|작아|too quiet|can't hear"),
    (r"어둡|어두|too dark", r"밝아|눈부|too bright"),
]
_CONTRADICTION_REGEX = [(re.compile(a, re.I), re.compile(b, re.I)) for a, b in _CONTRADICTION_PAIRS]

_STOPWORDS = {
    "너무", "정말", "진짜", "그냥", "조금", "좀", "더", "것", "수", "그리고", "근데", "이번", "다음",
    "the", "and", "is", "it", "to", "of", "in", "that", "this", "was", "too", "be", "for", "with",
    "are", "but", "so", "on", "we", "you", "they", "she", "he", "her", "his",
}


def _agglomerative(embeddings, threshold):
    if len(embeddings) < 2:
        return np.zeros(len(embeddings), dtype=int)
    return AgglomerativeClustering(
        n_clusters=None, metric="cosine", linkage="average", distance_threshold=threshold,
    ).fit_predict(embeddings)


def _cluster_embeddings(embeddings, threshold):
    """HDBSCAN 우선 / AgglomerativeClustering 폴백. 노이즈는 가장 가까운 중심으로 재배정."""
    n = len(embeddings)
    if not _HAS_HDBSCAN or n < _HDBSCAN_MIN_POINTS:
        return _agglomerative(embeddings, threshold)

    # 정규화 벡터에서 유클리드 거리² = 2 × 코사인 거리
    labels = hdbscan.HDBSCAN(
        min_cluster_size=2, min_samples=1, metric="euclidean",
        cluster_selection_epsilon=float(np.sqrt(2 * threshold) * 0.5),
    ).fit_predict(embeddings.astype(np.float64))

    found = sorted(set(labels) - {-1})
    if not found:
        return np.zeros(n, dtype=int)  # 모두 노이즈 → 단일 클러스터
    centroids = np.stack([embeddings[labels == k].mean(axis=0) for k in found])
    centroids /= np.linalg.norm(centroids, axis=1, keepdims=True) + 1e-12
    for i in np.where(labels == -1)[0]:
        labels[i] = found[int(np.argmax(centroids @ embeddings[i]))]
    return labels


def _compute_cohesion(embeddings):
    """클러스터 응집도 — 평균 쌍별 코사인 유사도."""
    n = len(embeddings)
    if n < 2:
        return 1.0
    sims = embeddings @ embeddings.T
    return float((sims.sum() - np.trace(sims)) / (n * (n - 1)))


def _select_representative(texts, embeddings, quality_scores):
    """품질 가중 대표 텍스트 — 중심근접도 50% + 품질 30% + 길이 20%."""
    centroid = embeddings.mean(axis=0)
    centroid /= np.linalg.norm(centroid) + 1e-12
    closeness = embeddings @ centroid
    span = closeness.max() - closeness.min()
    closeness = (closeness - closeness.min()) / span if span > 1e-9 else np.ones_like(closeness)
    length = np.array([min(len(t) / 80.0, 1.0) for t in texts])
    score = 0.5 * closeness + 0.3 * np.asarray(quality_scores) + 0.2 * length
    return int(np.argmax(score))


def _words(text):
    return [w for w in re.findall(r"[가-힣]{2,}|[a-zA-Z]{3,}", text.lower()) if w not in _STOPWORDS]


def _generate_cluster_summary(texts):
    """단어 빈도 기반 추출 요약 — 핵심어 + 핵심어를 가장 많이 담은 문장."""
    freq = Counter(w for t in texts for w in set(_words(t)))
    if not freq:
        return texts[0][:120]
    keywords = [w for w, _ in freq.most_common(3)]
    best, best_score = texts[0], -1.0
    for t in texts:
        for sent in re.split(r"(?<=[.!?。])\s+|\n+", t):
            words = _words(sent)
            if not words:
                continue
            score = sum(freq[w] for w in set(words)) / (len(words) ** 0.5)
            if score > best_score:
                best, best_score = sent.strip(), score
    return f"[{', '.join(keywords)}] {best[:150]}"


def _detect_poles(texts):
    poles = {}
    for i, (a, b) in enumerate(_CONTRADICTION_REGEX):
        a_hits = sum(1 for t in texts if a.search(t))
        b_hits = sum(1 for t in texts if b.search(t))
        if a_hits != b_hits and max(a_hits, b_hits) > 0:
            poles[i] = "a" if a_hits > b_hits else "b"
    return poles


def mark_contradictions(clusters):
    """같은 카테고리에서 반대 극을 가진 클러스터 쌍에 contradicted_by_key 표시."""
    for c in clusters:
        c["_poles"] = _detect_poles(c["member_texts"])
    for c in clusters:
        for other in clusters:
            if other is c or other["topic_category"] != c["topic_category"]:
                continue
            if any(other["_poles"].get(i) not in (None, pole) for i, pole in c["_poles"].items()):
                c["contradicted_by_key"] = other["cluster_key"]
                break
    for c in clusters:
        del c["_poles"]
    return clusters


def cluster_group(category, indices, texts, embeddings, quality_scores, base_threshold):
    """한 주제 그룹을 군집화해 클러스터 dict 목록을 반환."""
    indices = list(indices)
    group_emb = embeddings[indices]
    threshold = _CATEGORY_THRESHOLDS.get(category, base_threshold)
    labels = _cluster_embeddings(group_emb, threshold)

    groups = [[indices[i] for i in np.where(labels == k)[0]] for k in sorted(set(labels))]

    # 대형 클러스터 자동 서브클러스터링: 분할 후 모든 조각이 2개 이상일 때만 채택
    final_groups = []
    for members in groups:
        if len(members) > _SUBCLUSTER_MIN_COUNT:
            sub = _agglomerative(embeddings[members], threshold * _SUBCLUSTER_THRESHOLD_SCALE)
            parts = [[members[i] for i in np.where(sub == k)[0]] for k in sorted(set(sub))]
            if len(parts) > 1 and all(len(p) >= 2 for p in parts):
                final_groups.extend(parts)
                continue
        final_groups.append(members)

    clusters = []
    for n, members in enumerate(final_groups, start=1):
        m_emb = embeddings[members]
        m_texts = [texts[i] for i in members]
        rep = _select_representative(m_texts, m_emb, [quality_scores[i] for i in members])
        clusters.append({
            "cluster_key": f"{category}-{n}",
            "topic_category": category,
            "member_indices": members,
            "member_texts": m_texts,
            "representative_text": m_texts[rep],
            "cohesion_score": round(_compute_cohesion(m_emb), 3),
            "cluster_summary": _generate_cluster_summary(m_texts),
            "contradicted_by_key": None,
        })
    return clusters

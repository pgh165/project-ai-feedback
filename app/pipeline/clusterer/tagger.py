"""[팀 직접 구현] 2단계 클러스터링의 1단계 — 주제 태그별 그룹화."""
from collections import defaultdict


def group_by_topic(topic_categories):
    """list[str] → {category: [index, ...]}"""
    groups = defaultdict(list)
    for idx, category in enumerate(topic_categories):
        groups[category or "content"].append(idx)
    return dict(groups)

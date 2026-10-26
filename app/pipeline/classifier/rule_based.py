"""키워드 규칙 기반 폴백 분류기 (3순위, 완전 오프라인)."""
from .base import BaseClassifier
from .embedding_classifier import build_result, keyword_topic_scores


class RuleBasedClassifier(BaseClassifier):
    name = "rule_based"

    def __init__(self, specificity_threshold, validity_threshold):
        self.spec_thr = specificity_threshold
        self.val_thr = validity_threshold

    def _semantic_scores(self, texts):
        # 의미 점수 없음 → 중립값. 타당성은 규칙 신호만으로 결정된다.
        return [0.5] * len(texts)

    def classify_batch(self, texts):
        texts = list(texts)
        semantic = self._semantic_scores(texts)
        return [
            build_result(t, keyword_topic_scores(t), s, self.spec_thr, self.val_thr, self.name)
            for t, s in zip(texts, semantic)
        ]

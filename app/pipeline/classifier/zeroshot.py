"""NLI zero-shot 분류기 (2순위 폴백). 모델 로드 실패 시 factory가 규칙 기반으로 넘어간다."""
from .rule_based import RuleBasedClassifier

_LABELS = ["constructive feedback with a concrete suggestion or problem", "casual reaction or spam"]


class ZeroShotClassifier(RuleBasedClassifier):
    name = "zeroshot"

    def __init__(self, model_name, specificity_threshold, validity_threshold):
        super().__init__(specificity_threshold, validity_threshold)
        # [오픈소스] transformers zero-shot-classification 파이프라인
        from transformers import pipeline

        self._nli = pipeline("zero-shot-classification", model=model_name, device=-1)

    def _semantic_scores(self, texts):
        scores = []
        for out in self._nli(texts, _LABELS):
            scores.append(float(dict(zip(out["labels"], out["scores"]))[_LABELS[0]]))
        return scores

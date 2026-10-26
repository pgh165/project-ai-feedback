from abc import ABC, abstractmethod
from dataclasses import dataclass, field

TOPIC_CATEGORIES = ["story", "content", "pacing", "visual", "audio", "subtitle", "technical"]


@dataclass
class ClassificationResult:
    """6축 분류 결과: 구체성·타당성·심각도·긴급도·품질·멀티토픽."""

    is_constructive: bool
    specificity_score: float
    validity_score: float
    severity_score: float
    urgency_score: float
    quality_score: float
    topic_category: str
    topic_categories_multi: list = field(default_factory=list)  # [(category, score), ...]
    improvement_suggestion: str = ""
    language: str = ""
    needs_review: bool = False
    classifier_name: str = ""


class BaseClassifier(ABC):
    name = "base"

    @abstractmethod
    def classify_batch(self, texts):
        """list[str] → list[ClassificationResult]"""

    def classify(self, text):
        return self.classify_batch([text])[0]

    def update_prototype(self, label, text, alpha=0.1):
        """승인된 피드백으로 온라인 학습 — 지원하지 않는 분류기는 무시."""
        return False

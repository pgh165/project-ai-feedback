import logging

from .rule_based import RuleBasedClassifier

log = logging.getLogger(__name__)
_cache = {}

# EmbeddingClassifier는 규칙 기반 점수 비중이 커서 NLI보다 임계값을 낮춰 적용한다
_EMBEDDING_SPECIFICITY_SCALE = 0.75
_EMBEDDING_VALIDITY_SCALE = 0.70


def create_classifier(config, state_dir=None):
    """EmbeddingClassifier → ZeroShotClassifier → RuleBasedClassifier 순으로 자동 폴백."""
    spec, val = config["SPECIFICITY_THRESHOLD"], config["VALIDITY_THRESHOLD"]
    key = (config["CLASSIFIER_MODEL"], config["NLI_MODEL"], spec, val, state_dir)
    if key in _cache:
        return _cache[key]

    classifier = None
    try:
        from .embedding_classifier import EmbeddingClassifier

        classifier = EmbeddingClassifier(
            config["CLASSIFIER_MODEL"],
            spec * _EMBEDDING_SPECIFICITY_SCALE,
            val * _EMBEDDING_VALIDITY_SCALE,
            state_dir=state_dir,
        )
    except Exception as exc:
        log.warning("EmbeddingClassifier 로드 실패 → NLI 폴백: %s", exc)

    if classifier is None:
        try:
            from .zeroshot import ZeroShotClassifier

            classifier = ZeroShotClassifier(config["NLI_MODEL"], spec, val)
        except Exception as exc:
            log.warning("ZeroShotClassifier 로드 실패 → 규칙 기반 폴백: %s", exc)
            classifier = RuleBasedClassifier(spec, val)

    _cache[key] = classifier
    return classifier

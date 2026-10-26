"""다국어 문장 임베딩 — 모듈 레벨 캐시, mpnet → MiniLM → TF-IDF 폴백."""
import logging

import numpy as np

log = logging.getLogger(__name__)
_models = {}


def _load(model_name):
    if model_name not in _models:
        # [오픈소스] sentence-transformers — 로컬 CPU 실행
        from sentence_transformers import SentenceTransformer

        _models[model_name] = SentenceTransformer(model_name, device="cpu")
    return _models[model_name]


def _tfidf(texts):
    from sklearn.feature_extraction.text import TfidfVectorizer

    mat = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4)).fit_transform(texts).toarray()
    return mat / (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-12)


def get_embeddings(texts, model_name, fallback_model_name=None):
    """L2 정규화된 임베딩 행렬 (n, dim) 반환."""
    texts = list(texts)
    if not texts:
        return np.zeros((0, 1))
    for name in (model_name, fallback_model_name):
        if not name:
            continue
        try:
            return np.asarray(_load(name).encode(texts, normalize_embeddings=True))
        except Exception as exc:
            log.warning("임베딩 모델 %s 사용 불가 → 폴백: %s", name, exc)
    return _tfidf(texts)

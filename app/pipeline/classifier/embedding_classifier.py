"""[팀 직접 구현] v2 prototype 코사인 유사도 분류기.

원형(prototype) 문장 임베딩과의 코사인 유사도 + 규칙 신호(regex)를 결합해
건설적/비건설적 여부와 6축 점수를 산정한다. 임베딩 모델 자체는 [오픈소스].
"""
import os
import re

import numpy as np

from .base import TOPIC_CATEGORIES, BaseClassifier, ClassificationResult

# ── 원형 문장 (한국어+영어) ───────────────────────────────────────────────
_PROTOTYPES = {
    "constructive": [
        "자막이 너무 빨리 지나가서 읽기 힘들어요. 조금 더 오래 띄워주세요.",
        "배경음악이 대사보다 커서 잘 안 들립니다. 음악 볼륨을 줄였으면 좋겠어요.",
        "주인공이 갑자기 화해하는 전개가 어색했어요. 이유를 보여주는 장면이 필요해요.",
        "다음 화에서는 두 사람의 과거 이야기를 더 보여주면 좋겠습니다.",
        "장면 전환이 너무 급해서 내용을 따라가기 어려웠어요.",
        "화면이 너무 어두워서 인물 표정이 안 보여요.",
        "The subtitles disappear too fast, please keep them on screen longer.",
        "The music drowns out the dialogue, it should be quieter.",
        "The ending felt rushed, I wish we saw why she changed her mind.",
        "It would be better if the next episode explained the villain's motive.",
        "The scene is too dark to see the actors' faces.",
        "The pacing drags in the middle, the cafe scene could be shorter.",
    ],
    "non_constructive": [
        "ㅋㅋㅋ 재밌네요",
        "잘 보고 갑니다",
        "1등!",
        "최고예요 사랑해요",
        "노잼",
        "이게 뭐야 별로",
        "구독하고 갑니다",
        "lol nice",
        "first!",
        "love this so much",
        "this is trash",
        "who's watching in 2026",
        "great video",
    ],
    "story": [
        "주인공의 선택이 이해가 안 돼요. 다음 화에서 반전이 있었으면 좋겠어요.",
        "결말이 너무 뻔해요. 악역의 사연을 보여주세요.",
        "The plot twist came out of nowhere, the characters need more development.",
        "I want the two leads to end up together in the next episode.",
    ],
    "content": [
        "내용 설명이 부족해서 무슨 상황인지 모르겠어요.",
        "소재는 좋은데 분량이 너무 짧아요.",
        "The episode doesn't explain what is happening.",
    ],
    "pacing": [
        "전개가 너무 빨라서 따라가기 힘들어요.",
        "중간 부분이 늘어져서 지루했어요.",
        "The pacing is too slow, it drags in the middle.",
        "Everything felt rushed.",
    ],
    "visual": [
        "화면이 너무 어둡고 색감이 탁해요.",
        "인물 얼굴이 장면마다 달라 보여요.",
        "The lighting is too dark and the camera angle is awkward.",
        "The character's face keeps changing between shots.",
    ],
    "audio": [
        "배경음악이 너무 커서 목소리가 안 들려요.",
        "효과음이 갑자기 커져서 놀랐어요.",
        "The background music is louder than the voices.",
    ],
    "subtitle": [
        "자막 글씨가 너무 작고 빨리 사라져요.",
        "The subtitles are too small and disappear too quickly.",
    ],
    "technical": [
        "영상이 중간에 끊기고 화질이 깨져요.",
        "소리와 화면 싱크가 안 맞아요.",
        "The video glitches and the audio is out of sync.",
    ],
}

# ── 규칙 신호 ────────────────────────────────────────────────────────────
_F = re.IGNORECASE
_SPECIFICITY_SIGNALS = [re.compile(p, _F) for p in (
    r"\d+\s*(초|분|화|편|번째)|\d+:\d{2}|\b\d+\s*(sec|second|min|minute)",
    r"너무|조금|좀\s?더|훨씬|덜\s|더\s|\btoo\b|\bmore\b|\bless\b|a bit|slightly|\bmuch\b",
    r"장면|씬|대사|자막|음악|목소리|결말|엔딩|주인공|캐릭터|인물|컷|전환|"
    r"scene|dialogue|subtitle|music|voice|ending|character|\bcut\b|transition|\bshot\b",
    r"처음|중간|마지막|초반|후반|도입|at the (start|end|beginning)|halfway|intro|outro|middle",
)]
_VALIDITY_SIGNALS = [re.compile(p, _F) for p in (
    r"면\s?좋겠|으면\s?(좋|합니다|해요|한다)|었으면|였으면|했으면|주면\s?좋",
    r"해\s?주세요|해줘|주세요|바랍니다|부탁|필요(해|합|하)|추천|제안",
    r"\bshould\b|could you|would be (better|nice|great)|\bplease\b|needs? to|"
    r"i (wish|suggest|recommend|want)|it would help|how about",
    r"아서|어서|해서|때문에|라서|\bbecause\b|\bsince\b|so that|due to",
    r"힘들|어렵|안\s?들|안\s?보|어색|이상해|불편|아쉽|부족|지루|헷갈|산만|답답|뻔해|이해가 안|"
    r"hard to|difficult|can't|cannot|confusing|awkward|distracting|boring|unclear|\blacks?\b|rushed|drags?",
)]
_SUGGESTION_SIGNALS = _VALIDITY_SIGNALS[:3]
_SEVERITY_SIGNALS = [re.compile(p, _F) for p in (
    r"전혀|아예|도저히|못\s?(보|듣|읽)|불가능|최악|심각|끔찍|"
    r"completely|totally|unwatchable|impossible|terrible|worst|at all",
    r"깨[지져짐]|끊[기겨김]|오류|버그|안\s?나와|싱크|broken|glitch|\bbug\b|out of sync|crash|corrupt",
    r"껐|나갔|이탈|하차|포기|stopped watching|gave up|\bquit\b|clicked away",
)]
_URGENCY_SIGNALS = [re.compile(p, _F) for p in (
    r"당장|빨리\s?고|즉시|시급|얼른|asap|immediately|right away|urgent",
    r"다음\s?(화|편|영상|회)|next (episode|video|one|time)",
    r"계속|매번|여전히|아직도|every time|\bagain\b|\bstill\b|\balways\b|\bkeeps?\b",
)]
_TOPIC_KEYWORDS = {
    "story": ["스토리", "줄거리", "전개", "결말", "엔딩", "반전", "주인공", "캐릭터", "인물", "복선",
              "개연성", "떡밥", "러브라인", "악역", "서사", "사연", "과거", "story", "plot", "ending",
              "twist", "character", "protagonist", "villain", "romance", "backstory", "motive"],
    "content": ["내용", "주제", "정보", "설명", "소재", "분량", "content", "topic", "information", "explain"],
    "pacing": ["속도", "템포", "빠르", "빨라", "빨리", "느리", "느려", "지루", "길어", "길다", "늘어",
               "급하", "급해", "급전개", "pacing", "pace", "fast", "slow", "rushed", "drag", "too long"],
    "visual": ["화면", "영상미", "색감", "조명", "어둡", "어두", "밝", "화질", "구도", "카메라", "표정",
               "배경", "의상", "얼굴", "visual", "color", "lighting", "dark", "bright", "camera",
               "framing", "face"],
    "audio": ["소리", "음악", "배경음", "bgm", "목소리", "음량", "볼륨", "효과음", "더빙", "audio",
              "sound", "music", "voice", "volume", "loud", "quiet"],
    "subtitle": ["자막", "글씨", "폰트", "subtitle", "caption", "font"],
    "technical": ["버퍼", "렉", "끊기", "끊겨", "깨져", "깨지", "오류", "버그", "싱크", "해상도", "인코딩",
                  "재생", "로딩", "buffer", "lag", "glitch", "bug", "sync", "resolution", "encoding",
                  "playback"],
}
# 동점 시 더 구체적인 카테고리 우선
_TOPIC_PRIORITY = ["subtitle", "technical", "audio", "visual", "story", "pacing", "content"]
_TOPIC_REGEX = {
    cat: [re.compile((r"\b" + re.escape(k)) if k.isascii() else re.escape(k), _F) for k in kws]
    for cat, kws in _TOPIC_KEYWORDS.items()
}


def _hits(patterns, text):
    return sum(1 for p in patterns if p.search(text))


def _detect_lang(text):
    """한국어·영어·일본어·중국어·혼합어 판별."""
    counts = {"ko": 0, "ja": 0, "zh": 0, "en": 0}
    for ch in text:
        code = ord(ch)
        if 0xAC00 <= code <= 0xD7A3 or 0x3131 <= code <= 0x318E:
            counts["ko"] += 1
        elif 0x3040 <= code <= 0x30FF:
            counts["ja"] += 1
        elif 0x4E00 <= code <= 0x9FFF:
            counts["zh"] += 1
        elif ch.isascii() and ch.isalpha():
            counts["en"] += 1
    total = sum(counts.values())
    if total == 0:
        return "unknown"
    if counts["ja"] and counts["zh"]:  # 한자 + 가나 = 일본어
        counts["ja"] += counts["zh"]
        counts["zh"] = 0
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    if ranked[1][1] / total >= 0.3:
        return "mixed"
    return ranked[0][0]


def _token_count(text, lang):
    if lang in ("ja", "zh"):
        return max(1, len(text) // 3)
    return len(text.split())


def _split_sentences(text):
    parts = re.split(r"(?<=[.!?。…])\s+|\n+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def _extract_suggestion(text):
    """점수 기반 제안 추출 — 제안·문제 신호가 가장 많은 문장을 고른다."""
    best, best_score = "", 0
    for sent in _split_sentences(text):
        score = 2 * _hits(_SUGGESTION_SIGNALS, sent) + _hits(_VALIDITY_SIGNALS, sent) \
            + _hits(_SPECIFICITY_SIGNALS, sent)
        if score > best_score:
            best, best_score = sent, score
    has_suggestion = _hits(_SUGGESTION_SIGNALS, text) > 0
    return best[:200], has_suggestion


def keyword_topic_scores(text):
    return {
        cat: min(sum(1 for r in regs if r.search(text)) / 2.0, 1.0)
        for cat, regs in _TOPIC_REGEX.items()
    }


def _detect_topics_multi(topic_scores):
    """멀티레이블 토픽 감지 — 단일 댓글에서 최대 3개 카테고리."""
    ranked = sorted(
        topic_scores.items(),
        key=lambda kv: (-kv[1], _TOPIC_PRIORITY.index(kv[0])),
    )
    top_cat, top_score = ranked[0]
    if top_score < 0.15:
        return "content", []
    multi = [(c, round(float(s), 3)) for c, s in ranked if s >= 0.35 and s >= 0.5 * top_score][:3]
    return top_cat, multi or [(top_cat, round(float(top_score), 3))]


def _compute_quality_score(specificity, validity, tokens, has_suggestion):
    return float(np.clip(
        0.40 * specificity + 0.30 * validity + 0.15 * min(tokens / 20.0, 1.0)
        + 0.15 * (1.0 if has_suggestion else 0.0), 0, 1))


def build_result(text, topic_scores, emb_part, spec_thr, val_thr, name):
    """규칙 신호 + (선택) 임베딩 점수를 6축 결과로 결합. 모든 분류기가 공유."""
    lang = _detect_lang(text)
    tokens = _token_count(text, lang)
    topic, multi = _detect_topics_multi(topic_scores)
    top_strength = max(topic_scores.values())

    specificity = float(np.clip(
        0.25 * min(tokens / 12.0, 1.0)
        + 0.45 * min(_hits(_SPECIFICITY_SIGNALS, text) / 2.0, 1.0)
        + 0.30 * top_strength, 0, 1))
    validity = float(np.clip(
        0.55 * emb_part + 0.45 * min(_hits(_VALIDITY_SIGNALS, text) / 2.0, 1.0), 0, 1))
    severity = min(1.0, 0.2 + 0.4 * _hits(_SEVERITY_SIGNALS, text))
    urgency = min(1.0, 0.1 + 0.4 * _hits(_URGENCY_SIGNALS, text))
    suggestion, has_suggestion = _extract_suggestion(text)

    return ClassificationResult(
        is_constructive=specificity >= spec_thr and validity >= val_thr,
        specificity_score=round(specificity, 3),
        validity_score=round(validity, 3),
        severity_score=round(severity, 3),
        urgency_score=round(urgency, 3),
        quality_score=round(_compute_quality_score(specificity, validity, tokens, has_suggestion), 3),
        topic_category=topic,
        topic_categories_multi=multi,
        improvement_suggestion=suggestion,
        language=lang,
        needs_review=abs(specificity - spec_thr) < 0.05 or abs(validity - val_thr) < 0.05,
        classifier_name=name,
    )


def _normalize(v):
    return v / (np.linalg.norm(v, axis=-1, keepdims=True) + 1e-12)


class EmbeddingClassifier(BaseClassifier):
    name = "embedding"

    def __init__(self, model_name, specificity_threshold, validity_threshold, state_dir=None):
        # [오픈소스] sentence-transformers — 로컬 CPU 실행
        from sentence_transformers import SentenceTransformer

        self.model = SentenceTransformer(model_name, device="cpu")
        self.spec_thr = specificity_threshold
        self.val_thr = validity_threshold
        self._labels = list(_PROTOTYPES.keys())
        self._proto = {
            label: self.model.encode(sents, normalize_embeddings=True)
            for label, sents in _PROTOTYPES.items()
        }
        self._centroid = {label: _normalize(mat.mean(axis=0)) for label, mat in self._proto.items()}
        self._state_path = None
        if state_dir:
            safe = re.sub(r"[^A-Za-z0-9_.-]", "_", model_name)
            self._state_path = os.path.join(state_dir, f"prototypes_{safe}.npz")
            self._load_state()

    def _load_state(self):
        if not os.path.exists(self._state_path):
            return
        with np.load(self._state_path) as data:
            for label in self._labels:
                if label in data and data[label].shape == self._centroid[label].shape:
                    self._centroid[label] = data[label]

    def update_prototype(self, label, text, alpha=0.1):
        """동적 프로토타입 업데이트 — 승인된 피드백으로 중심 벡터를 지수이동평균 갱신."""
        if label not in self._centroid or not text.strip():
            return False
        emb = self.model.encode([text], normalize_embeddings=True)[0]
        self._centroid[label] = _normalize((1 - alpha) * self._centroid[label] + alpha * emb)
        if self._state_path:
            np.savez(self._state_path, **self._centroid)
        return True

    def _label_similarity(self, emb, label):
        sims = self._proto[label] @ emb
        top = np.sort(sims)[-3:].mean()
        return 0.5 * float(self._centroid[label] @ emb) + 0.5 * float(top)

    def classify_batch(self, texts):
        if not texts:
            return []
        embs = self.model.encode(list(texts), normalize_embeddings=True)
        results = []
        for text, emb in zip(texts, embs):
            margin = self._label_similarity(emb, "constructive") \
                - self._label_similarity(emb, "non_constructive")
            emb_part = float(np.clip(0.5 + 2.5 * margin, 0, 1))

            sims = np.array([float(self._centroid[c] @ emb) for c in TOPIC_CATEGORIES])
            rel = np.clip((sims - sims.mean()) / 0.15, 0, 1)
            kw = keyword_topic_scores(text)
            topic_scores = {
                c: 0.65 * kw[c] + 0.35 * float(rel[i]) for i, c in enumerate(TOPIC_CATEGORIES)
            }
            results.append(build_result(text, topic_scores, emb_part,
                                        self.spec_thr, self.val_thr, self.name))
        return results

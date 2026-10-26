"""수동 실행용: 지정 시리즈(기본: 최신)의 현재 화 댓글을 분류·클러스터링해 콘솔에 출력.

사용법: .\\venv\\Scripts\\python run_cluster.py [session_id]
DB에는 쓰지 않는다 (결과 확인 전용).
"""
import sys

from app import create_app
from app.models import PipelineSession, db
from app.pipeline.classifier import create_classifier
from app.pipeline.clusterer import run_clustering


def main():
    app = create_app()
    with app.app_context():
        if len(sys.argv) > 1:
            session = db.session.get(PipelineSession, int(sys.argv[1]))
        else:
            session = PipelineSession.query.order_by(PipelineSession.id.desc()).first()
        if session is None:
            print("시리즈가 없습니다. 먼저 웹에서 시리즈를 만들고 댓글을 수집하세요.")
            return
        round_obj = session.current_round
        texts = [f.comment_text for f in round_obj.feedbacks]
        print(f"[{session.title}] {round_obj.round_number}화 — 댓글 {len(texts)}개")
        if not texts:
            return

        classifier = create_classifier(app.config, state_dir=app.instance_path)
        results = classifier.classify_batch(texts)
        kept = [(t, r) for t, r in zip(texts, results) if r.is_constructive]
        print(f"분류기: {classifier.name} — 건설적 {len(kept)}개\n")

        clusters = run_clustering(
            texts=[t for t, _ in kept],
            topic_categories=[r.topic_category for _, r in kept],
            validity_scores=[r.validity_score for _, r in kept],
            severity_scores=[r.severity_score for _, r in kept],
            urgency_scores=[r.urgency_score for _, r in kept],
            quality_scores=[r.quality_score for _, r in kept],
            config=app.config,
        )
        for c in clusters:
            flag = f"  ⚠ 상반: {c['contradicted_by_key']}" if c["contradicted_by_key"] else ""
            print(f"#{c['rank']} [{c['cluster_key']}] P={c['priority_score']:.3f} "
                  f"n={c['feedback_count']} 응집도={c['cohesion_score']:.2f}{flag}")
            for t in c["member_texts"]:
                print(f"     - {t}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()

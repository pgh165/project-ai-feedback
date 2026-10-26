import os

from flask import Flask
from sqlalchemy import inspect, text

from config import Config

from .models import db


def _migrate_existing_tables():
    """기존 테이블에 모델의 신규 컬럼을 추가한다 (SQLite ALTER TABLE ADD COLUMN)."""
    inspector = inspect(db.engine)
    for table in db.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        existing = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            col_type = column.type.compile(db.engine.dialect)
            with db.engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}'))


def create_app(config_overrides=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(Config)
    if config_overrides:
        app.config.update(config_overrides)

    os.makedirs(app.instance_path, exist_ok=True)
    os.makedirs(os.path.join(app.instance_path, "videos"), exist_ok=True)

    db.init_app(app)
    with app.app_context():
        db.create_all()
        _migrate_existing_tables()

    # 재시작 전에 'running'이던 작업은 중단 처리하고, 남은 queued 작업은 워커가 이어받는다
    from .pipeline import jobs

    jobs.recover_interrupted(app)
    if not app.config.get("JOBS_SYNC"):
        from .models import GenerationJob

        with app.app_context():
            pending = GenerationJob.query.filter_by(status="queued").count()
        if pending:
            jobs.ensure_worker(app)[1].set()

    from .routes.session import bp as session_bp

    app.register_blueprint(session_bp)
    if app.config.get("DEV_TEST_PAGE", True):  # 로컬 점검 페이지 (/dev/pipeline-test)
        from .routes.devtest import bp as devtest_bp

        app.register_blueprint(devtest_bp)
    return app

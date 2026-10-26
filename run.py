"""Flask 앱 진입점."""
from app import create_app

app = create_app()

if __name__ == "__main__":
    # 리로더는 모델을 두 번 로드하므로 끈다
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)

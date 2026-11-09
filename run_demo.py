"""시연 촬영용 데모 앱 — 실제 앱과 따로 된 작업 폴더(instance_demo: DB·영상·토큰)로 5001번 포트에 띄운다.

  .\\venv\\Scripts\\python demo_setup.py     # 처음 한 번: 데모 작업 폴더·DB 만들기
  .\\venv\\Scripts\\python run_demo.py       # 데모 앱 → http://127.0.0.1:5001
  .\\venv\\Scripts\\python demo_reset.py     # 찍기 전마다: 데모 프로젝트를 처음 상태로

실제 앱(run.py, 5000)과 함께 켜 둬도 된다. 데모에서 무엇을 눌러도 실제 프로젝트는 바뀌지 않는다.
"""
import os

os.environ["DEMO_MODE"] = "true"
os.environ["DEV_TEST_PAGE"] = "false"  # 상단 '개발용 점검'·개발 페이지는 데모 앱에 필요 없다
DEMO_INSTANCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance_demo")

from app import create_app  # noqa: E402

app = create_app(instance_path=DEMO_INSTANCE)

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=False, threaded=True)

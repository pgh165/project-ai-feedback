"""데모 앱 작업 폴더(instance_demo)를 만든다 — 실제 DB에서 데모에 필요한 프로젝트만 복사하고 영상도 복사해 실제 앱과 완전히 나눈다.

  .\\venv\\Scripts\\python demo_setup.py            # 만들기 (이미 있으면 그만둔다)
  .\\venv\\Scripts\\python demo_setup.py --force    # 지우고 다시 만들기

남기는 프로젝트: 51번(2차 = 수정 후 결과, 제목에 '[데모 소스]'를 붙여 데모 화면에서 숨김),
'[데모 소스] 팀원 상용 영상 따라 만들기 — 이전 최종'(1차 결과), '임베디드소프트웨어학과 홍보영상'(찍는 프로젝트, 데모 폴더에만 있음).
그 뒤 demo_reset.py로 데모 프로젝트를 처음 상태로 만든다. 서버를 켜 둔 채 실행해도 된다 (실제 DB는 읽기만).
"""
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REAL, DEMO = ROOT / "instance", ROOT / "instance_demo"
SOURCE_AFTER, SOURCE_PREFIX = 51, "[데모 소스]"
TITLES = ("[데모 소스] 팀원 상용 영상 따라 만들기 — 이전 최종", "임베디드소프트웨어학과 홍보영상")

if DEMO.exists():
    if "--force" not in sys.argv:
        sys.exit(f"{DEMO} 가 이미 있습니다. 다시 만들려면 --force")
    shutil.rmtree(DEMO)
(DEMO / "videos").mkdir(parents=True)
for name in ("client_secret.json", "youtube_token.json"):
    if (REAL / name).exists():
        shutil.copyfile(REAL / name, DEMO / name)
for f in REAL.glob("prototypes_*.npz"):
    shutil.copyfile(f, DEMO / f.name)
    shutil.copyfile(f, DEMO / (f.name + ".base"))  # 분류기 기준 상태 — demo_reset.py가 매번 되돌린다 (승인 때 학습하므로)

src = sqlite3.connect(REAL / "pipeline.db")
dst = sqlite3.connect(DEMO / "pipeline.db")
src.backup(dst)  # 실제 DB를 그대로 복사 (켜져 있는 서버와 충돌 없이)
src.close()

keep = {SOURCE_AFTER} | {r[0] for r in dst.execute(
    f"select id from pipeline_session where title in ({','.join('?' * len(TITLES))})", TITLES)}
marks = ",".join("?" * len(keep))
rounds = [r[0] for r in dst.execute(f"select id from video_round where session_id not in ({marks})", tuple(keep))]
if rounds:
    rm = ",".join("?" * len(rounds))
    for table in ("shot_render", "feedback", "metric", "generation_job"):
        dst.execute(f"delete from {table} where round_id in ({rm})", rounds)
    dst.execute(f"delete from prompt_change where cluster_id in (select id from cluster_result where round_id in ({rm}))", rounds)
    dst.execute(f"delete from cluster_result where round_id in ({rm})", rounds)
dst.execute(f"delete from prompt_change where version_id in (select id from prompt_version where session_id not in ({marks}))", tuple(keep))
dst.execute(f"delete from prompt_version where session_id not in ({marks})", tuple(keep))
dst.execute(f"delete from video_round where session_id not in ({marks})", tuple(keep))
dst.execute(f"delete from pipeline_session where id not in ({marks})", tuple(keep))
dst.execute("update pipeline_session set title = ? || ' ' || title where id = ? and title not like ?",
            (SOURCE_PREFIX, SOURCE_AFTER, SOURCE_PREFIX + "%"))

# 영상 파일을 데모 폴더로 복사하고 DB 안의 경로를 바꾼다 (실제 앱의 파일에 기대지 않게)
for sid in keep:
    folder = REAL / "videos" / f"session_{sid}"
    if folder.exists():
        shutil.copytree(folder, DEMO / "videos" / f"session_{sid}")
old, new = str(REAL / "videos"), str(DEMO / "videos")
pairs = [(old, new), (old.replace("\\", "\\\\"), new.replace("\\", "\\\\"))]  # 그냥 경로 + JSON 안의 경로
for table, cols in (("video_round", ("video_path", "storyboard_json")),
                    ("shot_render", ("path", "image_path", "end_image_path"))):
    for col in cols:
        for a, b in pairs:
            dst.execute(f"update {table} set {col} = replace({col}, ?, ?) where {col} like ?", (a, b, f"%{a}%"))
dst.commit()
left = dst.execute("select id, title from pipeline_session order by id").fetchall()
dst.execute("vacuum")
dst.close()
print("데모 작업 폴더를 만들었습니다:", DEMO)
for row in left:
    print("  프로젝트", row)
print("다음: .\\venv\\Scripts\\python demo_reset.py  →  .\\venv\\Scripts\\python run_demo.py (http://127.0.0.1:5001)")

"""Проверка утечек (ЛР5): многократный повтор сценариев, контроль файловых дескрипторов
процесса, числа живых объектов и памяти Python (tracemalloc) до и после серии вызовов."""
import gc, json, os, shutil, sys, tempfile, tracemalloc
from datetime import datetime, timedelta

pkg, db_src, out = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, os.path.abspath(pkg))
from model.database import Database  # noqa
from model.security import Actor, Role  # noqa
from model import services  # noqa

BOSS = Actor(1, "boss", Role.MANAGER)
fds = lambda: len(os.listdir("/proc/self/fd"))
tmp = tempfile.mkdtemp(); w = os.path.join(tmp, "w.db"); shutil.copy(db_src, w)
db = Database(w); rs = services.ReportService(db); ms = services.MeasurementService(db)
today = datetime.now().date(); s, e = (today - timedelta(days=90)).isoformat(), today.isoformat()
res = {}


def cycle():
    rs.build(BOSS, s, e); ms.recent(BOSS, 50)


for _ in range(50): cycle()          # прогрев
gc.collect(); tracemalloc.start(); base_mem = tracemalloc.get_traced_memory()[0]; base_fd = fds(); base_obj = len(gc.get_objects())
snaps = []
for i in range(1, 2001):
    cycle()
    if i in (500, 1000, 2000):
        gc.collect(); snaps.append({"iter": i, "fds": fds() - base_fd, "py_mem_kb": (tracemalloc.get_traced_memory()[0] - base_mem) / 1024,
                                    "objects": len(gc.get_objects()) - base_obj})
tracemalloc.stop(); res["repeat_report_and_recent"] = snaps
n_audit = db.conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
res["audit_rows_added_by_2050_views"] = n_audit - db.conn.execute("SELECT COUNT(*) FROM audit_log WHERE at < ?", ((datetime.now() - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M"),)).fetchone()[0] if False else None
db.close()

# отказ при открытии БД: файл не является SQLite
bad = os.path.join(tmp, "bad.db"); open(bad, "wb").write(b"not a database" * 100)
f0 = fds(); n_ok = 0
for _ in range(300):
    try:
        Database(bad)
    except Exception:
        n_ok += 1
gc.collect()
res["open_corrupted_db_300x"] = {"errors": n_ok, "fd_growth": fds() - f0}
# то же с удержанием исключения (как делает окно ожидания: box['error'] = exc)
keep = []; f0 = fds()
for _ in range(300):
    try:
        Database(bad)
    except Exception as ex:
        keep.append(ex)
res["open_corrupted_db_300x_exceptions_kept"] = {"fd_growth": fds() - f0}
json.dump(res, open(out, "w"), ensure_ascii=False, indent=1); print(pkg, json.dumps(res, ensure_ascii=False))
shutil.rmtree(tmp)

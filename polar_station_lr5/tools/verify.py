"""Проверка идентичности результатов «до/после» (ЛР5): сценарии запускаются на одних и тех же
данных, результаты пишутся в JSON; затем файлы двух запусков сравниваются (compare_json).

  python tools/verify.py --pkg before       --db data_10000.db --out v_before.json
  python tools/verify.py --pkg polar_station --db data_10000.db --out v_after.json
"""
import argparse, json, os, random, shutil, sys, tempfile
from datetime import datetime, timedelta
from itertools import islice

ap = argparse.ArgumentParser()
ap.add_argument("--pkg", required=True); ap.add_argument("--db", required=True)
ap.add_argument("--out", required=True); ap.add_argument("--n", type=int, default=3000)
a = ap.parse_args()
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(a.pkg)); sys.path.insert(1, HERE)
from gen_data import measurement_rows  # noqa
from model.database import Database  # noqa
from model.errors import StationError  # noqa
from model.security import Actor, Role  # noqa
from model.seed import seed_demo  # noqa
from model import services  # noqa

BOSS = Actor(1, "boss", Role.MANAGER); ENG = Actor(3, "engineer", Role.ENGINEER, 4)
SUP = Actor(4, "supervisor", Role.SUPERVISOR)
FIXED = datetime.now().replace(second=0, microsecond=0)
tmp = tempfile.mkdtemp(); out = {}


def plain(o):
    if isinstance(o, dict): return {str(k): plain(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)): return [plain(v) for v in o]
    return str(o) if not isinstance(o, (int, float, str, type(None))) else o


# --- S1: отчёты за разные периоды, включая границы суток
w = os.path.join(tmp, "w.db"); shutil.copy(a.db, w); db = Database(w)
rs = services.ReportService(db, clock=lambda: FIXED)
today = FIXED.date()
periods = [(None, None), ((today - timedelta(days=90)).isoformat(), today.isoformat()),
           (today.isoformat(), today.isoformat()), ((today - timedelta(days=1)).isoformat(),
           (today - timedelta(days=1)).isoformat()), ("2000-01-01", "2000-01-02")]
out["S1"] = [plain(rs.build(BOSS, s, e)) for s, e in periods]
# --- S2
ms, gs = services.MeasurementService(db, clock=lambda: FIXED), services.GeneratorService(db, clock=lambda: FIXED)
out["S2"] = {"recent": [list(map(plain, tuple(r))) for r in ms.recent(BOSS, 50)],
             "weather": gs.maintenance_conditions(ENG)}
db.close()


# --- S3: массовое внесение на одинаковых данных при фиксированных часах
def rows(n):
    rng = random.Random(7)
    g = (r for r in measurement_rows(int(n * 1.2), rng, window_days=29, now=FIXED) if r[0] != "salinity")
    return list(islice(g, n))


def dump(db):
    c = db.conn
    return {"measurements": [list(r) for r in c.execute("SELECT * FROM measurements ORDER BY id")],
            "audit": [list(r)[1:] for r in c.execute("SELECT * FROM audit_log ORDER BY id")]}


data = rows(a.n)
db = Database(os.path.join(tmp, "m.db")); seed_demo(db, FIXED)
svc = services.MeasurementService(db, clock=lambda: FIXED)
if hasattr(svc, "record_many"):
    svc.record_many(BOSS, ({"kind_code": k, "value": v, "measured_at": t, "instrument_id": i,
                            "operator_id": o} for k, v, t, i, o in data))
else:
    for k, v, t, i, o in data:
        svc.record(BOSS, k, v, i, o, t)
out["S3"] = dump(db); db.close()

# --- ошибки: каждая невалидная строка даёт тот же класс исключения (текст может иметь префикс «Строка k:»)
db = Database(os.path.join(tmp, "e.db")); seed_demo(db, FIXED)
svc = services.MeasurementService(db, clock=lambda: FIXED)
bad = [("air_temp", 999, 1, 1, None), ("air_temp", -10, 99, 1, None), ("air_temp", -10, 6, 1, None),
       ("salinity", 33, 6, 1, None), ("air_temp", -10, 1, 1, (FIXED + timedelta(days=3)).strftime("%Y-%m-%d %H:%M")),
       ("air_temp", -10, 1, 1, (FIXED - timedelta(days=200)).strftime("%Y-%m-%d %H:%M")), ("wind_speed", 5, 1, 1, None)]
errs = []
for k, v, i, o, t in bad:
    try:
        if hasattr(svc, "record_many"):
            svc.record_many(BOSS, [{"kind_code": k, "value": v, "instrument_id": i, "operator_id": o, "measured_at": t}])
        else:
            svc.record(BOSS, k, v, i, o, t)
        errs.append("OK")
    except StationError as e:
        errs.append(type(e).__name__)
out["errors"] = errs
try:
    svc.record(Actor(2, "observer", Role.OBSERVER, 2), "air_temp", -5, 1, 4)
except StationError as e:
    out["observer_foreign"] = type(e).__name__
json.dump(out, open(a.out, "w"), ensure_ascii=False, indent=1)
shutil.rmtree(tmp)
print(a.pkg, "ok", len(out["S3"]["measurements"]), "измерений;", "ошибки:", errs)

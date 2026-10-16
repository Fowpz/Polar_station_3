"""Профилирование сценариев ЛР5: timeit-замер времени, cProfile (топ-5 по tottime и число
вызовов), tracemalloc (пик памяти) для N = 1 000 / 10 000 / 50 000 записей.

  python tools/bench.py --pkg before --label before --out results_before.json
Один процесс = один пакет (оба пакета называются model/), БД копируются во временный файл.
"""
import argparse
import cProfile
import gc
import json
import os
import pstats
import shutil
import statistics
import sys
import tempfile
import time
import tracemalloc
from datetime import datetime, timedelta
from itertools import islice

HERE = os.path.dirname(os.path.abspath(__file__))
ap = argparse.ArgumentParser()
ap.add_argument("--pkg", required=True)
ap.add_argument("--label", required=True)
ap.add_argument("--dbdir", required=True, help="каталог с data_<N>.db")
ap.add_argument("--out", required=True)
ap.add_argument("--sizes", default="1000,10000,50000")
ap.add_argument("--only", default="")
args = ap.parse_args()
sys.path.insert(0, os.path.abspath(args.pkg))
sys.path.insert(1, HERE)

from gen_data import measurement_rows  # noqa: E402
from model.database import Database  # noqa: E402
from model.security import Actor, Role  # noqa: E402
from model.seed import seed_demo  # noqa: E402
from model import services  # noqa: E402

BOSS = Actor(1, "boss", Role.MANAGER)
ENGINEER = Actor(3, "engineer", Role.ENGINEER, 4)
TMP = tempfile.mkdtemp(prefix="lr5_")


def copy_db(n):
    dst = os.path.join(TMP, f"w_{n}_{time.perf_counter_ns()}.db")
    shutil.copy(os.path.join(args.dbdir, f"data_{n}.db"), dst)
    return dst


# ---------------------------------------------------------------- сценарии
def prep_report(n):
    db = Database(copy_db(n))
    today = datetime.now().date()
    start, end = (today - timedelta(days=90)).isoformat(), today.isoformat()
    svc = services.ReportService(db)
    return db, (lambda: svc.build(BOSS, start, end))


def prep_view(n):
    db = Database(copy_db(n))
    ms, gs = services.MeasurementService(db), services.GeneratorService(db)

    def run():
        rows = ms.recent(BOSS, 50)
        reasons = gs.maintenance_conditions(ENGINEER)
        return rows, reasons
    return db, run


def mass_rows(n):
    import random
    rng = random.Random(7)
    # прибор «Солемер» просрочен (блокирует измерения) — в массовой загрузке его нет
    for r in measurement_rows(int(n * 1.2), rng, window_days=29):
        if r[0] != "salinity":
            yield r


def prep_mass(n):
    path = os.path.join(TMP, f"m_{n}_{time.perf_counter_ns()}.db")
    db = Database(path)
    seed_demo(db)
    svc = services.MeasurementService(db)
    gen = lambda: islice(mass_rows(n), n)  # noqa: E731

    def run():
        if hasattr(svc, "record_many"):
            return svc.record_many(BOSS, ({"kind_code": k, "value": v, "measured_at": t,
                                           "instrument_id": i, "operator_id": o}
                                          for k, v, t, i, o in gen()))
        for k, v, t, i, o in gen():
            svc.record(BOSS, k, v, i, o, t)
    return db, run


SCEN = {"S1 Сводный отчёт за период": (prep_report, 7),
        "S2 Последние наблюдения + погода для ТО": (prep_view, 15),
        "S3 Массовое внесение измерений": (prep_mass, 1)}


def timeit_ms(prep, n, repeat):
    times = []
    for _ in range(repeat):
        db, run = prep(n)
        gc.collect()
        t = time.perf_counter()
        run()
        times.append((time.perf_counter() - t) * 1000)
        db.close()
    return {"min": min(times), "median": statistics.median(times), "runs": len(times)}


def profile(prep, n):
    db, run = prep(n)
    pr = cProfile.Profile()
    pr.enable()
    run()
    pr.disable()
    db.close()
    st = pstats.Stats(pr)
    rows = []
    for (fn, line, name), (cc, nc, tt, ct, _) in sorted(
            st.stats.items(), key=lambda kv: kv[1][2], reverse=True)[:5]:
        rows.append({"func": f"{os.path.basename(fn)}:{line}({name})", "calls": nc,
                     "tottime_ms": tt * 1000, "cumtime_ms": ct * 1000})
    return {"total_calls": st.total_calls, "total_ms": st.total_tt * 1000, "top5": rows}


def peak_mem(prep, n):
    db, run = prep(n)
    gc.collect()
    tracemalloc.start()
    run()
    cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    db.close()
    return peak / 1e6


res = {}
for name, (prep, repeat) in SCEN.items():
    if args.only and args.only not in name:
        continue
    res[name] = {}
    for n in map(int, args.sizes.split(",")):
        r = repeat if n <= 10000 else max(1, repeat // 2 if repeat > 1 else 1)
        entry = {"time": timeit_ms(prep, n, r), "profile": profile(prep, n),
                 "peak_mb": peak_mem(prep, n)}
        res[name][n] = entry
        print(f"{args.label} | {name} | N={n}: {entry['time']['median']:.1f} мс, "
              f"{entry['profile']['total_calls']} вызовов, пик {entry['peak_mb']:.2f} МБ",
              flush=True)
json.dump(res, open(args.out, "w"), ensure_ascii=False, indent=1)
shutil.rmtree(TMP, ignore_errors=True)

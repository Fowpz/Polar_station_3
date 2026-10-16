"""Оценка целесообразности распараллеливания (ЛР5): реальные замеры, не оценки.

 P0  накладные расходы: создание пула из 4 процессов / потоков с пустой задачей
 P1  S1 (отчёт): три независимые группы запросов (измерения, расход, происшествия) —
     последовательно, в 3 потоках, в 3 процессах (у каждого своё соединение SQLite)
 P3  S3 (массовое внесение): проверка строк в пуле процессов, вставка — в главном процессе
"""
import json, multiprocessing as mp, os, shutil, sqlite3, sys, tempfile, time, statistics
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import datetime, timedelta
from itertools import islice
import random

pkg, dbdir, out = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, os.path.abspath(pkg)); sys.path.insert(1, os.path.dirname(os.path.abspath(__file__)))
from gen_data import measurement_rows  # noqa
from model.database import Database  # noqa
from model.security import Actor, Role  # noqa
from model.seed import seed_demo  # noqa
from model import services  # noqa

BOSS = Actor(1, "boss", Role.MANAGER)
Q = ["SELECT kind_code, COUNT(*) FROM measurements WHERE measured_at >= ? AND measured_at <= ? GROUP BY kind_code",
     "SELECT resource_id, -SUM(delta) FROM resource_movements WHERE delta < 0 AND at >= ? AND at <= ? GROUP BY resource_id",
     "SELECT kind, COUNT(*) FROM incidents WHERE occurred_at >= ? AND occurred_at <= ? GROUP BY kind"]


def noop(_=None): return 1


def one_query(args):
    path, q, p = args
    c = sqlite3.connect(path); r = c.execute(q, p).fetchall(); c.close(); return r


def timed(fn, repeat=5):
    ts = []
    for _ in range(repeat):
        t = time.perf_counter(); fn(); ts.append((time.perf_counter() - t) * 1000)
    return statistics.median(ts)


# --- worker для P3
_svc = None
def init_worker(path):
    global _svc
    _svc = services.MeasurementService(Database(path))

def check_chunk(args):
    rows, now = args
    inst = {}; emp = {}; sh = {}
    res = []
    for r in rows:
        kind, num, when, iid, oid = _svc._check(BOSS, r["kind_code"], r["value"], r["instrument_id"], r["operator_id"],
                                                r["measured_at"], now, _svc.instruments.get, _svc.employees.get, _svc.shifts.get)
        res.append((kind.code, num, when.strftime("%Y-%m-%d %H:%M"), iid, oid))
    return res


if __name__ == "__main__":
    res = {"cpu_count": os.cpu_count()}
    tmp = tempfile.mkdtemp()
    res["P0_start_pool_4_ms"] = {
        "процессы (ProcessPoolExecutor, 4)": timed(lambda: list(ProcessPoolExecutor(4).map(noop, range(4)))),
        "потоки (ThreadPoolExecutor, 4)": timed(lambda: list(ThreadPoolExecutor(4).map(noop, range(4))))}
    # P1
    path = os.path.join(tmp, "a.db"); shutil.copy(os.path.join(dbdir, "data_50000.db"), path); Database(path).close()
    t0 = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d"); t1 = datetime.now().strftime("%Y-%m-%d") + " 23:59"
    jobs = [(path, q, (t0, t1)) for q in Q]
    seq = lambda: [one_query(j) for j in jobs]
    thr = lambda: list(ThreadPoolExecutor(3).map(one_query, jobs))
    prc = lambda: list(ProcessPoolExecutor(3).map(one_query, jobs))
    assert sorted(map(str, seq())) == sorted(map(str, thr())) == sorted(map(str, prc()))
    res["P1_report_50000_ms"] = {"последовательно": timed(seq), "3 потока": timed(thr), "3 процесса": timed(prc)}
    # P3
    n = 50000
    rng = random.Random(7); now = datetime.now().replace(second=0, microsecond=0)
    data = [dict(kind_code=k, value=v, measured_at=t, instrument_id=i, operator_id=o)
            for k, v, t, i, o in islice((r for r in measurement_rows(int(n * 1.2), rng, window_days=29, now=now) if r[0] != "salinity"), n)]

    def fresh():
        p = os.path.join(tmp, f"m{time.perf_counter_ns()}.db"); d = Database(p); seed_demo(d); return p, d

    def seq3():
        p, d = fresh(); services.MeasurementService(d).record_many(BOSS, data); d.close()

    def par3(workers=4):
        p, d = fresh()
        chunks = [(data[i::workers], now) for i in range(workers)]   # независимые части
        with ProcessPoolExecutor(workers, initializer=init_worker, initargs=(p,)) as ex:
            parts = list(ex.map(check_chunk, chunks))
        m = services.MeasurementService(d)
        with d.tx():                                                  # запись — только в одном процессе
            for part in parts:
                d.conn.executemany("INSERT INTO measurements(kind_code,value,measured_at,instrument_id,operator_id) VALUES (?,?,?,?,?)", part)
        d.close()

    res["P3_mass_entry_50000_ms"] = {"последовательно (record_many)": timed(seq3, 3), "проверка в 4 процессах + запись в главном": timed(par3, 3)}
    json.dump(res, open(out, "w"), ensure_ascii=False, indent=1); print(json.dumps(res, ensure_ascii=False, indent=1))
    shutil.rmtree(tmp)

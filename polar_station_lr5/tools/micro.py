"""Микробенчмарки timeit для сравнения двух вариантов реализации (ЛР5)."""
import json, os, shutil, sqlite3, sys, tempfile, timeit
from collections import Counter
from datetime import datetime, timedelta
from functools import lru_cache

pkg, dbdir, out = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, os.path.abspath(pkg))
from model.database import Database, SCHEMA  # noqa
from model.entities import Shift, Employee, Specialty  # noqa
from model.repositories import ShiftRepository, EmployeeRepository, InstrumentRepository  # noqa

res = {}
tmp = tempfile.mkdtemp()


def best(fn, number, repeat=5):
    return min(timeit.repeat(fn, number=number, repeat=repeat)) / number * 1000  # мс на вызов


# 1. SQL: условие по дате на 50 000 измерений (3 варианта)
db = os.path.join(tmp, "a.db"); shutil.copy(os.path.join(dbdir, "data_50000.db"), db)
c = sqlite3.connect(db)
for ix in ("ix_meas_at_kind", "ix_meas_kind_at", "ix_mov_at", "ix_inc_at"):
    c.execute(f"DROP INDEX IF EXISTS {ix}")
t0 = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d"); t1 = datetime.now().strftime("%Y-%m-%d")
q_substr = "SELECT kind_code, COUNT(*) FROM measurements WHERE substr(measured_at,1,10) BETWEEN ? AND ? GROUP BY kind_code"
q_range = "SELECT kind_code, COUNT(*) FROM measurements WHERE measured_at >= ? AND measured_at <= ? GROUP BY kind_code"
run = lambda q: c.execute(q, (t0, t1 + " 23:59")).fetchall() if "measured_at >=" in q else c.execute(q, (t0, t1)).fetchall()
r1 = best(lambda: run(q_substr), 20)
c.execute("CREATE INDEX ix_meas_at_kind ON measurements(measured_at, kind_code)")
r2 = best(lambda: run(q_substr), 20)
r3 = best(lambda: run(q_range), 20)
assert sorted(run(q_substr)) == sorted(run(q_range))
res["sql_date_filter_50000_ms"] = {"substr, без индекса": r1, "substr + индекс": r2, "диапазон + индекс": r3}
c.close()

# 2. N+1 по сменам против одного запроса + Counter (100 смен × 30 сотрудников)
d = Database(os.path.join(tmp, "b.db")); today = datetime.now().date()
sr, er = ShiftRepository(d), EmployeeRepository(d)
specs = list(Specialty)
with d.tx():
    for s in range(100):
        sid = sr.add(Shift(f"Смена {s}", today - timedelta(days=10), today + timedelta(days=50)))
        for e in range(30):
            er.add(Employee(f"Сотрудник {s}-{e}", specs[e % 5], 3, today, "годен", sid))
shifts = sr.list()


def old():
    out = []
    for sh in shifts:
        have = {sp: 0 for sp in Specialty}
        for emp in er.list(sh.id):
            have[emp.specialty] += 1
        out.append(have)
    return out


def new():
    by = Counter((e.shift_id, e.specialty) for e in er.list())
    return [{sp: by[(sh.id, sp)] for sp in Specialty} for sh in shifts]


assert old() == new()
res["staffing_100_shifts_ms"] = {"запрос на смену + dict": best(old, 10), "один запрос + Counter": best(new, 10)}

# 3. Поиск прибора по id: обращение к БД против lru_cache (10 000 обращений, 8 приборов)
ir = InstrumentRepository(Database(os.path.join(dbdir, "data_1000.db")))
ids = [i % 8 + 1 for i in range(10000)]
cached = lru_cache(maxsize=1024)(ir.get)
res["lookup_10000_ms"] = {"InstrumentRepository.get (SQL)": best(lambda: [ir.get(i) for i in ids], 1, 3),
                          "lru_cache(get)": best(lambda: [cached(i) for i in ids], 1, 3)}
json.dump(res, open(out, "w"), ensure_ascii=False, indent=1)
print(json.dumps(res, ensure_ascii=False, indent=1))
shutil.rmtree(tmp)

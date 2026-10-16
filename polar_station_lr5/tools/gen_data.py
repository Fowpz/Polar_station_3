"""Генератор тестовых данных ЛР5: создаёт файл SQLite в формате системы и заполняет его
N измерениями (+ движения ресурсов, происшествия, записи аудита пропорционально N).

Запуск:  python tools/gen_data.py --pkg polar_station --db data_10000.db --n 10000
Данные детерминированы (--seed), значения лежат в допустимых диапазонах видов измерений.
"""
import argparse
import os
import random
import sys
from datetime import datetime, timedelta

KINDS = {  # код вида: (мин, макс, среднее, разброс) — реалистичные арктические значения
    "air_temp": (-55.0, 5.0, -22.0, 9.0), "humidity": (30.0, 100.0, 80.0, 10.0),
    "wind_speed": (0.0, 40.0, 8.0, 5.0), "wind_dir": (0.0, 360.0, 180.0, 90.0),
    "snow_depth": (0.0, 200.0, 60.0, 25.0), "salinity": (28.0, 36.0, 33.0, 1.0),
    "water_temp": (-2.0, 3.0, -1.5, 0.6), "ice": (0, 10, 7, 2)}
KIND_ORDER = ["air_temp", "humidity", "wind_speed", "wind_dir", "snow_depth", "salinity",
              "water_temp", "ice"]          # порядок прибо­ров в seed_demo: id = индекс + 1


def make_value(rng, code):
    lo, hi, mu, sd = KINDS[code]
    v = min(hi, max(lo, rng.gauss(mu, sd)))
    return float(round(v)) if code == "ice" else round(v, 1)


def measurement_rows(n, rng, days=365, now=None, window_days=None):
    """Генератор кортежей (kind, value, 'YYYY-MM-DD HH:MM', instrument_id, operator_id).
    window_days — если задано, времена берутся только за последние window_days суток."""
    now = (now or datetime.now()).replace(second=0, microsecond=0)
    span = (window_days or days) * 24 * 60
    for _ in range(n):
        code = rng.choice(KIND_ORDER)
        when = now - timedelta(minutes=rng.randrange(0, span))
        yield (code, make_value(rng, code), when.strftime("%Y-%m-%d %H:%M"),
               KIND_ORDER.index(code) + 1, rng.randint(1, 6))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pkg", required=True, help="каталог пакета (before / polar_station)")
    ap.add_argument("--db", required=True)
    ap.add_argument("--n", type=int, required=True)
    ap.add_argument("--seed", type=int, default=2026)
    a = ap.parse_args()
    sys.path.insert(0, os.path.abspath(a.pkg))
    from model.database import Database
    from model.seed import seed_demo
    if os.path.exists(a.db):
        os.remove(a.db)
    rng = random.Random(a.seed)
    db = Database(a.db)
    seed_demo(db)
    now = datetime.now().replace(second=0, microsecond=0)
    with db.tx():
        db.conn.executemany(
            "INSERT INTO measurements(kind_code,value,measured_at,instrument_id,operator_id)"
            " VALUES (?,?,?,?,?)", measurement_rows(a.n, rng, now=now))
        db.conn.executemany(
            "INSERT INTO resource_movements(resource_id,delta,at,username) VALUES (?,?,?,?)",
            ((rng.randint(1, 3), -round(rng.uniform(1, 50), 1),
              (now - timedelta(minutes=rng.randrange(0, 365 * 1440))).strftime("%Y-%m-%d %H:%M"),
              rng.choice(["engineer", "boss"])) for _ in range(a.n // 5)))
        db.conn.executemany(
            "INSERT INTO incidents(kind,occurred_at,description,measures_taken) VALUES (?,?,?,?)",
            ((rng.choice(["шторм", "пурга", "ледовый дрейф"]),
              (now - timedelta(minutes=rng.randrange(0, 365 * 1440))).strftime("%Y-%m-%d %H:%M"),
              "Событие по данным наблюдений", "Приняты штатные меры") for _ in range(max(1, a.n // 50))))
        db.conn.executemany(
            "INSERT INTO audit_log(at,username,role,action,object,details) VALUES (?,?,?,?,?,?)",
            ((( now - timedelta(minutes=rng.randrange(0, 365 * 1440))).strftime("%Y-%m-%d %H:%M"),
              "boss", "руководитель", "просмотр наблюдений", "measurements", "")
             for _ in range(a.n // 10)))
    db.close()
    print(f"{a.db}: {a.n} измерений, {os.path.getsize(a.db) / 1e6:.1f} МБ")


if __name__ == "__main__":
    main()

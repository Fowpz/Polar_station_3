"""Подключение к SQLite и схема БД. Часть ограничений дублируется в CHECK."""
import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS shifts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    CHECK (end_date > start_date)
);
CREATE TABLE IF NOT EXISTS employees(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name TEXT NOT NULL,
    specialty TEXT NOT NULL,
    qualification INTEGER NOT NULL CHECK (qualification BETWEEN 1 AND 5),
    arrival_date TEXT NOT NULL,
    health TEXT NOT NULL,
    shift_id INTEGER NOT NULL REFERENCES shifts(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL,
    employee_id INTEGER REFERENCES employees(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS instruments(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    kind_code TEXT NOT NULL,
    last_calibration TEXT NOT NULL,
    interval_days INTEGER NOT NULL CHECK (interval_days > 0)
);
CREATE TABLE IF NOT EXISTS measurements(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind_code TEXT NOT NULL,
    value REAL NOT NULL,
    measured_at TEXT NOT NULL,
    instrument_id INTEGER NOT NULL REFERENCES instruments(id),
    operator_id INTEGER NOT NULL REFERENCES employees(id)
);
CREATE TABLE IF NOT EXISTS resources(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL,
    unit TEXT NOT NULL,
    quantity REAL NOT NULL CHECK (quantity >= 0),
    min_norm REAL NOT NULL CHECK (min_norm >= 0)
);
CREATE TABLE IF NOT EXISTS resource_movements(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_id INTEGER NOT NULL REFERENCES resources(id),
    delta REAL NOT NULL,
    at TEXT NOT NULL,
    username TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS supply_requests(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_id INTEGER NOT NULL REFERENCES resources(id),
    quantity REAL NOT NULL CHECK (quantity > 0),
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    external_status TEXT NOT NULL DEFAULT 'не отправлена'
        CHECK (external_status IN ('не отправлена','в очереди','подтверждена','отказ внешней системы')),
    external_ref TEXT,
    delivery_days INTEGER CHECK (delivery_days IS NULL OR delivery_days BETWEEN 1 AND 365),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS generators(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    motohours REAL NOT NULL CHECK (motohours >= 0),
    last_service_mh REAL NOT NULL CHECK (last_service_mh >= 0)
);
CREATE TABLE IF NOT EXISTS incidents(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    description TEXT NOT NULL,
    measures_taken TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    username TEXT NOT NULL,
    role TEXT NOT NULL,
    action TEXT NOT NULL,
    object TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_audit_at ON audit_log(at);
-- ЛР5: индексы под отчёты за период и «последние наблюдения». Условия по датам в
-- репозиториях записаны как диапазон по самому столбцу (а не substr(...)), иначе индекс
-- не используется. Индексы создаются идемпотентно и в уже существующих БД.
CREATE INDEX IF NOT EXISTS ix_meas_at_kind ON measurements(measured_at, kind_code);
CREATE INDEX IF NOT EXISTS ix_meas_kind_at ON measurements(kind_code, measured_at);
CREATE INDEX IF NOT EXISTS ix_mov_at ON resource_movements(at);
CREATE INDEX IF NOT EXISTS ix_inc_at ON incidents(occurred_at);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'журнал аудита неизменяем'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'журнал аудита неизменяем'); END;
"""

# ЛР4: столбцы, добавленные в supply_requests (для БД, созданных в ЛР3)
_SUPPLY_NEW_COLUMNS = {
    "external_status": "TEXT NOT NULL DEFAULT 'не отправлена' CHECK (external_status IN "
                       "('не отправлена','в очереди','подтверждена','отказ внешней системы'))",
    "external_ref": "TEXT",
    "delivery_days": "INTEGER CHECK (delivery_days IS NULL OR delivery_days BETWEEN 1 AND 365)",
    "attempts": "INTEGER NOT NULL DEFAULT 0",
    "last_error": "TEXT",
}


class Database:
    def __init__(self, path: str = ":memory:"):
        # check_same_thread=False: GUI выполняет долгий сетевой вызов во внешнюю систему в
        # фоновом потоке, пока окно заблокировано модальным диалогом ожидания; одновременного
        # доступа к соединению из двух потоков при этом не бывает.
        self.conn = sqlite3.connect(path, check_same_thread=False)
        try:
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA foreign_keys = ON")
            self.conn.executescript(SCHEMA)
            self._migrate()
        except BaseException:
            # ЛР5: при сбое инициализации (повреждённая БД, нет прав) соединение не должно
            # оставаться открытым — иначе файл БД остаётся занятым до конца процесса.
            self.conn.close()
            raise

    def _migrate(self) -> None:
        """Идемпотентная миграция ЛР3 → ЛР4: данные существующей БД не теряются."""
        have = {r[1] for r in self.conn.execute("PRAGMA table_info(supply_requests)")}
        for name, ddl in _SUPPLY_NEW_COLUMNS.items():
            if name not in have:
                self.conn.execute(f"ALTER TABLE supply_requests ADD COLUMN {name} {ddl}")
        self.conn.commit()

    @contextmanager
    def tx(self):
        """Транзакция: commit при успехе, rollback при любой ошибке."""
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def close(self):
        self.conn.close()

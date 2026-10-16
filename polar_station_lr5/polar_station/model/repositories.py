"""Доступ к данным. Все запросы параметризованы (защита от SQL-инъекций)."""
from datetime import datetime
from typing import List, Optional

from .database import Database
from .entities import (Employee, Generator, Incident, Instrument, Resource, Shift,
                       SupplyRequest)
from .validation import DATETIME_FMT, DATE_FMT


def _dt(value: datetime) -> str:
    return value.strftime(DATETIME_FMT)


def _day_end(day: str) -> str:
    """Верхняя граница включительно для даты 'ГГГГ-ММ-ДД' в столбцах формата 'ГГГГ-ММ-ДД ЧЧ:ММ'.
    Условие `col >= начало AND col <= конец + ' 23:59'` равносильно прежнему
    `substr(col, 1, 10) BETWEEN начало AND конец`, но читает индекс по самому столбцу."""
    return day + " 23:59"


class _Repo:
    def __init__(self, db: Database):
        self.db = db

    def _one(self, sql, params=()):
        return self.db.conn.execute(sql, params).fetchone()

    def _all(self, sql, params=()):
        return self.db.conn.execute(sql, params).fetchall()


class UserRepository(_Repo):
    def add(self, username, password_hash, role, employee_id) -> int:
        cur = self.db.conn.execute(
            "INSERT INTO users(username, password_hash, role, employee_id) VALUES (?,?,?,?)",
            (username, password_hash, role, employee_id))
        return cur.lastrowid

    def get_by_username(self, username):
        return self._one("SELECT * FROM users WHERE username = ?", (username,))

    def count(self) -> int:
        return self._one("SELECT COUNT(*) FROM users")[0]


class ShiftRepository(_Repo):
    @staticmethod
    def _map(r) -> Shift:
        return Shift(r["name"], r["start_date"], r["end_date"], r["id"])

    def add(self, s: Shift) -> int:
        cur = self.db.conn.execute(
            "INSERT INTO shifts(name, start_date, end_date) VALUES (?,?,?)",
            (s.name, s.start_date.strftime(DATE_FMT), s.end_date.strftime(DATE_FMT)))
        return cur.lastrowid

    def get(self, shift_id: int) -> Optional[Shift]:
        r = self._one("SELECT * FROM shifts WHERE id = ?", (shift_id,))
        return self._map(r) if r else None

    def list(self) -> List[Shift]:
        return [self._map(r) for r in self._all("SELECT * FROM shifts ORDER BY start_date")]


class EmployeeRepository(_Repo):
    @staticmethod
    def _map(r) -> Employee:
        return Employee(r["full_name"], r["specialty"], r["qualification"],
                        r["arrival_date"], r["health"], r["shift_id"], r["id"])

    def add(self, e: Employee) -> int:
        cur = self.db.conn.execute(
            "INSERT INTO employees(full_name, specialty, qualification, arrival_date,"
            " health, shift_id) VALUES (?,?,?,?,?,?)",
            (e.full_name, e.specialty.value, e.qualification,
             e.arrival_date.strftime(DATE_FMT), e.health.value, e.shift_id))
        return cur.lastrowid

    def get(self, employee_id: int) -> Optional[Employee]:
        r = self._one("SELECT * FROM employees WHERE id = ?", (employee_id,))
        return self._map(r) if r else None

    def list(self, shift_id: Optional[int] = None) -> List[Employee]:
        if shift_id is None:
            rows = self._all("SELECT * FROM employees ORDER BY shift_id, full_name")
        else:
            rows = self._all("SELECT * FROM employees WHERE shift_id = ? ORDER BY full_name",
                             (shift_id,))
        return [self._map(r) for r in rows]


class InstrumentRepository(_Repo):
    @staticmethod
    def _map(r) -> Instrument:
        return Instrument(r["name"], r["kind_code"], r["last_calibration"],
                          r["interval_days"], r["id"])

    def add(self, i: Instrument) -> int:
        cur = self.db.conn.execute(
            "INSERT INTO instruments(name, kind_code, last_calibration, interval_days)"
            " VALUES (?,?,?,?)",
            (i.name, i.kind_code, i.last_calibration.strftime(DATE_FMT), i.interval_days))
        return cur.lastrowid

    def get(self, instrument_id: int) -> Optional[Instrument]:
        r = self._one("SELECT * FROM instruments WHERE id = ?", (instrument_id,))
        return self._map(r) if r else None

    def list(self) -> List[Instrument]:
        return [self._map(r) for r in self._all("SELECT * FROM instruments ORDER BY id")]

    def set_calibration(self, instrument_id: int, on) -> None:
        self.db.conn.execute("UPDATE instruments SET last_calibration = ? WHERE id = ?",
                             (on.strftime(DATE_FMT), instrument_id))


class MeasurementRepository(_Repo):
    def add(self, kind_code, value, measured_at, instrument_id, operator_id) -> int:
        cur = self.db.conn.execute(
            "INSERT INTO measurements(kind_code, value, measured_at, instrument_id,"
            " operator_id) VALUES (?,?,?,?,?)",
            (kind_code, value, _dt(measured_at), instrument_id, operator_id))
        return cur.lastrowid

    def recent(self, limit: int = 20):
        return self._all(
            "SELECT m.id, m.kind_code, m.value, m.measured_at, i.name AS instrument,"
            " e.full_name AS operator FROM measurements m"
            " JOIN instruments i ON i.id = m.instrument_id"
            " JOIN employees e ON e.id = m.operator_id"
            " ORDER BY m.measured_at DESC, m.id DESC LIMIT ?", (limit,))

    def latest(self, kind_code: str):
        """Последнее измерение данного вида (строка с value и measured_at) или None."""
        return self._one(
            "SELECT value, measured_at FROM measurements WHERE kind_code = ?"
            " ORDER BY measured_at DESC, id DESC LIMIT 1", (kind_code,))

    def count_by_kind(self, start: str, end: str):
        return self._all(
            "SELECT kind_code, COUNT(*) AS n FROM measurements"
            " WHERE measured_at >= ? AND measured_at <= ? GROUP BY kind_code",
            (start, _day_end(end)))


class ResourceRepository(_Repo):
    @staticmethod
    def _map(r) -> Resource:
        return Resource(r["name"], r["category"], r["unit"], r["quantity"],
                        r["min_norm"], r["id"])

    def add(self, r: Resource) -> int:
        cur = self.db.conn.execute(
            "INSERT INTO resources(name, category, unit, quantity, min_norm)"
            " VALUES (?,?,?,?,?)",
            (r.name, r.category.value, r.unit, r.quantity, r.min_norm))
        return cur.lastrowid

    def get(self, resource_id: int) -> Optional[Resource]:
        r = self._one("SELECT * FROM resources WHERE id = ?", (resource_id,))
        return self._map(r) if r else None

    def list(self) -> List[Resource]:
        return [self._map(r) for r in self._all("SELECT * FROM resources ORDER BY id")]

    def set_quantity(self, resource_id: int, quantity: float) -> None:
        self.db.conn.execute("UPDATE resources SET quantity = ? WHERE id = ?",
                             (quantity, resource_id))

    def add_movement(self, resource_id, delta, at: datetime, username) -> None:
        self.db.conn.execute(
            "INSERT INTO resource_movements(resource_id, delta, at, username)"
            " VALUES (?,?,?,?)", (resource_id, delta, _dt(at), username))

    def consumption(self, start: str, end: str):
        return self._all(
            "SELECT r.name, r.unit, -SUM(m.delta) AS total FROM resource_movements m"
            " JOIN resources r ON r.id = m.resource_id"
            " WHERE m.delta < 0 AND m.at >= ? AND m.at <= ?"
            " GROUP BY r.id ORDER BY r.id", (start, _day_end(end)))


class SupplyRequestRepository(_Repo):
    @staticmethod
    def _map(r) -> SupplyRequest:
        return SupplyRequest(r["resource_id"], r["quantity"], r["status"],
                             r["created_at"], r["decided_at"], r["id"],
                             r["external_status"], r["external_ref"], r["delivery_days"],
                             r["attempts"], r["last_error"])

    def add(self, q: SupplyRequest) -> int:
        cur = self.db.conn.execute(
            "INSERT INTO supply_requests(resource_id, quantity, status, created_at)"
            " VALUES (?,?,?,?)",
            (q.resource_id, q.quantity, q.status.value, _dt(q.created_at)))
        return cur.lastrowid

    def get(self, request_id: int) -> Optional[SupplyRequest]:
        r = self._one("SELECT * FROM supply_requests WHERE id = ?", (request_id,))
        return self._map(r) if r else None

    def list(self) -> List[SupplyRequest]:
        return [self._map(r) for r in
                self._all("SELECT * FROM supply_requests ORDER BY id DESC")]

    def has_open(self, resource_id: int) -> bool:
        """Открытая заявка — созданная или утверждённая, но ещё не исполненная."""
        return self._one(
            "SELECT 1 FROM supply_requests WHERE resource_id = ?"
            " AND status IN ('создана', 'утверждена')", (resource_id,)) is not None

    def set_status(self, request_id: int, status, decided_at: Optional[datetime]) -> None:
        self.db.conn.execute(
            "UPDATE supply_requests SET status = ?, decided_at = ? WHERE id = ?",
            (status.value, _dt(decided_at) if decided_at else None, request_id))


    def list_queue(self) -> List[SupplyRequest]:
        """Заявки, ожидающие повторной отправки (и всё ещё открытые)."""
        return [self._map(r) for r in self._all(
            "SELECT * FROM supply_requests WHERE external_status = 'в очереди'"
            " AND status IN ('создана', 'утверждена') ORDER BY id")]

    def set_external(self, request_id: int, status, ref, delivery_days, attempts: int,
                     last_error) -> None:
        self.db.conn.execute(
            "UPDATE supply_requests SET external_status = ?, external_ref = ?,"
            " delivery_days = ?, attempts = ?, last_error = ? WHERE id = ?",
            (status.value, ref, delivery_days, attempts, last_error, request_id))


class AuditRepository(_Repo):
    """Только добавление и чтение: изменять журнал нельзя (триггеры в БД тоже запрещают)."""
    def add(self, at: datetime, username, role, action, obj, details) -> None:
        self.db.conn.execute(
            "INSERT INTO audit_log(at, username, role, action, object, details)"
            " VALUES (?,?,?,?,?,?)", (_dt(at), username, role, action, obj, details))

    def add_many(self, at: datetime, username, role, entries) -> None:
        """Пакетная запись (ЛР5): entries — кортежи (action, object, details); одна команда
        executemany вместо N отдельных INSERT. Вызывать внутри транзакции."""
        stamp = _dt(at)
        self.db.conn.executemany(
            "INSERT INTO audit_log(at, username, role, action, object, details)"
            " VALUES (?,?,?,?,?,?)",
            ((stamp, username, role, a, o, d) for a, o, d in entries))

    def recent(self, limit: int = 50):
        return self._all("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))


class GeneratorRepository(_Repo):
    @staticmethod
    def _map(r) -> Generator:
        return Generator(r["name"], r["motohours"], r["last_service_mh"], r["id"])

    def add(self, g: Generator) -> int:
        cur = self.db.conn.execute(
            "INSERT INTO generators(name, motohours, last_service_mh) VALUES (?,?,?)",
            (g.name, g.motohours, g.last_service_mh))
        return cur.lastrowid

    def get(self, generator_id: int) -> Optional[Generator]:
        r = self._one("SELECT * FROM generators WHERE id = ?", (generator_id,))
        return self._map(r) if r else None

    def list(self) -> List[Generator]:
        return [self._map(r) for r in self._all("SELECT * FROM generators ORDER BY id")]

    def update(self, g: Generator) -> None:
        self.db.conn.execute(
            "UPDATE generators SET motohours = ?, last_service_mh = ? WHERE id = ?",
            (g.motohours, g.last_service_mh, g.id))


class IncidentRepository(_Repo):
    @staticmethod
    def _map(r) -> Incident:
        return Incident(r["kind"], r["occurred_at"], r["description"],
                        r["measures_taken"], r["id"])

    def add(self, i: Incident) -> int:
        cur = self.db.conn.execute(
            "INSERT INTO incidents(kind, occurred_at, description, measures_taken)"
            " VALUES (?,?,?,?)",
            (i.kind.value, _dt(i.occurred_at), i.description, i.measures_taken))
        return cur.lastrowid

    def list(self) -> List[Incident]:
        return [self._map(r) for r in
                self._all("SELECT * FROM incidents ORDER BY occurred_at DESC, id DESC")]

    def count_by_kind(self, start: str, end: str):
        return self._all(
            "SELECT kind, COUNT(*) AS n FROM incidents"
            " WHERE occurred_at >= ? AND occurred_at <= ? GROUP BY kind",
            (start, _day_end(end)))

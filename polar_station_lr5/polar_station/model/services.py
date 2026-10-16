"""Бизнес-логика станции. Каждая публичная операция проверяет права (AccessPolicy)
и данные независимо от того, из какого интерфейса её вызвали."""
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Callable, List, Optional

from .audit import AuditLog
from .database import Database
from .entities import (REQUIRED_CREW, Employee, Generator,
                       Health, Incident, Instrument, MEASUREMENT_KINDS, RequestStatus,
                       Resource, Shift, Specialty, SupplyRequest, get_kind)
from .errors import (AccessDeniedError, BusinessRuleError, NotFoundError,
                     StationError, ValidationError)
from .repositories import (AuditRepository, EmployeeRepository, GeneratorRepository, IncidentRepository,
                           InstrumentRepository, MeasurementRepository,
                           ResourceRepository, ShiftRepository, SupplyRequestRepository,
                           UserRepository)
from .security import Actor, AccessPolicy, Permission, Role, verify_password
from .validation import (DATE_FMT, DATETIME_FMT, parse_date, parse_datetime,
                         require_number)

Clock = Callable[[], datetime]


class _Service:
    def __init__(self, db: Database, clock: Clock = datetime.now):
        self.db = db
        self._clock = clock
        self.audit = AuditLog(db)

    def now(self) -> datetime:
        return self._clock().replace(second=0, microsecond=0)

    def _audit(self, actor: Actor, action: str, obj: str, details: str = "") -> None:
        """Запись в журнал аудита; вызывать внутри db.tx() вместе с самим действием."""
        self.audit.record(actor, action, obj, details, self.now())

    @staticmethod
    def _found(obj, what: str):
        if obj is None:
            raise NotFoundError(f"{what} не найден(а)")
        return obj


class AuthService(_Service):
    def login(self, username: str, password: str) -> Actor:
        row = UserRepository(self.db).get_by_username(str(username).strip())
        # Одинаковое сообщение для неверного логина и пароля — не раскрываем, что именно неверно.
        if row is None or not verify_password(str(password), row["password_hash"]):
            raise AccessDeniedError("Неверный логин или пароль")
        return Actor(row["id"], row["username"], Role(row["role"]), row["employee_id"])


class StaffService(_Service):
    def __init__(self, db, clock=datetime.now):
        super().__init__(db, clock)
        self.shifts = ShiftRepository(db)
        self.employees = EmployeeRepository(db)

    def create_shift(self, actor: Actor, name, start_date, end_date) -> Shift:
        AccessPolicy.require(actor, Permission.MANAGE_STAFF)
        shift = Shift(name, start_date, end_date)
        with self.db.tx():
            shift.id = self.shifts.add(shift)
            self._audit(actor, "создание смены", f"смена#{shift.id}", shift.name)
        return shift

    def register_employee(self, actor: Actor, full_name, specialty, qualification,
                          arrival_date, health, shift_id) -> Employee:
        AccessPolicy.require(actor, Permission.MANAGE_STAFF)
        shift_id = require_number(shift_id, "Смена", 1, integer=True)
        shift = self._found(self.shifts.get(shift_id), "Смена")
        employee = Employee(full_name, specialty, qualification, arrival_date, health,
                            shift_id)
        if employee.health == Health.UNFIT:
            raise BusinessRuleError("Сотрудник с заключением «не годен» не может быть "
                                    "зачислен в смену")
        if employee.arrival_date > shift.end_date:
            raise BusinessRuleError("Дата прибытия позже окончания смены")
        with self.db.tx():
            employee.id = self.employees.add(employee)
            # ЛР5: ФИО (персональные данные) в неизменяемый журнал не пишем — запись нельзя
            # ни исправить, ни удалить; сотрудник однозначно определяется по id
            self._audit(actor, "зачисление сотрудника", f"сотрудник#{employee.id}",
                        employee.specialty.value)
        return employee

    def list_shifts(self, actor: Actor) -> List[Shift]:
        AccessPolicy.require(actor, Permission.VIEW)
        return self.shifts.list()

    def list_employees(self, actor: Actor, shift_id: Optional[int] = None):
        AccessPolicy.require(actor, Permission.VIEW)
        return self.employees.list(shift_id)


class InstrumentService(_Service):
    def __init__(self, db, clock=datetime.now):
        super().__init__(db, clock)
        self.instruments = InstrumentRepository(db)

    def list(self, actor: Actor) -> List[Instrument]:
        AccessPolicy.require(actor, Permission.VIEW)
        return self.instruments.list()

    def calibrate(self, actor: Actor, instrument_id, on_date=None) -> Instrument:
        AccessPolicy.require(actor, Permission.CALIBRATE)
        instrument_id = require_number(instrument_id, "Прибор", 1, integer=True)
        instrument = self._found(self.instruments.get(instrument_id), "Прибор")
        day = parse_date(on_date, "Дата калибровки") if on_date else self.now().date()
        if day > self.now().date():
            raise ValidationError("Дата калибровки не может быть в будущем")
        if day < instrument.last_calibration:
            raise BusinessRuleError("Новая калибровка не может быть раньше предыдущей")
        with self.db.tx():
            self.instruments.set_calibration(instrument_id, day)
            self._audit(actor, "калибровка прибора", f"прибор#{instrument_id}",
                        day.strftime(DATE_FMT))
        instrument.last_calibration = day
        return instrument


class MeasurementService(_Service):
    def __init__(self, db, clock=datetime.now):
        super().__init__(db, clock)
        self.measurements = MeasurementRepository(db)
        self.instruments = InstrumentRepository(db)
        self.employees = EmployeeRepository(db)
        self.shifts = ShiftRepository(db)

    def _check(self, actor: Actor, kind_code, value, instrument_id, operator_id,
               measured_at, now, find_instrument, find_employee, find_shift):
        """Все проверки и правила внесения ОДНОГО измерения; общие для record() и
        record_many() — правила не дублируются. find_* — функции поиска по id: для
        одиночного вызова это обращения к репозиторию, для пакета — кэшированные."""
        kind = get_kind(kind_code)
        number = kind.validate(value)
        when = parse_datetime(measured_at, "Время измерения") if measured_at else now
        if when > now:
            raise ValidationError("Время измерения не может быть в будущем")

        instrument_id = require_number(instrument_id, "Прибор", 1, integer=True)
        instrument = self._found(find_instrument(instrument_id), "Прибор")
        if instrument.kind_code != kind.code:
            raise BusinessRuleError(f"Прибор «{instrument.name}» не предназначен для "
                                    f"измерения «{kind.title}»")
        if not instrument.is_calibrated(when.date()):
            raise BusinessRuleError(
                f"Измерения прибором «{instrument.name}» заблокированы: калибровка "
                f"просрочена (срок истёк {instrument.due_date.strftime(DATE_FMT)})")

        operator_id = require_number(operator_id, "Оператор", 1, integer=True)
        if actor.role == Role.OBSERVER and operator_id != actor.employee_id:
            raise AccessDeniedError("Наблюдатель может вносить измерения только от своего имени")
        operator = self._found(find_employee(operator_id), "Оператор")
        if operator.health == Health.UNFIT:
            raise BusinessRuleError("Оператор не годен по состоянию здоровья")
        shift = self._found(find_shift(operator.shift_id), "Смена оператора")
        if not shift.is_active(when.date()):
            raise BusinessRuleError(f"Оператор не на дежурстве: смена «{shift.name}» "
                                    f"не активна на {when.date()}")
        return kind, number, when, instrument_id, operator_id

    def record(self, actor: Actor, kind_code, value, instrument_id, operator_id,
               measured_at=None) -> int:
        AccessPolicy.require(actor, Permission.RECORD_MEASUREMENT)
        kind, number, when, instrument_id, operator_id = self._check(
            actor, kind_code, value, instrument_id, operator_id, measured_at, self.now(),
            self.instruments.get, self.employees.get, self.shifts.get)
        with self.db.tx():
            measurement_id = self.measurements.add(kind.code, number, when, instrument_id,
                                                   operator_id)
            self._audit(actor, "внесение измерения", f"измерение#{measurement_id}",
                        f"{kind.code}={number:g}")
        return measurement_id

    def record_many(self, actor: Actor, rows, chunk_size: int = 5000) -> int:
        """Массовое внесение (ЛР5). rows — итерируемое словарей с ключами kind_code, value,
        instrument_id, operator_id и (необязательно) measured_at; можно передавать генератор.

        Те же правила, что у record() (общий _check), но:
        * справочники (приборы, сотрудники, смены) читаются из БД один раз и кэшируются
          на время вызова (lru_cache) — было 3 запроса на каждую запись;
        * одна транзакция и один commit на всю партию — было N commit (каждый — fsync);
        * запись аудита пакетами (executemany) по chunk_size строк — память O(chunk_size).
        Партия атомарна: при ошибке в строке k откатывается вся партия, сообщение начинается
        с «Строка k:». Возвращает число внесённых измерений."""
        AccessPolicy.require(actor, Permission.RECORD_MEASUREMENT)
        now = self.now()
        # кэши живут только внутри вызова → нет «вечного» глобального кэша и устаревших данных
        find_instrument = lru_cache(maxsize=1024)(self.instruments.get)
        find_employee = lru_cache(maxsize=1024)(self.employees.get)
        find_shift = lru_cache(maxsize=1024)(self.shifts.get)
        count, pending = 0, []
        with self.db.tx():
            for index, row in enumerate(rows, 1):
                try:
                    kind, number, when, instrument_id, operator_id = self._check(
                        actor, row["kind_code"], row["value"], row["instrument_id"],
                        row["operator_id"], row.get("measured_at"), now,
                        find_instrument, find_employee, find_shift)
                except KeyError as exc:
                    raise ValidationError(f"Строка {index}: нет поля {exc}") from None
                except StationError as exc:
                    raise type(exc)(f"Строка {index}: {exc}") from None
                measurement_id = self.measurements.add(kind.code, number, when,
                                                       instrument_id, operator_id)
                pending.append(("внесение измерения", f"измерение#{measurement_id}",
                                f"{kind.code}={number:g}"))
                count += 1
                if len(pending) >= chunk_size:
                    self.audit.record_many(actor, pending, now)
                    pending.clear()
            self.audit.record_many(actor, pending, now)
        return count

    def recent(self, actor: Actor, limit: int = 20):
        AccessPolicy.require(actor, Permission.VIEW)
        with self.db.tx():  # просмотр наблюдений supervisor-ом тоже фиксируется
            self._audit(actor, "просмотр наблюдений", "measurements")
        return self.measurements.recent(limit)


@dataclass
class ConsumeResult:
    resource: Resource
    auto_request: Optional[SupplyRequest]


class SupplyService(_Service):
    def __init__(self, db, clock=datetime.now):
        super().__init__(db, clock)
        self.resources = ResourceRepository(db)
        self.requests = SupplyRequestRepository(db)

    def list_resources(self, actor: Actor) -> List[Resource]:
        AccessPolicy.require(actor, Permission.VIEW)
        return self.resources.list()

    def list_requests(self, actor: Actor) -> List[SupplyRequest]:
        AccessPolicy.require(actor, Permission.VIEW)
        return self.requests.list()

    def consume(self, actor: Actor, resource_id, amount) -> ConsumeResult:
        AccessPolicy.require(actor, Permission.CONSUME_RESOURCE)
        resource_id = require_number(resource_id, "Ресурс", 1, integer=True)
        amount = float(require_number(amount, "Расход", 0, min_exclusive=True))
        resource = self._found(self.resources.get(resource_id), "Ресурс")
        if amount > resource.quantity:
            raise BusinessRuleError(
                f"Расход {amount:g} {resource.unit} превышает остаток "
                f"{resource.quantity:g} {resource.unit}")
        with self.db.tx():
            resource.quantity = round(resource.quantity - amount, 3)
            self.resources.set_quantity(resource_id, resource.quantity)
            self.resources.add_movement(resource_id, -amount, self.now(), actor.username)
            auto_request = None
            if resource.below_norm and not self.requests.has_open(resource_id):
                auto_request = SupplyRequest(resource_id, resource.restock_amount,
                                             RequestStatus.CREATED, self.now())
                auto_request.id = self.requests.add(auto_request)
            self._audit(actor, "списание ресурса", f"ресурс#{resource_id}", f"{amount:g}")
        return ConsumeResult(resource, auto_request)

    def decide(self, actor: Actor, request_id, approve: bool) -> SupplyRequest:
        AccessPolicy.require(actor, Permission.APPROVE_REQUEST)
        request = self._get_request(request_id)
        if request.status != RequestStatus.CREATED:
            raise BusinessRuleError(f"Заявка уже в статусе «{request.status.value}»")
        request.status = RequestStatus.APPROVED if approve else RequestStatus.REJECTED
        request.decided_at = self.now()
        with self.db.tx():
            self.requests.set_status(request.id, request.status, request.decided_at)
            self._audit(actor, f"решение по заявке: {request.status.value}",
                        f"заявка#{request.id}")
        return request

    def receive(self, actor: Actor, request_id) -> SupplyRequest:
        """Поступление утверждённой заявки на склад."""
        AccessPolicy.require(actor, Permission.RECEIVE_SUPPLY)
        request = self._get_request(request_id)
        if request.status != RequestStatus.APPROVED:
            raise BusinessRuleError("Принять на склад можно только утверждённую заявку")
        resource = self._found(self.resources.get(request.resource_id), "Ресурс")
        with self.db.tx():
            self.resources.set_quantity(resource.id,
                                        round(resource.quantity + request.quantity, 3))
            self.resources.add_movement(resource.id, request.quantity, self.now(),
                                        actor.username)
            request.status = RequestStatus.FULFILLED
            self.requests.set_status(request.id, request.status, request.decided_at)
            self._audit(actor, "приёмка поставки", f"заявка#{request.id}")
        return request

    def _get_request(self, request_id) -> SupplyRequest:
        request_id = require_number(request_id, "Заявка", 1, integer=True)
        return self._found(self.requests.get(request_id), "Заявка")


# ЛР4: погодные ограничения планового ТО (пример новых условий для полярной станции)
SERVICE_MAX_WIND_MS = 15.0        # ТО запрещено при ветре выше 15 м/с
SERVICE_MIN_TEMP_C = -45.0        # ... и при температуре воздуха ниже −45 °C
WEATHER_FRESH_HOURS = 6           # измерения старше 6 часов считаются устаревшими


class GeneratorService(_Service):
    def __init__(self, db, clock=datetime.now):
        super().__init__(db, clock)
        self.generators = GeneratorRepository(db)
        self.measurements = MeasurementRepository(db)

    def maintenance_conditions(self, actor: Actor) -> List[str]:
        """Причины, по которым плановое ТО сейчас запрещено (пустой список — можно).
        Берутся последние измерения ветра и температуры не старше WEATHER_FRESH_HOURS;
        если свежих данных нет, ограничения не применяются (допущение — пробел описания)."""
        AccessPolicy.require(actor, Permission.VIEW)
        now = self.now()
        reasons = []
        for code, limit, bad, text in (
                ("wind_speed", SERVICE_MAX_WIND_MS, lambda v: v > SERVICE_MAX_WIND_MS,
                 "ветер {v:g} м/с выше допустимых {lim:g} м/с"),
                ("air_temp", SERVICE_MIN_TEMP_C, lambda v: v < SERVICE_MIN_TEMP_C,
                 "температура {v:g} °C ниже допустимых {lim:g} °C")):
            row = self.measurements.latest(code)
            if row is None:
                continue
            when = datetime.strptime(row["measured_at"], DATETIME_FMT)
            if now - when <= timedelta(hours=WEATHER_FRESH_HOURS) and bad(row["value"]):
                reasons.append(text.format(v=row["value"], lim=limit))
        return reasons

    def list(self, actor: Actor) -> List[Generator]:
        AccessPolicy.require(actor, Permission.VIEW)
        return self.generators.list()

    def log_hours(self, actor: Actor, generator_id, hours) -> Generator:
        AccessPolicy.require(actor, Permission.LOG_GENERATOR)
        hours = float(require_number(hours, "Моточасы за запись", 0, 24, min_exclusive=True))
        generator = self._get(generator_id)
        generator.motohours = round(generator.motohours + hours, 2)
        with self.db.tx():
            self.generators.update(generator)
            self._audit(actor, "внесение моточасов", f"генератор#{generator.id}", f"{hours:g}")
        return generator

    def service(self, actor: Actor, generator_id) -> Generator:
        """Плановое ТО (каждые 250 моточасов): обнуляет счётчик до следующего ТО."""
        AccessPolicy.require(actor, Permission.SERVICE_GENERATOR)
        generator = self._get(generator_id)
        reasons = self.maintenance_conditions(actor)
        if reasons:
            raise BusinessRuleError("Плановое ТО запрещено по погодным условиям: "
                                    + "; ".join(reasons) + ". Перенесите ТО; учёт моточасов "
                                    "продолжается, ТО остаётся просроченным.")
        generator.last_service_mh = generator.motohours
        with self.db.tx():
            self.generators.update(generator)
            self._audit(actor, "плановое ТО", f"генератор#{generator.id}")
        return generator

    def _get(self, generator_id) -> Generator:
        generator_id = require_number(generator_id, "Генератор", 1, integer=True)
        return self._found(self.generators.get(generator_id), "Генератор")


class IncidentService(_Service):
    def __init__(self, db, clock=datetime.now):
        super().__init__(db, clock)
        self.incidents = IncidentRepository(db)

    def log(self, actor: Actor, kind, occurred_at, description, measures_taken) -> Incident:
        AccessPolicy.require(actor, Permission.LOG_INCIDENT)
        incident = Incident(kind, occurred_at, description, measures_taken)
        if incident.occurred_at > self.now():
            raise ValidationError("Время события не может быть в будущем")
        with self.db.tx():
            incident.id = self.incidents.add(incident)
            self._audit(actor, "запись нештатной ситуации", f"НС#{incident.id}",
                        incident.kind.value)
        return incident

    def list(self, actor: Actor) -> List[Incident]:
        AccessPolicy.require(actor, Permission.VIEW)
        return self.incidents.list()


class AuditService(_Service):
    def recent(self, actor: Actor, limit: int = 50):
        AccessPolicy.require(actor, Permission.VIEW_AUDIT)
        limit = require_number(limit, "Число записей", 1, 500, integer=True)
        return AuditRepository(self.db).recent(limit)


class ReportService(_Service):
    def build(self, actor: Actor, start=None, end=None) -> dict:
        AccessPolicy.require(actor, Permission.VIEW_REPORTS)
        start_d = parse_date(start, "Начало периода") if start else None
        end_d = parse_date(end, "Конец периода") if end else None
        if start_d and end_d and start_d > end_d:
            raise ValidationError("Начало периода позже его конца")
        s = start_d.strftime(DATE_FMT) if start_d else "0000-01-01"
        e = end_d.strftime(DATE_FMT) if end_d else "9999-12-31"

        counts = {r["kind_code"]: r["n"]
                  for r in MeasurementRepository(self.db).count_by_kind(s, e)}
        observations = [(k.title, counts.get(code, 0))
                        for code, k in MEASUREMENT_KINDS.items()]
        consumption = [(r["name"], r["unit"], r["total"])
                       for r in ResourceRepository(self.db).consumption(s, e)]
        by_kind = [(r["kind"], r["n"]) for r in IncidentRepository(self.db).count_by_kind(s, e)]

        today = self.now().date()
        staffing = []
        # ЛР5: один запрос по всем сотрудникам + Counter вместо запроса на каждую смену (N+1)
        # и ручного подсчёта в словаре; O(сотрудники) вместо O(смены × запрос)
        have_by_shift = Counter((e.shift_id, e.specialty)
                                for e in EmployeeRepository(self.db).list())
        for shift in ShiftRepository(self.db).list():
            if not shift.is_active(today):
                continue
            rows = [(sp.value, have_by_shift[(shift.id, sp)], REQUIRED_CREW[sp],
                     "норма" if have_by_shift[(shift.id, sp)] >= REQUIRED_CREW[sp]
                     else "НЕДОКОМПЛЕКТ")
                    for sp in Specialty]
            staffing.append({"shift": shift.name, "rows": rows})

        return {"period": (start_d, end_d),
                "observations": observations,
                "consumption": consumption,
                "incidents_total": sum(n for _, n in by_kind),
                "incidents_by_kind": by_kind,
                "staffing": staffing}

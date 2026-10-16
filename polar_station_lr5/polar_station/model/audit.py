"""Журнал аудита (ЛР4): действия руководителя и научного supervisor.

Запись выполняется в той же транзакции, что и само действие: нет действия без
следа в журнале и нет следа без действия. Методов изменения/удаления нет.
"""
from datetime import datetime

from .database import Database
from .repositories import AuditRepository
from .security import Actor, Role

# Требование: фиксируются действия руководителя станции и supervisor.
# Чтобы расширить аудит на другие роли, достаточно дополнить множество.
AUDITED_ROLES = frozenset({Role.MANAGER, Role.SUPERVISOR})


class AuditLog:
    def __init__(self, db: Database):
        self._repo = AuditRepository(db)

    def record(self, actor: Actor, action: str, obj: str, details: str = "",
               at: datetime = None) -> bool:
        """Вызывать внутри db.tx(). Возвращает True, если запись сделана."""
        if actor is None or actor.role not in AUDITED_ROLES:
            return False
        self._repo.add(at or datetime.now(), actor.username, actor.role.value,
                       action, obj, details)
        return True

    def record_many(self, actor: Actor, entries, at: datetime = None) -> int:
        """Пакетная запись (ЛР5) для массовых операций: entries — (действие, объект, детали).
        Тот же отбор по ролям, что и в record(); вызывать внутри db.tx()."""
        if actor is None or actor.role not in AUDITED_ROLES:
            return 0
        entries = list(entries)
        self._repo.add_many(at or datetime.now(), actor.username, actor.role.value, entries)
        return len(entries)

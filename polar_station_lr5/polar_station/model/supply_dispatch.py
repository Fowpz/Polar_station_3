"""Отправка заявок на пополнение во внешнюю систему заказа (ЛР4).

* отправляются только открытые заявки («создана», «утверждена»);
* сетевой вызов выполняется ВНЕ транзакции БД — он не держит блокировку;
* таймаут / недоступность / ответ не по контракту → заявка остаётся в локальной
  очереди («в очереди») и уходит повторно в retry_pending;
* штатный отказ внешней системы → «отказ внешней системы», повторов нет;
* подтверждённая заявка повторно не отправляется; request_id служит ключом
  идемпотентности на стороне внешней системы;
* статус заявки и запись аудита сохраняются одной транзакцией.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import List

from .database import Database
from .entities import ExternalStatus, RequestStatus, SupplyRequest
from .errors import (BusinessRuleError, ExternalProtocolError, ExternalRejectedError,
                     ExternalTimeoutError, ExternalUnavailableError)
from .external_order import EXTERNAL_TIMEOUT_SECONDS, ExternalOrderGateway
from .repositories import ResourceRepository, SupplyRequestRepository
from .security import Actor, AccessPolicy, Permission
from .services import _Service
from .validation import require_number

_OPEN = (RequestStatus.CREATED, RequestStatus.APPROVED)
_TRANSIENT = (ExternalTimeoutError, ExternalUnavailableError, ExternalProtocolError)


@dataclass
class DispatchResult:
    request: SupplyRequest
    message: str

    @property
    def confirmed(self) -> bool:
        return self.request.external_status == ExternalStatus.CONFIRMED


class SupplyDispatchService(_Service):
    def __init__(self, db: Database, gateway: ExternalOrderGateway, clock=datetime.now,
                 timeout: float = EXTERNAL_TIMEOUT_SECONDS):
        super().__init__(db, clock)
        self.gateway = gateway
        self.timeout = timeout
        self.requests = SupplyRequestRepository(db)
        self.resources = ResourceRepository(db)

    def dispatch(self, actor: Actor, request_id) -> DispatchResult:
        AccessPolicy.require(actor, Permission.DISPATCH_SUPPLY)
        request_id = require_number(request_id, "Заявка", 1, integer=True)
        request = self._found(self.requests.get(request_id), "Заявка")
        return self._dispatch(actor, request)

    def retry_pending(self, actor: Actor) -> List[DispatchResult]:
        AccessPolicy.require(actor, Permission.DISPATCH_SUPPLY)
        results = [self._dispatch(actor, r) for r in self.requests.list_queue()]
        with self.db.tx():
            self._audit(actor, "повторная отправка очереди заявок", "supply_requests",
                        f"обработано: {len(results)}, подтверждено: "
                        f"{sum(r.confirmed for r in results)}")
        return results

    # ------------------------------------------------------------------
    def _dispatch(self, actor: Actor, request: SupplyRequest) -> DispatchResult:
        if request.external_status == ExternalStatus.CONFIRMED:
            return DispatchResult(request, "Заявка уже подтверждена внешней системой")
        if request.external_status == ExternalStatus.REFUSED:
            raise BusinessRuleError("Внешняя система уже отклонила эту заявку; "
                                    "нужна новая заявка")
        if request.status not in _OPEN:
            raise BusinessRuleError(f"Заявку в статусе «{request.status.value}» "
                                    f"отправлять нельзя")
        resource = self._found(self.resources.get(request.resource_id), "Ресурс")
        payload = {"request_id": request.id, "item": resource.name,
                   "category": resource.category.value, "quantity": request.quantity,
                   "unit": resource.unit}
        attempts = request.attempts + 1
        try:
            confirmation = self.gateway.send(payload, timeout=self.timeout)  # вне транзакции
        except _TRANSIENT as exc:
            return self._save(actor, request, ExternalStatus.QUEUED, attempts, error=str(exc),
                              message="Внешняя система недоступна — заявка сохранена локально "
                                      "и будет отправлена повторно",
                              details=f"{type(exc).__name__}: {exc}")
        except ExternalRejectedError as exc:
            return self._save(actor, request, ExternalStatus.REFUSED, attempts, error=str(exc),
                              message=f"Внешняя система отклонила заявку: {exc}",
                              details=f"отказ: {exc}")
        msg = (f"Заявка принята, заказ {confirmation.order_ref}, "
               f"доставка через {confirmation.delivery_days} дн.")
        return self._save(actor, request, ExternalStatus.CONFIRMED, attempts,
                          ref=confirmation.order_ref, days=confirmation.delivery_days,
                          message=msg, details=msg)

    def _save(self, actor, request, status, attempts, ref=None, days=None, error=None,
              message="", details="") -> DispatchResult:
        with self.db.tx():  # статус заявки + запись аудита — атомарно
            self.requests.set_external(request.id, status, ref, days, attempts, error)
            self._audit(actor, f"отправка заявки: {status.value}", f"заявка#{request.id}",
                        details)
        request.external_status, request.external_ref = status, ref
        request.delivery_days, request.attempts, request.last_error = days, attempts, error
        return DispatchResult(request, message)

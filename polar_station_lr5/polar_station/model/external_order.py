"""Шлюз к внешней системе заказа продовольствия и медикаментов (ЛР4).

Остальной код зависит только от абстракции ExternalOrderGateway: реальный HTTP-шлюз
заменяется заглушкой в тестах и другой реализацией в эксплуатации.

Допущение (пробел спецификации): обмен в формате JSON.
  запрос : {"request_id", "item", "category", "quantity", "unit"}
  ответ  : {"status": "accepted"|"rejected", "order_ref", "delivery_days", "reason"}
"""
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass

from .errors import (ExternalProtocolError, ExternalRejectedError, ExternalTimeoutError,
                     ExternalUnavailableError)

EXTERNAL_TIMEOUT_SECONDS = 30  # требование: ожидание ответа не более 30 с


def mask_url(url: str) -> str:
    """Адрес для показа в окне и журналах (ЛР5): без логина/пароля и строки запроса, где
    часто лежит токен доступа (http://user:pw@host/order?token=… → http://host/order)."""
    parts = urllib.parse.urlsplit(str(url))
    host = parts.hostname or ""
    if parts.port:
        host += f":{parts.port}"
    return urllib.parse.urlunsplit((parts.scheme, host, parts.path, "", ""))


@dataclass(frozen=True)
class OrderConfirmation:
    order_ref: str
    delivery_days: int

    def __post_init__(self):
        if not isinstance(self.order_ref, str) or not self.order_ref.strip():
            raise ExternalProtocolError("Во внешней системе не указан номер заказа")
        days = self.delivery_days
        if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 365:
            raise ExternalProtocolError(f"Недопустимый срок доставки: {days!r}")


class ExternalOrderGateway(ABC):
    @abstractmethod
    def send(self, payload: dict, timeout: float = EXTERNAL_TIMEOUT_SECONDS) -> OrderConfirmation:
        """Вернуть подтверждение или бросить ExternalTimeoutError /
        ExternalUnavailableError / ExternalProtocolError / ExternalRejectedError."""


class UnconfiguredGateway(ExternalOrderGateway):
    """Адрес внешней системы не задан: заявки остаются в локальной очереди."""

    def send(self, payload, timeout=EXTERNAL_TIMEOUT_SECONDS):
        raise ExternalUnavailableError("Адрес внешней системы заказа не настроен (--order-url)")


class HttpOrderGateway(ExternalOrderGateway):
    def __init__(self, url: str):
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ValueError("Адрес внешней системы должен начинаться с http:// или https://")
        self._url = url

    def send(self, payload, timeout=EXTERNAL_TIMEOUT_SECONDS):
        request = urllib.request.Request(
            self._url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read(1_000_000)  # ответ ограничен по размеру
        except socket.timeout as exc:
            raise ExternalTimeoutError(f"Нет ответа за {timeout:g} с") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, socket.timeout):
                raise ExternalTimeoutError(f"Нет ответа за {timeout:g} с") from exc
            raise ExternalUnavailableError(f"Внешняя система недоступна: {exc.reason}") from exc
        except OSError as exc:
            raise ExternalUnavailableError(f"Ошибка соединения: {exc}") from exc

        try:
            data = json.loads(body.decode("utf-8"))
            status = data["status"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ExternalProtocolError("Ответ внешней системы не соответствует контракту") from exc
        if status == "rejected":
            raise ExternalRejectedError(str(data.get("reason") or "причина не указана"))
        if status != "accepted":
            raise ExternalProtocolError(f"Неизвестный статус ответа: {status!r}")
        return OrderConfirmation(data.get("order_ref"), data.get("delivery_days"))

"""Исключения слоя Model. Controller превращает их в понятные сообщения для View."""


class StationError(Exception):
    """Базовая ошибка предметной области."""


class ValidationError(StationError):
    """Некорректные входные данные (пустое поле, значение вне диапазона)."""


class BusinessRuleError(StationError):
    """Нарушено бизнес-правило (прибор не откалиброван, остаток меньше расхода и т. д.)."""


class AccessDeniedError(StationError):
    """У роли нет права на действие."""


class NotFoundError(StationError):
    """Запрошенная запись не найдена."""


# --- ЛР4: обмен с внешней системой заказа
class ExternalError(StationError):
    """Базовая ошибка обращения к внешней системе."""


class ExternalTimeoutError(ExternalError):
    """Внешняя система не ответила за отведённое время (30 с)."""


class ExternalUnavailableError(ExternalError):
    """Внешняя система недоступна (нет канала связи, ошибка сети, 5xx)."""


class ExternalProtocolError(ExternalError):
    """Ответ внешней системы не соответствует контракту."""


class ExternalRejectedError(ExternalError):
    """Внешняя система штатно отклонила заявку (повтор бессмыслен)."""

"""Точка входа: сборка слоёв MVC и запуск окна tkinter."""
import argparse

from model.database import Database
from model.external_order import (EXTERNAL_TIMEOUT_SECONDS, ExternalOrderGateway,
                                  HttpOrderGateway, UnconfiguredGateway, mask_url)
from model.seed import seed_demo
from model.services import (AuditService, AuthService, GeneratorService, IncidentService,
                            InstrumentService, MeasurementService, ReportService,
                            StaffService, SupplyService)
from model.supply_dispatch import SupplyDispatchService


def build_services(db: Database, gateway: ExternalOrderGateway = None) -> tuple:
    gateway = gateway or UnconfiguredGateway()
    return (AuthService(db), StaffService(db), InstrumentService(db),
            MeasurementService(db), SupplyService(db), GeneratorService(db),
            IncidentService(db), ReportService(db),
            SupplyDispatchService(db, gateway, timeout=EXTERNAL_TIMEOUT_SECONDS),
            AuditService(db))


def build_controller(db: Database, view, gateway: ExternalOrderGateway = None,
                     order_info: str = ""):
    from controller.gui_controller import GuiController
    return GuiController(view, *build_services(db, gateway), order_info=order_info)


def main() -> None:
    parser = argparse.ArgumentParser(description="Полярная станция — учётная система")
    parser.add_argument("--db", default="polar_station.db", help="путь к файлу SQLite")
    parser.add_argument("--order-url", default=None,
                        help="адрес внешней системы заказа (без него заявки копятся в очереди)")
    args = parser.parse_args()
    db = Database(args.db)
    try:
        seed_demo(db)
        gateway = HttpOrderGateway(args.order_url) if args.order_url else None
        info = (f"Внешняя система заказа: {mask_url(args.order_url)}" if args.order_url
                else "Внешняя система заказа: не настроена (заявки копятся в очереди)")
        from view.gui_view import GuiView
        build_controller(db, GuiView(), gateway, order_info=info).run()
    except (KeyboardInterrupt, EOFError):
        print("\nЗавершение работы.")
    finally:
        db.close()


if __name__ == "__main__":
    main()

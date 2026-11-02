import argparse
import os
import sys

import uvicorn

from .db import Database
from .errors import DomainError
from .seed import seed_demo


class RussianParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, add_help=False, **kwargs)
        self.add_argument("-h", "--help", action="help", help="Показать справку и завершить работу")
        self._positionals.title = "Позиционные параметры"
        self._optionals.title = "Параметры"

    def format_usage(self):
        return super().format_usage().replace("usage:", "Использование:", 1)

    def format_help(self):
        return super().format_help().replace("usage:", "Использование:", 1)

    def error(self, message):
        translations = {
            "the following arguments are required:": "не заданы обязательные параметры:",
            "unrecognized arguments:": "неизвестные параметры:",
            "expected one argument": "ожидается одно значение",
            "invalid int value:": "недопустимое целое число:",
            "invalid choice:": "недопустимый вариант:",
            "choose from": "выберите из",
            "argument ": "параметр ",
        }
        for source, target in translations.items():
            message = message.replace(source, target)
        self.print_usage(sys.stderr)
        self.exit(2, f"Ошибка параметров: {message}\n")


def main():
    parser = RussianParser(prog="orderflow", description="OrderFlow · управление пунктом проката")
    parser.add_argument(
        "command",
        choices=["serve", "demo", "init", "create-indexes"],
        help="Запуск сервера, демо, создание таблиц или явное обновление индекса",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Адрес прослушивания")
    parser.add_argument("--port", type=int, default=8000, help="Порт сервера")
    parser.add_argument("--database", help="URL базы данных; можно задать ORDERFLOW_DATABASE_URL")
    args = parser.parse_args()
    default = "sqlite:///orderflow-demo.db" if args.command == "demo" else "sqlite:///orderflow.db"
    url = args.database or os.getenv("ORDERFLOW_DATABASE_URL", default)
    os.environ["ORDERFLOW_DATABASE_URL"] = url
    if args.command == "create-indexes":
        db = Database(url)
        try:
            db.create_indexes()
        except DomainError as error:
            message = (
                "После освобождения блокировки повторите create-indexes"
                if error.code == "storage_busy"
                else str(error)
            )
            print(f"Не удалось создать индекс ({error.code}): {message}", file=sys.stderr)
            raise SystemExit(1) from None
        finally:
            db.engine.dispose()
        print("Индекс проверен и создан при необходимости")
        return
    if args.command in {"init", "demo"}:
        db = Database(url)
        db.initialize()
        if args.command == "demo":
            if seed_demo(db):
                print("Добавлены учебные данные. Ключ оператора: demo-operator")
            else:
                print("Каталог уже заполнен; существующие данные сохранены.")
            os.environ["ORDERFLOW_DEMO"] = "1"
        db.engine.dispose()
    if args.command != "init":
        uvicorn.run("orderflow.api:from_env", factory=True, host=args.host, port=args.port)


if __name__ == "__main__":
    main()

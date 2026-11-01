from dataclasses import dataclass


@dataclass(frozen=True)
class Actor:
    id: str
    name: str


def actor(value):
    # Строковая форма предназначена для внутренних вызовов сервиса и учебного наполнения.
    return value if isinstance(value, Actor) else Actor(value, value)

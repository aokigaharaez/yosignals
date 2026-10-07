import math
import time
from .market import MarketError


def validate_entry(entry_at, now=None):
    if entry_at is None:
        return
    now = int(time.time()) if now is None else now
    if type(entry_at) is not int or entry_at % 60 or not now + 10 <= entry_at <= now + 86400:
        raise MarketError("invalid_entry", "Выберите время входа с точностью до минуты: минимум через 10 секунд, не дальше 24 часов.")


def plan_entry(data_as_of, entry_at=None, reserve=10, now=None):
    now = int(time.time()) if now is None else now
    validate_entry(entry_at, now)
    entry = entry_at if entry_at is not None else math.ceil((now + reserve) / 60) * 60
    return entry, max(1, (entry - data_as_of) // 60)

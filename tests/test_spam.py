"""
Тесты анти-спам логики check_phone_spam.

Запуск:
    python -m pytest tests/ -q
    # или без pytest:
    python tests/test_spam.py

Supabase подменяется in-memory заглушкой, поэтому реальная БД не нужна.
Заглушка моделирует УНИКАЛЬНЫЙ user_id (одна строка на IP) — то самое
поведение, которое исправление гарантирует на стороне БД.
"""
import os
import sys
import pathlib
from datetime import datetime, timedelta, timezone

# Заглушки окружения до импорта app (Supabase-клиент строится на импорте)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
# Должен совпадать с JWT-регуляркой supabase-клиента (три сегмента через точку)
os.environ.setdefault("SUPABASE_KEY", "aaaa.bbbb.cccc")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import app  # noqa: E402


# ── In-memory заглушка Supabase ──────────────────────────────────
class _Resp:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, db, table):
        self.db = db
        self.table = table
        self._op = None
        self._payload = None
        self._filter = {}

    def select(self, *a, **k):
        self._op = "select"
        return self

    def insert(self, row):
        self._op = "insert"
        self._payload = row
        return self

    def update(self, fields):
        self._op = "update"
        self._payload = fields
        return self

    def eq(self, col, val):
        self._filter[col] = val
        return self

    def order(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def execute(self):
        store = self.db.tables.setdefault(self.table, {})
        if self._op == "select":
            uid = self._filter.get("user_id")
            return _Resp([store[uid]] if uid in store else [])
        if self._op == "insert":
            uid = self._payload["user_id"]
            if uid in store:  # имитируем UNIQUE-конфликт
                raise Exception("duplicate user_id")
            store[uid] = dict(self._payload)
            return _Resp([store[uid]])
        if self._op == "update":
            uid = self._filter.get("user_id")
            if uid in store:
                store[uid].update(self._payload)
                return _Resp([store[uid]])
            return _Resp([])
        return _Resp([])


class FakeSupabase:
    def __init__(self):
        self.tables = {}

    def table(self, name):
        return _Query(self, name)


def setup_function(_=None):
    app.supabase = FakeSupabase()


# ── Тесты ────────────────────────────────────────────────────────
def test_distinct_numbers_decrement_monotonically():
    """Разные номера уменьшают остаток строго: 5,4,3,2,1 — без рандома и без 0."""
    setup_function()
    ip = "203.0.113.10"
    lefts = []
    for i in range(app.PHONE_CHECK_LIMIT):
        blocked, _, left = app.check_phone_spam(ip, f"90000000{i:02d}")
        assert not blocked
        lefts.append(left)
    expected = list(range(app.PHONE_CHECK_LIMIT, 0, -1))  # 5,4,3,2,1
    assert lefts == expected, f"{lefts} != {expected}"
    assert 0 not in lefts  # ноль пользователю не показываем


def test_repeat_same_number_does_not_consume_attempt():
    """Повтор того же номера не тратит попытку — остаток стабилен."""
    setup_function()
    ip = "203.0.113.11"
    assert app.check_phone_spam(ip, "9001112233")[2] == app.PHONE_CHECK_LIMIT
    assert app.check_phone_spam(ip, "9001112233")[2] == app.PHONE_CHECK_LIMIT
    assert app.check_phone_spam(ip, "9001112233")[2] == app.PHONE_CHECK_LIMIT
    # новый номер — минус одна попытка
    assert app.check_phone_spam(ip, "9009998877")[2] == app.PHONE_CHECK_LIMIT - 1


def test_block_after_limit_exceeded():
    """5 разных номеров проходят (5..1), 6-й — блокировка."""
    setup_function()
    ip = "203.0.113.12"
    for i in range(app.PHONE_CHECK_LIMIT):
        blocked, _, _ = app.check_phone_spam(ip, f"91111111{i:02d}")
        assert not blocked
    blocked, remaining, left = app.check_phone_spam(ip, "9222222222")
    assert blocked and left == 0 and remaining > 0


def test_window_reset_restores_attempts():
    """Через час окно сбрасывается, остаток снова полный."""
    setup_function()
    ip = "203.0.113.13"
    app.check_phone_spam(ip, "9000000001")
    app.check_phone_spam(ip, "9000000002")
    # сдвигаем last_check на 2 часа назад
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    app.supabase.tables["phone_check_attempts"][ip]["last_check"] = old.isoformat()
    _, _, left = app.check_phone_spam(ip, "9000000003")
    assert left == app.PHONE_CHECK_LIMIT


def test_same_ip_always_same_row():
    """Один IP — одна строка: счётчик не «прыгает» между запросами."""
    setup_function()
    ip = "203.0.113.14"
    for i in range(3):
        app.check_phone_spam(ip, f"93000000{i:02d}")
    rows = app.supabase.tables["phone_check_attempts"]
    assert list(rows.keys()) == [ip]
    assert rows[ip]["attempt_count"] == 3


def test_full_name_rejects_single_letter_words():
    """ФИО из одной буквы / инициалов и неверного числа слов — отклоняются."""
    bad = [
        "Абдуллин Венер н",       # отчество в 1 букву (случай со скриншота)
        "Иванов И Иванович",      # инициал
        "Иванов Иван",            # 2 слова
        "Иванов Иван Иванович Петрович",  # 4 слова
        "Ив4нов Иван Иванович",   # цифра
        "А Б В",                  # все по 1 букве
        "  ",                     # пусто
    ]
    for name in bad:
        assert not app.validate_full_name(name), f"должно быть невалидно: {name!r}"


def test_full_name_accepts_valid():
    """Корректные ФИО (в т.ч. с дефисом) проходят."""
    good = [
        "Абдуллин Венер Наилевич",
        "Иванов Иван Иванович",
        "Петров-Водкин Кузьма Сергеевич",
    ]
    for name in good:
        assert app.validate_full_name(name), f"должно быть валидно: {name!r}"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)

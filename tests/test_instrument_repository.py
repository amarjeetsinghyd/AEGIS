from contextlib import contextmanager
from datetime import datetime

from common.market import instrument_repository
from common.market.instrument_repository import InstrumentRepository


def test_weekly_option_tokens_include_expiry_date_for_the_full_day(monkeypatch):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 30, 9, 15)

    class Cursor:
        def execute(self, *_args, **_kwargs):
            return self

        def fetchall(self):
            return [
                {
                    "token": "today",
                    "symbol": "NIFTY30OCT2624000CE",
                    "expiry": "30-Oct-2026",
                    "strike": 24000.0,
                },
                {
                    "token": "tomorrow",
                    "symbol": "NIFTY31OCT2624000CE",
                    "expiry": "31-Oct-2026",
                    "strike": 24000.0,
                },
            ]

    class Connection:
        def cursor(self):
            return Cursor()

    @contextmanager
    def fake_connection():
        yield Connection()

    repo = object.__new__(InstrumentRepository)
    repo._get_connection = fake_connection
    monkeypatch.setattr(instrument_repository, "datetime", FixedDateTime)

    rows = repo.get_weekly_option_tokens("SHOONYA", "NIFTY", "NFO")

    assert [row["token"] for row in rows] == ["today"]

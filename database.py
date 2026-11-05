"""
database.py — асинхронна робота з SQLite (aiosqlite).

Захист від дублів: UNIQUE(symbol, timeframe, candle_timestamp).
Надійна доставка: кожен сигнал зберігається разом з текстом повідомлення
та прапорцем sent (0 — очікує, 1 — відправлено, 2 — відхилено Telegram).
Якщо процес впаде до відправки, сигнал буде перевідправлено після старту.
"""
import logging
import time
from typing import List, Optional, Tuple

import aiosqlite

import config

log = logging.getLogger(__name__)

SENT_PENDING, SENT_OK, SENT_REJECTED = 0, 1, 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol           TEXT    NOT NULL,
    timeframe        TEXT    NOT NULL,
    side             TEXT    NOT NULL,
    candle_timestamp INTEGER NOT NULL,
    entry            REAL    NOT NULL,
    sl               REAL    NOT NULL,
    tp1              REAL    NOT NULL,
    tp2              REAL    NOT NULL,
    message          TEXT    NOT NULL,
    sent             INTEGER NOT NULL DEFAULT 0,
    created_at       INTEGER NOT NULL,
    UNIQUE (symbol, timeframe, candle_timestamp)
);
CREATE INDEX IF NOT EXISTS idx_signals_lookup
    ON signals (symbol, timeframe, candle_timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_signals_unsent ON signals (sent, created_at);
"""


class Database:
    def __init__(self, path: str = config.DB_FILE):
        self.path = path
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self.path)
        # WAL — стійкість до аварійних зупинок і швидкі записи
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()
        log.info("БД підключено: %s", self.path)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
            log.info("БД закрито")

    async def last_signal_ts(self, symbol: str, timeframe: str) -> Optional[int]:
        """Таймстамп свічки останнього сигналу (для cooldown)."""
        async with self._conn.execute(
            "SELECT MAX(candle_timestamp) FROM signals WHERE symbol=? AND timeframe=?",
            (symbol, timeframe),
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row and row[0] is not None else None

    async def register_signal(self, symbol: str, timeframe: str, side: str,
                              candle_ts: int, entry: float, sl: float,
                              tp1: float, tp2: float, message: str) -> Optional[int]:
        """
        Атомарно реєструє сигнал.
        Повертає id нового запису або None, якщо для цієї свічки сигнал уже був (дубль).
        """
        cur = await self._conn.execute(
            "INSERT OR IGNORE INTO signals "
            "(symbol, timeframe, side, candle_timestamp, entry, sl, tp1, tp2, "
            " message, sent, created_at) VALUES (?,?,?,?,?,?,?,?,?,0,?)",
            (symbol, timeframe, side, candle_ts, entry, sl, tp1, tp2,
             message, int(time.time())),
        )
        await self._conn.commit()
        return cur.lastrowid if cur.rowcount == 1 else None

    async def mark_sent(self, signal_id: int, status: int = SENT_OK) -> None:
        await self._conn.execute("UPDATE signals SET sent=? WHERE id=?", (status, signal_id))
        await self._conn.commit()

    async def get_unsent(self, max_age_sec: int) -> List[Tuple[int, str]]:
        """Сигнали, які не були доставлені (наприклад, через падіння). Свіжі — найстаріші першими."""
        since = int(time.time()) - max_age_sec
        async with self._conn.execute(
            "SELECT id, message FROM signals WHERE sent=0 AND created_at>=? ORDER BY id",
            (since,),
        ) as cur:
            rows = await cur.fetchall()
        return [(r[0], r[1]) for r in rows]

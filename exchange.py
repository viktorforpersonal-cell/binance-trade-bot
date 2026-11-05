"""
exchange.py — ccxt.pro стрім свічок + ресинхронізація + watchdog + exponential backoff.

Один CandleStream = одна пара + один таймфрейм = власний екземпляр біржі.
Так watchdog може примусово розірвати саме проблемне з'єднання, не зачіпаючи інші.
"""
import asyncio
import logging
import random
import time
from typing import Awaitable, Callable, Optional

import ccxt
import ccxt.pro as ccxtpro
import pandas as pd

import config

log = logging.getLogger(__name__)

COLUMNS = ["ts", "open", "high", "low", "close", "volume"]

# Колбек: (symbol, timeframe, закриті_свічки, джерело "ws" | "resync")
OnCandle = Callable[[str, str, pd.DataFrame, str], Awaitable[None]]


def make_exchange():
    """Нова ccxt.pro-біржа з вбудованим rate limiter."""
    default_type = "future" if config.MARKET_TYPE == "futures" else "spot"
    return ccxtpro.binance({
        "enableRateLimit": True,
        "newUpdates": True,
        "options": {"defaultType": default_type},
    })


def to_symbol(pair: str) -> str:
    """BTC/USDT -> BTC/USDT:USDT для USDT-M ф'ючерсів."""
    return f"{pair}:USDT" if config.MARKET_TYPE == "futures" else pair


def merge_candles(df: Optional[pd.DataFrame], candles: list) -> pd.DataFrame:
    """Зливає нові свічки в датафрейм (оновлює формовану, додає нові, обрізає буфер)."""
    new = pd.DataFrame(candles, columns=COLUMNS).set_index("ts")
    if df is None:
        merged = new
    else:
        merged = pd.concat([df[~df.index.isin(new.index)], new]).sort_index()
    return merged.iloc[-config.MAX_BUFFER:]


async def check_clock_drift(factory: Callable = make_exchange) -> Optional[int]:
    """Різниця між часом біржі та локальним годинником у мс (None — не вдалося виміряти)."""
    ex = factory()
    try:
        t0 = time.time() * 1000
        server = await ex.fetch_time()
        t1 = time.time() * 1000
        return int(server - (t0 + t1) / 2)
    except Exception as e:
        log.warning("Не вдалося перевірити розсинхрон годинника: %s", type(e).__name__)
        return None
    finally:
        try:
            await ex.close()
        except Exception:
            pass


class CandleStream:
    def __init__(self, pair: str, timeframe: str, stop: asyncio.Event,
                 on_candle: OnCandle, start_delay: float = 0.0,
                 exchange_factory: Callable = make_exchange):
        self.symbol = to_symbol(pair)
        self.timeframe = timeframe
        self.name = f"{self.symbol}/{timeframe}"
        self._stop = stop
        self._on_candle = on_candle
        self._start_delay = start_delay
        self._factory = exchange_factory
        self._df: Optional[pd.DataFrame] = None
        self._last_closed_ts = 0
        self._attempt = 0
        self.last_update = 0.0       # час останніх отриманих даних (для heartbeat)

    @property
    def alive(self) -> bool:
        """Стрім вважається живим, якщо дані приходили нещодавно."""
        return (time.time() - self.last_update) < config.WATCHDOG_TIMEOUT_SEC + 30

    # ------------------------------------------------------------------ цикл
    async def run(self) -> None:
        if self._start_delay:
            await asyncio.sleep(self._start_delay)

        while not self._stop.is_set():
            exchange = self._factory()
            try:
                await self._sync_history(exchange)
                while not self._stop.is_set():
                    # Watchdog: немає даних WATCHDOG_TIMEOUT_SEC -> TimeoutError -> реконект
                    candles = await asyncio.wait_for(
                        exchange.watch_ohlcv(self.symbol, self.timeframe),
                        timeout=config.WATCHDOG_TIMEOUT_SEC)
                    if self._attempt:
                        log.info("[%s] З'єднання відновлено", self.name)
                        self._attempt = 0
                    self.last_update = time.time()
                    self._df = merge_candles(self._df, candles)
                    await self._emit("ws")

            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                log.warning("[%s] Watchdog: немає даних %d с — примусовий перезапуск стріму",
                            self.name, config.WATCHDOG_TIMEOUT_SEC)
            except (ccxt.DDoSProtection, ccxt.RateLimitExceeded) as e:
                # Біржа просить притримати коней — одразу йдемо на максимальну паузу.
                self._attempt = max(self._attempt, 10)
                log.error("[%s] Ліміт запитів біржі (%s) — довга пауза", self.name, type(e).__name__)
            except Exception as e:
                log.error("[%s] Помилка стріму: %s: %s", self.name, type(e).__name__, e)
            finally:
                try:
                    await exchange.close()   # закриваємо сокети
                except Exception:
                    pass

            if self._stop.is_set():
                break
            delay = min(config.BACKOFF_BASE_SEC * (2 ** self._attempt), config.BACKOFF_MAX_SEC)
            self._attempt = min(self._attempt + 1, 10)
            delay += random.uniform(0, config.BACKOFF_JITTER_SEC)
            log.info("[%s] Перепідключення через %.1f с", self.name, delay)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    # ----------------------------------------------------------- допоміжне
    async def _sync_history(self, exchange) -> None:
        """
        Перший запуск: 350 свічок через REST (один раз).
        Після розриву WebSocket: ресинхронізація останніми 10 свічками.
        """
        first = self._df is None
        limit = config.HISTORY_LIMIT if first else config.RESYNC_LIMIT
        candles = await exchange.fetch_ohlcv(self.symbol, self.timeframe, limit=limit)
        if not candles:
            raise RuntimeError("порожня відповідь REST")
        self._df = merge_candles(self._df, candles)
        self.last_update = time.time()
        log.info("[%s] %s: %d свічок", self.name,
                 "Історія завантажена" if first else "Ресинхронізація", len(candles))

        if first:
            # Старі свічки не аналізуємо: запам'ятовуємо лише межу.
            if len(self._df) >= 2:
                self._last_closed_ts = int(self._df.index[-2])
        else:
            # Під час розриву могла закритися свічка — перевіряємо.
            await self._emit("resync")

    async def _emit(self, source: str) -> None:
        """Якщо з'явилася нова ЗАКРИТА свічка — віддаємо колбеку закриті свічки."""
        df = self._df
        if df is None or len(df) < 2:
            return
        closed = df.iloc[:-1]            # остання свічка в df — формована, відкидаємо
        ts = int(closed.index[-1])
        if ts <= self._last_closed_ts:
            return
        self._last_closed_ts = ts
        try:
            await self._on_candle(self.symbol, self.timeframe, closed.copy(), source)
        except Exception:
            log.exception("[%s] Помилка в обробнику свічки", self.name)

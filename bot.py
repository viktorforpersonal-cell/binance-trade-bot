"""
bot.py — головний цикл: логування, Telegram-черга, ризик-менеджмент, heartbeat, graceful shutdown.
Запуск: python bot.py
"""
import asyncio
import html
import logging
import math
import os
import signal
import time
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from typing import List, Optional, Tuple

import aiohttp
from ccxt.base.exchange import Exchange

import config
from database import SENT_OK, SENT_REJECTED, Database
from exchange import CandleStream, check_clock_drift
from indicators import generate_signal

log = logging.getLogger("bot")


# ====================================================================== логування
class RedactingFormatter(logging.Formatter):
    """Прибирає секрети (токен Telegram) з усього, що потрапляє в лог."""

    def __init__(self, fmt: str, secrets: List[str]):
        super().__init__(fmt)
        self._secrets = [s for s in secrets if s]

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for s in self._secrets:
            text = text.replace(s, "***")
        return text


def setup_logging() -> None:
    fmt = RedactingFormatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
                             [config.TELEGRAM_BOT_TOKEN])
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    file_h = RotatingFileHandler(config.LOG_FILE, maxBytes=config.LOG_MAX_BYTES,
                                 backupCount=config.LOG_BACKUP_COUNT, encoding="utf-8")
    file_h.setFormatter(fmt)
    console_h = logging.StreamHandler()
    console_h.setFormatter(fmt)
    root.addHandler(file_h)
    root.addHandler(console_h)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


# ====================================================================== ризик-менеджмент
@dataclass
class RiskPlan:
    entry: float
    sl: float
    tp1: float
    tp2: float
    risk_usdt: float        # реальний ризик до SL
    position_usdt: float    # рекомендований розмір позиції (номінал)
    margin_usdt: float      # потрібна маржа = позиція / плече
    leverage: int
    qty: float              # кількість базового активу


def build_plan(side: str, entry: float, atr: float) -> Optional[RiskPlan]:
    """SL = 1.5*ATR, TP1/TP2 за RRR, розмір позиції під ризик %, плече за волатильністю."""
    sl_dist = config.ATR_SL_MULT * atr
    if side == "LONG":
        sl = entry - sl_dist
        tp1 = entry + sl_dist * config.TP1_RRR
        tp2 = entry + sl_dist * config.TP2_RRR
    else:
        sl = entry + sl_dist
        tp1 = entry - sl_dist * config.TP1_RRR
        tp2 = entry - sl_dist * config.TP2_RRR
    if sl <= 0 or tp1 <= 0 or tp2 <= 0 or sl_dist <= 0:
        return None   # нереалістичні рівні (надмірно велика волатильність)

    deposit = config.DEPOSIT_USDT
    risk_target = deposit * config.RISK_PER_TRADE_PCT / 100.0
    sl_frac = sl_dist / entry                        # відстань до стопу в частках від ціни
    desired_notional = risk_target / sl_frac         # позиція, що дає рівно risk_target при SL

    # Ліміт плеча: чим вища волатильність (ATR% від ціни), тим менше плече.
    max_lev = 1 if config.MARKET_TYPE == "spot" else config.MAX_LEVERAGE
    atr_pct = atr / entry * 100.0
    if atr_pct <= config.VOL_REF_ATR_PCT:
        vol_cap = max_lev
    else:
        vol_cap = max(1, math.floor(max_lev * config.VOL_REF_ATR_PCT / atr_pct))

    required = max(1, math.ceil(desired_notional / deposit))
    leverage = max(1, min(required, vol_cap, max_lev))

    # Позиція не може перевищити депозит * плече (тоді реальний ризик менший за цільовий).
    notional = min(desired_notional, deposit * leverage)
    return RiskPlan(entry=entry, sl=sl, tp1=tp1, tp2=tp2,
                    risk_usdt=notional * sl_frac, position_usdt=notional,
                    margin_usdt=notional / leverage, leverage=leverage,
                    qty=notional / entry)


def fmt_price(p: float) -> str:
    """Ціна без наукової нотації, з точністю за порядком величини."""
    decimals = 2 if p >= 100 else 4 if p >= 1 else 8
    return f"{p:.{decimals}f}"


def format_message(symbol: str, timeframe: str, side: str, candle_ts: int,
                   plan: RiskPlan, reason: str) -> str:
    """HTML-повідомлення; усі динамічні текстові поля екрануються."""
    e = html.escape
    icon = "🟢" if side == "LONG" else "🔴"
    candle_time = time.strftime("%Y-%m-%d %H:%M", time.gmtime(candle_ts / 1000))
    return (
        f"{icon} <b>{e(side)}</b> | <b>{e(symbol)}</b>\n"
        f"Ринок: {e(config.MARKET_TYPE)} | Таймфрейм: {e(timeframe)}\n"
        f"Свічка: {candle_time} UTC\n\n"
        f"Вхід: <code>{fmt_price(plan.entry)}</code>\n"
        f"SL: <code>{fmt_price(plan.sl)}</code>\n"
        f"TP1 (1:{config.TP1_RRR:g}): <code>{fmt_price(plan.tp1)}</code>\n"
        f"TP2 (1:{config.TP2_RRR:g}): <code>{fmt_price(plan.tp2)}</code>\n\n"
        f"Рекомендована позиція: <b>{plan.position_usdt:.2f} USDT</b>\n"
        f"Рекомендоване плече: <b>x{plan.leverage}</b> (маржа {plan.margin_usdt:.2f} USDT)\n"
        f"Ризик до SL: {plan.risk_usdt:.2f} USDT "
        f"({plan.risk_usdt / config.DEPOSIT_USDT * 100:.2f}% депозиту)\n\n"
        f"Причина: {e(reason)}"
    )


# ====================================================================== Telegram-черга
class TelegramQueue:
    """asyncio.Queue + м'який троттлінг + обробка 429. Доставка підтверджується в БД."""

    def __init__(self, db: Database, token: str = config.TELEGRAM_BOT_TOKEN,
                 chat_id: str = config.TELEGRAM_CHAT_ID,
                 api_base: str = "https://api.telegram.org"):
        self._db = db
        self._token = token
        self._chat_id = chat_id
        self._url = f"{api_base}/bot{token}/sendMessage"
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=config.TELEGRAM_QUEUE_SIZE)
        self._session: Optional[aiohttp.ClientSession] = None
        self._task: Optional[asyncio.Task] = None

    def _safe(self, err: object) -> str:
        return str(err).replace(self._token, "***")

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        self._task = asyncio.create_task(self._worker(), name="telegram-worker")

    def enqueue(self, text: str, signal_id: Optional[int] = None) -> None:
        try:
            self._queue.put_nowait((text, signal_id))
        except asyncio.QueueFull:
            # Сигнал уже в БД з sent=0 — буде перевідправлений після перезапуску.
            log.error("Черга Telegram переповнена")

    async def stop(self) -> None:
        """Даємо черзі шанс спорожніти, потім зупиняємо воркер і сесію."""
        try:
            await asyncio.wait_for(self._queue.join(), timeout=config.TELEGRAM_DRAIN_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            log.warning("Черга Telegram не спорожніла вчасно (сигнали лишились у БД)")
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self._session:
            await self._session.close()

    async def _worker(self) -> None:
        while True:
            text, signal_id = await self._queue.get()
            try:
                status = await self._send(text)
                if signal_id is not None and status is not None:
                    await self._db.mark_sent(signal_id, status)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error("Помилка відправки: %s", self._safe(e))
            finally:
                self._queue.task_done()
            await asyncio.sleep(config.TELEGRAM_THROTTLE_SEC)   # м'який троттлінг

    async def _send(self, text: str) -> Optional[int]:
        """SENT_OK / SENT_REJECTED (назавжди відхилено) / None (не вдалося, спробуємо після рестарту)."""
        payload = {"chat_id": self._chat_id, "text": text, "parse_mode": "HTML",
                   "disable_web_page_preview": True}
        for attempt in range(1, config.TELEGRAM_MAX_RETRIES + 1):
            try:
                async with self._session.post(self._url, json=payload) as resp:
                    if resp.status == 200:
                        return SENT_OK
                    data = await resp.json(content_type=None)
                    if resp.status == 429:
                        wait = data.get("parameters", {}).get("retry_after", 5)
                        log.warning("Telegram 429 — чекаю %s с", wait)
                        await asyncio.sleep(wait + 1)
                        continue
                    if 400 <= resp.status < 500:
                        log.error("Telegram відхилив повідомлення (%s): %s",
                                  resp.status, data.get("description"))
                        return SENT_REJECTED
                    log.warning("Telegram %s (спроба %d)", resp.status, attempt)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.warning("Мережева помилка Telegram (спроба %d): %s", attempt, self._safe(e))
            await asyncio.sleep(min(2 ** attempt, 30))
        log.error("Не вдалося доставити повідомлення після %d спроб", config.TELEGRAM_MAX_RETRIES)
        return None


# ====================================================================== обробка свічок
class SignalEngine:
    def __init__(self, db: Database, tg: TelegramQueue):
        self._db = db
        self._tg = tg

    async def on_candle(self, symbol: str, timeframe: str, closed_df, source: str) -> None:
        # pandas_ta — CPU-робота, виносимо з event loop.
        sig = await asyncio.to_thread(generate_signal, closed_df)
        if sig is None:
            return
        if sig.side == "SHORT" and config.MARKET_TYPE == "spot":
            return   # на споті шорт неможливий

        # Cooldown між сигналами по одній парі/таймфрейму
        tf_ms = Exchange.parse_timeframe(timeframe) * 1000
        last = await self._db.last_signal_ts(symbol, timeframe)
        if last is not None and sig.candle_ts - last < config.COOLDOWN_CANDLES * tf_ms:
            log.info("Cooldown: %s %s — сигнал пропущено", symbol, timeframe)
            return

        plan = build_plan(sig.side, sig.entry, sig.atr)
        if plan is None:
            log.warning("%s %s: нереалістичні рівні SL/TP — сигнал пропущено", symbol, timeframe)
            return

        message = format_message(symbol, timeframe, sig.side, sig.candle_ts, plan, sig.reason)
        # Спочатку БД (захист від дублів + гарантія доставки), потім черга.
        signal_id = await self._db.register_signal(
            symbol, timeframe, sig.side, sig.candle_ts,
            plan.entry, plan.sl, plan.tp1, plan.tp2, message)
        if signal_id is None:
            log.info("Дубль сигналу %s %s ts=%s проігноровано", symbol, timeframe, sig.candle_ts)
            return
        log.info("СИГНАЛ %s %s %s entry=%s (%s)", sig.side, symbol, timeframe,
                 fmt_price(plan.entry), source)
        self._tg.enqueue(message, signal_id)


# ====================================================================== health-check
def atomic_write(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)   # атомарно: зовнішній монітор ніколи не побачить напівзаписаний файл


async def heartbeat_loop(stop: asyncio.Event, streams: List[CandleStream]) -> None:
    while not stop.is_set():
        alive = sum(1 for s in streams if s.alive)
        try:
            atomic_write(config.HEARTBEAT_FILE,
                         f"{int(time.time())} {time.strftime('%Y-%m-%d %H:%M:%S')} "
                         f"streams_alive={alive}/{len(streams)}\n")
        except OSError as e:
            log.error("Не вдалося записати heartbeat: %s", e)
        try:
            await asyncio.wait_for(stop.wait(), timeout=config.HEARTBEAT_INTERVAL_SEC)
        except asyncio.TimeoutError:
            pass


# ====================================================================== main
async def amain() -> None:
    setup_logging()
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        raise SystemExit("Задайте TELEGRAM_BOT_TOKEN і TELEGRAM_CHAT_ID у змінних оточення")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:    # Windows
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))

    db = Database()
    await db.connect()
    tg = TelegramQueue(db)
    await tg.start()

    # Контроль синхронізації часу з біржею (NTP має бути налаштований на VPS).
    drift = await check_clock_drift()
    if drift is not None:
        if abs(drift) > config.CLOCK_DRIFT_WARN_MS:
            log.warning("Годинник відрізняється від Binance на %d мс — перевірте NTP!", drift)
        else:
            log.info("Розсинхрон часу з Binance: %d мс", drift)

    # Перевідправляємо сигнали, що не встигли доставитись до падіння/зупинки.
    pending: List[Tuple[int, str]] = await db.get_unsent(config.RESEND_MAX_AGE_SEC)
    for signal_id, text in pending:
        tg.enqueue(text, signal_id)
    if pending:
        log.info("Перевідправляю %d недоставлених сигналів", len(pending))

    engine = SignalEngine(db, tg)
    streams: List[CandleStream] = []
    for i, (pair, tf) in enumerate((p, t) for p in config.PAIRS for t in config.TIMEFRAMES):
        streams.append(CandleStream(pair, tf, stop, engine.on_candle,
                                    start_delay=i * config.START_STAGGER_SEC))

    tasks = [asyncio.create_task(heartbeat_loop(stop, streams), name="heartbeat")]
    tasks += [asyncio.create_task(s.run(), name=s.name) for s in streams]
    log.info("Бот запущено: %d стрімів", len(streams))
    tg.enqueue("✅ <b>Бот запущено</b>")

    await stop.wait()
    log.info("Отримано сигнал зупинки — коректне завершення...")
    for t in tasks:
        t.cancel()                                        # стріми закриють сокети у finally
    await asyncio.gather(*tasks, return_exceptions=True)
    tg.enqueue("⛔ <b>Бот зупинено</b>")
    await tg.stop()
    await db.close()
    log.info("Завершено коректно")


if __name__ == "__main__":
    asyncio.run(amain())

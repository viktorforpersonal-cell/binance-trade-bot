"""
indicators.py — математика: індикатори (pandas_ta) з NaN-перевіркою та генерація сигналів.

Правило: функція отримує ТІЛЬКИ закриті свічки (формовану відсікає exchange.py),
тому сигнали не перемальовуються.
"""
import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd
import pandas_ta as ta

import config

log = logging.getLogger(__name__)

_MACD = f"MACD_{config.MACD_FAST}_{config.MACD_SLOW}_{config.MACD_SIGNAL}"
_MACD_SIG = f"MACDs_{config.MACD_FAST}_{config.MACD_SLOW}_{config.MACD_SIGNAL}"
_REQUIRED = ["ema_fast", "ema_slow", "macd", "macd_sig", "rsi", "atr"]


@dataclass
class Signal:
    side: str          # "LONG" / "SHORT"
    candle_ts: int     # таймстамп закритої свічки (мс, відкриття)
    entry: float       # ціна закриття свічки
    atr: float
    rsi: float
    reason: str        # людське пояснення спрацювання


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Повертає копію датафрейму з колонками індикаторів."""
    out = df.copy()
    out["ema_fast"] = ta.ema(out["close"], length=config.EMA_FAST)
    out["ema_slow"] = ta.ema(out["close"], length=config.EMA_SLOW)

    macd = ta.macd(out["close"], fast=config.MACD_FAST,
                   slow=config.MACD_SLOW, signal=config.MACD_SIGNAL)
    if macd is None or _MACD not in macd.columns or _MACD_SIG not in macd.columns:
        out["macd"] = np.nan
        out["macd_sig"] = np.nan
    else:
        out["macd"] = macd[_MACD]
        out["macd_sig"] = macd[_MACD_SIG]

    out["rsi"] = ta.rsi(out["close"], length=config.RSI_PERIOD)
    out["atr"] = ta.atr(out["high"], out["low"], out["close"], length=config.ATR_PERIOD)
    return out


def generate_signal(closed_df: pd.DataFrame) -> Optional[Signal]:
    """closed_df — лише закриті свічки. Повертає Signal або None. Ніколи не кидає виняток."""
    try:
        if len(closed_df) < config.EMA_SLOW + 5:
            return None

        df = compute_indicators(closed_df)

        # Захист від NaN / inf: перевіряємо дві останні закриті свічки.
        tail = df[_REQUIRED].iloc[-2:]
        if tail.isna().any().any() or not np.isfinite(tail.to_numpy(dtype=float)).all():
            log.debug("NaN/inf в індикаторах — пропускаю ітерацію")
            return None

        cur, prev = df.iloc[-1], df.iloc[-2]
        if cur["atr"] <= 0 or cur["close"] <= 0:
            return None

        close = float(cur["close"])
        rsi = float(cur["rsi"])
        ts = int(df.index[-1])
        cross_up = prev["macd"] <= prev["macd_sig"] and cur["macd"] > cur["macd_sig"]
        cross_dn = prev["macd"] >= prev["macd_sig"] and cur["macd"] < cur["macd_sig"]

        lo, hi = config.RSI_LONG_RANGE
        if (close > cur["ema_slow"] and cur["ema_fast"] > cur["ema_slow"]
                and cross_up and lo <= rsi <= hi):
            return Signal(
                "LONG", ts, close, float(cur["atr"]), rsi,
                f"Тренд вгору (EMA{config.EMA_FAST} > EMA{config.EMA_SLOW}, ціна вище EMA{config.EMA_SLOW}), "
                f"MACD перетнув сигнальну лінію вгору, RSI {rsi:.1f}")

        lo, hi = config.RSI_SHORT_RANGE
        if (close < cur["ema_slow"] and cur["ema_fast"] < cur["ema_slow"]
                and cross_dn and lo <= rsi <= hi):
            return Signal(
                "SHORT", ts, close, float(cur["atr"]), rsi,
                f"Тренд вниз (EMA{config.EMA_FAST} < EMA{config.EMA_SLOW}, ціна нижче EMA{config.EMA_SLOW}), "
                f"MACD перетнув сигнальну лінію вниз, RSI {rsi:.1f}")
        return None
    except Exception:
        log.exception("Помилка розрахунку індикаторів — ітерацію пропущено")
        return None

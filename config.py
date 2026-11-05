"""
config.py — усі налаштування бота в одному місці.
Секрети (токен, chat_id) беруться ТІЛЬКИ зі змінних оточення (ніколи не комітьте їх у код).
"""
import os
from pathlib import Path

# Базова директорія проєкту: файли стану лежать поруч із кодом,
# незалежно від того, звідки запущено процес (systemd, cron, термінал).
BASE_DIR = Path(__file__).resolve().parent

# ---------- Telegram (секрети — через ENV) ----------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_THROTTLE_SEC = 1.2          # пауза між повідомленнями (захист від HTTP 429)
TELEGRAM_QUEUE_SIZE = 1000
TELEGRAM_MAX_RETRIES = 5
TELEGRAM_DRAIN_TIMEOUT_SEC = 20      # скільки чекати спорожнення черги при вимкненні
RESEND_MAX_AGE_SEC = 6 * 3600        # невідправлені сигнали старші за це не перевідправляємо

# ---------- Ринок ----------
MARKET_TYPE = "futures"              # "futures" (USDT-M perpetual) або "spot"
PAIRS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT"]
TIMEFRAMES = ["15m", "1h", "4h"]

# ---------- Історія та ресинхронізація ----------
HISTORY_LIMIT = 350                  # початкове завантаження через REST (один раз при старті)
RESYNC_LIMIT = 10                    # докачування через REST після розриву WebSocket
MAX_BUFFER = 600                     # максимум свічок, що тримаємо в пам'яті

# ---------- Надійність ----------
WATCHDOG_TIMEOUT_SEC = 90            # немає даних довше -> примусовий розрив і перезапуск
BACKOFF_BASE_SEC = 2                 # затримки: 2, 4, 8, 16, 32, 60, 60 ...
BACKOFF_MAX_SEC = 60
BACKOFF_JITTER_SEC = 1.0             # невеликий випадковий зсув, щоб стріми не стукали одночасно
START_STAGGER_SEC = 0.4              # зсув старту стрімів (не навантажувати REST)
CLOCK_DRIFT_WARN_MS = 1000           # попередження, якщо годинник відрізняється від Binance
HEARTBEAT_FILE = str(BASE_DIR / "heartbeat.txt")
HEARTBEAT_INTERVAL_SEC = 60

# ---------- Файли ----------
DB_FILE = str(BASE_DIR / "bot_state.db")
LOG_FILE = str(BASE_DIR / "bot.log")
LOG_MAX_BYTES = 5 * 1024 * 1024      # 5 МБ
LOG_BACKUP_COUNT = 3

# ---------- Індикатори ----------
EMA_FAST = 50
EMA_SLOW = 200
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
RSI_PERIOD = 14
ATR_PERIOD = 14

# Фільтри RSI для сигналів (уникаємо входу в уже перегрітий рух)
RSI_LONG_RANGE = (45, 70)
RSI_SHORT_RANGE = (30, 55)

# Cooldown: мінімум N свічок між сигналами по одній парі/таймфрейму
COOLDOWN_CANDLES = 3

# ---------- Ризик-менеджмент ----------
DEPOSIT_USDT = 1000.0                # депозит користувача
RISK_PER_TRADE_PCT = 1.0             # ризик на угоду, % від депозиту
MAX_LEVERAGE = 20                    # верхня межа плеча (для spot ігнорується -> x1)
ATR_SL_MULT = 1.5                    # SL = 1.5 * ATR
TP1_RRR = 1.5                        # TP1 = 1:1.5
TP2_RRR = 3.0                        # TP2 = 1:3
# Опорна волатильність: ATR у % від ціни, до якої дозволено повне MAX_LEVERAGE.
# Якщо ATR% вищий — плече знижується пропорційно.
VOL_REF_ATR_PCT = 0.5

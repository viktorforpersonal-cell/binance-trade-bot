# Розгортання на Linux VPS (Ubuntu 24.04 / Debian 12)

Рекомендації до сервера: 1 vCPU, 1 ГБ RAM достатньо. **Обирайте VPS поза США** — Binance
блокує американські IP (HTTP 451). Бот НЕ торгує: він лише читає публічні ринкові дані,
тому API-ключі Binance не потрібні і створювати їх не треба.

## 1. Telegram: бот і chat_id

1. У Telegram відкрийте `@BotFather` → `/newbot` → збережіть **токен**.
2. Напишіть своєму боту будь-яке повідомлення (інакше він не зможе вам писати).
3. Відкрийте `https://api.telegram.org/bot<ТОКЕН>/getUpdates` і знайдіть `"chat":{"id": ...}` — це **chat_id**.
   (Для каналу/групи додайте бота адміністратором; id групи починається з `-`.)

## 2. Підготовка системи

```bash
sudo apt update && sudo apt -y upgrade
sudo apt -y install python3 python3-venv python3-pip chrony ufw
sudo timedatectl set-timezone UTC          # сервер краще тримати в UTC
```

## 3. Синхронізація часу (NTP) — обов'язково

Розсинхрон годинника ламає розрахунок закриття свічок і підписані запити. Використовуємо `chrony`:

```bash
sudo systemctl enable --now chrony
sudo timedatectl set-ntp true
chronyc tracking            # "System time" має бути в межах кількох мілісекунд
chronyc sources -v          # хоча б одне джерело зі знаком ^*
timedatectl                 # має бути: System clock synchronized: yes
```

Якщо у вас працює `systemd-timesyncd`, `chrony` замінить його автоматично. Бот при старті
додатково порівнює час із Binance і пише WARNING у `bot.log`, якщо розбіжність > 1 с.

## 4. Користувач і код

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin binancebot
sudo mkdir -p /opt/binance-bot
# скопіюйте сюди config.py database.py indicators.py exchange.py bot.py requirements.txt
sudo chown -R binancebot:binancebot /opt/binance-bot

sudo -u binancebot python3 -m venv /opt/binance-bot/venv
sudo -u binancebot /opt/binance-bot/venv/bin/pip install --upgrade pip
sudo -u binancebot /opt/binance-bot/venv/bin/pip install -r /opt/binance-bot/requirements.txt
```

## 5. Секрети (поза кодом)

```bash
sudo tee /etc/binance-bot.env >/dev/null <<'EOF'
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=123456789
EOF
sudo chown root:root /etc/binance-bot.env
sudo chmod 600 /etc/binance-bot.env
```

Депозит, ризик, пари, таймфрейми і максимальне плече змінюються в `config.py`.

## 6. Пробний запуск (до systemd)

```bash
cd /opt/binance-bot
sudo -u binancebot env $(sudo cat /etc/binance-bot.env | xargs) ./venv/bin/python bot.py
```

У Telegram має прийти «Бот запущено», а в консолі — «Історія завантажена» для кожного стріму.
Зупиніть через `Ctrl+C` (перевірте, що прийшло «Бот зупинено» і в логах «Завершено коректно»).

## 7. Автозапуск (systemd)

```bash
sudo tee /etc/systemd/system/binance-bot.service >/dev/null <<'EOF'
[Unit]
Description=Binance Signal Bot
After=network-online.target chrony.service
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=binancebot
Group=binancebot
WorkingDirectory=/opt/binance-bot
EnvironmentFile=/etc/binance-bot.env
ExecStart=/opt/binance-bot/venv/bin/python bot.py
Restart=always
RestartSec=10
KillSignal=SIGTERM
TimeoutStopSec=45
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/opt/binance-bot

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now binance-bot
systemctl status binance-bot
journalctl -u binance-bot -f        # живі логи
tail -f /opt/binance-bot/bot.log    # файловий лог (ротація 5 МБ × 3)
```

`Restart=always` перезапускає процес після будь-якого падіння, `TimeoutStopSec=45` дає боту час
на graceful shutdown (закрити сокети, дослати чергу Telegram).

## 8. Зовнішній health-check (heartbeat.txt)

Бот щохвилини оновлює `/opt/binance-bot/heartbeat.txt`. Якщо файл не оновлювався понад 3 хвилини
(процес завис), примусово перезапускаємо сервіс:

```bash
sudo tee /usr/local/bin/binance-bot-health.sh >/dev/null <<'EOF'
#!/bin/bash
F=/opt/binance-bot/heartbeat.txt
if [ ! -f "$F" ] || [ -n "$(find "$F" -mmin +3)" ]; then
    logger -t binance-bot-health "heartbeat застарів — перезапуск"
    systemctl restart binance-bot
fi
EOF
sudo chmod +x /usr/local/bin/binance-bot-health.sh
echo '*/2 * * * * root /usr/local/bin/binance-bot-health.sh' | sudo tee /etc/cron.d/binance-bot-health
```

Додатково можна підключити `healthchecks.io`: додайте у cron `curl -fsS https://hc-ping.com/<uuid>`
лише коли файл свіжий — отримаєте сповіщення, якщо весь сервер упав.

## 9. Безпека сервера

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow OpenSSH
sudo ufw enable
```

Вхідні порти боту не потрібні (він лише ініціює вихідні з'єднання). Вимкніть вхід по паролю в SSH
(`PasswordAuthentication no`) і використовуйте ключі.

## 10. Оновлення і обслуговування

```bash
sudo systemctl stop binance-bot
# оновіть файли в /opt/binance-bot, за потреби: venv/bin/pip install -r requirements.txt
sudo systemctl start binance-bot
```

- Стан (`bot_state.db`) переживає перезапуски: дублі сигналів неможливі, а недоставлені
  повідомлення (до 6 годин) дошлються автоматично.
- Резервна копія: `sqlite3 /opt/binance-bot/bot_state.db ".backup /root/bot_state.bak"`.
- Якщо в логах часто з'являється «Watchdog: немає даних 90 с» на малоліквідних парах, додайте
  в `config.py` ліквідніші пари або збільшіть `WATCHDOG_TIMEOUT_SEC`.
- Сигнали — це інформаційні підказки, а не гарантія прибутку. Перед реальною торгівлею перевірте
  стратегію на історичних даних.

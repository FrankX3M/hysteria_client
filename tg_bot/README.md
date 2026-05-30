# sing-box Telegram Bot

Бот для управления конфигурациями sing-box VLESS+Reality через Telegram.
Поддерживает роли **admin** и **user**, генерацию QR-кодов и VLESS-ссылок.

---

## Установка

```bash
pip install python-telegram-bot qrcode[pil] Pillow
```

## Настройка

Отредактируй блок `CONFIG` в `singbox_bot.py` или задай переменные окружения:

| Переменная          | Описание                                              | По умолчанию |
|---------------------|-------------------------------------------------------|--------------|
| `BOT_TOKEN`         | Токен от @BotFather                                   | —            |
| `ADMIN_IDS`         | TG ID админов через запятую (`123456,789012`)         | —            |
| `SERVER_IP`         | Внешний IP сервера (автоопределяется если пусто)      | автo         |
| `SB_BASE_PORT`      | Начальный порт для новых конфигов                     | `8444`       |
| `OPEN_REGISTRATION` | `true` — юзеры могут сами зарегаться, `false` — только через `/adduser` | `true` |

## Запуск

```bash
# Напрямую
BOT_TOKEN=xxx ADMIN_IDS=123456 python3 singbox_bot.py

# Через systemd (рекомендуется)
```

### systemd unit `/etc/systemd/system/singbox-bot.service`

```ini
[Unit]
Description=sing-box Telegram Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/singbox-bot
Environment=BOT_TOKEN=YOUR_TOKEN_HERE
Environment=ADMIN_IDS=123456789
Environment=OPEN_REGISTRATION=true
ExecStart=/usr/bin/python3 /opt/singbox-bot/singbox_bot.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now singbox-bot
```

---

## Функционал

### 👤 Пользователь

| Действие              | Описание                                               |
|-----------------------|--------------------------------------------------------|
| `/start`              | Главное меню, автосоздание конфига при первом заходе   |
| 📋 Мой конфиг         | Просмотр своего конфига (UUID, порт, SNI, статус)      |
| 🔗 Получить ссылку    | VLESS-ссылка для импорта в клиент                      |
| 📷 QR-код             | QR-код для сканирования в мобильных клиентах           |
| 🔄 Обновить конфиг    | Перевыпуск UUID (новая ссылка + QR)                    |

### 👑 Администратор (все действия юзера +)

| Действие              | Описание                                               |
|-----------------------|--------------------------------------------------------|
| 📡 Все конфиги        | Список всех конфигов с статусом                        |
| 🟢/🔴 Вкл/выкл       | Включить или отключить конфиг без удаления             |
| 🗑 Удалить            | Удалить конфиг из sing-box                             |
| 👥 Пользователи       | Список всех пользователей                              |
| 🔑 Сделать админом    | Повышение роли пользователя                            |
| 🚫 Заблокировать      | Запрет доступа к боту                                  |
| ⚙️ Статус сервиса     | Версия sing-box, кол-во конфигов, IP сервера           |
| `/adduser <id>`       | Добавить пользователя вручную (для закрытой регистрации) |
| `/delconfig <id>`     | Удалить конфиг пользователя по TG ID                  |
| `/note <id> <текст>`  | Добавить заметку к пользователю                        |

---

## Логика работы

- Каждому пользователю — **один конфиг** (один порт, один UUID)
- При первом `/start` конфиг **создаётся автоматически**
- При **обновлении** меняется только UUID — порт остаётся прежним
- Все конфиги хранятся в SQLite (`/etc/sing-box/bot.db`)
- После каждого изменения бот **перезаписывает** `/etc/sing-box/config.json` и делает `systemctl restart sing-box`

---

## Требования

- Python 3.9+
- `sing-box` установлен в `/usr/local/bin/sing-box`
- `3x-ui` с VLESS+Reality inbound
- `hysteria2` с клиентским конфигом в `/etc/hysteria/client.yaml`
- Бот запускается от `root` (или с правами на `systemctl restart sing-box`)

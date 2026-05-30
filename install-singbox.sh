#!/bin/bash
# =============================================================================
# sing-box installer — автоустановка с конфигом из 3x-ui
# Использование: bash install-singbox.sh [PORT] [NAME]
#   PORT — порт для sing-box inbound (по умолчанию 8444)
#   NAME — имя конфига в ссылке (по умолчанию sing-box)

#   bash install-singbox.sh          # порт 8444 по умолчанию
#   bash install-singbox.sh 8445     # другой порт
#   bash install-singbox.sh 8444 myserver  # свой name в ссылке
# =============================================================================

set -euo pipefail

# --- Параметры ---
SB_PORT="${1:-8444}"
SB_NAME="${2:-sing-box}"
SB_BIN="/usr/local/bin/sing-box"
SB_CONF="/etc/sing-box/config.json"
SB_LOG="/var/log/sing-box.log"
XRAY_CONF="/usr/local/x-ui/bin/config.json"
XRAY_DB="/etc/x-ui/x-ui.db"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[✗]${NC} $*"; exit 1; }
info() { echo -e "    $*"; }

echo ""
echo "══════════════════════════════════════════"
echo "   sing-box installer"
echo "══════════════════════════════════════════"
echo ""

# --- Root check ---
[[ $EUID -ne 0 ]] && err "Запускай от root"

# =============================================================================
# 1. Читаем параметры из 3x-ui
# =============================================================================
echo "▶ Читаем конфиг 3x-ui..."

[[ ! -f "$XRAY_CONF" ]] && err "Не найден $XRAY_CONF — установлен ли 3x-ui?"
[[ ! -f "$XRAY_DB"   ]] && err "Не найден $XRAY_DB"

# Ищем inbound на нужном порту (приоритет) или первый VLESS+Reality
INBOUND=$(python3 - <<PYEOF
import json, sys

with open("$XRAY_CONF") as f:
    cfg = json.load(f)

target = None
fallback = None

for ib in cfg.get("inbounds", []):
    ss = ib.get("streamSettings", {})
    if ss.get("security") != "reality":
        continue
    if fallback is None:
        fallback = ib
    if ib.get("port") == 8443:
        target = ib
        break

chosen = target or fallback
if not chosen:
    print("NOT_FOUND")
    sys.exit(0)

print(json.dumps(chosen))
PYEOF
)

[[ "$INBOUND" == "NOT_FOUND" || -z "$INBOUND" ]] && err "VLESS+Reality inbound не найден в конфиге Xray"

# Парсим нужные поля
XRAY_PORT=$(echo "$INBOUND" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['port'])")
UUID=$(echo "$INBOUND" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['settings']['clients'][0]['id'])")
PRIVATE_KEY=$(echo "$INBOUND" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['streamSettings']['realitySettings']['privateKey'])")
PUBLIC_KEY=$(echo "$INBOUND" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['streamSettings']['realitySettings']['settings']['publicKey'])")
SHORT_IDS_JSON=$(echo "$INBOUND" | python3 -c "import json,sys; d=json.load(sys.stdin); ids=d['streamSettings']['realitySettings']['shortIds']; print(json.dumps(ids))")
SHORT_ID_FIRST=$(echo "$SHORT_IDS_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin)[0])")
SNI=$(echo "$INBOUND" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['streamSettings']['realitySettings']['serverNames'][0])" | sed 's|https\?://||;s|/.*||')

ok "Нашли VLESS+Reality inbound (port $XRAY_PORT)"
info "UUID:        $UUID"
info "SNI:         $SNI"
info "Short ID:    $SHORT_ID_FIRST"

# Читаем hysteria2 параметры из client.yaml
HY2_CONF="/etc/hysteria/client.yaml"
[[ ! -f "$HY2_CONF" ]] && err "Не найден $HY2_CONF"

HY2_SERVER=$(grep "^server:" "$HY2_CONF" | awk '{print $2}')
HY2_AUTH=$(grep "^auth:" "$HY2_CONF" | awk '{print $2}')
HY2_SNI=$(grep -A2 "^tls:" "$HY2_CONF" | grep "sni:" | awk '{print $2}')
HY2_OBFS_PASS=$(grep "password:" "$HY2_CONF" | tail -1 | awk '{print $2}')
HY2_HOST=$(echo "$HY2_SERVER" | cut -d: -f1)
HY2_PORT=$(echo "$HY2_SERVER" | cut -d: -f2)

ok "Нашли hysteria2 конфиг"
info "Server:      $HY2_SERVER"
info "Auth:        $HY2_AUTH"

# Определяем внешний IP сервера
SERVER_IP=$(curl -s --max-time 5 https://ifconfig.me || hostname -I | awk '{print $1}')
ok "IP сервера:  $SERVER_IP"

# =============================================================================
# 2. Устанавливаем sing-box (если не установлен или устарел)
# =============================================================================
echo ""
echo "▶ Устанавливаем sing-box..."

LATEST=$(curl -s https://api.github.com/repos/SagerNet/sing-box/releases/latest | grep '"tag_name"' | cut -d'"' -f4)
LATEST_VER="${LATEST#v}"

if command -v sing-box &>/dev/null; then
    CURRENT=$("$SB_BIN" version 2>/dev/null | grep "sing-box version" | awk '{print $3}' || echo "0")
    if [[ "$CURRENT" == "$LATEST_VER" ]]; then
        ok "sing-box $CURRENT уже установлен, пропускаем"
    else
        warn "Обновляем sing-box $CURRENT → $LATEST_VER"
        systemctl stop sing-box 2>/dev/null || true
        wget -q "https://github.com/SagerNet/sing-box/releases/download/${LATEST}/sing-box-${LATEST_VER}-linux-amd64.tar.gz" -O /tmp/sing-box.tar.gz
        tar -xzf /tmp/sing-box.tar.gz -C /tmp/
        cp "/tmp/sing-box-${LATEST_VER}-linux-amd64/sing-box" "$SB_BIN"
        chmod +x "$SB_BIN"
        rm -rf /tmp/sing-box*
        ok "sing-box обновлён до $LATEST_VER"
    fi
else
    wget -q "https://github.com/SagerNet/sing-box/releases/download/${LATEST}/sing-box-${LATEST_VER}-linux-amd64.tar.gz" -O /tmp/sing-box.tar.gz
    tar -xzf /tmp/sing-box.tar.gz -C /tmp/
    cp "/tmp/sing-box-${LATEST_VER}-linux-amd64/sing-box" "$SB_BIN"
    chmod +x "$SB_BIN"
    rm -rf /tmp/sing-box*
    ok "sing-box $LATEST_VER установлен"
fi

# =============================================================================
# 3. Генерируем конфиг
# =============================================================================
echo ""
echo "▶ Генерируем конфиг..."

mkdir -p /etc/sing-box

python3 - <<PYEOF
import json

short_ids = $SHORT_IDS_JSON

config = {
    "log": {
        "level": "info",
        "output": "$SB_LOG"
    },
    "inbounds": [
        {
            "type": "vless",
            "tag": "vless-in",
            "listen": "0.0.0.0",
            "listen_port": $SB_PORT,
            "users": [
                {
                    "uuid": "$UUID",
                    "flow": "xtls-rprx-vision"
                }
            ],
            "tls": {
                "enabled": True,
                "server_name": "$SNI",
                "reality": {
                    "enabled": True,
                    "handshake": {
                        "server": "$SNI",
                        "server_port": 443
                    },
                    "private_key": "$PRIVATE_KEY",
                    "short_id": short_ids
                }
            }
        }
    ],
    "outbounds": [
        {
            "type": "hysteria2",
            "tag": "hy2-out",
            "server": "$HY2_HOST",
            "server_port": $HY2_PORT,
            "password": "$HY2_AUTH",
            "obfs": {
                "type": "salamander",
                "password": "$HY2_OBFS_PASS"
            },
            "tls": {
                "enabled": True,
                "server_name": "$HY2_SNI"
            }
        },
        {
            "type": "direct",
            "tag": "direct"
        }
    ],
    "route": {
        "rules": [
            {
                "ip_is_private": True,
                "outbound": "direct"
            }
        ],
        "final": "hy2-out"
    }
}

with open("$SB_CONF", "w") as f:
    json.dump(config, f, indent=2, ensure_ascii=False)

print("OK")
PYEOF

ok "Конфиг записан в $SB_CONF"

# =============================================================================
# 4. Проверяем конфиг
# =============================================================================
echo ""
echo "▶ Валидация конфига..."

"$SB_BIN" check -c "$SB_CONF" && ok "Конфиг валиден" || err "Ошибка в конфиге"

# =============================================================================
# 5. Systemd сервис
# =============================================================================
echo ""
echo "▶ Настройка systemd..."

cat > /etc/systemd/system/sing-box.service << EOF
[Unit]
Description=sing-box service
After=network.target

[Service]
Type=simple
ExecStart=$SB_BIN run -c $SB_CONF
Restart=on-failure
RestartSec=3
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable sing-box &>/dev/null
systemctl restart sing-box
sleep 2

if systemctl is-active --quiet sing-box; then
    ok "sing-box запущен и добавлен в автозапуск"
else
    err "sing-box не запустился. Проверь: journalctl -u sing-box -n 20"
fi

# =============================================================================
# 6. Проверки
# =============================================================================
echo ""
echo "▶ Проверки..."

# Порт слушается
if ss -tlnp | grep -q ":$SB_PORT"; then
    ok "Порт $SB_PORT слушается"
else
    warn "Порт $SB_PORT не найден в ss — возможно сервис не запустился"
fi

# Hysteria2 туннель работает
HY2_IP=$(curl -s --max-time 10 --socks5 127.0.0.1:1080 https://ifconfig.me 2>/dev/null || echo "")
if [[ -n "$HY2_IP" ]]; then
    ok "Hysteria2 туннель работает (exit IP: $HY2_IP)"
else
    warn "Не удалось проверить hysteria2 туннель через SOCKS5 127.0.0.1:1080"
fi

# sing-box процесс
SB_PID=$(pgrep -f "sing-box run" || echo "")
if [[ -n "$SB_PID" ]]; then
    ok "sing-box процесс: PID $SB_PID"
else
    warn "Процесс sing-box не найден"
fi

# =============================================================================
# 7. Итог
# =============================================================================
VLESS_LINK="vless://${UUID}@${SERVER_IP}:${SB_PORT}?encryption=none&flow=xtls-rprx-vision&security=reality&sni=${SNI}&fp=chrome&pbk=${PUBLIC_KEY}&sid=${SHORT_ID_FIRST}&type=tcp#${SB_NAME}"

echo ""
echo "══════════════════════════════════════════"
echo -e "${GREEN}   Установка завершена!${NC}"
echo "══════════════════════════════════════════"
echo ""
echo "  Версия:    $("$SB_BIN" version | head -1)"
echo "  Конфиг:    $SB_CONF"
echo "  Лог:       $SB_LOG"
echo "  Порт:      $SB_PORT"
echo ""
echo "  Команды:"
echo "    systemctl status sing-box"
echo "    tail -f $SB_LOG"
echo "    journalctl -u sing-box -f"
echo ""
echo "──────────────────────────────────────────"
echo "  Ссылка для клиента:"
echo ""
echo "  $VLESS_LINK"
echo ""
echo "──────────────────────────────────────────"
echo ""

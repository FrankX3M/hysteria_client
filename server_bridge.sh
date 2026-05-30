#!/bin/bash
# ============================================================
#  Hysteria2 CLIENT — Server1
#  Подключение к Server2 (hy2.1rtp.ru:443)
#  Поднимает локальный SOCKS5 (:1080) и HTTP-прокси (:1081)
# ============================================================

set -eu

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

log_ok()   { echo -e "${GREEN}✓ $1${NC}"; }
log_warn() { echo -e "${YELLOW}⚠ $1${NC}"; }
log_err()  { echo -e "${RED}✗ $1${NC}"; exit 1; }
log_info() { echo -e "${CYAN}ℹ $1${NC}"; }
log_step() { echo -e "\n${CYAN}${BOLD}▶ $1${NC}"; }

[[ $EUID -ne 0 ]] && log_err "Запустите от root: sudo bash $0"

# ── Параметры Server2 (из ссылки) ────────────────────────────
SERVER_ADDR="hy2.1rtp.ru"
SERVER_PORT="443"
SERVER_PASS="lAVRsRoveHLw9IgLwIg8gAYDyJv0FvQI"
SERVER_SNI="hy2.1rtp.ru"
OBFS_PASS="fZZwBcujESP4eptW"

# ── Локальные порты на Server1 ────────────────────────────────
SOCKS5_PORT="1080"   # SOCKS5 прокси — для sing-box/xray outbound
HTTP_PORT="1081"     # HTTP прокси — резервный

# ─────────────────────────────────────────────────────────────

log_step "Установка Hysteria2..."
if command -v hysteria &>/dev/null; then
    log_warn "Hysteria2 уже установлена: $(hysteria version 2>/dev/null | head -1)"
else
    bash <(curl -fsSL https://get.hy2.sh/)
    log_ok "Hysteria2 установлена"
fi

log_step "Создание конфига клиента..."
mkdir -p /etc/hysteria

cat > /etc/hysteria/client.yaml << EOF
# ── Server2 ────────────────────────────────────────────────
server: ${SERVER_ADDR}:${SERVER_PORT}

auth: ${SERVER_PASS}

tls:
  sni: ${SERVER_SNI}
  insecure: false

obfs:
  type: salamander
  salamander:
    password: ${OBFS_PASS}

# ── Локальные прокси на Server1 ────────────────────────────
socks5:
  listen: 127.0.0.1:${SOCKS5_PORT}

http:
  listen: 127.0.0.1:${HTTP_PORT}

# ── Оптимизация ─────────────────────────────────────────────
bandwidth:
  up: 100 mbps
  down: 200 mbps

# ── Маршруты: трафик до Server2 идёт мимо туннеля ──────────
# (критично — защита от петли маршрутизации)
transport:
  udp:
    hopInterval: 30s
EOF

log_ok "Конфиг создан: /etc/hysteria/client.yaml"

log_step "Создание systemd-сервиса..."
cat > /etc/systemd/system/hysteria-client.service << EOF
[Unit]
Description=Hysteria2 Client → Server2
After=network.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/hysteria client --config /etc/hysteria/client.yaml
Restart=always
RestartSec=5
StartLimitIntervalSec=60
StartLimitBurst=5
Environment=HYSTERIA_LOG_LEVEL=info
CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_BIND_SERVICE CAP_NET_RAW
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_BIND_SERVICE CAP_NET_RAW

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable hysteria-client
systemctl restart hysteria-client
sleep 3

log_step "Проверка статуса..."
if systemctl is-active --quiet hysteria-client; then
    log_ok "hysteria-client запущен"
else
    log_warn "Сервис не запустился. Лог:"
    journalctl -xeu hysteria-client --no-pager -n 20
    exit 1
fi

log_step "Защита от петли маршрутизации..."
# Трафик до IP Server2 должен идти через основной шлюз,
# а не обратно в туннель
SERVER_IP=$(dig +short A "${SERVER_ADDR}" 2>/dev/null | tail -1 \
            || getent hosts "${SERVER_ADDR}" | awk '{print $1}')
GW=$(ip route | awk '/^default/{print $3; exit}')
DEV=$(ip route | awk '/^default/{print $5; exit}')

if [[ -n "$SERVER_IP" && -n "$GW" ]]; then
    # Удаляем старый маршрут если есть
    ip route del "${SERVER_IP}/32" 2>/dev/null || true
    ip route add "${SERVER_IP}/32" via "$GW" dev "$DEV"
    log_ok "Маршрут до Server2 (${SERVER_IP}) → через ${GW} (${DEV})"

    # Сохраняем маршрут в /etc/rc.local чтобы выжил после ребута
    RC="/etc/rc.local"
    if [[ ! -f "$RC" ]]; then
        echo '#!/bin/bash' > "$RC"
        chmod +x "$RC"
    fi
    grep -q "$SERVER_IP" "$RC" 2>/dev/null || \
        echo "ip route add ${SERVER_IP}/32 via ${GW} dev ${DEV} 2>/dev/null || true" >> "$RC"
    log_ok "Маршрут сохранён в ${RC}"
else
    log_warn "Не удалось определить IP Server2 или шлюз — добавьте маршрут вручную:"
    log_warn "  ip route add <IP_SERVER2>/32 via <ВАШ_ШЛЮ> dev <ИНТЕРФЕЙС>"
fi

log_step "Тест соединения через туннель..."
sleep 2
TEST=$(curl -s --max-time 10 \
    --socks5 "127.0.0.1:${SOCKS5_PORT}" \
    https://ifconfig.me 2>/dev/null || echo "")

if [[ -n "$TEST" ]]; then
    log_ok "Туннель работает! Внешний IP через Server2: ${TEST}"
else
    log_warn "Тест не прошёл — туннель ещё поднимается или есть проблема"
    log_warn "Проверьте вручную:"
    log_warn "  curl --socks5 127.0.0.1:${SOCKS5_PORT} https://ifconfig.me"
    log_warn "  journalctl -xeu hysteria-client -f"
fi

echo ""
echo -e "${GREEN}${BOLD}══════════════════════════════════════════${NC}"
echo -e "${GREEN}${BOLD}  Hysteria2 Client готов!${NC}"
echo -e "${GREEN}${BOLD}══════════════════════════════════════════${NC}"
echo -e "  SOCKS5:  ${CYAN}127.0.0.1:${SOCKS5_PORT}${NC}"
echo -e "  HTTP:    ${CYAN}127.0.0.1:${HTTP_PORT}${NC}"
echo -e ""
echo -e "  Следующий шаг: настройка входящего"
echo -e "  соединения на Server1 (VLESS/Naive)"
echo -e "  которое будет форвардить трафик в SOCKS5"
echo ""
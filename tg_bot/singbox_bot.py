#!/usr/bin/env python3
# =============================================================================
# sing-box Telegram Bot
# Управление конфигурациями VLESS+Reality через Telegram
#
# Установка зависимостей:
#   pip install python-telegram-bot qrcode[pil] Pillow
#
# Запуск:
#   python3 singbox_bot.py
#
# Переменные окружения (или задай прямо в CONFIG ниже):
#   BOT_TOKEN      — токен бота от @BotFather
#   ADMIN_IDS      — Telegram ID админов через запятую (например: 123456,789012)
#   SERVER_IP      — внешний IP сервера (автоопределяется если не задан)
#   SB_BASE_PORT   — начальный порт для новых конфигов (по умолчанию 8444)
# =============================================================================

import os
import json
import uuid
import asyncio
import logging
import subprocess
import io
import re
import sqlite3
from datetime import datetime
from pathlib import Path

import qrcode
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, ConversationHandler, MessageHandler, filters
)
from telegram.constants import ParseMode

# =============================================================================
# CONFIG — измени под себя
# =============================================================================
CONFIG = {
    "BOT_TOKEN":    os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE"),
    "ADMIN_IDS":    [int(x) for x in os.getenv("ADMIN_IDS", "0").split(",") if x.strip().isdigit()],
    "SERVER_IP":    os.getenv("SERVER_IP", ""),          # автоопределение если пусто
    "SB_BASE_PORT": int(os.getenv("SB_BASE_PORT", "8444")),
    "SB_BIN":       "/usr/local/bin/sing-box",
    "SB_CONF_DIR":  "/etc/sing-box",
    "SB_LOG":       "/var/log/sing-box.log",
    "XRAY_CONF":    "/usr/local/x-ui/bin/config.json",
    "HY2_CONF":     "/etc/hysteria/client.yaml",
    "DB_PATH":      "/etc/sing-box/bot.db",
    "SYSTEMD_UNIT": "sing-box",
    # Разрешить ли пользователям (не-админам) регистрироваться самостоятельно
    # False = только по инвайту/разрешению от админа
    "OPEN_REGISTRATION": bool(os.getenv("OPEN_REGISTRATION", "true").lower() in ("1","true","yes")),
}

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO
)
log = logging.getLogger("singbox-bot")

# ConversationHandler states
AWAIT_BLOCK_REASON = 1

# =============================================================================
# Database
# =============================================================================
class DB:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _conn(self):
        return sqlite3.connect(self.path)

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    tg_id       INTEGER PRIMARY KEY,
                    username    TEXT,
                    full_name   TEXT,
                    role        TEXT DEFAULT 'user',   -- 'admin' | 'user' | 'blocked'
                    created_at  TEXT,
                    note        TEXT
                );
                CREATE TABLE IF NOT EXISTS configs (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    tg_id       INTEGER NOT NULL,
                    name        TEXT NOT NULL,
                    port        INTEGER NOT NULL UNIQUE,
                    uuid        TEXT NOT NULL,
                    sni         TEXT,
                    public_key  TEXT,
                    short_id    TEXT,
                    server_ip   TEXT,
                    enabled     INTEGER DEFAULT 1,
                    created_at  TEXT,
                    updated_at  TEXT,
                    FOREIGN KEY(tg_id) REFERENCES users(tg_id)
                );
            """)

    # --- Users ---
    def upsert_user(self, tg_id, username, full_name, role=None):
        with self._conn() as c:
            exists = c.execute("SELECT role FROM users WHERE tg_id=?", (tg_id,)).fetchone()
            if exists:
                c.execute(
                    "UPDATE users SET username=?, full_name=? WHERE tg_id=?",
                    (username, full_name, tg_id)
                )
                return exists[0]
            else:
                r = role or "user"
                c.execute(
                    "INSERT INTO users(tg_id,username,full_name,role,created_at) VALUES(?,?,?,?,?)",
                    (tg_id, username, full_name, r, _now())
                )
                return r

    def get_user(self, tg_id):
        with self._conn() as c:
            row = c.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()
            if row:
                return dict(zip([d[0] for d in c.description], row))
        return None

    def get_all_users(self):
        with self._conn() as c:
            rows = c.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
            cols = [d[0] for d in c.description]
            return [dict(zip(cols, r)) for r in rows]

    def set_role(self, tg_id, role):
        with self._conn() as c:
            c.execute("UPDATE users SET role=? WHERE tg_id=?", (role, tg_id))

    def set_note(self, tg_id, note):
        with self._conn() as c:
            c.execute("UPDATE users SET note=? WHERE tg_id=?", (note, tg_id))

    # --- Configs ---
    def add_config(self, tg_id, name, port, uuid_, sni, public_key, short_id, server_ip):
        with self._conn() as c:
            c.execute(
                """INSERT INTO configs(tg_id,name,port,uuid,sni,public_key,short_id,server_ip,enabled,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,1,?,?)""",
                (tg_id, name, port, uuid_, sni, public_key, short_id, server_ip, _now(), _now())
            )
            return c.lastrowid

    def get_config_by_user(self, tg_id):
        with self._conn() as c:
            row = c.execute("SELECT * FROM configs WHERE tg_id=?", (tg_id,)).fetchone()
            if row:
                return dict(zip([d[0] for d in c.description], row))
        return None

    def get_config_by_id(self, cfg_id):
        with self._conn() as c:
            row = c.execute("SELECT * FROM configs WHERE id=?", (cfg_id,)).fetchone()
            if row:
                return dict(zip([d[0] for d in c.description], row))
        return None

    def get_all_configs(self):
        with self._conn() as c:
            rows = c.execute(
                "SELECT c.*, u.username, u.full_name FROM configs c LEFT JOIN users u ON c.tg_id=u.tg_id ORDER BY c.created_at DESC"
            ).fetchall()
            cols = [d[0] for d in c.description]
            return [dict(zip(cols, r)) for r in rows]

    def update_config(self, cfg_id, **kwargs):
        kwargs["updated_at"] = _now()
        sets = ", ".join(f"{k}=?" for k in kwargs)
        vals = list(kwargs.values()) + [cfg_id]
        with self._conn() as c:
            c.execute(f"UPDATE configs SET {sets} WHERE id=?", vals)

    def delete_config(self, cfg_id):
        with self._conn() as c:
            c.execute("DELETE FROM configs WHERE id=?", (cfg_id,))

    def next_free_port(self, base):
        with self._conn() as c:
            used = {r[0] for r in c.execute("SELECT port FROM configs").fetchall()}
        port = base
        while port in used:
            port += 1
        return port


db = DB(CONFIG["DB_PATH"])

# =============================================================================
# sing-box helpers
# =============================================================================

def _now():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")


def get_server_ip() -> str:
    if CONFIG["SERVER_IP"]:
        return CONFIG["SERVER_IP"]
    try:
        r = subprocess.run(["curl", "-s", "--max-time", "5", "https://ifconfig.me"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip()
    except Exception:
        return "127.0.0.1"


def load_xray_reality() -> dict:
    """Читает первый VLESS+Reality inbound из xray конфига."""
    with open(CONFIG["XRAY_CONF"]) as f:
        cfg = json.load(f)
    for ib in cfg.get("inbounds", []):
        ss = ib.get("streamSettings", {})
        if ss.get("security") == "reality":
            rs = ss["realitySettings"]
            return {
                "private_key": rs["privateKey"],
                "public_key":  rs["settings"]["publicKey"],
                "short_ids":   rs["shortIds"],
                "sni":         rs["serverNames"][0].replace("https://", "").split("/")[0],
            }
    raise RuntimeError("VLESS+Reality inbound не найден в xray конфиге")


def load_hy2() -> dict:
    with open(CONFIG["HY2_CONF"]) as f:
        content = f.read()

    def extract(key, block=None):
        if block:
            m = re.search(rf"{block}:.*?{key}:\s*(\S+)", content, re.DOTALL)
        else:
            m = re.search(rf"^{key}:\s*(\S+)", content, re.MULTILINE)
        return m.group(1) if m else ""

    server = extract("server")
    host, _, port = server.rpartition(":")
    return {
        "server": server,
        "host": host,
        "port": int(port) if port.isdigit() else 443,
        "auth": extract("auth"),
        "sni": extract("sni"),
        "obfs_pass": re.findall(r"password:\s*(\S+)", content)[-1] if "password:" in content else "",
    }


def build_sb_config(configs: list) -> dict:
    """Строит единый конфиг sing-box из списка конфигов БД."""
    try:
        hy2 = load_hy2()
        xray = load_xray_reality()
    except Exception as e:
        raise RuntimeError(f"Ошибка чтения конфигов xray/hy2: {e}")

    inbounds = []
    for cfg in configs:
        if not cfg["enabled"]:
            continue
        inbounds.append({
            "type": "vless",
            "tag": f"vless-in-{cfg['id']}",
            "listen": "0.0.0.0",
            "listen_port": cfg["port"],
            "users": [{"uuid": cfg["uuid"], "flow": "xtls-rprx-vision"}],
            "tls": {
                "enabled": True,
                "server_name": cfg["sni"],
                "reality": {
                    "enabled": True,
                    "handshake": {"server": cfg["sni"], "server_port": 443},
                    "private_key": xray["private_key"],
                    "short_id": xray["short_ids"],
                }
            }
        })

    return {
        "log": {"level": "info", "output": CONFIG["SB_LOG"]},
        "inbounds": inbounds,
        "outbounds": [
            {
                "type": "hysteria2",
                "tag": "hy2-out",
                "server": hy2["host"],
                "server_port": hy2["port"],
                "password": hy2["auth"],
                "obfs": {"type": "salamander", "password": hy2["obfs_pass"]},
                "tls": {"enabled": True, "server_name": hy2["sni"]}
            },
            {"type": "direct", "tag": "direct"}
        ],
        "route": {
            "rules": [{"ip_is_private": True, "outbound": "direct"}],
            "final": "hy2-out"
        }
    }


def apply_sb_config():
    """Перезаписывает /etc/sing-box/config.json и перезапускает сервис."""
    configs = db.get_all_configs()
    sb_cfg = build_sb_config(configs)
    path = Path(CONFIG["SB_CONF_DIR"]) / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(sb_cfg, f, indent=2, ensure_ascii=False)

    # Валидация
    r = subprocess.run([CONFIG["SB_BIN"], "check", "-c", str(path)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"Ошибка валидации конфига:\n{r.stderr}")

    subprocess.run(["systemctl", "restart", CONFIG["SYSTEMD_UNIT"]], check=True)


def make_vless_link(cfg: dict) -> str:
    return (
        f"vless://{cfg['uuid']}@{cfg['server_ip']}:{cfg['port']}"
        f"?encryption=none&flow=xtls-rprx-vision"
        f"&security=reality&sni={cfg['sni']}"
        f"&fp=chrome&pbk={cfg['public_key']}"
        f"&sid={cfg['short_id']}&type=tcp"
        f"#{cfg['name']}"
    )


def make_qr(text: str) -> bytes:
    img = qrcode.make(text)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def status_emoji(enabled: bool) -> str:
    return "🟢" if enabled else "🔴"


# =============================================================================
# Auth helpers
# =============================================================================

def is_admin(tg_id: int) -> bool:
    if tg_id in CONFIG["ADMIN_IDS"]:
        return True
    u = db.get_user(tg_id)
    return u and u["role"] == "admin"


def register_user(update: Update) -> dict:
    u = update.effective_user
    role = "admin" if u.id in CONFIG["ADMIN_IDS"] else "user"
    db.upsert_user(u.id, u.username or "", u.full_name or "", role)
    return db.get_user(u.id)


def check_access(user: dict) -> bool:
    if not user:
        return False
    return user["role"] in ("admin", "user")


# =============================================================================
# Keyboards
# =============================================================================

def kb_main(is_admin_user: bool) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("📋 Мой конфиг", callback_data="my_config")],
        [InlineKeyboardButton("🔗 Получить ссылку", callback_data="my_link"),
         InlineKeyboardButton("📷 QR-код", callback_data="my_qr")],
        [InlineKeyboardButton("🔄 Обновить конфиг", callback_data="refresh_config")],
    ]
    if is_admin_user:
        buttons += [
            [InlineKeyboardButton("━━━ Админ панель ━━━", callback_data="noop")],
            [InlineKeyboardButton("👥 Пользователи", callback_data="admin_users"),
             InlineKeyboardButton("📡 Все конфиги", callback_data="admin_configs")],
            [InlineKeyboardButton("⚙️ Статус сервиса", callback_data="admin_status")],
        ]
    return InlineKeyboardMarkup(buttons)


def kb_config_actions(cfg_id: int, enabled: bool, is_admin_user: bool) -> InlineKeyboardMarkup:
    toggle_label = "🔴 Отключить" if enabled else "🟢 Включить"
    buttons = [
        [InlineKeyboardButton("🔗 Ссылка", callback_data=f"link_{cfg_id}"),
         InlineKeyboardButton("📷 QR", callback_data=f"qr_{cfg_id}")],
    ]
    if is_admin_user:
        buttons += [
            [InlineKeyboardButton(toggle_label, callback_data=f"toggle_{cfg_id}"),
             InlineKeyboardButton("🗑 Удалить", callback_data=f"delete_{cfg_id}")],
        ]
    buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(buttons)


def kb_user_actions(tg_id: int, role: str) -> InlineKeyboardMarkup:
    buttons = []
    cfg = db.get_config_by_user(tg_id)
    if cfg:
        buttons.append([InlineKeyboardButton(f"📋 Конфиг #{cfg['id']}", callback_data=f"admin_cfg_{cfg['id']}")])

    if role == "blocked":
        buttons.append([InlineKeyboardButton("✅ Разблокировать", callback_data=f"unblock_{tg_id}")])
    elif role == "user":
        buttons += [
            [InlineKeyboardButton("🔑 Сделать админом", callback_data=f"makeadmin_{tg_id}"),
             InlineKeyboardButton("🚫 Заблокировать", callback_data=f"block_{tg_id}")],
        ]
    elif role == "admin":
        buttons.append([InlineKeyboardButton("👤 Разжаловать", callback_data=f"demote_{tg_id}")])

    buttons.append([InlineKeyboardButton("◀️ К списку", callback_data="admin_users")])
    return InlineKeyboardMarkup(buttons)


# =============================================================================
# /start
# =============================================================================

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = register_user(update)

    if user["role"] == "blocked":
        await update.message.reply_text("⛔ Ваш аккаунт заблокирован.")
        return

    if not CONFIG["OPEN_REGISTRATION"] and not is_admin(update.effective_user.id):
        existing = db.get_user(update.effective_user.id)
        if not existing or existing["role"] not in ("admin", "user"):
            await update.message.reply_text(
                "👋 Привет! Регистрация по инвайту. Обратитесь к администратору."
            )
            return

    name = update.effective_user.first_name
    admin_tag = " (admin)" if is_admin(update.effective_user.id) else ""
    await update.message.reply_text(
        f"👋 Привет, *{name}*{admin_tag}!\n\n"
        f"Этот бот управляет конфигурациями sing-box VLESS+Reality.\n"
        f"Каждый пользователь может иметь один конфиг.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb_main(is_admin(update.effective_user.id))
    )


# =============================================================================
# Callback handlers
# =============================================================================

async def cb_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    tg_id = q.from_user.id
    admin = is_admin(tg_id)

    user = db.get_user(tg_id)
    if not user or user["role"] == "blocked":
        await q.edit_message_text("⛔ Доступ запрещён.")
        return

    # ─── User: my config ───
    if data == "my_config":
        await show_my_config(q, tg_id, admin)

    elif data == "my_link":
        await send_my_link(q, tg_id)

    elif data == "my_qr":
        await send_my_qr(q, tg_id)

    elif data == "refresh_config":
        await do_refresh_config(q, tg_id, admin)

    # ─── Admin: config detail ───
    elif data.startswith("link_"):
        cfg_id = int(data.split("_")[1])
        await send_cfg_link(q, cfg_id, admin)

    elif data.startswith("qr_"):
        cfg_id = int(data.split("_")[1])
        await send_cfg_qr(q, cfg_id, admin)

    elif data.startswith("toggle_") and admin:
        cfg_id = int(data.split("_")[1])
        await do_toggle(q, cfg_id)

    elif data.startswith("delete_") and admin:
        cfg_id = int(data.split("_")[1])
        await do_delete(q, cfg_id)

    # ─── Admin: all configs ───
    elif data == "admin_configs" and admin:
        await show_admin_configs(q)

    elif data.startswith("admin_cfg_") and admin:
        cfg_id = int(data.split("_")[2])
        await show_cfg_detail(q, cfg_id, admin)

    # ─── Admin: users ───
    elif data == "admin_users" and admin:
        await show_admin_users(q)

    elif data.startswith("admin_user_") and admin:
        uid = int(data.split("_")[2])
        await show_user_detail(q, uid)

    elif data.startswith("makeadmin_") and admin:
        uid = int(data.split("_")[1])
        db.set_role(uid, "admin")
        await q.edit_message_text(f"✅ Пользователь {uid} теперь админ.",
                                   reply_markup=InlineKeyboardMarkup([[
                                       InlineKeyboardButton("◀️ Назад", callback_data="admin_users")
                                   ]]))

    elif data.startswith("demote_") and admin:
        uid = int(data.split("_")[1])
        if uid in CONFIG["ADMIN_IDS"]:
            await q.answer("Нельзя разжаловать хардкод-админа!", show_alert=True)
            return
        db.set_role(uid, "user")
        await q.edit_message_text(f"✅ Пользователь {uid} разжалован до user.",
                                   reply_markup=InlineKeyboardMarkup([[
                                       InlineKeyboardButton("◀️ Назад", callback_data="admin_users")
                                   ]]))

    elif data.startswith("block_") and admin:
        uid = int(data.split("_")[1])
        if uid in CONFIG["ADMIN_IDS"]:
            await q.answer("Нельзя заблокировать хардкод-админа!", show_alert=True)
            return
        db.set_role(uid, "blocked")
        await q.edit_message_text(f"🚫 Пользователь {uid} заблокирован.",
                                   reply_markup=InlineKeyboardMarkup([[
                                       InlineKeyboardButton("◀️ Назад", callback_data="admin_users")
                                   ]]))

    elif data.startswith("unblock_") and admin:
        uid = int(data.split("_")[1])
        db.set_role(uid, "user")
        await q.edit_message_text(f"✅ Пользователь {uid} разблокирован.",
                                   reply_markup=InlineKeyboardMarkup([[
                                       InlineKeyboardButton("◀️ Назад", callback_data="admin_users")
                                   ]]))

    elif data == "admin_status" and admin:
        await show_status(q)

    elif data == "back_main":
        await q.edit_message_text(
            "Главное меню:",
            reply_markup=kb_main(admin)
        )

    elif data == "noop":
        pass


# =============================================================================
# User actions
# =============================================================================

async def show_my_config(q, tg_id: int, admin: bool):
    cfg = db.get_config_by_user(tg_id)
    if not cfg:
        # Автосоздаём конфиг
        try:
            cfg = await _create_config_for(tg_id, q.from_user.username or str(tg_id))
        except Exception as e:
            await q.edit_message_text(f"❌ Ошибка создания конфига:\n`{e}`",
                                       parse_mode=ParseMode.MARKDOWN)
            return

    st = status_emoji(cfg["enabled"])
    text = (
        f"*Ваш конфиг* #{cfg['id']}\n\n"
        f"{st} Статус: {'активен' if cfg['enabled'] else 'отключён'}\n"
        f"🏷 Имя: `{cfg['name']}`\n"
        f"🔌 Порт: `{cfg['port']}`\n"
        f"🔑 UUID: `{cfg['uuid']}`\n"
        f"🌐 SNI: `{cfg['sni']}`\n"
        f"📅 Создан: {cfg['created_at']}\n"
        f"🔄 Обновлён: {cfg['updated_at']}"
    )
    await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN,
                               reply_markup=kb_config_actions(cfg["id"], cfg["enabled"], admin))


async def send_my_link(q, tg_id: int):
    cfg = db.get_config_by_user(tg_id)
    if not cfg:
        await q.edit_message_text("У вас нет конфига. Нажмите «Мой конфиг» для создания.")
        return
    link = make_vless_link(cfg)
    await q.edit_message_text(
        f"🔗 *Ваша VLESS ссылка:*\n\n`{link}`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="back_main")]])
    )


async def send_my_qr(q, tg_id: int):
    cfg = db.get_config_by_user(tg_id)
    if not cfg:
        await q.edit_message_text("У вас нет конфига.")
        return
    link = make_vless_link(cfg)
    qr_bytes = make_qr(link)
    await q.message.reply_photo(
        photo=qr_bytes,
        caption=f"📷 QR-код вашего конфига *{cfg['name']}*\n\n`{link}`",
        parse_mode=ParseMode.MARKDOWN
    )
    await q.answer()


async def do_refresh_config(q, tg_id: int, admin: bool):
    """Обновляет UUID пользователя (перевыпуск конфига)."""
    cfg = db.get_config_by_user(tg_id)
    if not cfg:
        await show_my_config(q, tg_id, admin)
        return

    new_uuid = str(uuid.uuid4())
    db.update_config(cfg["id"], uuid=new_uuid)

    try:
        apply_sb_config()
    except Exception as e:
        await q.edit_message_text(f"❌ Ошибка применения конфига:\n`{e}`",
                                   parse_mode=ParseMode.MARKDOWN)
        return

    cfg = db.get_config_by_id(cfg["id"])
    link = make_vless_link(cfg)
    qr_bytes = make_qr(link)
    await q.message.reply_photo(
        photo=qr_bytes,
        caption=(
            f"✅ *Конфиг обновлён!*\n\n"
            f"🔑 Новый UUID: `{new_uuid}`\n\n"
            f"🔗 Ссылка:\n`{link}`"
        ),
        parse_mode=ParseMode.MARKDOWN
    )
    await q.answer("✅ Конфиг обновлён")


# =============================================================================
# Config creation
# =============================================================================

async def _create_config_for(tg_id: int, username: str) -> dict:
    xray = load_xray_reality()
    server_ip = get_server_ip()
    port = db.next_free_port(CONFIG["SB_BASE_PORT"])
    new_uuid = str(uuid.uuid4())
    name = f"user-{username}"
    short_id = xray["short_ids"][0] if xray["short_ids"] else ""

    db.add_config(
        tg_id=tg_id,
        name=name,
        port=port,
        uuid_=new_uuid,
        sni=xray["sni"],
        public_key=xray["public_key"],
        short_id=short_id,
        server_ip=server_ip,
    )
    apply_sb_config()
    return db.get_config_by_user(tg_id)


# =============================================================================
# Admin: config actions
# =============================================================================

async def send_cfg_link(q, cfg_id: int, admin: bool):
    cfg = db.get_config_by_id(cfg_id)
    if not cfg:
        await q.edit_message_text("Конфиг не найден.")
        return
    link = make_vless_link(cfg)
    await q.edit_message_text(
        f"🔗 *Ссылка конфига #{cfg_id}*\n\n`{link}`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ Назад", callback_data=f"admin_cfg_{cfg_id}")
        ]])
    )


async def send_cfg_qr(q, cfg_id: int, admin: bool):
    cfg = db.get_config_by_id(cfg_id)
    if not cfg:
        await q.answer("Конфиг не найден", show_alert=True)
        return
    link = make_vless_link(cfg)
    qr_bytes = make_qr(link)
    await q.message.reply_photo(
        photo=qr_bytes,
        caption=f"📷 QR-код конфига *{cfg['name']}* (#{cfg_id})\n\n`{link}`",
        parse_mode=ParseMode.MARKDOWN
    )


async def do_toggle(q, cfg_id: int):
    cfg = db.get_config_by_id(cfg_id)
    if not cfg:
        await q.answer("Конфиг не найден", show_alert=True)
        return
    new_state = 0 if cfg["enabled"] else 1
    db.update_config(cfg_id, enabled=new_state)
    try:
        apply_sb_config()
    except Exception as e:
        await q.answer(f"Ошибка: {e}", show_alert=True)
        return
    cfg = db.get_config_by_id(cfg_id)
    await show_cfg_detail(q, cfg_id, True)
    await q.answer(f"{'🟢 Включён' if new_state else '🔴 Отключён'}")


async def do_delete(q, cfg_id: int):
    cfg = db.get_config_by_id(cfg_id)
    if not cfg:
        await q.answer("Конфиг не найден", show_alert=True)
        return
    db.delete_config(cfg_id)
    try:
        apply_sb_config()
    except Exception as e:
        await q.edit_message_text(f"⚠️ Конфиг удалён из БД, но ошибка применения:\n`{e}`",
                                   parse_mode=ParseMode.MARKDOWN)
        return
    await q.edit_message_text(
        f"🗑 Конфиг #{cfg_id} удалён.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ К конфигам", callback_data="admin_configs")
        ]])
    )


# =============================================================================
# Admin: lists
# =============================================================================

async def show_admin_configs(q):
    configs = db.get_all_configs()
    if not configs:
        await q.edit_message_text(
            "Нет конфигов.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="back_main")]])
        )
        return

    buttons = []
    for c in configs:
        st = status_emoji(c["enabled"])
        label = f"{st} #{c['id']} {c['name']} :{c['port']}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"admin_cfg_{c['id']}")])
    buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="back_main")])

    await q.edit_message_text(
        f"📡 *Все конфиги* ({len(configs)} шт.)",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def show_cfg_detail(q, cfg_id: int, admin: bool):
    cfg = db.get_config_by_id(cfg_id)
    if not cfg:
        await q.edit_message_text("Конфиг не найден.")
        return
    st = status_emoji(cfg["enabled"])
    text = (
        f"*Конфиг #{cfg['id']}*\n\n"
        f"{st} Статус: {'активен' if cfg['enabled'] else 'отключён'}\n"
        f"🏷 Имя: `{cfg['name']}`\n"
        f"👤 TG ID: `{cfg['tg_id']}`\n"
        f"🔌 Порт: `{cfg['port']}`\n"
        f"🔑 UUID: `{cfg['uuid']}`\n"
        f"🌐 SNI: `{cfg['sni']}`\n"
        f"🖥 IP: `{cfg['server_ip']}`\n"
        f"📅 Создан: {cfg['created_at']}\n"
        f"🔄 Обновлён: {cfg['updated_at']}"
    )
    await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN,
                               reply_markup=kb_config_actions(cfg_id, cfg["enabled"], admin))


async def show_admin_users(q):
    users = db.get_all_users()
    if not users:
        await q.edit_message_text(
            "Нет пользователей.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="back_main")]])
        )
        return

    role_emoji = {"admin": "👑", "user": "👤", "blocked": "🚫"}
    buttons = []
    for u in users:
        em = role_emoji.get(u["role"], "❓")
        name = u["full_name"] or u["username"] or str(u["tg_id"])
        buttons.append([InlineKeyboardButton(
            f"{em} {name} ({u['role']})",
            callback_data=f"admin_user_{u['tg_id']}"
        )])
    buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="back_main")])

    await q.edit_message_text(
        f"👥 *Пользователи* ({len(users)} чел.)",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def show_user_detail(q, tg_id: int):
    u = db.get_user(tg_id)
    if not u:
        await q.edit_message_text("Пользователь не найден.")
        return
    cfg = db.get_config_by_user(tg_id)
    cfg_info = f"Конфиг #{cfg['id']} (порт {cfg['port']})" if cfg else "нет конфига"
    text = (
        f"*Пользователь*\n\n"
        f"👤 {u['full_name']} (@{u['username']})\n"
        f"🆔 TG ID: `{u['tg_id']}`\n"
        f"🔑 Роль: `{u['role']}`\n"
        f"📡 {cfg_info}\n"
        f"📅 Зарегистрирован: {u['created_at']}\n"
        f"📝 Заметка: {u['note'] or '—'}"
    )
    await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN,
                               reply_markup=kb_user_actions(tg_id, u["role"]))


# =============================================================================
# Admin: status
# =============================================================================

async def show_status(q):
    try:
        r = subprocess.run(
            ["systemctl", "is-active", CONFIG["SYSTEMD_UNIT"]],
            capture_output=True, text=True
        )
        active = r.stdout.strip() == "active"

        r2 = subprocess.run(
            [CONFIG["SB_BIN"], "version"],
            capture_output=True, text=True
        )
        version = r2.stdout.strip().split("\n")[0] if r2.returncode == 0 else "н/д"

        configs = db.get_all_configs()
        enabled = sum(1 for c in configs if c["enabled"])
        users = db.get_all_users()

        text = (
            f"⚙️ *Статус сервиса*\n\n"
            f"{'🟢 Активен' if active else '🔴 Остановлен'} — `{CONFIG['SYSTEMD_UNIT']}`\n"
            f"📦 Версия: `{version}`\n\n"
            f"📡 Конфигов: {len(configs)} (активных: {enabled})\n"
            f"👥 Пользователей: {len(users)}\n"
            f"🖥 IP: `{get_server_ip()}`"
        )
    except Exception as e:
        text = f"❌ Ошибка получения статуса:\n`{e}`"

    await q.edit_message_text(
        text, parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ Назад", callback_data="back_main")
        ]])
    )


# =============================================================================
# Admin commands
# =============================================================================

async def cmd_adduser(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Добавить пользователя по TG ID (только для случая OPEN_REGISTRATION=false)."""
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text("Использование: /adduser <tg_id> [username]")
        return
    uid = int(ctx.args[0])
    uname = ctx.args[1] if len(ctx.args) > 1 else str(uid)
    existing = db.get_user(uid)
    if existing:
        await update.message.reply_text(f"Пользователь {uid} уже есть (роль: {existing['role']}).")
        return
    db.upsert_user(uid, uname, uname, "user")
    await update.message.reply_text(f"✅ Пользователь {uid} добавлен.")


async def cmd_deluser(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Удалить конфиг пользователя по TG ID."""
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text("Использование: /delconfig <tg_id>")
        return
    uid = int(ctx.args[0])
    cfg = db.get_config_by_user(uid)
    if not cfg:
        await update.message.reply_text("У пользователя нет конфига.")
        return
    db.delete_config(cfg["id"])
    try:
        apply_sb_config()
        await update.message.reply_text(f"✅ Конфиг пользователя {uid} удалён и сервис перезапущен.")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Удалён из БД, ошибка применения: {e}")


async def cmd_note(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Добавить заметку к пользователю: /note <tg_id> <текст>"""
    if not is_admin(update.effective_user.id):
        return
    if len(ctx.args) < 2:
        await update.message.reply_text("Использование: /note <tg_id> <текст>")
        return
    uid = int(ctx.args[0])
    note = " ".join(ctx.args[1:])
    db.set_note(uid, note)
    await update.message.reply_text(f"✅ Заметка сохранена для {uid}.")


# =============================================================================
# Main
# =============================================================================

async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "Главное меню"),
        BotCommand("adduser", "[admin] Добавить пользователя вручную"),
        BotCommand("delconfig", "[admin] Удалить конфиг пользователя"),
        BotCommand("note", "[admin] Добавить заметку к пользователю"),
    ])


def main():
    token = CONFIG["BOT_TOKEN"]
    if not token or token == "YOUR_BOT_TOKEN_HERE":
        log.error("BOT_TOKEN не задан! Укажи в CONFIG или переменной окружения BOT_TOKEN.")
        return

    app = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("adduser", cmd_adduser))
    app.add_handler(CommandHandler("delconfig", cmd_deluser))
    app.add_handler(CommandHandler("note", cmd_note))
    app.add_handler(CallbackQueryHandler(cb_handler))

    log.info("Бот запущен.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

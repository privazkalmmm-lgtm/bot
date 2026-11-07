#!/usr/bin/env python3
"""
Stake Whale Monitor
Polls Stake's publicly exposed highroller sports feed and sends Telegram alerts.
"""
import json
import time
from pathlib import Path
from datetime import datetime, timezone
import getpass

import requests

BASE = Path(__file__).resolve().parent
CONFIG = BASE / "config.json"
SEEN_FILE = BASE / "seen_bets.json"
STATS_FILE = BASE / "stats.json"
USERS_FILE = BASE / "users.json"
QUEUE_FILE = BASE / "queue.json"  # Файл для копилки сигналов
WELCOME_IMG = BASE / "welcome.jpg"

STAKE_URL = "https://stake.com/_api/graphql"
BINANCE_URL = "https://api.binance.com/api/v3/ticker/price"
TELEGRAM_API = "https://api.telegram.org"

CREATOR_TAG = "@CrazyRup"
TRIAL_SECONDS = 24 * 3600
BATCH_SIZE = 10  # Сколько сигналов копим перед отправкой сводки

# Глобальный логгер в памяти
ACTIVITY_LOGS = []
LOGGING_ENABLED = True

QUERY = """
query GetHighrollers($limit: Int) {
  highrollerSportBets(limit: $limit) {
    id
    bet {
      ... on SportBet {
        id
        amount
        currency
        status
        active
        potentialMultiplier
        payoutMultiplier
        createdAt
        user { name preferenceHideBets }
        outcomes {
          odds
          fixtureName
          outcome { name }
          fixture {
            name
            slug
            tournament {
              name
              category { sport { name slug } }
            }
          }
        }
      }
    }
  }
}
"""

HEADERS = {
    "content-type": "application/json",
    "x-language": "en",
    "user-agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36",
}

STABLES = {"USD": 1.0, "USDT": 1.0, "USDC": 1.0, "BUSD": 1.0, "DAI": 1.0}

# ----------------------------- logging -----------------------------

def log_action(chat_id, username, action):
    if not LOGGING_ENABLED:
        return
    t = datetime.now().strftime("%H:%M:%S")
    name = f"@{username}" if username else str(chat_id)
    ACTIVITY_LOGS.append(f"[{t}] {name}: {action}")
    if len(ACTIVITY_LOGS) > 50:
        ACTIVITY_LOGS.pop(0)

# ----------------------------- config -----------------------------

def default_config():
    return {
        "telegram_token": "",
        "threshold_usd": 50000,
        "interval_seconds": 20,
        "chat_id": None,
        "admin_ids": [],
    }


def load_config():
    if CONFIG.exists():
        cfg = default_config()
        cfg.update(json.loads(CONFIG.read_text(encoding="utf-8")))
        if not isinstance(cfg.get("admin_ids"), list):
            cfg["admin_ids"] = []
        if cfg.get("chat_id") and not cfg.get("admin_ids"):
            cfg["admin_ids"] = [int(cfg["chat_id"])]
            save_config(cfg)
        return cfg

    print("\n=== Stake Whale Monitor setup ===")
    token = getpass.getpass("Paste your NEW Telegram bot token (input hidden): ").strip()
    if not token:
        raise SystemExit("Token is required.")
        
    admin_text = input("Введи свой Telegram ID (узнать у @userinfobot): ").strip()
    try:
        admin_id = int(admin_text)
    except ValueError:
        admin_id = None

    threshold_text = input("Minimum alert in USD [50000]: ").strip() or "50000"
    interval_text = input("Check interval in seconds [20]: ").strip() or "20"
    
    try:
        threshold = float(threshold_text)
        interval = max(15, int(interval_text))
    except ValueError:
        raise SystemExit("Threshold and interval must be numbers.")

    cfg = default_config()
    cfg.update({
        "telegram_token": token,
        "threshold_usd": threshold,
        "interval_seconds": interval,
    })
    if admin_id:
        cfg["admin_ids"] = [admin_id]
        
    save_config(cfg)
    return cfg


def save_config(cfg):
    CONFIG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    try:
        CONFIG.chmod(0o600)
    except OSError:
        pass


def is_admin(cfg, chat_id):
    try:
        return int(chat_id) in {int(x) for x in cfg.get("admin_ids", [])}
    except (TypeError, ValueError):
        return False


# ----------------------------- users -----------------------------

def load_users():
    if USERS_FILE.exists():
        try:
            return json.loads(USERS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_users(users):
    USERS_FILE.write_text(json.dumps(users, indent=2), encoding="utf-8")


def get_user(users, chat_id):
    key = str(chat_id)
    if key not in users:
        users[key] = {
            "trial_started_at": None, 
            "is_paid": False,
            "paid_until": None,
            "username": None,
        }
    else:
        users[key].setdefault("paid_until", None)
        users[key].setdefault("username", None)
        users[key].setdefault("trial_started_at", None)
    return users[key]


def trial_remaining(user):
    started = user.get("trial_started_at")
    if not started:
        return TRIAL_SECONDS 
    if user.get("is_paid") and not user.get("paid_until"):
        return None
    return max(0.0, TRIAL_SECONDS - (time.time() - float(started)))


def paid_until_remaining(user):
    if user.get("is_paid") and user.get("paid_until"):
        return max(0.0, float(user["paid_until"]) - time.time())
    return None


def trial_active(user):
    if user.get("is_paid"):
        if not user.get("paid_until"):
            return True
        return float(user["paid_until"]) > time.time()
    started = user.get("trial_started_at")
    if not started:
        return False 
    return (time.time() - float(started)) < TRIAL_SECONDS


def access_label(user):
    if user.get("is_paid"):
        if not user.get("paid_until"):
            return "💎 Платный доступ (бессрочно)"
        rem = paid_until_remaining(user)
        d = int(rem // 86400)
        h = int((rem % 86400) // 3600)
        if rem <= 0:
            return "⛔ Подписка истекла"
        return f"💎 Подписка: осталось {d}д {h}ч"
    started = user.get("trial_started_at")
    if not started:
        return "⏳ Доступ не активирован"
    rem = trial_remaining(user)
    if rem > 0:
        h = int(rem // 3600)
        m = int((rem % 3600) // 60)
        return f"🎁 Пробный период: осталось {h}ч {m}м"
    return "⛔ Пробный период истёк"


# ----------------------------- telegram -----------------------------

def telegram_call(token, method, payload=None, timeout=35):
    url = f"{TELEGRAM_API}/bot{token}/{method}"
    response = requests.post(url, json=payload or {}, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")
    return data["result"]


def answer_callback(token, callback_id, text=""):
    try:
        telegram_call(token, "answerCallbackQuery", {
            "callback_query_id": callback_id,
            "text": text,
        }, timeout=10)
    except Exception:
        pass


# ----------------------------- prices / stake -----------------------------

def get_binance_prices(session):
    prices = dict(STABLES)
    try:
        response = session.get(BINANCE_URL, timeout=12)
        response.raise_for_status()
        for row in response.json():
            symbol = row.get("symbol", "")
            if symbol.endswith("USDT"):
                asset = symbol[:-4]
                try:
                    prices[asset.upper()] = float(row["price"])
                except (KeyError, TypeError, ValueError):
                    pass
    except requests.RequestException:
        pass
    return prices


def to_usd(amount, currency, prices):
    cur = (currency or "").upper()
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return None
    rate = prices.get(cur)
    if rate is None:
        return None
    return value * rate


def fetch_bets(session):
    response = session.post(
        STAKE_URL,
        headers=HEADERS,
        json={"query": QUERY, "variables": {"limit": 25}},
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    if data.get("errors"):
        raise RuntimeError(f"Stake GraphQL returned errors: {data['errors']}")
    return (data.get("data") or {}).get("highrollerSportBets") or []


# ----------------------------- seen / stats / queue -----------------------------

def load_seen():
    if SEEN_FILE.exists():
        try:
            raw = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if isinstance(raw, list):
            now = time.time()
            return {str(x): now for x in raw}
        if isinstance(raw, dict):
            return {str(k): float(v) for k, v in raw.items()}
    return {}


def save_seen(seen, ttl_seconds=24 * 3600):
    cutoff = time.time() - ttl_seconds
    pruned = {k: v for k, v in seen.items() if v >= cutoff}
    SEEN_FILE.write_text(json.dumps(pruned), encoding="utf-8")
    return pruned


def load_stats():
    if STATS_FILE.exists():
        try:
            return json.loads(STATS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"alerts": 0, "total_usd": 0.0, "by_sport": {}, "top": []}


def save_stats(stats):
    STATS_FILE.write_text(json.dumps(stats, indent=2), encoding="utf-8")


def record_stat(stats, bet, usd, sport_name, sport_slug, created_iso):
    stats["alerts"] = stats.get("alerts", 0) + 1
    stats["total_usd"] = round(stats.get("total_usd", 0.0) + usd, 2)
    by_sport = stats.setdefault("by_sport", {})
    key = sport_slug or sport_name or "unknown"
    by_sport[key] = by_sport.get(key, 0) + 1
    top = stats.setdefault("top", [])
    top.append({
        "usd": round(usd, 2),
        "sport": sport_name or "—",
        "event": (bet.get("outcomes") or [{}])[0].get("fixtureName") or "—",
        "id": str(bet.get("id") or ""),
        "at": created_iso or datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    top.sort(key=lambda x: x["usd"], reverse=True)
    del top[5:]


def load_queue():
    if QUEUE_FILE.exists():
        try:
            return json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def save_queue(queue):
    QUEUE_FILE.write_text(json.dumps(queue, indent=2, ensure_ascii=False), encoding="utf-8")


# ----------------------------- formatting -----------------------------

def format_alert(bet, usd):
    user = bet.get("user") or {}
    raw_name = (user.get("name") or "").strip()
    hidden = user.get("preferenceHideBets") or not raw_name
    username = "скрыт пользователем" if hidden else raw_name

    outcomes = bet.get("outcomes") or []
    first = outcomes[0] if outcomes else {}
    fixture = first.get("fixture") or {}
    tournament = fixture.get("tournament") or {}
    category = tournament.get("category") or {}
    sport = category.get("sport") or {}

    sport_name = sport.get("name") or "Неизвестный спорт"
    event = first.get("fixtureName") or fixture.get("name") or "Событие не указано"
    selection = (first.get("outcome") or {}).get("name") or "Не указано"
    odds = first.get("odds")
    currency = bet.get("currency") or "?"
    amount = bet.get("amount", "?")
    multiplier = bet.get("potentialMultiplier")
    created = bet.get("createdAt")
    
    lines = [
        "🐋 <b>КРУПНАЯ СТАВКА STAKE</b>",
        f"💰 <b>Сумма:</b> ${usd:,.2f}",
        f"👤 <b>Игрок:</b> {username}",
        f"🏅 <b>Спорт:</b> {sport_name}",
        f"🎯 <b>Событие:</b> {event}",
        f"📌 <b>Выбор:</b> {selection}",
        f"📈 <b>Коэффициент:</b> {odds if odds is not None else '—'}",
        f"🪙 <b>Исходная сумма:</b> {amount} {currency}",
    ]
    if multiplier is not None:
        lines.append(f"✖️ <b>Потенциальный множитель:</b> {multiplier}")
    if created:
        lines.append(f"🕒 <b>Время Stake:</b> {created}")
        
    lines.append(f"\nМожете отблагодарить создателя {CREATOR_TAG}")
    return "\n".join(lines)


def format_batch(queue, batch_num):
    lines = [
        f"📦 <b>ПАЧКА СИГНАЛОВ #{batch_num}</b>",
        f"Всего сигналов: {len(queue)}",
        "Ниже список для проверки результатов нейросетью.\n"
    ]
    
    for i, item in enumerate(queue, 1):
        lines.append(f"<b>{i}. {item['sport']}</b> | {item['event']}")
        lines.append(f"   Выбор: {item['selection']} (Кэф: {item['odds']})")
        lines.append(f"   Сумма: ${item['usd']:,.0f}\n")
        
    lines.append("---")
    lines.append("<i>Скопируй этот текст и отправь нейросети вместе с промптом для проверки.</i>")
    return "\n".join(lines)


def format_stats(cfg, stats, uptime_seconds, processed, user):
    def fmt_duration(sec):
        h, rem = divmod(int(sec), 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h}ч {m}м"
        if m:
            return f"{m}м {s}с"
        return f"{s}с"

    lines = [
        "📊 <b>Статистика монитора</b>",
        "",
        f"🔑 <b>Доступ:</b> {access_label(user)}",
        f"🎚 <b>Порог:</b> ${float(cfg.get('threshold_usd', 0)):,.0f}",
        f"⏱ <b>Интервал:</b> {cfg.get('interval_seconds', '?')} сек",
        f"🕒 <b>Uptime:</b> {fmt_duration(uptime_seconds)}",
        f"🔎 <b>Обработано ставок:</b> {processed}",
        "",
        f"🚨 <b>Отправлено алертов:</b> {stats.get('alerts', 0)}",
        f"💰 <b>Суммарный объём:</b> ${stats.get('total_usd', 0):,.0f}",
    ]
    by_sport = stats.get("by_sport") or {}
    if by_sport:
        lines.append("")
        lines.append("🏆 <b>По видам спорта:</b>")
        for slug, count in sorted(by_sport.items(), key=lambda kv: kv[1], reverse=True)[:5]:
            lines.append(f"  • {slug}: {count}")
    top = stats.get("top") or []
    if top:
        lines.append("")
        lines.append("🐋 <b>Топ-5 ставок за сессию:</b>")
        for i, item in enumerate(top, 1):
            lines.append(f"  {i}. <b>${item['usd']:,.0f}</b> — {item.get('event', '—')}")
    return "\n".join(lines)


# ----------------------------- keyboards -----------------------------

def main_keyboard(cfg, chat_id):
    rows = [
        [{"text": "📊 Статистика"}, {"text": "🎚 Порог"}],
        [{"text": "⚙️ Настройки"}, {"text": "ℹ️ Помощь"}],
    ]
    if is_admin(cfg, chat_id):
        rows.append([{"text": "🛠 Админка"}])
    return {
        "keyboard": rows,
        "resize_keyboard": True,
        "is_persistent": True,
    }


WELCOME_KEYBOARD = {
    "inline_keyboard": [
        [{"text": "✅ Получить доступ (24ч)", "callback_data": "get_access"}],
        [{"text": "ℹ️ Информация о боте", "callback_data": "info"}],
    ]
}

PAY_KEYBOARD = {
    "inline_keyboard": [[
        {"text": "💳 Оплатить", "callback_data": "pay"},
    ]],
}

def get_admin_keyboard():
    status = "ВКЛ" if LOGGING_ENABLED else "ВЫКЛ"
    return {
        "inline_keyboard": [
            [{"text": "👥 Пользователи", "callback_data": "adm_users"}],
            [{"text": "📊 Сводка", "callback_data": "adm_summary"}],
            [{"text": f"🔔 Логи: {status}", "callback_data": "adm_toggle_logs"},
             {"text": "📝 Действия", "callback_data": "adm_view_logs"}],
            [{"text": "🎁 Выдать", "callback_data": "adm_grant_hint"},
             {"text": "⛔ Забрать", "callback_data": "adm_revoke_hint"}],
            [{"text": "❌ Закрыть", "callback_data": "adm_close"}],
        ],
    }

WELCOME_TEXT = (
    "🤖 <b>ScoreBot</b> — бот, который находит аномалии в киберспорте везде и сразу\n\n"
    "💸 <b>Крупные ставки на странные матчи</b>\n"
    "• Софт парсит ставки от $500 до $10.000.000 на Stake.com и показывает их в удобном формате. "
    "Следите, куда уходят крупные деньги в прямом эфире и находите прогрузы на матчи сомнительного характера. "
    "Фильтры отсеивают мусор и шум\n\n"
    "🇨🇳 <b>Китайские падения коэффициентов</b>\n"
    "• Софт фиксирует сильные падения коэффициентов по китайским букам. Выявить настоящий прогруз очень легко: "
    "пока в Китае кэф скатился с 5.00 до 2.00, на русских и европейских БК продолжает висеть 5.00, так как оснований для его снижения нету. "
    "Следим за ВСЕМИ китайскими, а так же русскими и европейскими БК, сравниваем кэфы и ловим реальные 322 без инсайдов\n\n"
    "✅ <b>Следить за всем вручную невозможно.</b>\n"
    "Нажми кнопку ниже, чтобы получить бесплатный доступ на 24 часа."
)

INFO_TEXT = (
    "ℹ️ <b>Информация</b>\n\n"
    "Этот бот отслеживает публичную ленту крупных ставок на Stake и присылает уведомления, "
    "когда сумма ставки превышает заданный порог.\n\n"
    "Нажми «✅ Получить доступ (24ч)», чтобы активировать триал."
)


def send_main_keyboard(token, chat_id, cfg, text="Выбери действие 👇", parse_mode=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "reply_markup": main_keyboard(cfg, chat_id),
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    telegram_call(token, "sendMessage", payload)


# ----------------------------- admin -----------------------------

def format_user_line(key, user):
    uname = user.get("username")
    name = f"@{uname}" if uname else "—"
    return f"• <code>{key}</code> {name}\n   {access_label(user)}"


def admin_summary(users):
    total = len(users)
    paid = 0
    trial = 0
    inactive = 0
    for u in users.values():
        if u.get("is_paid"):
            if not u.get("paid_until") or float(u["paid_until"]) > time.time():
                paid += 1
            else:
                inactive += 1
        elif u.get("trial_started_at") and trial_remaining(u) > 0:
            trial += 1
        else:
            inactive += 1
    return (
        "📊 <b>Сводка</b>\n\n"
        f"👥 Всего пользователей: <b>{total}</b>\n"
        f"💎 Платных: <b>{paid}</b>\n"
        f"🎁 На триале: <b>{trial}</b>\n"
        f"⛔ Истекли/Не активировали: <b>{inactive}</b>"
    )


def admin_users_text(users, limit=30):
    if not users:
        return "👥 Пользователей пока нет."
    items = sorted(users.items(), key=lambda kv: float(kv[1].get("trial_started_at") or 0))
    shown = items[:limit]
    lines = ["👥 <b>Пользователи</b> (последние " + str(len(shown)) + "):", ""]
    for key, user in shown:
        lines.append(format_user_line(key, user))
    if len(items) > limit:
        lines.append(f"\n…и ещё {len(items) - limit}")
    return "\n".join(lines)


def grant_subscription(users, target, days=None):
    key = str(target)
    user = users.get(key)
    if not user:
        user = {
            "trial_started_at": time.time(),
            "is_paid": True,
            "paid_until": None,
            "username": None,
        }
        users[key] = user
    user["is_paid"] = True
    if days is None:
        user["paid_until"] = None
    else:
        base = time.time()
        if user.get("paid_until") and float(user["paid_until"]) > base:
            base = float(user["paid_until"])
        user["paid_until"] = base + days * 86400
    return user


def revoke_subscription(users, target):
    key = str(target)
    user = users.get(key)
    if not user:
        return False
    user["is_paid"] = False
    user["paid_until"] = None
    return True


# ----------------------------- handlers -----------------------------

HELP_TEXT = (
    "🤖 <b>Stake Whale Monitor</b>\n\n"
    "Кнопки внизу:\n"
    "• 📊 Статистика — сводка и топ ставок\n"
    "• 🎚 Порог — текущий порог и как изменить\n"
    "• ⚙️ Настройки — доступ и интервал\n"
    "• ℹ️ Помощь — это сообщение\n\n"
    "Команды:\n"
    "/threshold 100000 — установить порог\n"
    "/stats — статистика\n"
    "/status — состояние\n\n"
    f"Можете отблагодарить создателя — {CREATOR_TAG}"
)

ADMIN_HELP = (
    "🛠 <b>Админ-команды</b>\n\n"
    "/grant <code>chat_id</code> — выдать бессрочно\n"
    "/grant <code>chat_id</code> <code>дней</code> — выдать на N дней\n"
    "/revoke <code>chat_id</code> — забрать подписку\n"
    "/users — список пользователей\n"
    "/summary — сводка\n\n"
    "Пример: <code>/grant 123456789 30</code>"
)


def resolve_target(users, raw):
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw.startswith("@") or not raw.isdigit():
        uname = raw.lstrip("@").lower()
        for key, user in users.items():
            if (user.get("username") or "").lower() == uname:
                return key
        return None
    return raw


def handle_text(token, chat_id, text, cfg, users, state):
    global LOGGING_ENABLED
    text = (text or "").strip()
    low = text.lower()
    user = get_user(users, chat_id)
    admin = is_admin(cfg, chat_id)
    uname = user.get("username")

    if low.startswith("/"):
        parts = text.split()
        cmd = parts[0].lstrip("/").split("@")[0].lower()

        if cmd in ("start", "help"):
            log_action(chat_id, uname, f"Команда /{cmd}")
            if WELCOME_IMG.exists():
                try:
                    telegram_call(token, "sendPhoto", {
                        "chat_id": chat_id,
                        "photo": WELCOME_IMG,
                        "caption": WELCOME_TEXT,
                        "parse_mode": "HTML",
                        "reply_markup": WELCOME_KEYBOARD
                    }, timeout=60)
                except Exception:
                    telegram_call(token, "sendMessage", {
                        "chat_id": chat_id,
                        "text": WELCOME_TEXT,
                        "parse_mode": "HTML",
                        "reply_markup": WELCOME_KEYBOARD,
                    })
            else:
                telegram_call(token, "sendMessage", {
                    "chat_id": chat_id,
                    "text": WELCOME_TEXT,
                    "parse_mode": "HTML",
                    "reply_markup": WELCOME_KEYBOARD,
                })
            return

        if cmd == "threshold":
            if len(parts) == 1:
                log_action(chat_id, uname, "Запрос порога")
                cur = float(cfg.get("threshold_usd", 0))
                send_main_keyboard(token, chat_id, cfg,
                    f"🎚 Текущий порог: <b>${cur:,.0f}</b>\n\nИзменить: <code>/threshold 100000</code>",
                    parse_mode="HTML")
                return
            try:
                new_threshold = float(parts[1].replace(",", "").replace("_", ""))
                if new_threshold <= 0:
                    raise ValueError
            except ValueError:
                send_main_keyboard(token, chat_id, cfg, "❌ Нужно положительное число.")
                return
            
            cfg["threshold_usd"] = new_threshold
            save_config(cfg)
            log_action(chat_id, uname, f"Сменил порог на ${new_threshold:,.0f}")
            send_main_keyboard(token, chat_id, cfg,
                f"✅ Новый порог: <b>${new_threshold:,.0f}</b>", parse_mode="HTML")
            return

        if cmd == "stats":
            log_action(chat_id, uname, "Команда /stats")
            telegram_call(token, "sendMessage", {
                "chat_id": chat_id,
                "text": format_stats(cfg, state["stats"], time.time() - state["started_at"], state["processed"], user),
                "parse_mode": "HTML",
                "reply_markup": main_keyboard(cfg, chat_id),
                "disable_web_page_preview": True,
            })
            return

        if cmd == "status":
            log_action(chat_id, uname, "Команда /status")
            uptime = int(time.time() - state["started_at"])
            h, rem = divmod(uptime, 3600)
            m, s = divmod(rem, 60)
            send_main_keyboard(token, chat_id, cfg,
                "🟢 <b>Монитор работает</b>\n\n"
                f"🎚 Порог: <b>${float(cfg.get('threshold_usd', 0)):,.0f}</b>\n"
                f"⏱ Интервал: <b>{cfg.get('interval_seconds', '?')} сек</b>\n"
                f"🕒 Uptime: <b>{h}ч {m}м {s}с</b>\n"
                f"🔎 Обработано: <b>{state['processed']}</b>\n"
                f"🚨 Алертов: <b>{state['stats'].get('alerts', 0)}</b>",
                parse_mode="HTML")
            return

        if cmd in ("grant", "revoke", "users", "summary", "admin"):
            if not admin:
                send_main_keyboard(token, chat_id, cfg, "⛔ Только для админа.")
                return
            log_action(chat_id, uname, f"Админ-команда /{cmd}")

            if cmd == "admin":
                telegram_call(token, "sendMessage", {
                    "chat_id": chat_id,
                    "text": "🛠 <b>Админка</b>\n\n" + ADMIN_HELP,
                    "parse_mode": "HTML",
                    "reply_markup": get_admin_keyboard(),
                })
                return
            if cmd == "summary":
                send_main_keyboard(token, chat_id, cfg, admin_summary(users), parse_mode="HTML")
                return
            if cmd == "users":
                telegram_call(token, "sendMessage", {
                    "chat_id": chat_id,
                    "text": admin_users_text(users),
                    "parse_mode": "HTML",
                })
                return
            if cmd == "grant":
                if len(parts) < 2:
                    send_main_keyboard(token, chat_id, cfg, "Пример: <code>/grant 123456789 30</code>", parse_mode="HTML")
                    return
                target = resolve_target(users, parts[1])
                if not target:
                    send_main_keyboard(token, chat_id, cfg, "❌ Пользователь не найден.")
                    return
                days = None
                if len(parts) >= 3:
                    try:
                        days = int(parts[2])
                    except ValueError:
                        send_main_keyboard(token, chat_id, cfg, "❌ Дни — число.")
                        return
                grant_subscription(users, target, days)
                save_users(users)
                label = "бессрочно" if days is None else f"на {days} дн."
                send_main_keyboard(token, chat_id, cfg, f"✅ Подписка выдана <code>{target}</code> ({label}).", parse_mode="HTML")
                try:
                    telegram_call(token, "sendMessage", {"chat_id": int(target), "text": "🎉 <b>Вам активирована подписка!</b>", "parse_mode": "HTML"})
                except Exception:
                    pass
                return
            if cmd == "revoke":
                if len(parts) < 2:
                    send_main_keyboard(token, chat_id, cfg, "Пример: <code>/revoke 123456789</code>", parse_mode="HTML")
                    return
                target = resolve_target(users, parts[1])
                if not target or not revoke_subscription(users, target):
                    send_main_keyboard(token, chat_id, cfg, "❌ Пользователь не найден.")
                    return
                save_users(users)
                send_main_keyboard(token, chat_id, cfg, f"⛔ Подписка забрана у <code>{target}</code>.", parse_mode="HTML")
                try:
                    telegram_call(token, "sendMessage", {"chat_id": int(target), "text": "⛔ Подписка закончилась. Алерты приостановлены."})
                except Exception:
                    pass
                return

        send_main_keyboard(token, chat_id, cfg, "Не знаю такую команду. Жми кнопки внизу.")
        return

    # --- обычные кнопки ---
    if "📊" in text:
        log_action(chat_id, uname, "Нажал 'Статистика'")
        telegram_call(token, "sendMessage", {
            "chat_id": chat_id,
            "text": format_stats(cfg, state["stats"], time.time() - state["started_at"], state["processed"], user),
            "parse_mode": "HTML",
            "reply_markup": main_keyboard(cfg, chat_id),
            "disable_web_page_preview": True,
        })
        return

    if "🎚" in text:
        log_action(chat_id, uname, "Нажал 'Порог'")
        cur = float(cfg.get("threshold_usd", 0))
        send_main_keyboard(token, chat_id, cfg,
            f"🎚 Текущий порог: <b>${cur:,.0f}</b>\n\nИзменить: <code>/threshold 100000</code>",
            parse_mode="HTML")
        return

    if "⚙️" in text:
        log_action(chat_id, uname, "Нажал 'Настройки'")
        text_out = (
            "⚙️ <b>Настройки</b>\n\n"
            f"🔑 <b>Доступ:</b> {access_label(user)}\n"
            f"🎚 <b>Порог:</b> ${float(cfg.get('threshold_usd', 0)):,.0f}\n"
            f"⏱ <b>Интервал:</b> {cfg.get('interval_seconds', '?')} сек"
        )
        reply_markup = PAY_KEYBOARD if not trial_active(user) else main_keyboard(cfg, chat_id)
        telegram_call(token, "sendMessage", {
            "chat_id": chat_id,
            "text": text_out,
            "parse_mode": "HTML",
            "reply_markup": reply_markup,
        })
        return

    if "ℹ️" in text:
        log_action(chat_id, uname, "Нажал 'Помощь'")
        telegram_call(token, "sendMessage", {
            "chat_id": chat_id,
            "text": HELP_TEXT + ("\n\n" + ADMIN_HELP if admin else ""),
            "parse_mode": "HTML",
            "reply_markup": main_keyboard(cfg, chat_id),
        })
        return

    if "🛠" in text:
        if not admin:
            send_main_keyboard(token, chat_id, cfg, "⛔ Только для админа.")
            return
        log_action(chat_id, uname, "Открыл 'Админку'")
        telegram_call(token, "sendMessage", {
            "chat_id": chat_id,
            "text": "🛠 <b>Админка</b>\n\n" + ADMIN_HELP,
            "parse_mode": "HTML",
            "reply_markup": get_admin_keyboard(),
        })
        return


def handle_callback(token, callback, cfg, users, state):
    global LOGGING_ENABLED
    data = callback.get("data") or ""
    cid = callback.get("id")
    msg = callback.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    admin = is_admin(cfg, chat_id)
    user = get_user(users, chat_id)
    uname = user.get("username")

    if data == "get_access":
        log_action(chat_id, uname, "Нажал 'Получить доступ'")
        if trial_active(user) or user.get("is_paid"):
            answer_callback(token, cid, "У вас уже есть доступ!")
            return
        user["trial_started_at"] = time.time()
        save_users(users)
        answer_callback(token, cid, "Доступ на 24 часа активирован! 🎉")
        telegram_call(token, "sendMessage", {
            "chat_id": chat_id,
            "text": (
                "🎉 <b>Пробный доступ активирован!</b>\n\n"
                "Вы будете получать уведомления о крупных ставках в течение 24 часов.\n"
                "Жми кнопки внизу 👇"
            ),
            "parse_mode": "HTML",
            "reply_markup": main_keyboard(cfg, chat_id),
        })
        return

    if data == "info":
        log_action(chat_id, uname, "Нажал 'Информация'")
        answer_callback(token, cid)
        telegram_call(token, "sendMessage", {
            "chat_id": chat_id,
            "text": INFO_TEXT,
            "parse_mode": "HTML",
            "reply_markup": WELCOME_KEYBOARD,
        })
        return

    if data == "pay":
        log_action(chat_id, uname, "Нажал 'Оплатить'")
        answer_callback(token, cid, "Оплата скоро появится 💳")
        telegram_call(token, "sendMessage", {
            "chat_id": chat_id,
            "text": "💳 <b>Оплата скоро появится</b>",
            "parse_mode": "HTML",
            "reply_markup": main_keyboard(cfg, chat_id),
        })
        return

    if not data.startswith("adm_"):
        answer_callback(token, cid)
        return

    if not admin:
        answer_callback(token, cid, "Только для админа")
        return

    log_action(chat_id, uname, f"Админ-кнопка: {data}")

    if data == "adm_users":
        answer_callback(token, cid)
        telegram_call(token, "sendMessage", {
            "chat_id": chat_id,
            "text": admin_users_text(users),
            "parse_mode": "HTML",
            "reply_markup": get_admin_keyboard(),
        })
        return

    if data == "adm_summary":
        answer_callback(token, cid)
        telegram_call(token, "sendMessage", {
            "chat_id": chat_id,
            "text": admin_summary(users),
            "parse_mode": "HTML",
            "reply_markup": get_admin_keyboard(),
        })
        return

    if data == "adm_toggle_logs":
        LOGGING_ENABLED = not LOGGING_ENABLED
        status = "включено" if LOGGING_ENABLED else "выключено"
        answer_callback(token, cid, f"Логирование {status}")
        telegram_call(token, "sendMessage", {
            "chat_id": chat_id,
            "text": f"⚙️ Логирование действий теперь <b>{status.upper()}</b>.",
            "parse_mode": "HTML",
            "reply_markup": get_admin_keyboard(),
        })
        return

    if data == "adm_view_logs":
        answer_callback(token, cid)
        if not ACTIVITY_LOGS:
            log_text = "📝 Логов пока нет."
        else:
            log_text = "📝 <b>Последние действия:</b>\n\n" + "\n".join(reversed(ACTIVITY_LOGS[-15:]))
        telegram_call(token, "sendMessage", {
            "chat_id": chat_id,
            "text": log_text,
            "parse_mode": "HTML",
            "reply_markup": get_admin_keyboard(),
        })
        return

    if data == "adm_grant_hint":
        answer_callback(token, cid, "Отправь команду")
        telegram_call(token, "sendMessage", {
            "chat_id": chat_id,
            "text": (
                "🎁 <b>Выдать подписку</b>\n\n"
                "Отправь:\n"
                "<code>/grant 123456789</code> — бессрочно\n"
                "<code>/grant 123456789 30</code> — на 30 дней\n"
                "<code>/grant @username 30</code> — по нику\n\n"
                "Список с chat_id: /users"
            ),
            "parse_mode": "HTML",
        })
        return

    if data == "adm_revoke_hint":
        answer_callback(token, cid, "Отправь команду")
        telegram_call(token, "sendMessage", {
            "chat_id": chat_id,
            "text": (
                "⛔ <b>Забрать подписку</b>\n\n"
                "Отправь: <code>/revoke 123456789</code>\n"
                "Или: <code>/revoke @username</code>"
            ),
            "parse_mode": "HTML",
        })
        return

    if data == "adm_close":
        answer_callback(token, cid)
        try:
            telegram_call(token, "deleteMessage", {
                "chat_id": chat_id,
                "message_id": msg.get("message_id"),
            })
        except Exception:
            pass
        return

    answer_callback(token, cid)


# ----------------------------- main -----------------------------

def main():
    cfg = load_config()
    token = cfg["telegram_token"]
    interval = max(15, int(cfg.get("interval_seconds", 20)))

    session = requests.Session()
    session.headers.update({"user-agent": HEADERS["user-agent"]})

    print("\nПроверяю Telegram-токен...")
    me = telegram_call(token, "getMe")
    print(f"Telegram-бот подключён: @{me.get('username')}")

    try:
        updates = telegram_call(token, "getUpdates", {"timeout": 0})
        offset = max([u["update_id"] for u in updates], default=0) + 1
    except Exception:
        offset = 0

    users = load_users()
    chat_id = cfg.get("chat_id")

    if chat_id:
        print(f"Использую сохранённый chat_id: {chat_id}")
        try:
            send_main_keyboard(token, chat_id, cfg, "🔄 <b>Монитор перезапущен и продолжает работу.</b>", parse_mode="HTML")
        except Exception as exc:
            print(f"[warning] не смог написать в сохранённый чат: {exc}")
            chat_id = None

    if not chat_id:
        print("Открой своего бота в Telegram и отправь /start.")
        while chat_id is None:
            try:
                updates = telegram_call(token, "getUpdates", {"timeout": 20, "offset": offset}, timeout=30)
                for update in updates:
                    offset = max(offset, update["update_id"] + 1)
                    msg = update.get("message") or update.get("edited_message") or {}
                    chat = msg.get("chat") or {}
                    text = (msg.get("text") or "").strip()
                    if chat.get("type") == "private" and text.startswith("/start"):
                        chat_id = chat.get("id")
                        cfg["chat_id"] = chat_id
                        save_config(cfg)
                        get_user(users, chat_id)
                        users[str(chat_id)]["username"] = chat.get("username")
                        save_users(users)
                        handle_text(token, chat_id, "/start", cfg, users, {"stats": {}, "started_at": time.time(), "processed": 0})
                        print("Telegram chat connected.")
                        break
            except Exception as e:
                print(f"Ожидание /start... ({e})")
                time.sleep(3)

    get_user(users, chat_id)
    save_users(users)

    seen = load_seen()
    queue = load_queue()
    state = {
        "started_at": time.time(),
        "processed": 0,
        "stats": load_stats(),
    }
    last_offset = offset

    print(f"Мониторинг запущен. Порог ${float(cfg['threshold_usd']):,.0f}; интервал {interval} сек.")
    print("Остановить: Ctrl+C.")

    while True:
        try:
            try:
                updates = telegram_call(
                    token, "getUpdates",
                    {"timeout": 0, "offset": last_offset, "allowed_updates": ["message", "callback_query"]},
                    timeout=10,
                )
                for update in updates:
                    last_offset = max(last_offset, update["update_id"] + 1)
                    if "callback_query" in update:
                        handle_callback(token, update["callback_query"], cfg, users, state)
                    else:
                        msg = update.get("message") or update.get("edited_message") or {}
                        chat = msg.get("chat") or {}
                        if chat.get("type") == "private":
                            if chat.get("username"):
                                get_user(users, chat.get("id"))["username"] = chat.get("username")
                            handle_text(token, chat.get("id"), msg.get("text"), cfg, users, state)
                save_users(users)
            except Exception as exc:
                pass

            prices = get_binance_prices(session)
            rows = fetch_bets(session)
            new_count = 0
            threshold = float(cfg.get("threshold_usd", 50000))
            user = get_user(users, chat_id)
            can_send = trial_active(user)

            for row in rows:
                bet = row.get("bet") or {}
                bet_id = str(bet.get("id") or row.get("id") or "")
                if not bet_id or bet_id in seen:
                    continue

                usd = to_usd(bet.get("amount"), bet.get("currency"), prices)
                seen[bet_id] = time.time()
                state["processed"] += 1

                if usd is None or usd < threshold:
                    continue

                outcomes = bet.get("outcomes") or []
                first = outcomes[0] if outcomes else {}
                fixture = first.get("fixture") or {}
                tour = fixture.get("tournament") or {}
                cat = tour.get("category") or {}
                sport = cat.get("sport") or {}
                record_stat(
                    state["stats"], bet, usd,
                    sport.get("name") or "—",
                    (sport.get("slug") or "").lower(),
                    bet.get("createdAt"),
                )

                if not can_send:
                    continue

                try:
                    # 1. Отправляем как обычно
                    telegram_call(token, "sendMessage", {
                        "chat_id": chat_id,
                        "text": format_alert(bet, usd),
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    })
                    new_count += 1
                    
                    # 2. Складываем в копилку
                    sport_name = sport.get("name") or "Неизвестный спорт"
                    event_name = first.get("fixtureName") or fixture.get("name") or "Событие не указано"
                    selection_name = (first.get("outcome") or {}).get("name") or "Не указано"
                    odds_val = first.get("odds")
                    
                    queue.append({
                        "id": bet_id,
                        "sport": sport_name,
                        "event": event_name,
                        "selection": selection_name,
                        "odds": odds_val if odds_val is not None else "—",
                        "usd": usd
                    })
                    save_queue(queue)
                    
                    # 3. Если набралось 10 штук, отправляем сводку и очищаем копилку
                    if len(queue) >= BATCH_SIZE:
                        batch_text = format_batch(queue, int(time.time() // 86400)) # условный номер пачки
                        telegram_call(token, "sendMessage", {
                            "chat_id": chat_id,
                            "text": batch_text,
                            "parse_mode": "HTML",
                        })
                        queue = [] # очищаем
                        save_queue(queue)
                        
                except Exception as exc:
                    pass

            seen = save_seen(seen)
            save_stats(state["stats"])
            save_users(users)
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Ставок: {len(rows)}; новых: {new_count}; в копилке: {len(queue)}")
            
            sleep_end = time.time() + interval
            while time.time() < sleep_end:
                try:
                    updates = telegram_call(
                        token, "getUpdates",
                        {"timeout": 0, "offset": last_offset, "allowed_updates": ["message", "callback_query"]},
                        timeout=5,
                    )
                    for update in updates:
                        last_offset = max(last_offset, update["update_id"] + 1)
                        if "callback_query" in update:
                            handle_callback(token, update["callback_query"], cfg, users, state)
                        else:
                            msg = update.get("message") or update.get("edited_message") or {}
                            chat = msg.get("chat") or {}
                            if chat.get("type") == "private":
                                handle_text(token, chat.get("id"), msg.get("text"), cfg, users, state)
                except Exception:
                    pass
                time.sleep(1)

        except KeyboardInterrupt:
            print("\nМонитор остановлен.")
            break
        except Exception as exc:
            print(f"[error] {exc}")
            time.sleep(30)


if __name__ == "__main__":
    main()

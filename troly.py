# -*- coding: utf-8 -*-
import os
import re
import time
import json
import logging
import threading
import signal
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, List, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from flask import Flask

# ==========================================================
# CONFIG
# ==========================================================
TELEGRAM_BOT_TOKEN = os.environ.get(
    "TELEGRAM_BOT_TOKEN",
    "7725834820:AAH9utkQjOP7wumhhpSTOGYbp8PbtSQTjvg",
)
PORT = int(os.environ.get("PORT", 10000))

# Self-ping (Render keep-alive)
SELF_PING_INTERVAL_SEC = 240

def get_render_url() -> Optional[str]:
    if os.environ.get("RENDER_EXTERNAL_URL"):
        return os.environ.get("RENDER_EXTERNAL_URL")
    service_name = os.environ.get("RENDER_SERVICE_NAME", "")
    if service_name:
        return f"https://{service_name}.onrender.com"
    return None

RENDER_EXTERNAL_URL = get_render_url()

# Timezone VN
try:
    from zoneinfo import ZoneInfo
    VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
except Exception:
    VN_TZ = timezone(timedelta(hours=7))

def now_vn() -> datetime:
    return datetime.now(VN_TZ)

def fmt_dt() -> str:
    return now_vn().strftime("%H:%M • %d/%m/%Y")

def fmt_time() -> str:
    return now_vn().strftime("%H:%M")

# Telegram
TG_CONNECT_TIMEOUT = 10
TG_READ_TIMEOUT = 35
UPDATES_LONGPOLL = 35

# Scheduler
SCHED_TICK = 20

# Files
USERS_FILE = "assistant_users.json"

# ==========================================================
# LOGGING
# ==========================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("urllib3").setLevel(logging.WARNING)
log = logging.getLogger("ASSISTANT_BOT")

# ==========================================================
# GLOBALS
# ==========================================================
shutdown_event = threading.Event()

# ==========================================================
# HOLIDAYS (SOLAR / LUNAR - mapping to SOLAR dates by year)
# ==========================================================
# Ngày lễ cố định (Dương lịch) - key: "MM-DD"
SOLAR_HOLIDAYS: Dict[str, str] = {
    "01-01": "🎊 Tết Dương Lịch",
    "02-14": "💝 Valentine",
    "03-08": "🌸 Quốc tế Phụ nữ",
    "04-30": "🇻🇳 Giải phóng miền Nam",
    "05-01": "⚒️ Quốc tế Lao động",
    "06-01": "👶 Quốc tế Thiếu nhi",
    "09-02": "🇻🇳 Quốc khánh Việt Nam",
    "10-20": "👩 Ngày Phụ nữ Việt Nam",
    "11-20": "👨‍🏫 Ngày Nhà giáo Việt Nam",
    "12-24": "🎄 Giáng sinh",
    "12-25": "🎅 Lễ Noel",
}

# Ngày lễ Âm lịch (đã quy đổi sang DƯƠNG LỊCH của năm đó) - key: "MM-DD"
# (Bạn cần update hàng năm nếu muốn chính xác)
LUNAR_HOLIDAYS_2025: Dict[str, str] = {
    "01-29": "🧧 Tết Nguyên Đán 2025",
    "01-30": "🧧 Mùng 2 Tết",
    "01-31": "🧧 Mùng 3 Tết",
    "02-01": "🧧 Mùng 4 Tết",
    "02-14": "💐 Rằm tháng Giêng",
    "04-05": "🌺 Giỗ Tổ Hùng Vương (10/3 ÂL)",
    "05-31": "🥮 Tết Đoan Ngọ (5/5 ÂL)",
    "08-05": "🌕 Tết Trung Thu (15/8 ÂL)",
    "10-02": "🕯️ Vu Lan (15/7 ÂL)",
    "11-29": "🍲 Tết Ông Công Ông Táo (23/12 ÂL)",
}

def check_holiday(mm_dd: str) -> Optional[str]:
    """Check if date is a holiday (MM-DD format)."""
    if mm_dd in SOLAR_HOLIDAYS:
        return SOLAR_HOLIDAYS[mm_dd]
    if mm_dd in LUNAR_HOLIDAYS_2025:
        return LUNAR_HOLIDAYS_2025[mm_dd]
    return None

# ==========================================================
# SELF-PING KEEPER
# ==========================================================
class SelfPingKeeper:
    def __init__(self, session: requests.Session):
        self.session = session
        self.url = (RENDER_EXTERNAL_URL.rstrip("/") + "/ping") if RENDER_EXTERNAL_URL else None
        self.ping_count = 0

    def ping_self(self):
        if not self.url:
            return
        try:
            r = self.session.get(self.url, timeout=10)
            if r.status_code == 200:
                self.ping_count += 1
                log.info("🏓 Self-ping OK (#%d)", self.ping_count)
            else:
                log.warning("⚠️ Self-ping HTTP %s", r.status_code)
        except Exception as e:
            log.warning("⚠️ Self-ping error: %s", e)

def run_self_pinger():
    if not RENDER_EXTERNAL_URL:
        log.warning("⚠️ Self-ping disabled (no RENDER_EXTERNAL_URL/RENDER_SERVICE_NAME)")
        return

    session = requests.Session()
    session.headers.update({"User-Agent": "Assistant-Ping/1.0"})
    keeper = SelfPingKeeper(session)

    log.info("🏓 Self-ping started: %s", RENDER_EXTERNAL_URL)

    while not shutdown_event.is_set():
        try:
            keeper.ping_self()
        except Exception as e:
            log.warning("⚠️ Self-ping loop error: %s", e)
        time.sleep(SELF_PING_INTERVAL_SEC)

# ==========================================================
# HTTP SESSION
# ==========================================================
def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "POST"),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    s.headers.update({"User-Agent": "Mozilla/5.0"})
    return s

HTTP = make_session()

# ==========================================================
# STORAGE
# ==========================================================
_io_lock = threading.Lock()

def load_json(path: str, default: Any) -> Any:
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return default

def save_json(path: str, data: Any):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        log.error("Save error: %s", e)

def get_users() -> Dict[str, Any]:
    return load_json(USERS_FILE, {"users": {}})

def set_users(d: Dict[str, Any]):
    save_json(USERS_FILE, d)

def ensure_user(chat_id: Any) -> Dict[str, Any]:
    with _io_lock:
        data = get_users()
        users = data.setdefault("users", {})
        u = users.get(str(chat_id))
        if not u:
            u = {
                "enabled": True,
                "created_at": fmt_dt(),

                # Water tracking
                "water_enabled": True,
                "water_goal_ml": 2000,
                "water_drunk_ml": 0,
                "water_last_reset": now_vn().strftime("%Y-%m-%d"),
                "water_reminder_interval_min": 90,
                "last_water_reminder_ts": 0,

                # Sleep time
                "sleep_enabled": True,
                "sleep_time": "22:00",

                # Morning greeting
                "morning_enabled": True,
                "morning_time": "07:00",

                # Important dates (personal)
                "important_dates": {},  # { "MM-DD": "desc" }

                # Pending input state (for ADD_DATE, etc.)
                "pending": None,  # {"type":"add_date"} or None

                # State
                "last_fire": {},  # {event_key: "YYYY-MM-DD HH:MM"}
            }
            users[str(chat_id)] = u
            set_users(data)
        return u

def update_user(chat_id: Any, patch: Dict[str, Any]):
    with _io_lock:
        data = get_users()
        u = data.setdefault("users", {}).setdefault(str(chat_id), {})
        u.update(patch)
        set_users(data)

def patch_user_nested(chat_id: Any, key: str, value: Any):
    """Helper update a nested dict value safely."""
    with _io_lock:
        data = get_users()
        u = data.setdefault("users", {}).setdefault(str(chat_id), {})
        u[key] = value
        set_users(data)

def list_enabled_chat_ids() -> List[int]:
    data = get_users()
    out: List[int] = []
    for cid_str, u in (data.get("users") or {}).items():
        if isinstance(u, dict) and u.get("enabled"):
            try:
                out.append(int(cid_str))
            except Exception:
                pass
    return out

# ==========================================================
# TELEGRAM API
# ==========================================================
TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

def tg_call(method: str, *, params=None, payload=None, read_timeout=TG_READ_TIMEOUT) -> Dict:
    url = f"{TG_API}/{method}"
    timeout = (TG_CONNECT_TIMEOUT, read_timeout)
    try:
        if payload is not None:
            r = HTTP.post(url, json=payload, params=params, timeout=timeout)
        else:
            r = HTTP.get(url, params=params, timeout=timeout)
        try:
            return r.json()
        except Exception:
            return {"ok": False, "description": f"Non-JSON response: {r.text[:200]}"}
    except Exception as e:
        return {"ok": False, "description": str(e)}

def tg_send(chat_id: Any, text: str, reply_markup=None) -> bool:
    # Telegram limit ~4096 chars; giữ an toàn
    chunks = [text[i:i + 3900] for i in range(0, len(text), 3900)] or [""]
    for chunk in chunks:
        payload = {
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup

        d = tg_call("sendMessage", payload=payload)
        if not d.get("ok"):
            log.error("❌ Send failed: %s", d)
            return False
    return True

def tg_answer_callback(cq_id: str, text: str = ""):
    tg_call("answerCallbackQuery", payload={"callback_query_id": cq_id, "text": text}, read_timeout=15)

# ==========================================================
# UI
# ==========================================================
def kb_main(user: Dict) -> dict:
    goal = max(1, int(user.get("water_goal_ml", 2000)))
    drunk = max(0, int(user.get("water_drunk_ml", 0)))
    water_pct = min(100, int(drunk / goal * 100))

    bot_status = "🟢" if user.get("enabled") else "🔴"
    water_status = "💧" if user.get("water_enabled") else "❌"
    sleep_status = "🌙" if user.get("sleep_enabled") else "❌"
    morning_status = "🌅" if user.get("morning_enabled") else "❌"

    return {
        "inline_keyboard": [
            [{"text": f"{bot_status} Bot", "callback_data": "TOGGLE_BOT"}],
            [
                {"text": f"{water_status} Nước {water_pct}%", "callback_data": "WATER_MENU"},
                {"text": f"{sleep_status} Ngủ", "callback_data": "TOGGLE_SLEEP"},
            ],
            [
                {"text": f"{morning_status} Sáng", "callback_data": "TOGGLE_MORNING"},
                {"text": "📅 Ngày lễ", "callback_data": "DATES_MENU"},
            ],
            [{"text": "📊 Xem tổng quan", "callback_data": "SHOW_OVERVIEW"}],
        ]
    }

def kb_water() -> dict:
    return {
        "inline_keyboard": [
            [{"text": "💧 Đã uống 250ml", "callback_data": "DRANK_250"}],
            [{"text": "💧 Đã uống 500ml", "callback_data": "DRANK_500"}],
            [{"text": "🔄 Reset hôm nay", "callback_data": "WATER_RESET"}],
            [{"text": "⬅️ Quay lại", "callback_data": "BACK"}],
        ]
    }

def kb_dates() -> dict:
    return {
        "inline_keyboard": [
            [{"text": "📅 Xem ngày lễ sắp tới", "callback_data": "VIEW_HOLIDAYS"}],
            [{"text": "➕ Thêm ngày quan trọng", "callback_data": "ADD_DATE"}],
            [{"text": "📋 Ngày của tôi", "callback_data": "MY_DATES"}],
            [{"text": "⬅️ Quay lại", "callback_data": "BACK"}],
        ]
    }

# ==========================================================
# MESSAGES
# ==========================================================
def build_overview(u: Dict) -> str:
    bot = "🟢 ĐANG BẬT" if u.get("enabled") else "🔴 ĐÃ TẮT"

    drunk = int(u.get("water_drunk_ml", 0))
    goal = max(1, int(u.get("water_goal_ml", 2000)))
    pct = min(100, int(drunk / goal * 100))
    remaining = max(0, goal - drunk)

    msg = "╔════════════════════╗\n"
    msg += "║  🤖 <b>TRỢ LÝ NHẮC VIỆC</b> ║\n"
    msg += "╚════════════════════╝\n\n"

    msg += f"📊 <b>Trạng thái:</b> {bot}\n"
    msg += f"🕐 <b>Bây giờ:</b> <code>{fmt_dt()}</code>\n\n"

    msg += "━━━━━━━━━━━━━━━━━━━━\n"
    msg += "<b>💧 UỐNG NƯỚC HÔM NAY</b>\n"
    msg += f"• Đã uống: <b>{drunk}ml / {goal}ml</b> ({pct}%)\n"
    msg += f"• Còn lại: <b>{remaining}ml</b>\n"
    if u.get("water_enabled"):
        msg += f"• Nhắc mỗi: <b>{int(u.get('water_reminder_interval_min', 90))}p</b>\n"
    else:
        msg += "• Nhắc: <b>Đã tắt</b>\n"

    msg += "\n━━━━━━━━━━━━━━━━━━━━\n"
    msg += "<b>🌙 GIỜ NGỦ</b>\n"
    if u.get("sleep_enabled"):
        msg += f"• Nhắc lúc: <b>{u.get('sleep_time', '22:00')}</b>\n"
    else:
        msg += "• <b>Đã tắt</b>\n"

    msg += "\n━━━━━━━━━━━━━━━━━━━━\n"
    msg += "<b>🌅 CHÀO BUỔI SÁNG</b>\n"
    if u.get("morning_enabled"):
        msg += f"• Nhắc lúc: <b>{u.get('morning_time', '07:00')}</b>\n"
        msg += "• Kèm: Ngày lễ + Ngày quan trọng\n"
    else:
        msg += "• <b>Đã tắt</b>\n"

    dates_count = len(u.get("important_dates", {}) or {})
    msg += "\n━━━━━━━━━━━━━━━━━━━━\n"
    msg += "<b>📅 NGÀY QUAN TRỌNG</b>\n"
    msg += f"• Bạn có: <b>{dates_count} ngày</b> đã lưu\n"

    pending = u.get("pending")
    if pending and isinstance(pending, dict):
        msg += "\n\n📝 <i>Bạn đang ở chế độ nhập liệu. Gõ /cancel để hủy.</i>"

    return msg

def get_upcoming_holidays(days: int = 30) -> List[Tuple[datetime, str]]:
    today = now_vn().replace(hour=0, minute=0, second=0, microsecond=0)
    upcoming: List[Tuple[datetime, str]] = []

    for i in range(days):
        d = today + timedelta(days=i)
        mm_dd = d.strftime("%m-%d")
        name = check_holiday(mm_dd)
        if name:
            upcoming.append((d, name))

    return upcoming

def build_holidays_message() -> str:
    upcoming = get_upcoming_holidays(60)

    msg = "📅 <b>NGÀY LỄ SẮP TỚI</b>\n\n"
    if not upcoming:
        msg += "⚠️ Không có ngày lễ nào trong 60 ngày tới.\n"
        return msg

    today = now_vn().replace(hour=0, minute=0, second=0, microsecond=0)
    for d, name in upcoming[:10]:
        days_left = (d - today).days
        if days_left == 0:
            when = "Hôm nay"
        elif days_left == 1:
            when = "Ngày mai"
        else:
            when = f"Còn {days_left} ngày"

        msg += f"• {name}\n"
        msg += f"  📆 {d.strftime('%d/%m/%Y')} ({when})\n\n"

    return msg

def build_morning_greeting(u: Dict) -> str:
    today = now_vn()
    mm_dd = today.strftime("%m-%d")

    weekday_names = ["Hai", "Ba", "Tư", "Năm", "Sáu", "Bảy", "CN"]
    msg = "🌅 <b>CHÀO BUỔI SÁNG!</b>\n\n"
    msg += f"📅 Hôm nay: <b>{today.strftime('%d/%m/%Y')}</b>\n"
    msg += f"📆 Thứ: <b>{weekday_names[today.weekday()]}</b>\n\n"

    holiday = check_holiday(mm_dd)
    if holiday:
        msg += f"🎉 <b>{holiday}</b>\n\n"

    personal_dates = u.get("important_dates", {}) or {}
    if mm_dd in personal_dates:
        msg += f"⭐ <b>{personal_dates[mm_dd]}</b>\n\n"

    upcoming = get_upcoming_holidays(7)
    if upcoming:
        today0 = today.replace(hour=0, minute=0, second=0, microsecond=0)
        future = [(d, name) for d, name in upcoming if d > today0]
        if future:
            msg += "📌 <b>Sắp tới:</b>\n"
            for d, name in future[:3]:
                days = (d - today0).days
                msg += f"• {name} ({days} ngày nữa)\n"
            msg += "\n"

    msg += "💪 Chúc bạn một ngày tuyệt vời!\n"
    msg += "💧 Nhớ uống nước đầy đủ nhé!"
    return msg

def help_text() -> str:
    return (
        "🤖 <b>Trợ lý nhắc việc</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "• /start : Bắt đầu dùng bot\n"
        "• /overview : Xem tổng quan\n"
        "• /water : Menu uống nước\n"
        "• /dates : Menu ngày lễ / ngày quan trọng\n"
        "• /cancel : Hủy chế độ nhập (thêm ngày)\n"
        "• /stop : Tắt bot (không gửi nhắc)\n"
    )

# ==========================================================
# DATE INPUT PARSING (PERSONAL IMPORTANT DATES)
# ==========================================================
_MM_DD_RE = re.compile(r"^\s*(\d{1,2})\s*[-/\.]\s*(\d{1,2})\s*(.*)$")

def normalize_mm_dd(text: str) -> Optional[Tuple[str, str]]:
    """
    Parse: 'MM-DD noi dung' or 'MM/DD noi dung' or 'MM.DD noi dung'
    Return: (mm_dd, desc) or None.
    """
    m = _MM_DD_RE.match(text or "")
    if not m:
        return None
    mm = int(m.group(1))
    dd = int(m.group(2))
    desc = (m.group(3) or "").strip()

    if not (1 <= mm <= 12 and 1 <= dd <= 31):
        return None

    mm_dd = f"{mm:02d}-{dd:02d}"
    return mm_dd, desc

def build_add_date_prompt() -> str:
    return (
        "➕ <b>THÊM NGÀY QUAN TRỌNG</b>\n\n"
        "Hãy gửi theo format:\n"
        "• <code>MM-DD Nội dung</code>\n"
        "Ví dụ:\n"
        "• <code>03-15 Sinh nhật mẹ</code>\n"
        "• <code>12-01 Kỷ niệm cưới</code>\n\n"
        "Gõ /cancel để hủy."
    )

# ==========================================================
# SCHEDULER
# ==========================================================
def should_fire(u: Dict, event_key: str, now: datetime) -> bool:
    last = (u.get("last_fire", {}) or {}).get(event_key)
    k = now.strftime("%Y-%m-%d %H:%M")
    return last != k

def mark_fired(chat_id: Any, u: Dict, event_key: str, now: datetime):
    lf = dict(u.get("last_fire", {}) or {})
    lf[event_key] = now.strftime("%Y-%m-%d %H:%M")
    update_user(chat_id, {"last_fire": lf})

def reset_water_if_needed(chat_id: Any, u: Dict) -> Dict:
    """Reset water counter at midnight; return updated user dict."""
    today = now_vn().strftime("%Y-%m-%d")
    last_reset = u.get("water_last_reset", "")
    if last_reset != today:
        patch = {"water_drunk_ml": 0, "water_last_reset": today}
        update_user(chat_id, patch)
        u = dict(u)
        u.update(patch)
    return u

def scheduler_loop():
    log.info("⏰ Scheduler started")

    while not shutdown_event.is_set():
        try:
            data = get_users()
            users = data.get("users", {}) or {}
            now = now_vn()
            hhmm = now.strftime("%H:%M")

            for cid_str, u in list(users.items()):
                if not isinstance(u, dict):
                    continue
                if not u.get("enabled"):
                    continue

                try:
                    chat_id = int(cid_str)
                except Exception:
                    continue

                # Ensure baseline fields (in case file edited)
                u = ensure_user(chat_id)
                u = reset_water_if_needed(chat_id, u)

                # Morning greeting
                if u.get("morning_enabled"):
                    if hhmm == u.get("morning_time", "07:00") and should_fire(u, "morning", now):
                        tg_send(chat_id, build_morning_greeting(u), reply_markup=kb_main(u))
                        mark_fired(chat_id, u, "morning", now)

                # Sleep reminder
                if u.get("sleep_enabled"):
                    if hhmm == u.get("sleep_time", "22:00") and should_fire(u, "sleep", now):
                        msg = (
                            "🌙 <b>GIỜ ĐI NGỦ RỒI!</b>\n\n"
                            "💤 Tắt điện thoại\n"
                            "📖 Đọc sách hoặc nghe nhạc nhẹ\n"
                            "🧘 Thở sâu và thư giãn\n\n"
                            "Chúc bạn ngủ ngon! 😴"
                        )
                        tg_send(chat_id, msg, reply_markup=kb_main(u))
                        mark_fired(chat_id, u, "sleep", now)

                # Water reminder
                if u.get("water_enabled"):
                    interval_min = int(u.get("water_reminder_interval_min", 90))
                    last_ts = int(u.get("last_water_reminder_ts", 0))

                    # Only remind during waking hours (7am - 10pm)
                    if 7 <= now.hour < 22:
                        if time.time() - last_ts >= interval_min * 60:
                            drunk = int(u.get("water_drunk_ml", 0))
                            goal = max(1, int(u.get("water_goal_ml", 2000)))
                            remaining = max(0, goal - drunk)

                            msg = (
                                "💧 <b>UỐNG NƯỚC NÀO!</b>\n\n"
                                f"🎯 Mục tiêu hôm nay: <b>{goal}ml</b>\n"
                                f"✅ Đã uống: <b>{drunk}ml</b>\n"
                                f"📊 Còn lại: <b>{remaining}ml</b>\n\n"
                                "Bấm nút bên dưới sau khi uống! 👇"
                            )
                            tg_send(chat_id, msg, reply_markup=kb_water())
                            update_user(chat_id, {"last_water_reminder_ts": int(time.time())})

        except Exception as e:
            log.exception("Scheduler error: %s", e)

        time.sleep(SCHED_TICK)

# ==========================================================
# COMMANDS + MESSAGE HANDLING
# ==========================================================
def handle_command(chat_id: int, text: str):
    u = ensure_user(chat_id)
    t = (text or "").strip()

    if t.lower() in ("/start", "start"):
        # bật bot + clear pending
        update_user(chat_id, {"enabled": True, "pending": None})
        u = ensure_user(chat_id)
        tg_send(chat_id, "✅ <b>Đã sẵn sàng!</b>\n\n" + build_overview(u), reply_markup=kb_main(u))
        return

    if t.lower() in ("/stop", "stop"):
        update_user(chat_id, {"enabled": False, "pending": None})
        u = ensure_user(chat_id)
        tg_send(chat_id, "🛑 <b>Đã tắt bot.</b>\nGõ /start để bật lại.", reply_markup=kb_main(u))
        return

    if t.lower() in ("/help", "help"):
        tg_send(chat_id, help_text(), reply_markup=kb_main(u))
        return

    if t.lower() in ("/overview", "overview"):
        tg_send(chat_id, build_overview(u), reply_markup=kb_main(u))
        return

    if t.lower() in ("/water", "water"):
        tg_send(chat_id, "💧 <b>Menu uống nước</b>\n\nBấm nút bên dưới:", reply_markup=kb_water())
        return

    if t.lower() in ("/dates", "dates"):
        tg_send(chat_id, "📅 <b>QUẢN LÝ NGÀY</b>\n\nChọn chức năng:", reply_markup=kb_dates())
        return

    if t.lower() in ("/cancel", "cancel"):
        update_user(chat_id, {"pending": None})
        u = ensure_user(chat_id)
        tg_send(chat_id, "✅ Đã hủy chế độ nhập.", reply_markup=kb_main(u))
        return

    # If pending input
    pending = u.get("pending")
    if pending and isinstance(pending, dict):
        ptype = pending.get("type")
        if ptype == "add_date":
            parsed = normalize_mm_dd(t)
            if not parsed:
                tg_send(chat_id, "⚠️ Sai format. Ví dụ: <code>03-15 Sinh nhật mẹ</code>\nGõ /cancel để hủy.")
                return

            mm_dd, desc = parsed
            if not desc:
                tg_send(chat_id, "⚠️ Bạn chưa nhập nội dung. Ví dụ: <code>03-15 Sinh nhật mẹ</code>")
                return

            # save
            dates = dict(u.get("important_dates", {}) or {})
            dates[mm_dd] = desc
            update_user(chat_id, {"important_dates": dates, "pending": None})
            u = ensure_user(chat_id)

            tg_send(
                chat_id,
                f"✅ Đã lưu: <b>{mm_dd}</b> — {desc}\n\n" + build_overview(u),
                reply_markup=kb_main(u),
            )
            return

        # Unknown pending -> clear
        update_user(chat_id, {"pending": None})
        u = ensure_user(chat_id)
        tg_send(chat_id, build_overview(u), reply_markup=kb_main(u))
        return

    # Fallback: show overview
    tg_send(chat_id, "📌 Mình chưa hiểu. Gõ /help để xem lệnh.\n\n" + build_overview(u), reply_markup=kb_main(u))

# ==========================================================
# CALLBACK HANDLING
# ==========================================================
def handle_callback(cq: Dict):
    cq_id = cq.get("id", "")
    msg_obj = cq.get("message") or {}
    chat = msg_obj.get("chat") or {}
    chat_id = chat.get("id")
    action = (cq.get("data") or "").strip().upper()

    if not chat_id:
        tg_answer_callback(cq_id, "Thiếu chat_id")
        return

    u = ensure_user(chat_id)

    if action == "TOGGLE_BOT":
        newv = not bool(u.get("enabled"))
        update_user(chat_id, {"enabled": newv, "pending": None})
        tg_answer_callback(cq_id, "✅ Đã cập nhật")
        u = ensure_user(chat_id)
        tg_send(chat_id, build_overview(u), reply_markup=kb_main(u))
        return

    if action == "TOGGLE_SLEEP":
        newv = not bool(u.get("sleep_enabled"))
        update_user(chat_id, {"sleep_enabled": newv})
        tg_answer_callback(cq_id, "✅")
        u = ensure_user(chat_id)
        tg_send(chat_id, build_overview(u), reply_markup=kb_main(u))
        return

    if action == "TOGGLE_MORNING":
        newv = not bool(u.get("morning_enabled"))
        update_user(chat_id, {"morning_enabled": newv})
        tg_answer_callback(cq_id, "✅")
        u = ensure_user(chat_id)
        tg_send(chat_id, build_overview(u), reply_markup=kb_main(u))
        return

    if action == "SHOW_OVERVIEW":
        tg_answer_callback(cq_id, "📊")
        tg_send(chat_id, build_overview(u), reply_markup=kb_main(u))
        return

    if action == "WATER_MENU":
        tg_answer_callback(cq_id, "💧")
        drunk = int(u.get("water_drunk_ml", 0))
        goal = max(1, int(u.get("water_goal_ml", 2000)))
        pct = min(100, int(drunk / goal * 100))
        msg = (
            "💧 <b>UỐNG NƯỚC HÔM NAY</b>\n\n"
            f"📊 Tiến độ: <b>{pct}%</b>\n"
            f"✅ Đã uống: <b>{drunk}ml</b>\n"
            f"🎯 Mục tiêu: <b>{goal}ml</b>\n\n"
            "Bấm nút sau khi uống:"
        )
        tg_send(chat_id, msg, reply_markup=kb_water())
        return

    if action == "DRANK_250":
        new_amount = int(u.get("water_drunk_ml", 0)) + 250
        update_user(chat_id, {"water_drunk_ml": new_amount, "last_water_reminder_ts": int(time.time())})
        tg_answer_callback(cq_id, "✅ +250ml")
        u = ensure_user(chat_id)
        tg_send(chat_id, build_overview(u), reply_markup=kb_main(u))
        return

    if action == "DRANK_500":
        new_amount = int(u.get("water_drunk_ml", 0)) + 500
        update_user(chat_id, {"water_drunk_ml": new_amount, "last_water_reminder_ts": int(time.time())})
        tg_answer_callback(cq_id, "✅ +500ml")
        u = ensure_user(chat_id)
        tg_send(chat_id, build_overview(u), reply_markup=kb_main(u))
        return

    if action == "WATER_RESET":
        update_user(chat_id, {"water_drunk_ml": 0})
        tg_answer_callback(cq_id, "🔄 Đã reset")
        u = ensure_user(chat_id)
        tg_send(chat_id, build_overview(u), reply_markup=kb_main(u))
        return

    if action == "DATES_MENU":
        tg_answer_callback(cq_id, "📅")
        update_user(chat_id, {"pending": None})
        tg_send(chat_id, "📅 <b>QUẢN LÝ NGÀY</b>\n\nChọn chức năng:", reply_markup=kb_dates())
        return

    if action == "VIEW_HOLIDAYS":
        tg_answer_callback(cq_id, "📅")
        tg_send(chat_id, build_holidays_message(), reply_markup=kb_dates())
        return

    if action == "MY_DATES":
        tg_answer_callback(cq_id, "📋")
        dates = u.get("important_dates", {}) or {}
        if not dates:
            msg = "📋 <b>NGÀY QUAN TRỌNG CỦA BẠN</b>\n\n⚠️ Bạn chưa có ngày nào."
        else:
            msg = "📋 <b>NGÀY QUAN TRỌNG CỦA BẠN</b>\n\n"
            for mm_dd, desc in sorted(dates.items()):
                msg += f"• <b>{mm_dd}</b>: {desc}\n"
            msg += "\n\nGợi ý: Muốn sửa, chỉ cần thêm lại đúng <code>MM-DD</code> là sẽ ghi đè."
        tg_send(chat_id, msg, reply_markup=kb_dates())
        return

    if action == "ADD_DATE":
        # chuyển sang chế độ nhập (pending)
        tg_answer_callback(cq_id, "➕")
        update_user(chat_id, {"pending": {"type": "add_date"}})
        tg_send(chat_id, build_add_date_prompt(), reply_markup=kb_dates())
        return

    if action == "BACK":
        tg_answer_callback(cq_id, "⬅️")
        update_user(chat_id, {"pending": None})
        u = ensure_user(chat_id)
        tg_send(chat_id, build_overview(u), reply_markup=kb_main(u))
        return

    tg_answer_callback(cq_id, "Không hỗ trợ")

# ==========================================================
# UPDATES LOOP
# ==========================================================
def handle_updates_forever():
    log.info("📱 Updates handler started")
    offset = 0

    # Quick sanity check
    me = tg_call("getMe", read_timeout=15)
    if me.get("ok"):
        log.info("🤖 Bot username: %s", (me.get("result") or {}).get("username"))
    else:
        log.warning("⚠️ getMe failed: %s", me.get("description"))

    while not shutdown_event.is_set():
        try:
            d = tg_call(
                "getUpdates",
                params={"offset": offset + 1, "timeout": UPDATES_LONGPOLL},
                read_timeout=UPDATES_LONGPOLL + 15,
            )

            if not d.get("ok"):
                time.sleep(2)
                continue

            for upd in d.get("result", []) or []:
                offset = upd.get("update_id", offset)

                # Callback queries
                if "callback_query" in upd:
                    try:
                        handle_callback(upd["callback_query"])
                    except Exception as e:
                        log.exception("Callback error: %s", e)
                    continue

                # Normal messages
                if "message" in upd:
                    msg = upd["message"] or {}
                    chat = msg.get("chat") or {}
                    chat_id = chat.get("id")
                    text = msg.get("text", "")

                    if not chat_id:
                        continue

                    try:
                        handle_command(int(chat_id), text)
                    except Exception as e:
                        log.exception("Message handle error: %s", e)
                        tg_send(int(chat_id), "⚠️ Có lỗi xảy ra, thử lại giúp mình nhé.")
                    continue

        except Exception as e:
            log.exception("Updates loop error: %s", e)
            time.sleep(3)

# ==========================================================
# FLASK APP (RENDER KEEP-ALIVE)
# ==========================================================
app = Flask(__name__)

@app.get("/")
def home():
    return "OK", 200

@app.get("/ping")
def ping():
    return "pong", 200

# ==========================================================
# SHUTDOWN
# ==========================================================
def _handle_signal(sig, frame):
    log.warning("🛑 Signal received (%s) - shutting down...", sig)
    shutdown_event.set()

try:
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
except Exception:
    pass

# ==========================================================
# MAIN
# ==========================================================
def main():
    # Start background threads
    t_updates = threading.Thread(target=handle_updates_forever, name="tg-updates", daemon=True)
    t_sched = threading.Thread(target=scheduler_loop, name="scheduler", daemon=True)
    t_updates.start()
    t_sched.start()

    if RENDER_EXTERNAL_URL:
        t_ping = threading.Thread(target=run_self_pinger, name="self-ping", daemon=True)
        t_ping.start()

    log.info("🚀 Service starting on port %d", PORT)
    try:
        app.run(host="0.0.0.0", port=PORT)
    finally:
        shutdown_event.set()
        log.info("👋 Service stopped")

if __name__ == "__main__":
    main()

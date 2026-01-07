# -*- coding: utf-8 -*-
import os
import time
import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, List

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from flask import Flask

# ==========================================================
# CONFIG
# ==========================================================
TELEGRAM_BOT_TOKEN = "PUT_YOUR_TOKEN_HERE"
PORT = int(os.environ.get("PORT", 10000))

# Timezone VN (không cần tzdata)
try:
    from zoneinfo import ZoneInfo
    try:
        VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
    except Exception:
        VN_TZ = timezone(timedelta(hours=7))
except Exception:
    VN_TZ = timezone(timedelta(hours=7))

def now_vn() -> datetime:
    return datetime.now(VN_TZ)

def fmt_dt() -> str:
    return now_vn().strftime("%H:%M • %d/%m/%Y")

# Telegram networking
TG_CONNECT_TIMEOUT = 10
TG_READ_TIMEOUT = 35
UPDATES_LONGPOLL = 35

# Scheduler tick (seconds)
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
# HTTP SESSION (retry)
# ==========================================================
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json,text/plain,*/*",
    "Connection": "keep-alive",
}

def make_session(total: int, backoff: float) -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=total,
        connect=total,
        read=total,
        backoff_factor=backoff,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "POST"),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    s.headers.update(DEFAULT_HEADERS)
    return s

HTTP = make_session(total=3, backoff=0.5)

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

def save_json(path: str, data: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def get_users() -> Dict[str, Any]:
    return load_json(USERS_FILE, {"users": {}})

def set_users(d: Dict[str, Any]) -> None:
    save_json(USERS_FILE, d)

def ensure_user(chat_id: Any) -> Dict[str, Any]:
    """
    Default schedule:
    - Wake: 07:00
    - Work: 09:00 - 18:00 (Mon-Fri)
    - Sleep: 23:00
    - Water: every 60 min (08:00-22:00)
    """
    with _io_lock:
        data = get_users()
        u = data.setdefault("users", {}).get(str(chat_id))
        if not u:
            u = {
                "enabled": True,
                "created_at": fmt_dt(),

                "wake_time": "07:00",
                "sleep_time": "23:00",

                "work_enabled": True,
                "work_start": "09:00",
                "work_end": "18:00",
                "work_days": [0, 1, 2, 3, 4],  # Mon..Fri (Python weekday: Mon=0)

                "water_enabled": True,
                "water_every_min": 60,
                "water_window_start": "08:00",
                "water_window_end": "22:00",

                # internal state
                "last_fire": {},          # key -> "YYYY-MM-DD HH:MM"
                "last_water_ts": 0,       # unix ts
            }
            data["users"][str(chat_id)] = u
            set_users(data)
        return u

def update_user(chat_id: Any, patch: Dict[str, Any]) -> None:
    with _io_lock:
        data = get_users()
        u = data.setdefault("users", {}).setdefault(str(chat_id), {})
        u.update(patch)
        set_users(data)

# ==========================================================
# TELEGRAM API
# ==========================================================
TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

def tg_call(method: str, *, params: Optional[Dict] = None, payload: Optional[Dict] = None,
            read_timeout: int = TG_READ_TIMEOUT) -> Dict:
    url = f"{TG_API}/{method}"
    timeout = (TG_CONNECT_TIMEOUT, read_timeout)
    try:
        if payload is not None:
            r = HTTP.post(url, json=payload, params=params, timeout=timeout)
        else:
            r = HTTP.get(url, params=params, timeout=timeout)
        return r.json()
    except requests.exceptions.Timeout:
        return {"ok": False, "description": "Timeout"}
    except Exception as e:
        return {"ok": False, "description": str(e)}

def tg_send(chat_id: Any, text: str, reply_markup: Optional[dict] = None) -> bool:
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    d = tg_call("sendMessage", payload=payload, read_timeout=25)
    if not d.get("ok"):
        log.error(f"Telegram send failed: {d.get('error_code')} | {d.get('description')}")
        return False
    return True

def tg_answer_callback(cq_id: str, text: str = "") -> None:
    tg_call("answerCallbackQuery", payload={"callback_query_id": cq_id, "text": text}, read_timeout=15)

# ==========================================================
# UI
# ==========================================================
def kb_main(user: Dict[str, Any]) -> dict:
    water = "✅" if user.get("water_enabled") else "❌"
    work = "✅" if user.get("work_enabled") else "❌"
    enabled = "✅" if user.get("enabled") else "❌"

    return {
        "inline_keyboard": [
            [{"text": f"{enabled} Bật/Tắt bot", "callback_data": "TOGGLE_BOT"}],
            [{"text": f"{water} Uống nước", "callback_data": "TOGGLE_WATER"},
             {"text": f"{work} Làm việc", "callback_data": "TOGGLE_WORK"}],
            [{"text": "⏰ Xem lịch", "callback_data": "SHOW"},
             {"text": "⚙️ Cài nhanh", "callback_data": "QUICK"}],
        ]
    }

def kb_quick() -> dict:
    return {
        "inline_keyboard": [
            [{"text": "🌅 Thức dậy 07:00", "callback_data": "SET_WAKE_07"},
             {"text": "🌅 Thức dậy 06:30", "callback_data": "SET_WAKE_0630"}],
            [{"text": "🧑‍💻 Làm 09:00-18:00", "callback_data": "SET_WORK_9_18"},
             {"text": "🧑‍💻 Làm 08:30-17:30", "callback_data": "SET_WORK_830_1730"}],
            [{"text": "🌙 Ngủ 23:00", "callback_data": "SET_SLEEP_23"},
             {"text": "🌙 Ngủ 22:30", "callback_data": "SET_SLEEP_2230"}],
            [{"text": "💧 Nước mỗi 60p", "callback_data": "SET_WATER_60"},
             {"text": "💧 Nước mỗi 90p", "callback_data": "SET_WATER_90"}],
            [{"text": "⬅️ Quay lại", "callback_data": "BACK"}],
        ]
    }

# ==========================================================
# TIME HELPERS
# ==========================================================
def parse_hhmm(hhmm: str) -> Optional[Tuple[int, int]]:
    try:
        parts = hhmm.strip().split(":")
        if len(parts) != 2:
            return None
        h = int(parts[0])
        m = int(parts[1])
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except Exception:
        return None
    return None

def in_window(now: datetime, start_hm: str, end_hm: str) -> bool:
    s = parse_hhmm(start_hm)
    e = parse_hhmm(end_hm)
    if not s or not e:
        return False
    sh, sm = s
    eh, em = e
    start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = now.replace(hour=eh, minute=em, second=0, microsecond=0)
    if end <= start:
        # window crosses midnight
        return now >= start or now <= end
    return start <= now <= end

def key_at(now: datetime) -> str:
    return now.strftime("%Y-%m-%d %H:%M")

# ==========================================================
# MESSAGES
# ==========================================================
def build_schedule_text(u: Dict[str, Any]) -> str:
    return (
        f"🧠 <b>LỊCH CỦA BẠN</b>\n"
        f"🕒 <i>{fmt_dt()}</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🌅 <b>Thức dậy</b>: <code>{u.get('wake_time')}</code>\n"
        f"🌙 <b>Đi ngủ</b>: <code>{u.get('sleep_time')}</code>\n\n"
        f"🧑‍💻 <b>Làm việc</b>: "
        f"{'BẬT' if u.get('work_enabled') else 'TẮT'}\n"
        f"• Giờ: <code>{u.get('work_start')}</code> → <code>{u.get('work_end')}</code>\n"
        f"• Ngày: <code>Th2-Th6</code>\n\n"
        f"💧 <b>Uống nước</b>: "
        f"{'BẬT' if u.get('water_enabled') else 'TẮT'}\n"
        f"• Chu kỳ: <code>{u.get('water_every_min')} phút</code>\n"
        f"• Khung: <code>{u.get('water_window_start')}</code> → <code>{u.get('water_window_end')}</code>\n"
    )

# ==========================================================
# SCHEDULER LOGIC
# ==========================================================
def should_fire_once_per_minute(u: Dict[str, Any], event_key: str, now: datetime) -> bool:
    last = u.get("last_fire", {}).get(event_key)
    k = key_at(now)
    if last == k:
        return False
    return True

def mark_fired(u: Dict[str, Any], chat_id: Any, event_key: str, now: datetime) -> None:
    lf = dict(u.get("last_fire", {}))
    lf[event_key] = key_at(now)
    update_user(chat_id, {"last_fire": lf})

def scheduler_loop() -> None:
    log.info("⏰ Scheduler started")
    while True:
        try:
            data = get_users()
            users = data.get("users", {})

            now = now_vn()
            weekday = now.weekday()  # Mon=0
            hhmm = now.strftime("%H:%M")

            for cid_str, u in list(users.items()):
                if not u.get("enabled"):
                    continue

                chat_id = int(cid_str)
                # wake reminder
                if hhmm == u.get("wake_time") and should_fire_once_per_minute(u, "wake", now):
                    tg_send(chat_id, "🌅 <b>THỨC DẬY THÔI!</b>\nUống 1 ly nước + kéo giãn 2 phút nhé 💪")
                    mark_fired(u, chat_id, "wake", now)

                # sleep reminder
                if hhmm == u.get("sleep_time") and should_fire_once_per_minute(u, "sleep", now):
                    tg_send(chat_id, "🌙 <b>ĐẾN GIỜ ĐI NGỦ</b>\nTắt màn hình, thư giãn 5 phút rồi ngủ nha 😴")
                    mark_fired(u, chat_id, "sleep", now)

                # work start / end reminders (Mon-Fri default)
                if u.get("work_enabled") and weekday in (u.get("work_days") or []):
                    if hhmm == u.get("work_start") and should_fire_once_per_minute(u, "work_start", now):
                        tg_send(chat_id, "🧑‍💻 <b>BẮT ĐẦU LÀM VIỆC</b>\nChọn 1 việc quan trọng nhất để làm trước ✅")
                        mark_fired(u, chat_id, "work_start", now)

                    if hhmm == u.get("work_end") and should_fire_once_per_minute(u, "work_end", now):
                        tg_send(chat_id, "🏁 <b>KẾT THÚC LÀM VIỆC</b>\nTổng kết 3 việc đã làm + lên 1 kế hoạch nhỏ cho ngày mai ✍️")
                        mark_fired(u, chat_id, "work_end", now)

                # water reminders
                if u.get("water_enabled"):
                    if in_window(now, u.get("water_window_start", "08:00"), u.get("water_window_end", "22:00")):
                        every_min = int(u.get("water_every_min") or 60)
                        last_ts = int(u.get("last_water_ts") or 0)
                        if last_ts == 0:
                            # initialize baseline when first enabled
                            update_user(chat_id, {"last_water_ts": int(time.time())})
                        else:
                            if time.time() - last_ts >= every_min * 60:
                                tg_send(chat_id, "💧 <b>NHẮC UỐNG NƯỚC</b>\nUống vài ngụm nước nhé 🚰")
                                update_user(chat_id, {"last_water_ts": int(time.time())})

        except Exception as e:
            log.error(f"Scheduler error: {e}")

        time.sleep(SCHED_TICK)

# ==========================================================
# TELEGRAM UPDATES LOOP
# ==========================================================
def handle_updates_forever() -> None:
    log.info("📱 Updates handler started")
    offset = 0

    while True:
        try:
            d = tg_call(
                "getUpdates",
                params={"offset": offset + 1, "timeout": UPDATES_LONGPOLL},
                read_timeout=UPDATES_LONGPOLL + 15,
            )
            if not d.get("ok"):
                time.sleep(2)
                continue

            for upd in d.get("result", []):
                offset = upd.get("update_id", offset)

                # buttons
                if "callback_query" in upd:
                    cq = upd["callback_query"]
                    cid = cq.get("message", {}).get("chat", {}).get("id")
                    action = (cq.get("data") or "").upper()
                    if not cid:
                        continue

                    u = ensure_user(cid)

                    if action == "TOGGLE_BOT":
                        newv = not bool(u.get("enabled"))
                        update_user(cid, {"enabled": newv})
                        tg_answer_callback(cq["id"], "✅ Đã cập nhật")
                        u = ensure_user(cid)
                        tg_send(cid, build_schedule_text(u), reply_markup=kb_main(u))

                    elif action == "TOGGLE_WATER":
                        newv = not bool(u.get("water_enabled"))
                        patch = {"water_enabled": newv}
                        if newv:
                            patch["last_water_ts"] = int(time.time())
                        update_user(cid, patch)
                        tg_answer_callback(cq["id"], "✅ Đã cập nhật")
                        u = ensure_user(cid)
                        tg_send(cid, build_schedule_text(u), reply_markup=kb_main(u))

                    elif action == "TOGGLE_WORK":
                        newv = not bool(u.get("work_enabled"))
                        update_user(cid, {"work_enabled": newv})
                        tg_answer_callback(cq["id"], "✅ Đã cập nhật")
                        u = ensure_user(cid)
                        tg_send(cid, build_schedule_text(u), reply_markup=kb_main(u))

                    elif action == "SHOW":
                        tg_answer_callback(cq["id"], "✅")
                        u = ensure_user(cid)
                        tg_send(cid, build_schedule_text(u), reply_markup=kb_main(u))

                    elif action == "QUICK":
                        tg_answer_callback(cq["id"], "⚙️ Cài nhanh")
                        tg_send(cid, "⚙️ <b>CÀI NHANH</b>\nChọn mẫu lịch bạn muốn:", reply_markup=kb_quick())

                    elif action == "BACK":
                        tg_answer_callback(cq["id"], "⬅️")
                        u = ensure_user(cid)
                        tg_send(cid, build_schedule_text(u), reply_markup=kb_main(u))

                    elif action.startswith("SET_"):
                        # quick presets
                        patch = {}
                        if action == "SET_WAKE_07":
                            patch["wake_time"] = "07:00"
                        elif action == "SET_WAKE_0630":
                            patch["wake_time"] = "06:30"
                        elif action == "SET_WORK_9_18":
                            patch["work_enabled"] = True
                            patch["work_start"] = "09:00"
                            patch["work_end"] = "18:00"
                        elif action == "SET_WORK_830_1730":
                            patch["work_enabled"] = True
                            patch["work_start"] = "08:30"
                            patch["work_end"] = "17:30"
                        elif action == "SET_SLEEP_23":
                            patch["sleep_time"] = "23:00"
                        elif action == "SET_SLEEP_2230":
                            patch["sleep_time"] = "22:30"
                        elif action == "SET_WATER_60":
                            patch["water_enabled"] = True
                            patch["water_every_min"] = 60
                            patch["last_water_ts"] = int(time.time())
                        elif action == "SET_WATER_90":
                            patch["water_enabled"] = True
                            patch["water_every_min"] = 90
                            patch["last_water_ts"] = int(time.time())

                        if patch:
                            update_user(cid, patch)

                        tg_answer_callback(cq["id"], "✅ Đã lưu")
                        u = ensure_user(cid)
                        tg_send(cid, build_schedule_text(u), reply_markup=kb_main(u))

                    continue

                # text messages
                msg = upd.get("message", {})
                text = (msg.get("text") or "").strip()
                if not text:
                    continue

                cid = msg["chat"]["id"]
                cmd = text.split()[0].lower()

                u = ensure_user(cid)

                if cmd == "/start":
                    subscribe(cid)
                    tg_send(
                        cid,
                        "🤖 <b>TRỢ LÝ NHẮC VIỆC</b>\n"
                        "✅ Mình đã bật lịch mặc định cho bạn.\n\n"
                        "Bạn có thể bấm nút để xem / chỉnh nhanh 👇",
                        reply_markup=kb_main(u),
                    )
                    tg_send(cid, build_schedule_text(u), reply_markup=kb_main(u))

                elif cmd == "/show":
                    tg_send(cid, build_schedule_text(u), reply_markup=kb_main(u))

                elif cmd == "/on":
                    update_user(cid, {"enabled": True})
                    u = ensure_user(cid)
                    tg_send(cid, "✅ <b>Đã bật bot</b>", reply_markup=kb_main(u))

                elif cmd == "/off":
                    update_user(cid, {"enabled": False})
                    u = ensure_user(cid)
                    tg_send(cid, "🔕 <b>Đã tắt bot</b>", reply_markup=kb_main(u))

                else:
                    tg_send(cid, "Chọn nhanh bằng nút bên dưới 👇", reply_markup=kb_main(u))

        except Exception as e:
            log.error(f"Updates loop error: {e}")
            time.sleep(2)

# ==========================================================
# FLASK (optional)
# ==========================================================
app = Flask(__name__)

@app.route("/")
def home():
    return {"status": "online", "service": "Reminder Assistant", "time": fmt_dt()}

@app.route("/health")
def health():
    return {"status": "healthy", "timestamp": now_vn().isoformat()}

# ==========================================================
# MAIN
# ==========================================================
if __name__ == "__main__":
    log.info("🚀 Starting Reminder Assistant Bot...")

    me = tg_call("getMe", read_timeout=20)
    if me.get("ok"):
        log.info(f"✅ Telegram connected: @{me.get('result', {}).get('username')}")
    else:
        log.warning(f"⚠️ Telegram connection issue: {me.get('description')}")

    # Flask thread
    threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=PORT, debug=False),
        daemon=True
    ).start()
    log.info(f"✅ Flask running on port {PORT}")

    # Scheduler background
    threading.Thread(target=scheduler_loop, daemon=True).start()
    log.info("✅ Scheduler started")

    # Updates loop (blocking)
    handle_updates_forever()

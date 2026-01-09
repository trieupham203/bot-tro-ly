# -*- coding: utf-8 -*-
import os
import time
import json
import logging
import threading
import signal
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, List, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from flask import Flask

# ==========================================================
# CONFIG
# ==========================================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "7725834820:AAH9utkQjOP7wumhhpSTOGYbp8PbtSQTjvg")
PORT = int(os.environ.get("PORT", 10000))

# Self-ping configuration
SELF_PING_INTERVAL_SEC = 240

def get_render_url():
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

def fmt_time() -> str:
    return now_vn().strftime("%H:%M")

# Telegram networking
TG_CONNECT_TIMEOUT = 10
TG_READ_TIMEOUT = 35
UPDATES_LONGPOLL = 35

# Scheduler tick
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
# SELF-PING KEEPER
# ==========================================================
class SelfPingKeeper:
    def __init__(self, session: requests.Session):
        self.session = session
        self.url = RENDER_EXTERNAL_URL.rstrip('/') + '/ping' if RENDER_EXTERNAL_URL else None
        self.ping_count = 0
        self.fail_count = 0
        
    def ping_self(self):
        if not self.url:
            return
            
        try:
            r = self.session.get(self.url, timeout=10)
            if r.status_code == 200:
                self.ping_count += 1
                log.info("🏓 Self-ping OK (#%d)", self.ping_count)
            else:
                self.fail_count += 1
                log.warning("⚠️ Self-ping failed: %d", r.status_code)
        except Exception as e:
            self.fail_count += 1
            log.warning("⚠️ Self-ping error: %s", e)

def run_self_pinger():
    if not RENDER_EXTERNAL_URL:
        log.warning("⚠️ Cannot detect service URL, self-ping disabled")
        return
    
    session = requests.Session()
    session.headers.update({'User-Agent': 'Assistant-SelfPing/1.0'})
    keeper = SelfPingKeeper(session)
    
    log.info("🏓 Self-ping keeper started")
    log.info("🌐 Target URL: %s", RENDER_EXTERNAL_URL)
    
    while not shutdown_event.is_set():
        try:
            keeper.ping_self()
            time.sleep(SELF_PING_INTERVAL_SEC)
        except Exception as e:
            log.exception("❌ Self-ping keeper error: %s", e)
            time.sleep(30)

# ==========================================================
# HTTP SESSION
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
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        log.error(f"Save error: {e}")

def get_users() -> Dict[str, Any]:
    return load_json(USERS_FILE, {"users": {}})

def set_users(d: Dict[str, Any]) -> None:
    save_json(USERS_FILE, d)

def ensure_user(chat_id: Any) -> Dict[str, Any]:
    """Initialize user with smart defaults"""
    with _io_lock:
        data = get_users()
        u = data.setdefault("users", {}).get(str(chat_id))
        if not u:
            u = {
                "enabled": True,
                "created_at": fmt_dt(),
                "timezone": "VN",
                
                # Daily schedule
                "wake_time": "07:00",
                "sleep_time": "23:00",
                
                # Work schedule
                "work_enabled": True,
                "work_start": "09:00",
                "work_end": "18:00",
                "work_days": [0, 1, 2, 3, 4],  # Mon-Fri
                
                # Break reminders
                "break_enabled": True,
                "break_every_min": 120,  # Every 2 hours
                "break_window_start": "09:00",
                "break_window_end": "18:00",
                
                # Water reminders
                "water_enabled": True,
                "water_every_min": 60,
                "water_window_start": "08:00",
                "water_window_end": "22:00",
                
                # Eye care
                "eye_enabled": True,
                "eye_every_min": 30,
                "eye_window_start": "08:00",
                "eye_window_end": "22:00",
                
                # Posture reminder
                "posture_enabled": True,
                "posture_every_min": 45,
                "posture_window_start": "08:00",
                "posture_window_end": "22:00",
                
                # Exercise reminder
                "exercise_enabled": True,
                "exercise_time": "18:30",
                
                # Meal reminders
                "meal_enabled": True,
                "breakfast_time": "07:30",
                "lunch_time": "12:00",
                "dinner_time": "18:30",
                
                # Internal state
                "last_fire": {},
                "last_water_ts": 0,
                "last_break_ts": 0,
                "last_eye_ts": 0,
                "last_posture_ts": 0,
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
        log.error(f"❌ Telegram send failed: {d}")
        return False
    return True

def tg_answer_callback(cq_id: str, text: str = "") -> None:
    tg_call("answerCallbackQuery", payload={"callback_query_id": cq_id, "text": text}, read_timeout=15)

# ==========================================================
# UI - MODERN DESIGN
# ==========================================================
def kb_main(user: Dict[str, Any]) -> dict:
    """Main keyboard with visual status indicators"""
    bot_status = "🟢" if user.get("enabled") else "🔴"
    
    return {
        "inline_keyboard": [
            [{"text": f"{bot_status} Trạng thái Bot", "callback_data": "TOGGLE_BOT"}],
            [
                {"text": "⏰ Lịch Hàng Ngày", "callback_data": "MENU_DAILY"},
                {"text": "🧑‍💻 Lịch Làm Việc", "callback_data": "MENU_WORK"},
            ],
            [
                {"text": "💧 Sức Khỏe", "callback_data": "MENU_HEALTH"},
                {"text": "🍱 Bữa Ăn", "callback_data": "MENU_MEAL"},
            ],
            [
                {"text": "📊 Xem Tổng Quan", "callback_data": "SHOW_ALL"},
                {"text": "⚙️ Cài Nhanh", "callback_data": "MENU_QUICK"},
            ],
        ]
    }

def kb_daily(user: Dict[str, Any]) -> dict:
    """Daily schedule keyboard"""
    return {
        "inline_keyboard": [
            [{"text": f"🌅 Thức dậy: {user.get('wake_time')}", "callback_data": "EDIT_WAKE"}],
            [{"text": f"🌙 Đi ngủ: {user.get('sleep_time')}", "callback_data": "EDIT_SLEEP"}],
            [{"text": "⬅️ Quay lại", "callback_data": "BACK_MAIN"}],
        ]
    }

def kb_work(user: Dict[str, Any]) -> dict:
    """Work schedule keyboard"""
    work_status = "✅" if user.get("work_enabled") else "❌"
    break_status = "✅" if user.get("break_enabled") else "❌"
    
    return {
        "inline_keyboard": [
            [{"text": f"{work_status} Làm việc", "callback_data": "TOGGLE_WORK"}],
            [{"text": f"🕐 Bắt đầu: {user.get('work_start')}", "callback_data": "EDIT_WORK_START"}],
            [{"text": f"🕔 Kết thúc: {user.get('work_end')}", "callback_data": "EDIT_WORK_END"}],
            [{"text": f"{break_status} Nhắc nghỉ giải lao", "callback_data": "TOGGLE_BREAK"}],
            [{"text": "⬅️ Quay lại", "callback_data": "BACK_MAIN"}],
        ]
    }

def kb_health(user: Dict[str, Any]) -> dict:
    """Health reminders keyboard"""
    water = "✅" if user.get("water_enabled") else "❌"
    eye = "✅" if user.get("eye_enabled") else "❌"
    posture = "✅" if user.get("posture_enabled") else "❌"
    exercise = "✅" if user.get("exercise_enabled") else "❌"
    
    return {
        "inline_keyboard": [
            [{"text": f"{water} Uống nước ({user.get('water_every_min')}p)", "callback_data": "TOGGLE_WATER"}],
            [{"text": f"{eye} Nghỉ mắt ({user.get('eye_every_min')}p)", "callback_data": "TOGGLE_EYE"}],
            [{"text": f"{posture} Tư thế ({user.get('posture_every_min')}p)", "callback_data": "TOGGLE_POSTURE"}],
            [{"text": f"{exercise} Tập luyện {user.get('exercise_time')}", "callback_data": "TOGGLE_EXERCISE"}],
            [{"text": "⬅️ Quay lại", "callback_data": "BACK_MAIN"}],
        ]
    }

def kb_meal(user: Dict[str, Any]) -> dict:
    """Meal reminders keyboard"""
    meal_status = "✅" if user.get("meal_enabled") else "❌"
    
    return {
        "inline_keyboard": [
            [{"text": f"{meal_status} Nhắc bữa ăn", "callback_data": "TOGGLE_MEAL"}],
            [{"text": f"🌅 Sáng: {user.get('breakfast_time')}", "callback_data": "EDIT_BREAKFAST"}],
            [{"text": f"☀️ Trưa: {user.get('lunch_time')}", "callback_data": "EDIT_LUNCH"}],
            [{"text": f"🌙 Tối: {user.get('dinner_time')}", "callback_data": "EDIT_DINNER"}],
            [{"text": "⬅️ Quay lại", "callback_data": "BACK_MAIN"}],
        ]
    }

def kb_quick() -> dict:
    """Quick setup presets"""
    return {
        "inline_keyboard": [
            [{"text": "🌟 Lịch Tiêu Chuẩn", "callback_data": "PRESET_STANDARD"}],
            [{"text": "💼 Văn Phòng Viên", "callback_data": "PRESET_OFFICE"}],
            [{"text": "💻 Lập Trình Viên", "callback_data": "PRESET_DEVELOPER"}],
            [{"text": "🎓 Sinh Viên", "callback_data": "PRESET_STUDENT"}],
            [{"text": "🏋️ Người Tập Gym", "callback_data": "PRESET_FITNESS"}],
            [{"text": "⬅️ Quay lại", "callback_data": "BACK_MAIN"}],
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
        return now >= start or now <= end
    return start <= now <= end

def key_at(now: datetime) -> str:
    return now.strftime("%Y-%m-%d %H:%M")

# ==========================================================
# MESSAGES - BEAUTIFUL FORMAT
# ==========================================================
def build_overview(u: Dict[str, Any]) -> str:
    """Comprehensive overview with visual hierarchy"""
    bot_status = "🟢 ĐANG BẬT" if u.get("enabled") else "🔴 ĐÃ TẮT"
    
    msg = f"╔═══════════════════════╗\n"
    msg += f"║  🤖 <b>TRỢ LÝ CÁ NHÂN</b>  ║\n"
    msg += f"╚═══════════════════════╝\n\n"
    
    msg += f"📊 <b>Trạng thái:</b> {bot_status}\n"
    msg += f"🕐 <b>Thời gian:</b> <code>{fmt_dt()}</code>\n\n"
    
    msg += f"┏━━━━━━━━━━━━━━━━━━━━━━━┓\n"
    msg += f"┃  📅 <b>LỊCH HÀNG NGÀY</b>  ┃\n"
    msg += f"┗━━━━━━━━━━━━━━━━━━━━━━━┛\n"
    msg += f"🌅 Thức dậy:  <code>{u.get('wake_time')}</code>\n"
    msg += f"🌙 Đi ngủ:    <code>{u.get('sleep_time')}</code>\n\n"
    
    msg += f"┏━━━━━━━━━━━━━━━━━━━━━━━┓\n"
    msg += f"┃  🧑‍💻 <b>LỊCH LÀM VIỆC</b>  ┃\n"
    msg += f"┗━━━━━━━━━━━━━━━━━━━━━━━┛\n"
    if u.get("work_enabled"):
        msg += f"✅ <b>Đang bật</b>\n"
        msg += f"• Giờ làm: <code>{u.get('work_start')}</code> → <code>{u.get('work_end')}</code>\n"
        msg += f"• Ngày: <code>Thứ 2 - Thứ 6</code>\n"
        if u.get("break_enabled"):
            msg += f"• Nghỉ giải lao: <code>Mỗi {u.get('break_every_min')} phút</code>\n"
    else:
        msg += f"❌ Đã tắt\n"
    msg += "\n"
    
    msg += f"┏━━━━━━━━━━━━━━━━━━━━━━━┓\n"
    msg += f"┃  💧 <b>SỨC KHỎE</b>        ┃\n"
    msg += f"┗━━━━━━━━━━━━━━━━━━━━━━━┛\n"
    
    if u.get("water_enabled"):
        msg += f"💧 Uống nước: ✅ <code>Mỗi {u.get('water_every_min')}p</code>\n"
    else:
        msg += f"💧 Uống nước: ❌\n"
    
    if u.get("eye_enabled"):
        msg += f"👁️ Nghỉ mắt: ✅ <code>Mỗi {u.get('eye_every_min')}p</code>\n"
    else:
        msg += f"👁️ Nghỉ mắt: ❌\n"
    
    if u.get("posture_enabled"):
        msg += f"🧘 Tư thế: ✅ <code>Mỗi {u.get('posture_every_min')}p</code>\n"
    else:
        msg += f"🧘 Tư thế: ❌\n"
    
    if u.get("exercise_enabled"):
        msg += f"🏋️ Tập luyện: ✅ <code>{u.get('exercise_time')}</code>\n"
    else:
        msg += f"🏋️ Tập luyện: ❌\n"
    msg += "\n"
    
    msg += f"┏━━━━━━━━━━━━━━━━━━━━━━━┓\n"
    msg += f"┃  🍱 <b>BỮA ĂN</b>         ┃\n"
    msg += f"┗━━━━━━━━━━━━━━━━━━━━━━━┛\n"
    
    if u.get("meal_enabled"):
        msg += f"✅ <b>Đang bật</b>\n"
        msg += f"🌅 Sáng: <code>{u.get('breakfast_time')}</code>\n"
        msg += f"☀️ Trưa: <code>{u.get('lunch_time')}</code>\n"
        msg += f"🌙 Tối: <code>{u.get('dinner_time')}</code>\n"
    else:
        msg += f"❌ Đã tắt\n"
    
    return msg

# ==========================================================
# REMINDER MESSAGES
# ==========================================================
WAKE_MESSAGES = [
    "🌅 <b>CHÀO BUỔI SÁNG!</b>\n\n"
    "✨ Một ngày mới tràn đầy năng lượng!\n"
    "💧 Uống 1 ly nước ấm\n"
    "🧘 Kéo giãn 2-3 phút\n"
    "📋 Xem lại kế hoạch hôm nay",
    
    "🌅 <b>THỨC DẬY THÔI!</b>\n\n"
    "☀️ Mở cửa sổ đón ánh sáng tự nhiên\n"
    "💧 Hydrate ngay với 1 ly nước\n"
    "🏃 Vận động nhẹ 5 phút\n"
    "🎯 Chuẩn bị tinh thần cho ngày mới!",
]

SLEEP_MESSAGES = [
    "🌙 <b>GIỜ ĐI NGỦ RỒI!</b>\n\n"
    "📱 Tắt màn hình điện tử\n"
    "📖 Đọc sách 10 phút thư giãn\n"
    "🧘 Thở sâu 5 lần\n"
    "💤 Ngủ ngon và hẹn gặp sáng mai!",
    
    "🌙 <b>ĐẾN GIỜ NGHỈ NGƠI</b>\n\n"
    "💡 Điều chỉnh ánh sáng vừa phải\n"
    "🌡️ Nhiệt độ phòng 20-22°C là tốt nhất\n"
    "🧘 Meditation 5-10 phút\n"
    "💤 Chúc bạn ngủ ngon!",
]

WORK_START_MESSAGES = [
    "🧑‍💻 <b>BẮT ĐẦU LÀM VIỆC</b>\n\n"
    "☕ Chuẩn bị 1 cốc cafe/trà\n"
    "📋 Review task list\n"
    "🎯 Chọn 1-3 việc quan trọng nhất\n"
    "⏰ Làm task khó nhất TRƯỚC TIÊN!\n\n"
    "💪 Let's crush it today!",
]

WORK_END_MESSAGES = [
    "🏁 <b>KẾT THÚC NGÀY LÀM VIỆC</b>\n\n"
    "✅ Tổng kết những gì đã hoàn thành\n"
    "📝 Ghi lại điểm cần cải thiện\n"
    "📅 Lên kế hoạch ngày mai (3-5 task)\n"
    "💼 Dọn dẹp workspace\n\n"
    "🎉 Great job today!",
]

BREAK_MESSAGES = [
    "⏸️ <b>NGHỈ GIẢI LAO</b>\n\n"
    "🚶 Đứng dậy đi bộ 5 phút\n"
    "💧 Uống nước\n"
    "🪟 Nhìn xa 20 giây\n"
    "🧘 Duỗi người, xoay cổ\n\n"
    "Quay lại sau 5-10 phút!",
]

WATER_MESSAGES = [
    "💧 <b>UỐNG NƯỚC NÀO!</b>\n\n"
    "🚰 Uống 200-300ml nước lọc\n"
    "✨ Giữ cơ thể luôn tràn đầy năng lượng",
    
    "💧 <b>HYDRATE TIME!</b>\n\n"
    "💦 Cơ thể cần nước để hoạt động tốt\n"
    "🎯 Mục tiêu: 2-2.5L/ngày",
]

EYE_MESSAGES = [
    "👁️ <b>NGHỈ MẮT</b>\n\n"
    "🪟 Nhìn xa 6m trong 20 giây\n"
    "👀 Chớp mắt 10 lần\n"
    "🙈 Đắp mắt và thở sâu\n\n"
    "Bảo vệ đôi mắt của bạn!",
]

POSTURE_MESSAGES = [
    "🧘 <b>KIỂM TRA TƯ THẾ</b>\n\n"
    "🪑 Lưng thẳng, vai thả lỏng\n"
    "💺 Chân đặt sát sàn\n"
    "🖥️ Màn hình ngang tầm mắt\n"
    "✋ Cổ tay thẳng khi gõ phím\n\n"
    "Tư thế đúng = Sức khỏe lâu dài!",
]

EXERCISE_MESSAGES = [
    "🏋️ <b>GIỜ TẬP LUYỆN!</b>\n\n"
    "🏃 30 phút cardio/tập tạ\n"
    "🧘 Hoặc yoga/pilates\n"
    "🚴 Hoặc đạp xe, bơi lội\n\n"
    "💪 Hãy chăm sóc cơ thể bạn!",
]

MEAL_MESSAGES = {
    "breakfast": [
        "🌅 <b>GIỜ ĂN SÁNG!</b>\n\n"
        "🥚 Protein: trứng, thịt, cá\n"
        "🥖 Carb: bánh mì, yến mạch\n"
        "🥗 Rau xanh, trái cây\n"
        "☕ Đồ uống: nước, café, trà\n\n"
        "Bữa sáng = Năng lượng cả ngày!",
    ],
    "lunch": [
        "☀️ <b>GIỜ ĂN TRƯA!</b>\n\n"
        "🍚 Cơm/bún/phở + rau + protein\n"
        "🥗 Cân bằng dinh dưỡng\n"
        "💧 Uống đủ nước\n\n"
        "Ăn no, nghỉ ngắn, làm tiếp!",
    ],
    "dinner": [
        "🌙 <b>GIỜ ĂN TỐI!</b>\n\n"
        "🍲 Ăn nhẹ hơn bữa trưa\n"
        "🥗 Nhiều rau, ít tinh bột\n"
        "🚫 Tránh ăn quá no\n"
        "⏰ Ăn trước 19:00 là tốt nhất\n\n"
        "Ăn tối hợp lý = Ngủ ngon!",
    ],
}

import random

def get_random_message(messages: List[str]) -> str:
    return random.choice(messages)

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
    while not shutdown_event.is_set():
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
                
                # Wake reminder
                if hhmm == u.get("wake_time") and should_fire_once_per_minute(u, "wake", now):
                    tg_send(chat_id, get_random_message(WAKE_MESSAGES))
                    mark_fired(u, chat_id, "wake", now)

                # Sleep reminder
                if hhmm == u.get("sleep_time") and should_fire_once_per_minute(u, "sleep", now):
                    tg_send(chat_id, get_random_message(SLEEP_MESSAGES))
                    mark_fired(u, chat_id, "sleep", now)

                # Work start/end
                if u.get("work_enabled") and weekday in (u.get("work_days") or []):
                    if hhmm == u.get("work_start") and should_fire_once_per_minute(u, "work_start", now):
                        tg_send(chat_id, get_random_message(WORK_START_MESSAGES))
                        mark_fired(u, chat_id, "work_start", now)

                    if hhmm == u.get("work_end") and should_fire_once_per_minute(u, "work_end", now):
                        tg_send(chat_id, get_random_message(WORK_END_MESSAGES))
                        mark_fired(u, chat_id, "work_end", now)

                # Break reminders (during work hours)
                if u.get("break_enabled"):
                    if in_window(now, u.get("break_window_start", "09:00"), u.get("break_window_end", "18:00")):
                        every_min = int(u.get("break_every_min") or 120)
                        last_ts = int(u.get("last_break_ts") or 0)
                        if last_ts == 0:
                            update_user(chat_id, {"last_break_ts": int(time.time())})
                        else:
                            if time.time() - last_ts >= every_min * 60:
                                tg_send(chat_id, get_random_message(BREAK_MESSAGES))
                                update_user(chat_id, {"last_break_ts": int(time.time())})

                # Water reminders
                if u.get("water_enabled"):
                    if in_window(now, u.get("water_window_start", "08:00"), u.get("water_window_end", "22:00")):
                        every_min = int(u.get("water_every_min") or 60)
                        last_ts = int(u.get("last_water_ts") or 0)
                        if last_ts == 0:
                            update_user(chat_id, {"last_water_ts": int(time.time())})
                        else:
                            if time.time() - last_ts >= every_min * 60:
                                tg_send(chat_id, get_random_message(WATER_MESSAGES))
                                update_user(chat_id, {"last_water_ts": int(time.time())})

                # Eye care reminders
                if u.get("eye_enabled"):
                    if in_window(now, u.get("eye_window_start", "08:00"), u.get("eye_window_end", "22:00")):
                        every_min = int(u.get("eye_every_min") or 30)
                        last_ts = int(u.get("last_eye_ts") or 0)
                        if last_ts == 0:
                            update_user(chat_id, {"last_eye_ts": int(time.time())})
                        else:
                            if time.time() - last_ts >= every_min * 60:
                                tg_send(chat_id, get_random_message(EYE_MESSAGES))
                                update_user(chat_id, {"last_eye_ts": int(time.time())})

                # Posture reminders
                if u.get("posture_enabled"):
                    if in_window(now, u.get("posture_window_start", "08:00"), u.get("posture_window_end", "22:00")):
                        every_min = int(u.get("posture_every_min") or 45)
                        last_ts = int(u.get("last_posture_ts") or 0)
                        if last_ts == 0:
                            update_user(chat_id, {"last_posture_ts": int(time.time())})
                        else:
                            if time.time() - last_ts >= every_min * 60:
                                tg_send(chat_id, get_random_message(POSTURE_MESSAGES))
                                update_user(chat_id, {"last_posture_ts": int(time.time())})

                # Exercise reminder
                if u.get("exercise_enabled"):
                    if hhmm == u.get("exercise_time") and should_fire_once_per_minute(u, "exercise", now):
                        tg_send(chat_id, get_random_message(EXERCISE_MESSAGES))
                        mark_fired(u, chat_id, "exercise", now)

                # Meal reminders
                if u.get("meal_enabled"):
                    if hhmm == u.get("breakfast_time") and should_fire_once_per_minute(u, "breakfast", now):
                        tg_send(chat_id, get_random_message(MEAL_MESSAGES["breakfast"]))
                        mark_fired(u, chat_id, "breakfast", now)
                    
                    if hhmm == u.get("lunch_time") and should_fire_once_per_minute(u, "lunch", now):
                        tg_send(chat_id, get_random_message(MEAL_MESSAGES["lunch"]))
                        mark_fired(u, chat_id, "lunch", now)
                    
                    if hhmm == u.get("dinner_time") and should_fire_once_per_minute(u, "dinner", now):
                        tg_send(chat_id, get_random_message(MEAL_MESSAGES["dinner"]))
                        mark_fired(u, chat_id, "dinner", now)

        except Exception as e:
            log.exception(f"Scheduler error: {e}")

        time.sleep(SCHED_TICK)

# ==========================================================
# PRESETS
# ==========================================================
def apply_preset(chat_id: Any, preset: str) -> None:
    """Apply predefined schedule presets"""
    presets = {
        "STANDARD": {
            "wake_time": "07:00",
            "sleep_time": "23:00",
            "work_enabled": True,
            "work_start": "09:00",
            "work_end": "18:00",
            "break_enabled": True,
            "break_every_min": 120,
            "water_enabled": True,
            "water_every_min": 60,
            "eye_enabled": True,
            "eye_every_min": 30,
            "posture_enabled": True,
            "posture_every_min": 45,
            "exercise_enabled": True,
            "exercise_time": "18:30",
            "meal_enabled": True,
            "breakfast_time": "07:30",
            "lunch_time": "12:00",
            "dinner_time": "18:30",
        },
        "OFFICE": {
            "wake_time": "06:30",
            "sleep_time": "23:00",
            "work_enabled": True,
            "work_start": "08:30",
            "work_end": "17:30",
            "break_enabled": True,
            "break_every_min": 90,
            "water_enabled": True,
            "water_every_min": 45,
            "eye_enabled": True,
            "eye_every_min": 25,
            "posture_enabled": True,
            "posture_every_min": 40,
            "exercise_enabled": True,
            "exercise_time": "18:00",
            "meal_enabled": True,
            "breakfast_time": "07:00",
            "lunch_time": "12:00",
            "dinner_time": "18:30",
        },
        "DEVELOPER": {
            "wake_time": "07:30",
            "sleep_time": "00:00",
            "work_enabled": True,
            "work_start": "10:00",
            "work_end": "19:00",
            "break_enabled": True,
            "break_every_min": 90,
            "water_enabled": True,
            "water_every_min": 60,
            "eye_enabled": True,
            "eye_every_min": 20,
            "posture_enabled": True,
            "posture_every_min": 40,
            "exercise_enabled": True,
            "exercise_time": "19:30",
            "meal_enabled": True,
            "breakfast_time": "08:00",
            "lunch_time": "12:30",
            "dinner_time": "19:30",
        },
        "STUDENT": {
            "wake_time": "06:00",
            "sleep_time": "22:30",
            "work_enabled": True,
            "work_start": "07:30",
            "work_end": "17:00",
            "break_enabled": True,
            "break_every_min": 120,
            "water_enabled": True,
            "water_every_min": 60,
            "eye_enabled": True,
            "eye_every_min": 30,
            "posture_enabled": True,
            "posture_every_min": 45,
            "exercise_enabled": True,
            "exercise_time": "17:30",
            "meal_enabled": True,
            "breakfast_time": "06:30",
            "lunch_time": "11:30",
            "dinner_time": "18:00",
        },
        "FITNESS": {
            "wake_time": "05:30",
            "sleep_time": "22:00",
            "work_enabled": True,
            "work_start": "08:00",
            "work_end": "17:00",
            "break_enabled": True,
            "break_every_min": 120,
            "water_enabled": True,
            "water_every_min": 30,
            "eye_enabled": True,
            "eye_every_min": 30,
            "posture_enabled": True,
            "posture_every_min": 45,
            "exercise_enabled": True,
            "exercise_time": "06:00",
            "meal_enabled": True,
            "breakfast_time": "07:00",
            "lunch_time": "12:00",
            "dinner_time": "18:00",
        },
    }
    
    if preset in presets:
        patch = presets[preset]
        # Initialize all timestamp fields
        patch["last_water_ts"] = int(time.time())
        patch["last_break_ts"] = int(time.time())
        patch["last_eye_ts"] = int(time.time())
        patch["last_posture_ts"] = int(time.time())
        update_user(chat_id, patch)

# ==========================================================
# TELEGRAM UPDATES LOOP
# ==========================================================
def handle_updates_forever() -> None:
    log.info("📱 Updates handler started")
    offset = 0

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

            for upd in d.get("result", []):
                offset = upd.get("update_id", offset)

                # Callback queries (buttons)
                if "callback_query" in upd:
                    cq = upd["callback_query"]
                    cid = cq.get("message", {}).get("chat", {}).get("id")
                    action = (cq.get("data") or "").upper()
                    if not cid:
                        continue

                    u = ensure_user(cid)

                    # Toggle bot
                    if action == "TOGGLE_BOT":
                        newv = not bool(u.get("enabled"))
                        update_user(cid, {"enabled": newv})
                        tg_answer_callback(cq["id"], "✅ Đã cập nhật")
                        u = ensure_user(cid)
                        tg_send(cid, build_overview(u), reply_markup=kb_main(u))

                    # Menu navigation
                    elif action == "MENU_DAILY":
                        tg_answer_callback(cq["id"], "📅")
                        tg_send(cid, "📅 <b>LỊCH HÀNG NGÀY</b>\n\nChọn mục bạn muốn chỉnh:", reply_markup=kb_daily(u))
                    
                    elif action == "MENU_WORK":
                        tg_answer_callback(cq["id"], "🧑‍💻")
                        tg_send(cid, "🧑‍💻 <b>LỊCH LÀM VIỆC</b>\n\nQuản lý thời gian làm việc:", reply_markup=kb_work(u))
                    
                    elif action == "MENU_HEALTH":
                        tg_answer_callback(cq["id"], "💧")
                        tg_send(cid, "💧 <b>SỨC KHỎE</b>\n\nCác nhắc nhở chăm sóc sức khỏe:", reply_markup=kb_health(u))
                    
                    elif action == "MENU_MEAL":
                        tg_answer_callback(cq["id"], "🍱")
                        tg_send(cid, "🍱 <b>BỮA ĂN</b>\n\nLịch bữa ăn hàng ngày:", reply_markup=kb_meal(u))
                    
                    elif action == "MENU_QUICK":
                        tg_answer_callback(cq["id"], "⚙️")
                        tg_send(cid, "⚙️ <b>CÀI ĐẶT NHANH</b>\n\nChọn mẫu lịch phù hợp với bạn:", reply_markup=kb_quick())
                    
                    elif action == "SHOW_ALL":
                        tg_answer_callback(cq["id"], "📊")
                        u = ensure_user(cid)
                        tg_send(cid, build_overview(u), reply_markup=kb_main(u))
                    
                    elif action == "BACK_MAIN":
                        tg_answer_callback(cq["id"], "⬅️")
                        u = ensure_user(cid)
                        tg_send(cid, build_overview(u), reply_markup=kb_main(u))

                    # Toggles
                    elif action == "TOGGLE_WORK":
                        newv = not bool(u.get("work_enabled"))
                        update_user(cid, {"work_enabled": newv})
                        tg_answer_callback(cq["id"], "✅")
                        u = ensure_user(cid)
                        tg_send(cid, "🧑‍💻 <b>LỊCH LÀM VIỆC</b>\n\nQuản lý thời gian làm việc:", reply_markup=kb_work(u))
                    
                    elif action == "TOGGLE_BREAK":
                        newv = not bool(u.get("break_enabled"))
                        patch = {"break_enabled": newv}
                        if newv:
                            patch["last_break_ts"] = int(time.time())
                        update_user(cid, patch)
                        tg_answer_callback(cq["id"], "✅")
                        u = ensure_user(cid)
                        tg_send(cid, "🧑‍💻 <b>LỊCH LÀM VIỆC</b>\n\nQuản lý thời gian làm việc:", reply_markup=kb_work(u))
                    
                    elif action == "TOGGLE_WATER":
                        newv = not bool(u.get("water_enabled"))
                        patch = {"water_enabled": newv}
                        if newv:
                            patch["last_water_ts"] = int(time.time())
                        update_user(cid, patch)
                        tg_answer_callback(cq["id"], "✅")
                        u = ensure_user(cid)
                        tg_send(cid, "💧 <b>SỨC KHỎE</b>\n\nCác nhắc nhở chăm sóc sức khỏe:", reply_markup=kb_health(u))
                    
                    elif action == "TOGGLE_EYE":
                        newv = not bool(u.get("eye_enabled"))
                        patch = {"eye_enabled": newv}
                        if newv:
                            patch["last_eye_ts"] = int(time.time())
                        update_user(cid, patch)
                        tg_answer_callback(cq["id"], "✅")
                        u = ensure_user(cid)
                        tg_send(cid, "💧 <b>SỨC KHỎE</b>\n\nCác nhắc nhở chăm sóc sức khỏe:", reply_markup=kb_health(u))
                    
                    elif action == "TOGGLE_POSTURE":
                        newv = not bool(u.get("posture_enabled"))
                        patch = {"posture_enabled": newv}
                        if newv:
                            patch["last_posture_ts"] = int(time.time())
                        update_user(cid, patch)
                        tg_answer_callback(cq["id"], "✅")
                        u = ensure_user(cid)
                        tg_send(cid, "💧 <b>SỨC KHỎE</b>\n\nCác nhắc nhở chăm sóc sức khỏe:", reply_markup=kb_health(u))
                    
                    elif action == "TOGGLE_EXERCISE":
                        newv = not bool(u.get("exercise_enabled"))
                        update_user(cid, {"exercise_enabled": newv})
                        tg_answer_callback(cq["id"], "✅")
                        u = ensure_user(cid)
                        tg_send(cid, "💧 <b>SỨC KHỎE</b>\n\nCác nhắc nhở chăm sóc sức khỏe:", reply_markup=kb_health(u))
                    
                    elif action == "TOGGLE_MEAL":
                        newv = not bool(u.get("meal_enabled"))
                        update_user(cid, {"meal_enabled": newv})
                        tg_answer_callback(cq["id"], "✅")
                        u = ensure_user(cid)
                        tg_send(cid, "🍱 <b>BỮA ĂN</b>\n\nLịch bữa ăn hàng ngày:", reply_markup=kb_meal(u))

                    # Presets
                    elif action.startswith("PRESET_"):
                        preset = action.replace("PRESET_", "")
                        apply_preset(cid, preset)
                        tg_answer_callback(cq["id"], "✅ Đã áp dụng lịch!")
                        u = ensure_user(cid)
                        tg_send(cid, build_overview(u), reply_markup=kb_main(u))

                    continue

                # Text messages
                msg = upd.get("message", {})
                text = (msg.get("text") or "").strip()
                if not text:
                    continue

                cid = msg["chat"]["id"]
                cmd = text.split()[0].lower()

                u = ensure_user(cid)

                if cmd == "/start":
                    tg_send(
                        cid,
                        "╔═══════════════════════╗\n"
                        "║  🤖 <b>TRỢ LÝ CÁ NHÂN</b>  ║\n"
                        "╚═══════════════════════╝\n\n"
                        "Chào mừng bạn! 👋\n\n"
                        "Mình là trợ lý thông minh giúp bạn:\n"
                        "✅ Quản lý thời gian hiệu quả\n"
                        "✅ Chăm sóc sức khỏe toàn diện\n"
                        "✅ Duy trì thói quen tốt\n\n"
                        "Bấm nút bên dưới để bắt đầu! 🚀",
                        reply_markup=kb_main(u),
                    )
                    time.sleep(0.5)
                    tg_send(cid, build_overview(u), reply_markup=kb_main(u))

                elif cmd == "/show":
                    tg_send(cid, build_overview(u), reply_markup=kb_main(u))

                elif cmd == "/on":
                    update_user(cid, {"enabled": True})
                    u = ensure_user(cid)
                    tg_send(cid, "✅ <b>ĐÃ BẬT BOT</b>\n\nMình sẽ nhắc bạn theo lịch đã cài!", reply_markup=kb_main(u))

                elif cmd == "/off":
                    update_user(cid, {"enabled": False})
                    u = ensure_user(cid)
                    tg_send(cid, "🔴 <b>ĐÃ TẮT BOT</b>\n\nGõ /on để bật lại nhé!", reply_markup=kb_main(u))

                elif cmd == "/help":
                    help_text = (
                        "📚 <b>HƯỚNG DẪN SỬ DỤNG</b>\n\n"
                        "<b>Lệnh cơ bản:</b>\n"
                        "/start - Khởi động bot\n"
                        "/show - Xem lịch hiện tại\n"
                        "/on - Bật bot\n"
                        "/off - Tắt bot\n"
                        "/help - Xem hướng dẫn\n\n"
                        "<b>Tính năng:</b>\n"
                        "🌅 Nhắc thức dậy & đi ngủ\n"
                        "🧑‍💻 Quản lý giờ làm việc\n"
                        "💧 Nhắc uống nước định kỳ\n"
                        "👁️ Nhắc nghỉ mắt\n"
                        "🧘 Nhắc kiểm tra tư thế\n"
                        "🏋️ Nhắc tập luyện\n"
                        "🍱 Nhắc bữa ăn\n"
                        "⏸️ Nhắc nghỉ giải lao\n\n"
                        "Sử dụng nút để cài đặt nhanh!"
                    )
                    tg_send(cid, help_text, reply_markup=kb_main(u))

                else:
                    tg_send(cid, "Bấm nút bên dưới để điều khiển bot 👇", reply_markup=kb_main(u))

        except Exception as e:
            log.exception(f"Updates loop error: {e}")
            time.sleep(2)

# ==========================================================
# FLASK
# ==========================================================
app = Flask(__name__)

@app.route("/")
def home():
    return {
        "status": "online",
        "service": "Professional Reminder Assistant",
        "time": fmt_dt(),
        "features": {
            "self_ping": RENDER_EXTERNAL_URL is not None,
            "scheduler": True,
            "multi_reminder": True
        }
    }

@app.route("/health")
def health():
    return {"status": "healthy", "timestamp": now_vn().isoformat()}

@app.route("/ping")
def ping():
    return {"pong": fmt_dt()}

# ==========================================================
# SHUTDOWN
# ==========================================================
def _handle_signal(signum, frame):
    log.warning(f"Signal {signum} received. Shutting down...")
    shutdown_event.set()

signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)

# ==========================================================
# MAIN
# ==========================================================
if __name__ == "__main__":
    log.info("=" * 60)
    log.info("🚀 PROFESSIONAL REMINDER ASSISTANT v2.0")
    log.info("=" * 60)
    log.info("🌐 Service URL: %s", RENDER_EXTERNAL_URL or "Not detected")

    me = tg_call("getMe", read_timeout=20)
    if me.get("ok"):
        log.info(f"✅ Telegram connected: @{me.get('result', {}).get('username')}")
    else:
        log.warning(f"⚠️ Telegram connection issue: {me.get('description')}")

    # Start self-ping keeper
    pinger_thread = threading.Thread(target=run_self_pinger, daemon=True, name="SelfPingerThread")
    pinger_thread.start()
    log.info("✅ Self-ping keeper started")

    # Start scheduler
    scheduler_thread = threading.Thread(target=scheduler_loop, daemon=True, name="SchedulerThread")
    scheduler_thread.start()
    log.info("✅ Scheduler started")

    # Flask thread
    flask_thread = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=PORT, debug=False),
        daemon=True,
        name="FlaskThread"
    )
    flask_thread.start()
    log.info(f"✅ Flask running on port {PORT}")

    # Updates loop (blocking - keeps main thread alive)
    handle_updates_forever()

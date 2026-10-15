"""javcrawler 状态与日志持久化：
- state.json 记录每个影片的刮削结果（success/failed）
- logs/YYYY-MM-DD.log 按日期存放运行日志
"""
import datetime
import json
import os
import threading

DATA_DIR = os.environ.get("JAVCRAWLER_DATA", "/app/data")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
LOG_DIR = os.path.join(DATA_DIR, "logs")

_lock = threading.Lock()


def _ensure():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)


def load_state():
    _ensure()
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            st = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        st = {}
    st.setdefault("movies", {})
    st.setdefault("last_auto_run", 0)
    return st


def save_state(st):
    _ensure()
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_PATH)


def set_movie(path, dvdid, status, error=""):
    """记录单个影片的刮削结果"""
    with _lock:
        st = load_state()
        st["movies"][path] = {
            "dvdid": dvdid,
            "status": status,
            "error": error,
            "time": datetime.datetime.now().isoformat(timespec="seconds"),
        }
        save_state(st)


def set_last_auto_run(ts):
    with _lock:
        st = load_state()
        st["last_auto_run"] = ts
        save_state(st)


def append_log(msg):
    """按日期追加运行日志"""
    _ensure()
    today = datetime.date.today().isoformat()
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    try:
        with open(os.path.join(LOG_DIR, today + ".log"), "a", encoding="utf-8") as f:
            f.write("[{}] {}\n".format(ts, msg))
    except OSError:
        pass


def log_dates():
    """返回有日志文件的日期列表（新→旧）"""
    _ensure()
    dates = []
    for f in os.listdir(LOG_DIR):
        if f.endswith(".log"):
            try:
                datetime.date.fromisoformat(f[:-4])
                dates.append(f[:-4])
            except ValueError:
                pass
    return sorted(dates, reverse=True)


def read_log(date):
    p = os.path.join(LOG_DIR, date + ".log")
    try:
        with open(p, "r", encoding="utf-8") as f:
            return f.read()
    except (FileNotFoundError, OSError):
        return ""


def purge_logs():
    """删除全部日志文件，返回被删除的日期列表"""
    _ensure()
    removed = []
    for f in os.listdir(LOG_DIR):
        if not f.endswith(".log"):
            continue
        d = f[:-4]
        try:
            datetime.date.fromisoformat(d)
        except ValueError:
            continue
        try:
            os.remove(os.path.join(LOG_DIR, f))
            removed.append(d)
        except OSError:
            pass
    return sorted(removed)


def cleanup_logs(days=7):
    """删除 N 天前的日志，返回被删除的日期列表"""
    _ensure()
    cutoff = datetime.date.today() - datetime.timedelta(days=days)
    removed = []
    for f in os.listdir(LOG_DIR):
        if not f.endswith(".log"):
            continue
        d = f[:-4]
        try:
            if datetime.date.fromisoformat(d) < cutoff:
                os.remove(os.path.join(LOG_DIR, f))
                removed.append(d)
        except ValueError:
            pass
    return sorted(removed)

"""javcrawler FastAPI 应用：WebUI + 多目录就地刮削 + 定时调度 + 历史日志"""
import datetime
import logging
import os
import shutil
import threading
import time

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from . import scraper, config as cfgmod, store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("javcrawler.main")

app = FastAPI(title="JavCrawler", version="2.0.0")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 刮削运行状态（内存）
_state = {"running": False, "log": [], "result": None, "cancel": False,
          "progress": {"current": 0, "total": 0}}
_lock = threading.Lock()


def _add_log(msg):
    with _lock:
        _state["log"].append(msg)
        _state["log"] = _state["log"][-500:]
    store.append_log(msg)
    log.info(msg)


def _apply_timezone(tz):
    """应用时区（定时任务与日志按此时区）。非法时区回退 Asia/Shanghai"""
    if not tz:
        tz = "Asia/Shanghai"
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(tz)  # 校验
    except Exception:
        tz = "Asia/Shanghai"
    os.environ["TZ"] = tz
    time.tzset()
    return tz


def _current_tz():
    return os.environ.get("TZ") or "Asia/Shanghai"


# ---------------- 页面 ----------------

@app.get("/", response_class=HTMLResponse)
def index():
    html_path = os.path.join(BASE_DIR, "templates", "index.html")
    with open(html_path, encoding="utf-8") as f:
        return HTMLResponse(f.read())


# ---------------- 配置 ----------------

@app.get("/api/config")
def get_config():
    return JSONResponse(cfgmod.load_config())


@app.post("/api/config")
def set_config(payload: dict):
    cfg = cfgmod.update_config(**payload)
    _apply_timezone(cfg.get("timezone"))
    return JSONResponse(cfg)


# ---------------- 目录浏览（手动选择目录用） ----------------

@app.get("/api/browse")
def browse(path: str = "/"):
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        return JSONResponse({"ok": False, "msg": "目录不存在", "path": path,
                             "parent": None, "dirs": []})
    try:
        names = os.listdir(path)
    except PermissionError:
        return JSONResponse({"ok": False, "msg": "无权限读取", "path": path,
                             "parent": None, "dirs": []})
    dirs = sorted(n for n in names
                  if not n.startswith(".") and os.path.isdir(os.path.join(path, n)))
    parent = os.path.dirname(path) if path != "/" else None
    return JSONResponse({"ok": True, "path": path, "parent": parent, "dirs": dirs})


# ---------------- 刮削控制 ----------------

@app.post("/api/scrape")
def start_scrape(payload: dict = None):
    force = bool((payload or {}).get("force"))
    cfg = cfgmod.load_config()
    with _lock:
        if _state["running"]:
            return JSONResponse({"ok": False, "msg": "已有刮削任务在运行"}, status_code=400)
        _state.update(running=True, log=[], result=None, cancel=False,
                      progress={"current": 0, "total": 0})
    t = threading.Thread(target=_run_scrape, args=(cfg, force), daemon=True)
    t.start()
    return JSONResponse({"ok": True, "msg": "强制重刮已启动（全部影片将重新刮削）" if force else "刮削已启动"})


@app.post("/api/cancel")
def cancel_scrape():
    _state["cancel"] = True
    return JSONResponse({"ok": True, "msg": "已发送取消指令"})


@app.get("/api/status")
def get_status():
    with _lock:
        return JSONResponse({
            "running": _state["running"],
            "log": _state["log"][-100:],
            "result": _state["result"],
            "progress": _state["progress"],
        })


# ---------------- 统计 ----------------

@app.get("/api/stats")
def get_stats():
    cfg = cfgmod.load_config()
    st = store.load_state()
    total = scraped_n = failed_n = 0
    per_dir = []
    failed_list = []
    for d in cfg.get("input_directories", []):
        if not d or not os.path.isdir(d):
            per_dir.append({"dir": d, "exists": False, "total": 0,
                            "scraped": 0, "failed": 0, "pending": 0})
            continue
        movies = scraper.list_movies(d)
        s = f = 0
        for m in movies:
            if scraper.is_scraped(m["file"]):
                s += 1
            else:
                rec = st["movies"].get(m["file"])
                if rec and rec.get("status") == "failed":
                    f += 1
                    if len(failed_list) < 100:
                        failed_list.append({"file": m["file"], "dvdid": m["dvdid"],
                                            "error": rec.get("error", ""),
                                            "time": rec.get("time", "")})
        p = len(movies) - s - f
        per_dir.append({"dir": d, "exists": True, "total": len(movies),
                        "scraped": s, "failed": f, "pending": p})
        total += len(movies)
        scraped_n += s
        failed_n += f
    return JSONResponse({
        "total": total,
        "scraped": scraped_n,
        "failed": failed_n,
        "pending": total - scraped_n - failed_n,
        "dirs": per_dir,
        "failed_list": failed_list,
        "last_auto_run": st.get("last_auto_run", 0),
        "timezone": _current_tz(),
    })


@app.get("/api/movies")
def list_movies_api(status: str = "all"):
    """影片明细列表，status: all / scraped / pending / failed"""
    cfg = cfgmod.load_config()
    st = store.load_state()
    result = []
    for d in cfg.get("input_directories", []):
        if not d or not os.path.isdir(d):
            continue
        for m in scraper.list_movies(d):
            if scraper.is_scraped(m["file"]):
                stt = "scraped"
            else:
                rec = st["movies"].get(m["file"]) or {}
                stt = "failed" if rec.get("status") == "failed" else "pending"
            if status not in ("all", stt):
                continue
            rec = st["movies"].get(m["file"]) or {}
            result.append({"dir": d, "file": m["file"], "dvdid": m["dvdid"],
                           "status": stt, "error": rec.get("error", ""),
                           "time": rec.get("time", "")})
    return JSONResponse({"ok": True, "count": len(result), "movies": result})


# ---------------- 历史日志 ----------------

@app.get("/api/logs")
def get_logs(date: str = None):
    if date:
        return JSONResponse({"ok": True, "date": date, "content": store.read_log(date)})
    return JSONResponse({"ok": True, "dates": store.log_dates()})


@app.post("/api/logs/cleanup")
def logs_cleanup(days: int = 7, all: bool = False):
    if all:
        removed = store.purge_logs()
        return JSONResponse({"ok": True, "removed": removed,
                             "msg": "已删除全部 {} 个日志文件".format(len(removed))})
    removed = store.cleanup_logs(days)
    return JSONResponse({"ok": True, "removed": removed,
                         "msg": "已删除 {} 天前的 {} 个日志文件".format(days, len(removed))})


# ---------------- 定时调度 ----------------

def _compute_next_run(sched, last_auto_run):
    """根据调度配置计算下次运行时间戳"""
    now = time.time()
    mode = sched.get("mode", "daily")
    if mode == "interval":
        # 只支持 4 / 8 小时两档，锚定服务器时间 0:00 起算的固定时间点：
        # 8h -> 0:00 / 8:00 / 16:00；4h -> 0:00 / 4:00 / 8:00 / 12:00 / 16:00 / 20:00
        hours = 8 if int(sched.get("interval_hours") or 8) >= 8 else 4
        if not last_auto_run:
            return now + 60  # 首次启用：1 分钟后先跑一轮
        midnight = datetime.datetime.combine(
            datetime.date.today(), datetime.time(0, 0)
        ).timestamp()
        slot = int((now - midnight) // (hours * 3600)) + 1
        return midnight + slot * hours * 3600
    # daily
    hh, mm = 3, 0
    try:
        parts = str(sched.get("daily_time") or "03:00").split(":")
        hh, mm = int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        pass
    target = datetime.datetime.combine(
        datetime.date.today(), datetime.time(min(hh, 23), min(mm, 59))
    ).timestamp()
    if target <= now:
        target += 86400
    return target


def _scheduler_loop():
    log.info("定时调度线程已启动")
    while True:
        try:
            cfg = cfgmod.load_config()
            sched = cfg.get("auto_schedule") or {}
            if sched.get("enabled"):
                st = store.load_state()
                nxt = _compute_next_run(sched, st.get("last_auto_run", 0))
                if time.time() >= nxt:
                    if not _state["running"]:
                        _add_log("⏰ 定时任务触发，开始自动检查刮削")
                        with _lock:
                            if not _state["running"]:
                                _state.update(running=True, log=[], result=None,
                                              cancel=False, progress={"current": 0, "total": 0})
                                t = threading.Thread(target=_run_scrape, args=(cfg,), daemon=True)
                                t.start()
                        store.set_last_auto_run(time.time())
                    else:
                        # 正在运行中，顺延到下一周期
                        store.set_last_auto_run(time.time())
        except Exception:
            log.exception("调度线程异常")
        time.sleep(30)


@app.on_event("startup")
def _on_startup():
    cfg = cfgmod.load_config()
    tz = _apply_timezone(cfg.get("timezone"))
    log.info("时区: %s", tz)
    threading.Thread(target=_scheduler_loop, daemon=True).start()


# ---------------- 刮削主流程 ----------------

def _ensure_folder_poster(video_file):
    """单视频文件夹：把同名 -poster.jpg 复制一份为 poster.jpg（媒体库文件夹海报）。
    返回 True 表示本次新生成了 poster.jpg"""
    poster_path = scraper.poster_path_for(video_file)
    if not os.path.exists(poster_path):
        return False
    vdir = os.path.dirname(video_file)
    try:
        vids = [f for f in os.listdir(vdir)
                if not f.startswith(scraper.IGNORE_PREFIX)
                and os.path.splitext(f)[1].lower() in scraper.VIDEO_EXTS]
    except OSError:
        return False
    if len(vids) != 1:
        return False
    folder_poster = os.path.join(vdir, "poster.jpg")
    if os.path.abspath(folder_poster) == os.path.abspath(poster_path):
        return False
    try:
        shutil.copyfile(poster_path, folder_poster)
        return True
    except OSError as e:
        _add_log("  ! 文件夹海报生成失败 {}: {}".format(os.path.basename(vdir), e))
        return False


def _make_engines(cfg):
    """按配置创建主/备刮削引擎，返回 (primary, backup, primary_name, backup_name)"""
    engine = (cfg.get("engine") or "javbus").strip().lower()
    cookie = cfg.get("javdb_cookie", "")
    proxy = cfg.get("proxy", "")
    fb = bool(cfg.get("engine_fallback", True))
    if engine == "javdb":
        primary = scraper.JavdbScraper(cookie=cookie, proxy=proxy)
        backup = scraper.JavBusScraper(cookie=cookie, proxy=proxy) if fb else None
        return primary, backup, "javdb", "javbus"
    primary = scraper.JavBusScraper(cookie=cookie, proxy=proxy)
    backup = scraper.JavdbScraper(cookie=cookie, proxy=proxy) if fb else None
    return primary, backup, "javbus", "javdb"


def _run_scrape(cfg, force=False):
    try:
        dirs = [d for d in (cfg.get("input_directories") or []) if d and os.path.isdir(d)]
        skipped_cfg = [d for d in (cfg.get("input_directories") or []) if d and not os.path.isdir(d)]
        if not dirs:
            _add_log("错误：未配置刮削目录，或配置的目录在容器内不存在")
            if skipped_cfg:
                _add_log("  （容器内不存在的目录: {}）".format(", ".join(skipped_cfg)))
            _state["result"] = {"ok": False, "msg": "未配置刮削目录或目录不存在"}
            return
        if skipped_cfg:
            _add_log("跳过容器内不存在的目录: " + ", ".join(skipped_cfg))

        movies = []
        for d in dirs:
            _add_log("扫描目录: {}".format(d))
            movies += scraper.list_movies(d)
        if not movies:
            _add_log("未扫描到 JAV 番号视频文件")
            _state["result"] = {"ok": True, "success": 0, "fail": 0, "skip": 0, "total": 0}
            return

        if force:
            targets = list(movies)
            _add_log("🔁 强制重刮模式：全部 {} 个影片将重新刮削（更新标题/海报等）".format(len(movies)))
        else:
            targets = [m for m in movies if not scraper.is_scraped(m["file"])]
            _add_log("共 {} 个影片，已刮削 {}，本次待处理 {}".format(
                len(movies), len(movies) - len(targets), len(targets)))

        # 已刮削影片：补齐单视频文件夹的 poster.jpg（兼容之前版本刮好的数据）
        for m in movies:
            if m not in targets and _ensure_folder_poster(m["file"]):
                _add_log("📁 已补齐文件夹海报: {}/poster.jpg".format(os.path.dirname(m["file"])))

        ok = fail = 0
        if targets:
            primary, backup, p_name, b_name = _make_engines(cfg)
            if backup:
                _add_log("刮削引擎: {}（备用: {}）".format(p_name, b_name))
            else:
                _add_log("刮削引擎: {}（未启用备用）".format(p_name))
            for idx, m in enumerate(targets, 1):
                if _state["cancel"]:
                    _add_log("已取消")
                    break
                _state["progress"] = {"current": idx, "total": len(targets)}
                _add_log("[{}/{}] {}: {}".format(
                    idx, len(targets), m["dvdid"], os.path.basename(m["file"])))
                nfo_path = scraper.nfo_path_for(m["file"])
                poster_path = scraper.poster_path_for(m["file"])
                fanart_path = scraper.fanart_path_for(m["file"])
                try:
                    try:
                        s, data = primary, primary.scrape(m["dvdid"])
                    except Exception as pe:
                        s, data = primary, None
                        _add_log("  · 主引擎 {} 请求异常: {}".format(p_name, pe))
                    if not data and backup:
                        _add_log("  · {} 未找到，改用 {} 补抓".format(p_name, b_name))
                        try:
                            s, data = backup, backup.scrape(m["dvdid"])
                        except Exception as be:
                            s, data = backup, None
                            _add_log("  · 备用引擎 {} 请求异常: {}".format(b_name, be))
                    if not data or not data.get("title"):
                        raise RuntimeError("{} 未找到该番号或数据不完整".format(p_name))
                    # 标题选择：默认用原始标题；开启中文选项且 javdb 有翻译时 title=中文、originaltitle=原始
                    d2 = dict(data)
                    d2["title_original"] = data.get("title") or m["dvdid"]
                    if cfg.get("use_chinese_title") and data.get("title_local"):
                        d2["title"] = data["title_local"]
                    scraper.generate_nfo(m["dvdid"], d2, nfo_path,
                                         poster_name=os.path.basename(poster_path),
                                         fanart_name=os.path.basename(fanart_path))
                    # fanart = 横版主封面；poster = 预览竖图，没有则取横图右半边（DVD 盒正面）
                    try:
                        fanart_ok, poster_ok = s.download_images(data, fanart_path, poster_path)
                        if not fanart_ok:
                            _add_log("  ! 背景图下载失败（不影响 NFO）")
                        elif not poster_ok:
                            _add_log("  ! 海报获取失败（不影响 NFO）")
                    except Exception as ce:
                        _add_log("  ! 封面下载失败（不影响 NFO）: {}".format(ce))
                    store.set_movie(m["file"], m["dvdid"], "success")
                    if _ensure_folder_poster(m["file"]):
                        _add_log("  ✓ 已生成文件夹海报: poster.jpg")
                    _add_log("  ✓ 已生成元数据: {}".format(os.path.basename(nfo_path)))
                    ok += 1
                except Exception as e:
                    store.set_movie(m["file"], m["dvdid"], "failed", str(e))
                    _add_log("  ✗ 失败: {}".format(e))
                    fail += 1

        _state["result"] = {"ok": True, "success": ok, "fail": fail,
                            "skip": len(movies) - len(targets), "total": len(movies)}
        _add_log("完成：本次成功 {}，失败 {}（另跳过已刮削 {}）".format(
            ok, fail, len(movies) - len(targets)))
    except Exception as e:
        log.exception("刮削任务异常")
        _add_log("刮削任务异常: {}".format(e))
        _state["result"] = {"ok": False, "msg": str(e)}
    finally:
        _state["running"] = False
        _state["progress"] = {"current": 0, "total": 0}

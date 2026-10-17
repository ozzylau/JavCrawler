"""javcrawler FastAPI 应用：WebUI + 多目录就地刮削 + 定时调度 + 历史日志"""
import datetime
import logging
import os
import re
import shutil
import threading
import time
import urllib.parse

import requests
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

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


# ---------- 实验性：反爬验证检查（内嵌浏览器代理） ----------
# 用途：采集源（javbus/javdb）触发反爬验证时，用户可在 WebUI 内嵌页面里人工完成
# 验证码/答题，本代理会捕获验证后发放的 cookie，一键保存并应用到刮削器。
# 说明：实验性功能——站点资源多为绝对路径，内嵌页面样式可能不完整，能过验证即可。

INSPECT_TARGETS = {"javbus": "https://www.javbus.com", "javdb": "https://javdb.com"}
_inspect_sessions = {}
for _name, _base in INSPECT_TARGETS.items():
    _s = requests.Session()
    _s.headers.update({
        "User-Agent": scraper.UA,
        "Accept-Language": "zh-CN,zh;q=0.9,zh-TW;q=0.8,ja;q=0.5,en;q=0.6",
    })
    _inspect_sessions[_name] = _s
# 代理域名→对应的检查代理前缀，用于重写 HTML 里的跨域绝对链接（实验性，尽力而为）
_INSPECT_ALIAS = {
    "javbus": {"https://www.javbus.com": "/api/inspect/javbus", "https://javbus.com": "/api/inspect/javbus"},
    "javdb": {"https://javdb.com": "/api/inspect/javdb", "https://www.javdb.com": "/api/inspect/javdb"},
}
_HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
                "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length"}


def _inspect_sessions_dict():
    return _inspect_sessions


def _rewrite_absolute_urls(html, engine):
    """把 HTML 里指向采集源本站的绝对 URL 改写为代理路径（仅限 href/src/action 属性开头处）。"""
    for domain, prefix in _INSPECT_ALIAS.get(engine, {}).items():
        html = html.replace(f'"{domain}/', f'"{prefix}/')
        html = html.replace(f"'{domain}/", f"'{prefix}/")
    return html


def _rewrite_relative_urls(html, engine):
    """把以 / 开头的根相对路径（img src、a href 等）改写为检查代理路径。

    否则浏览器会把 /pics/thumb/x.jpg 这类路径解析到本服务
    （如 http://10.20.0.228:8300/pics/thumb/x.jpg），导致图片裂图。
    """
    prefix = f"/api/inspect/{engine}"
    html = re.sub(
        r'(\s(?:src|href|action|poster)\s*=\s*)(["\'])/(?!/)(?!api/inspect/)',
        r"\1\2" + prefix + "/",
        html)
    # 协议相对地址（//www.javbus.com/...）
    for domain, p in _INSPECT_ALIAS.get(engine, {}).items():
        host = domain.split("://", 1)[1]
        html = html.replace(f'"//{host}/', f'"{p}/')
        html = html.replace(f"'//{host}/", f"'{p}/")
    # 内联 CSS url(/...)
    html = html.replace("url(/", f"url({prefix}/")
    return html


@app.api_route("/api/inspect/{engine}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS"])
async def inspect_proxy(engine: str, path: str, request: Request):
    if engine not in INSPECT_TARGETS:
        return JSONResponse({"error": "engine 必须是 javbus 或 javdb"}, status_code=404)
    sess = _inspect_sessions[engine]
    base = INSPECT_TARGETS[engine]
    qs = request.url.query
    url = f"{base}/{path}" + (f"?{qs}" if qs else "")
    # cookie 由 session jar 管理（捕获 Set-Cookie），不透传浏览器 cookie。
    # referer/origin 不能透传：浏览器带的是本服务地址，javbus 的图片 CDN 校验
    # Referer 必须为本站，否则图片 403 裂图。统一补成本站 Referer。
    fwd_headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in _HOP_HEADERS
                   and k.lower() not in ("cookie", "accept-encoding", "referer", "origin")}
    fwd_headers["Referer"] = base + "/"
    if request.method in ("POST", "PUT"):
        fwd_headers["Origin"] = base
    body = await request.body() if request.method in ("POST", "PUT") else None
    # 与刮削器一致：使用引擎配置的代理（服务器直连站点往往不通）
    cfg = cfgmod.load_config()
    proxy_url = (cfg.get("proxy_javbus") or "").strip() if engine == "javbus" else (cfg.get("proxy_javdb") or "").strip()
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    try:
        from starlette.concurrency import run_in_threadpool

        def _do():
            return sess.request(request.method, url, headers=fwd_headers, data=body,
                                proxies=proxies, allow_redirects=False, timeout=15)

        try:
            resp = await run_in_threadpool(_do)
        except Exception:
            # 代理链路偶发 SSL 中断（SSLEOFError 等），重试一次
            resp = await run_in_threadpool(_do)
    except Exception as e:
        # 返回可读的 HTML 错误页（iframe 内可见），而非裸 JSON
        msg = f"通过{'代理 ' + proxy_url if proxy_url else '直连'}请求 {base} 失败：{e}"
        html = (f'<html><body style="font-family:sans-serif;padding:24px;color:#c0392b">'
                f'<h3>检查代理请求失败</h3><p style="word-break:break-all">{msg}</p>'
                f'<p style="color:#555">请检查「数据源设置」里该引擎的代理配置是否正确，保存后重试。</p></body></html>')
        return HTMLResponse(html, status_code=502)
    out_headers = {}
    for k, v in resp.headers.items():
        if k.lower() in ("content-encoding", "content-length", "transfer-encoding", "connection",
                         "x-frame-options", "content-security-policy",
                         "strict-transport-security", "set-cookie"):
            continue
        out_headers[k] = v
    if resp.headers.get("location"):
        loc = resp.headers["location"]
        parsed = urllib.parse.urlparse(loc)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin in _INSPECT_ALIAS.get(engine, {}):
            loc = _INSPECT_ALIAS[engine][origin] + parsed.path + (f"?{parsed.query}" if parsed.query else "")
        elif loc.startswith("/"):
            loc = f"/api/inspect/{engine}{loc}"
        out_headers["Location"] = loc
    content = resp.content
    ctype = resp.headers.get("content-type", "")
    if "text/html" in ctype:
        try:
            html = content.decode(resp.encoding or "utf-8", "ignore")
            html = _rewrite_absolute_urls(html, engine)
            html = _rewrite_relative_urls(html, engine)
            base_tag = f'<base href="/api/inspect/{engine}/">'
            low = html.lower()
            if "<head" in low:
                pos = low.index("<head") + low[low.index("<head"):].index(">") + 1
                html = html[:pos] + base_tag + html[pos:]
            else:
                html = base_tag + html
            content = html.encode("utf-8")
        except Exception:
            pass
    return Response(content, status_code=resp.status_code, headers=out_headers)


@app.get("/api/inspect-cookies/{engine}")
def inspect_cookies(engine: str):
    """查看检查代理当前捕获到的 cookie（未保存状态）"""
    if engine not in INSPECT_TARGETS:
        return JSONResponse({"error": "engine 必须是 javbus 或 javdb"}, status_code=404)
    jar = _inspect_sessions[engine].cookies
    cookie_str = "; ".join(f"{c.name}={c.value}" for c in jar)
    return {"engine": engine, "cookie": cookie_str, "count": len(jar)}


@app.post("/api/inspect-save/{engine}")
def inspect_save_cookies(engine: str):
    """把检查代理捕获的 cookie 保存到配置并应用到刮削器"""
    if engine not in INSPECT_TARGETS:
        return JSONResponse({"error": "engine 必须是 javbus 或 javdb"}, status_code=404)
    jar = _inspect_sessions[engine].cookies
    if not len(jar):
        return {"ok": False, "msg": "当前没有捕获到任何 cookie，请先在检查页面里完成验证"}
    cookie_str = "; ".join(f"{c.name}={c.value}" for c in jar)
    field = "javbus_cookie" if engine == "javbus" else "javdb_cookie"
    cfgmod.update_config(**{field: cookie_str})
    _add_log("已保存 {} 的验证 cookie（{} 项），刮削器将在下次任务中生效".format(engine, len(jar)))
    return {"ok": True, "field": field, "count": len(jar)}


def _confirm_javdb_age():
    """javdb 对新会话渲染「年滿 18 歲」服务端确认 modal（/over18?respond=1&rurl=...）。
    自动替用户完成一次确认（作用于检查会话），之后内嵌浏览器不会再被年龄弹窗挡住，
    也不会因点击确认链接跳错页面。"""
    sess = _inspect_sessions["javdb"]
    cfg = cfgmod.load_config()
    proxy_url = (cfg.get("proxy_javdb") or "").strip()
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    try:
        r = sess.get("https://javdb.com/", proxies=proxies, timeout=15)
        if "over18-modal" not in r.text:
            return
        m = re.search(r'href="(/over18\?respond=[^"]+)"', r.text)
        if m:
            sess.get("https://javdb.com" + m.group(1).replace("&amp;", "&"),
                     proxies=proxies, timeout=15)
            log.info("javdb 年龄确认已自动完成")
    except Exception as e:
        log.warning("javdb 年龄确认自动处理失败(可在内嵌页面手动点击): %s", e)


@app.get("/api/inspect-open/{engine}/{keyword}")
def inspect_open(engine: str, keyword: str):
    """实验室检查器入口：解析番号详情页并 302 到检查代理路径，
    让内嵌浏览器直接进入采集流程实际访问的详情页（javbus 详情页 = /番号，
    javdb 详情页地址需先搜索获得）。失败回退站点首页。"""
    if engine not in INSPECT_TARGETS:
        return JSONResponse({"error": "engine 必须是 javbus 或 javdb"}, status_code=404)
    kw = keyword.strip()
    if not kw:
        return RedirectResponse(f"/api/inspect/{engine}/", status_code=302)
    if engine == "javbus":
        return RedirectResponse(f"/api/inspect/javbus/{urllib.parse.quote(kw)}", status_code=302)
    _confirm_javdb_age()
    cookie_str = "; ".join(f"{c.name}={c.value}" for c in _inspect_sessions[engine].cookies)
    cfg = cfgmod.load_config()
    proxy_url = (cfg.get("proxy_javdb") or "").strip()
    url = None
    try:
        sc = scraper.JavdbScraper(cookie=cookie_str, proxy=proxy_url, timeout=15)
        url, _, _ = sc.search(kw)
    except Exception:
        url = None
    if url:
        path = url if url.startswith("/") else urllib.parse.urlparse(url).path
        return RedirectResponse(f"/api/inspect/javdb{path}", status_code=302)
    return RedirectResponse("/api/inspect/javdb/", status_code=302)


@app.post("/api/inspect-test/{engine}")
async def inspect_test(engine: str, request: Request):
    """实验室抓取验证：用检查会话（含捕获的 cookie）+ 引擎配置的代理试抓一个番号，
    返回整理后的 NFO 元数据预览。仅验证抓取链路，不写盘、不落库。"""
    if engine not in INSPECT_TARGETS:
        return JSONResponse({"error": "engine 必须是 javbus 或 javdb"}, status_code=404)
    try:
        payload = await request.json() or {}
    except Exception:
        payload = {}
    keyword = str(payload.get("keyword") or "").strip()
    if not keyword:
        return {"ok": False, "msg": "请输入要验证的番号，例如 SSIS-129"}
    cookie_str = "; ".join(f"{c.name}={c.value}" for c in _inspect_sessions[engine].cookies)
    cfg = cfgmod.load_config()
    proxy_url = (cfg.get("proxy_javbus") or "").strip() if engine == "javbus" else (cfg.get("proxy_javdb") or "").strip()

    def _run():
        if engine == "javbus":
            sc = scraper.JavBusScraper(cookie=cookie_str, proxy=proxy_url, timeout=20)
        else:
            sc = scraper.JavdbScraper(cookie=cookie_str, proxy=proxy_url, timeout=20)
        return sc.scrape(keyword)

    from starlette.concurrency import run_in_threadpool
    try:
        data = await run_in_threadpool(_run)
    except Exception as e:
        return {"ok": False, "msg": "抓取失败：{}（网络：{}）".format(e, proxy_url or "直连")}
    if not data:
        return {"ok": False, "msg": "{} 未搜索到 {}（若反复失败，可能被反爬拦截，请先在左侧页面完成验证）".format(engine, keyword)}
    result = {
        "dvdid": data.get("dvdid") or keyword.upper(),
        "title": data.get("title") or "",
        "title_local": data.get("title_local") or "",
        "date": data.get("date") or "",
        "duration": data.get("duration") or "",
        "director": data.get("director") or "",
        "maker": data.get("maker") or "",
        "actors": data.get("actors") or [],
        "genres": data.get("genres") or [],
        "plot": (data.get("plot") or "")[:300],
        "poster": data.get("poster_url") or data.get("poster") or "",
        "fanart": data.get("fanart") or "",
        "url": data.get("url") or "",
    }
    return {"ok": True, "engine": engine, "data": result, "proxy": proxy_url or "直连"}


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
    """按配置创建主/备刮削引擎，返回 (primary, backup, primary_name, backup_name)。
    代理按引擎独立配置：留空=直连，填写=只走代理。"""
    engine = (cfg.get("engine") or "javbus").strip().lower()
    cookie_jb = cfg.get("javbus_cookie", "")
    cookie_db = cfg.get("javdb_cookie", "")
    px_jb = cfg.get("proxy_javbus", "")
    px_javdb = cfg.get("proxy_javdb", "")
    fb = bool(cfg.get("engine_fallback", True))
    if engine == "javdb":
        primary = scraper.JavdbScraper(cookie=cookie_db, proxy=px_javdb)
        backup = scraper.JavBusScraper(cookie=cookie_jb, proxy=px_jb) if fb else None
        return primary, backup, "javdb", "javbus"
    primary = scraper.JavBusScraper(cookie=cookie_jb, proxy=px_jb)
    backup = scraper.JavdbScraper(cookie=cookie_db, proxy=px_javdb) if fb else None
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
            zh_ready = None  # javdb 中文标题补抓引擎（懒创建；None=未初始化）
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
                    # 标题选择：默认用原始标题；开启中文选项且引擎有翻译时 title=中文、originaltitle=原始
                    d2 = dict(data)
                    d2["title_original"] = data.get("title") or m["dvdid"]
                    if cfg.get("use_chinese_title") and data.get("title_local"):
                        d2["title"] = data["title_local"]
                    # javbus 主引擎 + 勾选中文标题：javbus 只有原始标题，向 javdb 单独补抓标题
                    # （只取标题字段，不依赖“备用引擎补抓”开关；javdb 请求失败不影响本次刮削）
                    if cfg.get("use_chinese_title") and data.get("engine") == "javbus":
                        if zh_ready is None:
                            if b_name == "javdb" and backup is not None:
                                zh_ready = backup
                            else:
                                zh_ready = scraper.JavdbScraper(
                                    cookie=cfg.get("javdb_cookie", ""),
                                    proxy=cfg.get("proxy_javdb", ""))
                        try:
                            zd = zh_ready.scrape(m["dvdid"])
                            if zd and zd.get("title_local") and zd["title_local"] != d2["title_original"]:
                                d2["title"] = zd["title_local"]
                                _add_log("  · 已从 javdb 补抓中文标题")
                        except Exception as te:
                            _add_log("  ! javdb 中文标题补抓失败: {}".format(te))
                    # 演员头像：默认 NFO 直接用采集源 URL；勾选"缓存到本地"则下载到视频同目录 .actors/（演员名.jpg），
                    # NFO 的 thumb 改用本地相对路径 .actors/文件名（需在生成 NFO 前完成）
                    if cfg.get("actor_thumbs_local"):
                        try:
                            local_map = scraper.download_actor_thumbs(s, data, m["file"])
                            if local_map:
                                d2["actor_thumbs"] = local_map
                                _add_log("  · 已缓存 {} 张演员头像到 .actors/ 目录".format(len(local_map)))
                        except Exception as ae:
                            _add_log("  ! 演员头像缓存失败（不影响 NFO）: {}".format(ae))
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

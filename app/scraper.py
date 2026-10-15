"""javcrawler 刮削引擎：使用 javdb 数据刮削 JAV 影片，生成标准 NFO + 封面"""
import logging
import os
import re
import shutil
import struct
import xml.etree.ElementTree as ET

import requests
from lxml import html as lxml_html

log = logging.getLogger("javcrawler.scraper")

# JAV 番号常见模式：字母组(2-9位)-数字(2-6位)，如 MIZD-490 / migd-745；FC2 长番号如 FC2-PPV-1234567
JAV_PATTERN = re.compile(r"\b(fc2-?(?:ppv-?)?\d{5,8}|[a-z]{2,9}[-_ ]\d{2,7}|[a-z]{2,9}\d{2,6})\b", re.IGNORECASE)
# 忽略的目录/文件前缀
IGNORE_PREFIX = ("._", "#", ".")
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".m4v", ".ts", ".wmv", ".flv", ".iso", ".rmvb", ".webm", ".mpeg", ".mpg", ".strm"}

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def guess_dvdid(filename):
    """从文件名提取番号，返回标准大写形式，如 MIZD-490"""
    m = JAV_PATTERN.search(filename)
    if not m:
        return None
    raw = m.group(1)
    raw = raw.replace("_", "-").replace(" ", "-")
    low = raw.lower()
    if low.startswith("fc2"):
        fm = re.match(r"fc2-?(?:ppv-?)?(\d+)$", low)
        if fm:
            return "FC2-PPV-" + fm.group(1)
    raw = re.sub(r"([a-z])(\d)", r"\1-\2", raw, flags=re.IGNORECASE)
    return raw.upper()


def list_movies(root_dir):
    """扫描目录下所有视频文件（跳过已整理的 #整理完成 和隐藏文件）"""
    movies = []
    for dirpath, dirnames, filenames in os.walk(root_dir):
        dirnames[:] = [d for d in dirnames if not d.startswith(IGNORE_PREFIX)]
        for fn in filenames:
            if fn.startswith(IGNORE_PREFIX):
                continue
            ext = os.path.splitext(fn)[1].lower()
            if ext not in VIDEO_EXTS:
                continue
            dvdid = guess_dvdid(fn)
            if dvdid:
                movies.append({"file": os.path.join(dirpath, fn), "dvdid": dvdid})
    return movies


class JavdbScraper:
    """javdb 刮削器：带登录 cookie + 可选 http 代理"""

    def __init__(self, cookie="", proxy="", timeout=30):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": UA,
            "Accept-Language": "zh-CN,zh;q=0.9,zh-TW;q=0.8,en-US;q=0.7,en;q=0.6,ja;q=0.5",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        if cookie:
            self.session.headers["Cookie"] = cookie.strip()
        self.proxy = proxy.strip() or None
        self.timeout = timeout
        self.base = "https://javdb.com"

    def _proxies(self):
        if self.proxy:
            return {"http": self.proxy, "https": self.proxy}
        return None

    def _get(self, url):
        r = self.session.get(url, proxies=self._proxies(), timeout=self.timeout)
        if r.status_code in (403, 503):
            raise RuntimeError(f"javdb 返回 {r.status_code}，被反爬拦截，请检查 cookie 是否有效")
        r.raise_for_status()
        if len(r.content) < 500:
            raise RuntimeError(f"javdb 返回空内容({r.status_code})，可能被拦截，请检查 cookie")
        return r

    def search(self, dvdid):
        """搜索番号，返回 (详情url, 标题, 搜索页竖图缩略url)。找不到返回 (None, None, None)"""
        r = self._get(f"{self.base}/search?q={dvdid}")
        tree = lxml_html.fromstring(r.content.decode("utf-8", "ignore"))
        dvdid_l = dvdid.lower()
        items = []
        for a in tree.xpath("//a[@class='box']"):
            ids = a.xpath("div[@class='video-title']/strong/text()")
            imgs = a.xpath("div[@class='cover']//img/@src") or a.xpath(".//img/@src")
            items.append({
                "id": ids[0].strip().lower() if ids else "",
                "url": a.get("href", ""),
                "title": a.get("title", ""),
                "thumb": imgs[0] if imgs else "",
            })
        pick = next((it for it in items if it["id"] == dvdid_l), None)
        if not pick:
            pick = next((it for it in items if dvdid_l in it["id"] or it["id"] in dvdid_l), None)
        if pick:
            return pick["url"], pick["title"], pick["thumb"]
        return None, None, None

    def detail(self, url):
        """抓取影片详情"""
        full = url if url.startswith("http") else self.base + url
        r = self._get(full)
        tree = lxml_html.fromstring(r.content.decode("utf-8", "ignore"))
        data = {"url": full}

        # 标题：优先原始标题（javdb 的中文是 AI 翻译，质量差），无翻译时退回主标题
        origin = tree.xpath("//h2/span[contains(@class,'origin-title')]/text()")
        if origin and origin[0].strip():
            data["title"] = origin[0].strip()
        else:
            t = tree.xpath("//h2/strong[@class='current-title']/text()")
            data["title"] = t[0].strip() if t else ""
        c = tree.xpath("//img[@class='video-cover']/@src")
        data["fanart"] = c[0] if c else ""  # 主封面（横版，DVD 盒展开图）→ fanart
        # 预览大图列表（_l_ 原图；其中可能是竖版海报或横版截图）
        data["previews"] = tree.xpath(
            "//div[contains(@class,'tile-images preview-images')]//a[contains(@class,'tile-item')]/@href")

        atag = tree.xpath("//strong[text()='演員:']/../span")
        if atag:
            names = atag[0].xpath("a/text()")
            genders = atag[0].xpath("strong/text()")
            act = [n for i, n in enumerate(names) if i < len(genders) and genders[i] == "♀"]
            data["actors"] = act if act else list(names)
        else:
            data["actors"] = []

        p = tree.xpath("//nav[contains(@class,'movie-panel-info')]//div[contains(@class,'message-body')]/p//text()")
        data["plot"] = "".join(p).strip() if p else ""

        date = tree.xpath("//strong[text()='日期:']/following-sibling::span[1]//text()")
        data["date"] = "".join(date).strip() if date else ""
        dur = tree.xpath("//strong[text()='時長:']/following-sibling::span[1]//text()")
        data["duration"] = re.sub(r"\D", "", "".join(dur)) if dur else ""
        maker = tree.xpath("//strong[text()='片商:']/following-sibling::span[1]//text()")
        data["maker"] = "".join(maker).strip() if maker else ""
        director = tree.xpath("//strong[text()='導演:']/following-sibling::span[1]//text()")
        data["director"] = "".join(director).strip() if director else ""
        genres = tree.xpath("//strong[text()='類別:']/../span/a/text()")
        data["genres"] = [g.strip() for g in genres]
        dvd = tree.xpath("//strong[text()='番號:']/following-sibling::span[1]//text()")
        data["dvdid"] = "".join(dvd).strip() if dvd else ""
        sc = tree.xpath("//span[@class='score-stars']")
        data["score"] = ""
        if sc:
            m = re.search(r"([\d.]+)分", sc[0].tail or "")
            data["score"] = "{:.2f}".format(float(m.group(1)) * 2) if m else ""
        return data

    def scrape(self, dvdid):
        """完整刮削一个番号，返回详情 dict（含搜索页竖图 poster）或 None"""
        url, _, thumb = self.search(dvdid)
        if not url:
            log.warning("%s: javdb 未找到", dvdid)
            return None
        data = self.detail(url)
        if data:
            data["poster"] = thumb  # 搜索页竖图 → 用作 poster
        return data

    def download_cover(self, url, dest):
        """下载封面到本地"""
        if not url:
            return False
        r = self.session.get(url, proxies=self._proxies(), timeout=self.timeout)
        r.raise_for_status()
        with open(dest, "wb") as f:
            f.write(r.content)
        return True

    def _get_bytes(self, url):
        r = self.session.get(url, proxies=self._proxies(), timeout=self.timeout)
        r.raise_for_status()
        return r.content

    def download_images(self, data, fanart_path, poster_path):
        """下载横版 fanart + 竖版 poster，返回 (fanart_ok, poster_ok)。

        poster 策略：优先取详情页预览图里的竖图（前 8 张内逐一检查宽高）；
        找不到竖图时，从横版主封面取右半边（javdb 横图是 DVD 盒正反面展开，右半为正面）。
        """
        fanart_ok = poster_ok = False
        fanart_url = data.get("fanart") or ""
        if fanart_url:
            try:
                self.download_cover(fanart_url, fanart_path)
                fanart_ok = True
            except Exception as e:
                log.warning("fanart 下载失败: %s", e)
        for u in (data.get("previews") or [])[:8]:
            try:
                # 注意：javdb 的 _s_ 小图固定裁成 120x90 横图，不能反映原图宽高，必须检测 _l_ 大图
                b = self._get_bytes(u)
                w, h = jpg_dimensions(b)
                if h > w:
                    with open(poster_path, "wb") as f:
                        f.write(b)
                    poster_ok = True
                    break
            except Exception:
                continue
        if not poster_ok:
            if fanart_ok:
                crop_right_half(fanart_path, poster_path)
                poster_ok = True
            elif data.get("poster"):
                try:
                    self.download_cover(data["poster"], poster_path)
                    poster_ok = True
                except Exception as e:
                    log.warning("poster 兜底下载失败: %s", e)
        return fanart_ok, poster_ok


def nfo_path_for(video_file):
    """与视频同名的 NFO：/path/Movie.mp4 -> /path/Movie.nfo（Emby/Jellyfin/Kodi/Infuse 均支持）"""
    return os.path.splitext(video_file)[0] + ".nfo"


def poster_path_for(video_file):
    """与视频同名的竖版海报：/path/Movie.mp4 -> /path/Movie-poster.jpg"""
    return os.path.splitext(video_file)[0] + "-poster.jpg"


def fanart_path_for(video_file):
    """与视频同名的横版背景图：/path/Movie.mp4 -> /path/Movie-fanart.jpg"""
    return os.path.splitext(video_file)[0] + "-fanart.jpg"


def is_scraped(video_file):
    """判断影片是否已刮削：存在与视频完全同名的 .nfo"""
    return os.path.exists(nfo_path_for(video_file))


def jpg_dimensions(data):
    """解析 JPG 二进制宽高（纯标准库，用于快速判断横竖）"""
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
        i += 2 + seg_len
    return 0, 0


def crop_right_half(fanart_path, poster_path):
    """从横版封面取右半边作为竖版海报。
    javdb 横图是 DVD 盒正反面展开图，右半边为正面封面。"""
    try:
        from PIL import Image
    except ImportError:
        shutil.copyfile(fanart_path, poster_path)
        return poster_path
    img = Image.open(fanart_path).convert("RGB")
    w, h = img.size
    img = img.crop((w // 2, 0, w, h))
    img.save(poster_path, "JPEG", quality=92)
    return poster_path


def safe_name(s):
    """清理文件名非法字符"""
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', " ", s)
    return re.sub(r"\s+", " ", s).strip()


def generate_nfo(dvdid, data, out_path, poster_name="poster.jpg", fanart_name="fanart.jpg"):
    """生成标准 NFO（Kodi/Jellyfin/Emby 兼容），与视频文件同名同目录"""
    year = data.get("date", "")[:4] if data.get("date") else ""
    movie = ET.Element("movie")
    title = data.get("title") or dvdid
    ET.SubElement(movie, "title").text = f"{dvdid} {safe_name(title)}".strip()
    ET.SubElement(movie, "originaltitle").text = safe_name(title)
    ET.SubElement(movie, "sorttitle").text = dvdid
    ET.SubElement(movie, "uniqueid", {"type": "javdb", "default": "true"}).text = dvdid
    if year:
        ET.SubElement(movie, "year").text = year
    if data.get("date"):
        ET.SubElement(movie, "premiered").text = data["date"]
        ET.SubElement(movie, "releasedate").text = data["date"]
    if data.get("plot"):
        ET.SubElement(movie, "plot").text = data["plot"]
        ET.SubElement(movie, "outline").text = data["plot"]
    for g in data.get("genres", []):
        ET.SubElement(movie, "genre").text = g
    for a in data.get("actors", []):
        actor = ET.SubElement(movie, "actor")
        ET.SubElement(actor, "name").text = a
    if data.get("maker"):
        ET.SubElement(movie, "studio").text = data["maker"]
    if data.get("director"):
        ET.SubElement(movie, "director").text = data["director"]
    if data.get("duration"):
        ET.SubElement(movie, "runtime").text = data["duration"]
    if data.get("score"):
        ET.SubElement(movie, "rating").text = data["score"]
    ET.SubElement(movie, "mpaa").text = "R18"
    art = ET.SubElement(movie, "art")
    ET.SubElement(art, "poster").text = poster_name
    ET.SubElement(art, "fanart").text = fanart_name
    # XML 声明 + 美化
    ET.indent(movie, space="  ")
    xmlstr = ET.tostring(movie, encoding="utf-8", xml_declaration=True).decode()
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(xmlstr + "\n")
    return out_path

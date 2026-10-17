"""javcrawler 配置管理：多目录 + 定时调度"""
import json
import os

CONFIG_PATH = os.environ.get("JAVCRAWLER_CONFIG", "/app/config.json")

DEFAULTS = {
    "input_directories": [],                  # 刮削目录列表（可多个）
    "javdb_cookie": "",                       # javdb cookie（可选，被反爬拦截时配置）
    "proxy_javbus": "",                       # JavBus 代理（留空=直连；填写后只走代理，不回退直连）
    "proxy_javdb": "",                        # JavDB 代理（留空=直连）
    "engine": "javbus",                       # 刮削引擎: javbus(默认) / javdb
    "engine_fallback": True,                  # 主引擎未找到番号时用另一引擎补抓
    "timezone": "Asia/Shanghai",              # 时区（定时任务与日志按此时区）
    "use_chinese_title": False,               # True: title 用 javdb 中文翻译, originaltitle 用原始标题
    "auto_schedule": {                        # 定时自动刮削
        "enabled": False,
        "mode": "daily",                      # daily=每天固定时间 / interval=每 N 小时
        "daily_time": "03:00",
        "interval_hours": 6,
    },
}


def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))  # deep copy
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            saved = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        saved = {}

    # 兼容旧版单目录配置
    if "input_directories" not in saved and saved.get("input_directory"):
        saved["input_directories"] = [saved["input_directory"]]

    for k, v in saved.items():
        if k in cfg:
            if isinstance(cfg[k], dict) and isinstance(v, dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    # 旧版单代理配置迁移：同时应用到两个引擎
    legacy = saved.get("proxy") or ""
    if legacy and not (cfg.get("proxy_javbus") or cfg.get("proxy_javdb")):
        cfg["proxy_javbus"] = legacy
        cfg["proxy_javdb"] = legacy
    # 目录去重、去空
    dirs = cfg.get("input_directories") or []
    seen, uniq = set(), []
    for d in dirs:
        d = str(d).strip()
        if d and d not in seen:
            seen.add(d)
            uniq.append(d)
    cfg["input_directories"] = uniq
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def update_config(**kwargs):
    cfg = load_config()
    for k, v in kwargs.items():
        if k in cfg:
            if isinstance(cfg[k], dict) and isinstance(v, dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    save_config(cfg)
    return cfg

#!/usr/bin/env python3
"""garmin-run-coach 共用工具：路径、密钥与令牌、带重试的 HTTP 请求。

只用 Python 标准库（3.8+）。任何函数都不会打印 API 密钥或访问令牌。

密钥查找顺序与佳明官方客户端 connect-personal-api-python 一致：
  1. 环境变量 GARMIN_PERSONAL_API_KEY
  2. --key-file 指定的文件
  3. 默认文件 ~/.config/garmin/connect-personal-api-key
"""
import base64
import hashlib
import http.client
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HOSTS = {
    "global": "https://personal.connectapi.garmin.com",
    "cn": "https://personal.connectapi.garmin.cn",
}
KEY_PAGES = {
    "global": "https://connect.garmin.com/app/settings/personalApiToken",
    "cn": "https://connect.garmin.cn/app/settings/personalApiToken",
}
TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"
KEY_ENV = "GARMIN_PERSONAL_API_KEY"
DEFAULT_KEY_FILE = Path.home() / ".config" / "garmin" / "connect-personal-api-key"
CONF_DIR = Path.home() / ".config" / "garmin-run-coach"
CONF_FILE = CONF_DIR / "config.json"
TOKEN_CACHE = CONF_DIR / "token.json"
USER_AGENT = "garmin-run-coach/1.4 (personal use)"
MIN_INTERVAL = 0.35  # 两次请求之间至少间隔的秒数，对佳明服务器客气一点

_last_call = [0.0]
_token_mem = {}


class GarminError(Exception):
    pass


# ---------------------------------------------------------------- 路径

def coach_home() -> Path:
    """所有数据、报告、课表的根目录。可用环境变量 GARMIN_COACH_HOME 改。"""
    p = Path(os.environ.get("GARMIN_COACH_HOME", Path.home() / "garmin-coach")).expanduser()
    ensure_private_dir(p)
    gi = p / ".gitignore"
    if not gi.exists():
        gi.write_text("*\n", encoding="utf-8")  # 防止误把私人数据提交到 git
    return p


def raw_dir() -> Path:
    p = coach_home() / "raw"
    ensure_private_dir(p)
    return p


def ensure_private_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(p, 0o700)
    except OSError:
        pass


def write_private(path: Path, text: str):
    ensure_private_dir(path.parent)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def save_json(path: Path, obj):
    write_private(Path(path), json.dumps(obj, ensure_ascii=False, indent=1))


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def load_config() -> dict:
    return load_json(CONF_FILE, {}) or {}


def save_config(cfg: dict):
    save_json(CONF_FILE, cfg)


# ---------------------------------------------------------------- 密钥

def resolve_key(key_file=None):
    """返回 (key, 来源说明)。找不到时返回 (None, 原因)。"""
    v = os.environ.get(KEY_ENV, "").strip()
    if v:
        return v, "环境变量 " + KEY_ENV
    path = Path(key_file).expanduser() if key_file else DEFAULT_KEY_FILE
    if path.exists():
        try:
            v = path.read_text(encoding="utf-8").strip()
        except OSError as e:
            return None, "读不了密钥文件 %s：%s" % (path, e)
        if v:
            try:
                mode = path.stat().st_mode & 0o077
                if mode and os.name == "posix":
                    os.chmod(path, 0o600)
            except OSError:
                pass
            return v, "文件 %s" % path
        return None, "密钥文件是空的：%s" % path
    return None, "没找到密钥（环境变量 %s 或文件 %s）" % (KEY_ENV, path)


def save_key(key: str):
    ensure_private_dir(DEFAULT_KEY_FILE.parent)
    write_private(DEFAULT_KEY_FILE, key.strip() + "\n")


def key_fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:16]


# ---------------------------------------------------------------- 令牌

def jwt_claims(token: str) -> dict:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part.encode()))
    except Exception:
        return {}


def _parse_token_body(body: str):
    s = body.strip()
    if s.startswith("{"):
        try:
            j = json.loads(s)
        except ValueError:
            return None
        for k in ("access_token", "accessToken", "token", "jwt"):
            if isinstance(j.get(k), str):
                return j[k]
        return None
    s = s.strip('"')
    return s if s.count(".") == 2 else None


def _post_token_urllib(host: str, key: str):
    req = urllib.request.Request(host + TOKEN_PATH, data=b"", method="POST")
    req.add_header("Authorization", "ApiKey " + key)
    req.add_header("User-Agent", USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, str(e)


def _post_token_curl(host: str, key: str):
    """备用通道：把参数从标准输入传给 curl，密钥不会出现在进程列表里。"""
    curl = shutil.which("curl")
    if not curl:
        return 0, "no curl"
    cfg = "\n".join([
        'url = "%s%s"' % (host, TOKEN_PATH),
        'request = "POST"',
        'header = "Authorization: ApiKey %s"' % key,
        'header = "Accept:"',
        'header = "User-Agent: %s"' % USER_AGENT,
        "silent", "show-error", "max-time = 45",
        'write-out = "\\n%{http_code}"',
    ]) + "\n"
    try:
        p = subprocess.run([curl, "-K", "-"], input=cfg, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 0, str(e)
    out = p.stdout.rstrip()
    body, _, code = out.rpartition("\n")
    try:
        return int(code), body
    except ValueError:
        return 0, out


def exchange_token(host: str, key: str, tries: int = 4):
    """用密钥换 24 小时访问令牌。返回 (token 或 None, http 状态码, 说明)。
    先用 urllib；不成功时马上用 curl 再试一次（2026-11 曾遇到 urllib 返回 520 而 curl 正常）。"""
    last = (0, "")
    for i in range(tries):
        results = [_post_token_urllib(host, key)]
        if results[0][0] != 200:
            c = _post_token_curl(host, key)
            if c[0]:
                results.append(c)
        for status, body in results:
            if status == 200:
                tok = _parse_token_body(body)
                if tok:
                    return tok, 200, "ok"
                last = (200, "返回内容不像令牌")
        status = max(r[0] for r in results)
        last = (status, results[-1][1][:200]) if status != 200 else last
        if all(r[0] in (400, 401, 403, 404) for r in results):
            break  # 密钥无效、过期或地区不对，重试没用
        time.sleep(min(2 ** i * 2, 15))
    return None, last[0], last[1]


def detect_region(key: str):
    """先试配置里的地区，再依次试国际区和中国区。返回 (region, token, exp)。"""
    cfg = load_config()
    order = [cfg.get("region")] if cfg.get("region") in HOSTS else []
    order += [r for r in ("global", "cn") if r not in order]
    errors = []
    for region in order:
        tok, status, msg = exchange_token(HOSTS[region], key)
        if tok:
            return region, tok, jwt_claims(tok).get("exp")
        errors.append("%s: HTTP %s" % (region, status))
    raise GarminError("两个地区都换不到令牌（%s）。请检查密钥是否复制完整、是否已过期或被删除。" % "；".join(errors))


def get_token(key_file=None, force=False):
    """返回 (host, token)。优先用缓存（同一把密钥、剩余时间超过 1 小时）。"""
    key, src = resolve_key(key_file)
    if not key:
        raise GarminError(src + "。请先运行：python3 scripts/garmin_setup.py setup")
    fp = key_fingerprint(key)
    now = time.time()
    if not force:
        if _token_mem.get("fp") == fp and _token_mem.get("exp", 0) - now > 3600:
            return _token_mem["host"], _token_mem["token"]
        c = load_json(TOKEN_CACHE, {}) or {}
        if c.get("fp") == fp and c.get("exp", 0) - now > 3600 and c.get("host") in HOSTS.values():
            _token_mem.update(c)
            return c["host"], c["token"]
    region, tok, exp = detect_region(key)
    exp = exp or (now + 23 * 3600)
    cfg = load_config()
    if cfg.get("region") != region:
        cfg["region"] = region
        save_config(cfg)
    rec = {"fp": fp, "host": HOSTS[region], "token": tok, "exp": exp, "region": region}
    _token_mem.clear()
    _token_mem.update(rec)
    save_json(TOKEN_CACHE, rec)
    return rec["host"], tok


# ---------------------------------------------------------------- 请求

def api_get(path: str, params=None, raw=False, key_file=None, tries=5, timeout=90):
    """GET 一个个人 API 路径。成功返回 JSON（raw=True 时返回 bytes）；404 返回 None。"""
    host, tok = get_token(key_file)
    qs = ("?" + urllib.parse.urlencode(params)) if params else ""
    refreshed = False
    for i in range(tries):
        wait = MIN_INTERVAL - (time.time() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()
        req = urllib.request.Request(host + path + qs)
        req.add_header("Authorization", "Bearer " + tok)
        req.add_header("User-Agent", USER_AGENT)
        if not raw:
            req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
                if raw:
                    return body
                if not body.strip():
                    return None
                return json.loads(body.decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            code = e.code
            if code == 404:
                return None
            if code == 401 and not refreshed:
                host, tok = get_token(key_file, force=True)
                refreshed = True
                continue
            if code in (400, 403):
                raise GarminError("HTTP %s：%s" % (code, path))
            if code == 429:
                time.sleep(30 * (i + 1))
                continue
            time.sleep(min(2 ** i * 2, 30))  # 5xx、520 等：退避后重试
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, http.client.HTTPException):
            time.sleep(min(2 ** i * 2, 30))
    raise GarminError("多次重试仍失败：" + path)


# ---------------------------------------------------------------- 日期

WINDOW_EPOCH = date(2000, 1, 3)  # 一个周一；固定窗口边界，避免重复下载


def window_start(d: date, days=28) -> date:
    n = (d - WINDOW_EPOCH).days // days
    return WINDOW_EPOCH + timedelta(days=n * days)


def windows(start: date, end: date, days=28):
    s = window_start(start, days)
    while s <= end:
        e = s + timedelta(days=days - 1)
        yield s, e
        s = e + timedelta(days=1)


def parse_date(s) -> date:
    if isinstance(s, date) and not isinstance(s, datetime):
        return s
    return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()


def fmt_dur(sec) -> str:
    if sec is None:
        return "-"
    sec = int(round(sec))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return "%d:%02d:%02d" % (h, m, s) if h else "%d:%02d" % (m, s)


def fmt_pace(speed_ms) -> str:
    """m/s → 分:秒/km"""
    if not speed_ms or speed_ms <= 0:
        return "-"
    s = 1000.0 / speed_ms
    m, sec = divmod(int(round(s)), 60)
    return "%d:%02d" % (m, sec)


def parse_pace(p) -> float:
    """'4:30' → 秒/km"""
    m, s = str(p).strip().split(":")
    return int(m) * 60 + float(s)


def parse_time(t) -> float:
    """'3:20:00' 或 '44:00' → 秒"""
    parts = [float(x) for x in str(t).strip().split(":")]
    sec = 0.0
    for x in parts:
        sec = sec * 60 + x
    return sec


def eprint(*a):
    print(*a, file=sys.stderr)


# ---------------------------------------------------------------- 版本与跑步记录（v1.4）

SKILL_VERSION = "1.4.0"


def list_runs(raw=None):
    """activities.json 里的跑步，按开始时间排序：[{id, date, start, km, sec, type, treadmill, race, name}]。"""
    raw = Path(raw) if raw else raw_dir()
    out = []
    for a in load_json(raw / "activities.json", []) or []:
        t = a.get("activityType") or {}
        k = t.get("typeKey", "") or ""
        if not ("run" in k or t.get("parentTypeId") == 1):
            continue
        try:
            sl = datetime.strptime(str(a.get("startTimeLocal"))[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        out.append({"id": a.get("activityId"), "date": sl.date(), "start": sl, "km": (a.get("distance") or 0) / 1000.0,
                    "sec": a.get("movingDuration") or a.get("duration") or 0, "type": k,
                    "treadmill": ("treadmill" in k or "indoor" in k), "name": a.get("activityName") or "",
                    "race": ((a.get("eventType") or {}).get("typeKey") == "race")})
    out.sort(key=lambda r: r["start"])
    return out


def longest_before(runs, d, days=30):
    """d 之前 days 天内（不含 d 当天）最长的一次跑步（km）；没有跑步时返回 0。规则 P1 的参照值 [LOAD-3]。"""
    xs = [r["km"] for r in runs if d - timedelta(days=days) <= r["date"] < d]
    return max(xs) if xs else 0.0


def append_diary(entry: dict):
    """跑者自己说的话（疼痛、生病、感觉、预案）记进 ~/garmin-coach/diary.jsonl。只追加，不改写旧记录。"""
    p = coach_home() / "diary.jsonl"
    ensure_private_dir(p.parent)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def read_diary():
    p = coach_home() / "diary.jsonl"
    out = []
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def plan_week_rows(plan, until=None):
    """课表每周的计划跑量和课次：[{week, start, end, planned_km, sessions}]（until 之后的周不返回）。"""
    rows = []
    for w in (plan or {}).get("weeks", []):
        start = parse_date(w["start"])
        if until and start > until:
            break
        ses = [s for s in w.get("sessions", []) if s.get("km") and s.get("type") not in ("rest", "strength")]
        rows.append({"week": w.get("week"), "start": start, "end": start + timedelta(days=6), "phase": w.get("phase"),
                     "planned_km": sum(s["km"] for s in ses), "sessions": ses, "recovery": w.get("recovery")})
    return rows

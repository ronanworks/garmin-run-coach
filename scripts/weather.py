#!/usr/bin/env python3
"""用历史天气决定关键课放在几点：统计各月、各时段“不适合跑步”的天数比例和平均体感温度。

  python3 weather.py --lat 31.23 --lon 121.47 --months 12,1,2
  python3 weather.py --lat 31.23 --lon 121.47 --months 6,7,8 --hot
  python3 weather.py --lat 31.23 --lon 121.47 --race-date 2027-03-28 --hour 8 --goal 3:30:00
      # 比赛日天气（15 天内用预报，否则用近 5 年同期）→ WBGT 粗估 → 目标成绩按天气放慢多少

只用城市级坐标（保留两位小数，约 1 km 精度），不要用家庭住址。数据来自 Open-Meteo 历史天气
（免费、无需密钥，模型格点数据，海边、山区可能有偏差）。
“不适合”的默认定义（经验做法，可改）：时段内任一小时体感温度低于 −5 °C、累计降水 ≥2 mm、
或阵风 ≥50 km/h；加 --hot 时改为体感温度 ≥30 °C。
"""
import argparse
import json
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date

SLOTS = [("6–8 点", 6, 8), ("8–11 点", 8, 11), ("11–14 点", 11, 14), ("14–17 点", 14, 17), ("17–20 点", 17, 20)]


def fetch(lat, lon, start, end):
    q = urllib.parse.urlencode({
        "latitude": "%.2f" % lat, "longitude": "%.2f" % lon,
        "start_date": start, "end_date": end, "timezone": "auto",
        "hourly": "apparent_temperature,precipitation,wind_gusts_10m",
    })
    url = "https://archive-api.open-meteo.com/v1/archive?" + q
    req = urllib.request.Request(url, headers={"User-Agent": "garmin-run-coach/1.0"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read().decode())


def wbgt_abm(t, rh):
    """WBGT 简化估算（澳大利亚气象局公式，假设中等日照、微风；不含日晒和风，强日照下实际更高）。"""
    import math
    if t is None or rh is None:
        return None
    e = rh / 100.0 * 6.105 * math.exp(17.27 * t / (237.7 + t))
    return 0.567 * t + 0.393 * e + 3.94


def race_mode(a):
    """比赛日 WBGT 与成绩修正：WBGT 在 7.5–15 °C 之外，每差 1 °C 成绩约慢 0.3–0.4%（Mantzios 2022，[HEAT-4]）。"""
    import math
    import statistics as st
    from datetime import timedelta
    sys.path.insert(0, __import__("os").path.dirname(__file__))
    from gclib import fmt_dur, parse_time
    rd = date.fromisoformat(a.race_date)
    goal = parse_time(a.goal) if a.goal else None
    hours = max(1, math.ceil(goal / 3600)) if goal else a.hours
    hrs = list(range(a.hour, min(24, a.hour + hours)))
    base = {"latitude": "%.2f" % a.lat, "longitude": "%.2f" % a.lon, "timezone": "auto",
            "hourly": "temperature_2m,relative_humidity_2m"}
    vals, src = [], ""
    days_ahead = (rd - date.today()).days
    if 0 <= days_ahead <= 15:
        url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(dict(base, forecast_days=16))
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "garmin-run-coach/1.3"}), timeout=90) as r:
            h = json.loads(r.read().decode())["hourly"]
        w = [wbgt_abm(t, rh) for tt, t, rh in zip(h["time"], h["temperature_2m"], h["relative_humidity_2m"])
             if tt[:10] == rd.isoformat() and int(tt[11:13]) in hrs]
        vals = [st.mean(w)] if w else []
        src = "预报（%d 天后，越近越准）" % days_ahead
    else:
        y1 = date.today().year - (1 if (rd.month, rd.day) >= (date.today().month, date.today().day) else 0)
        for y in range(y1 - a.years + 1, y1 + 1):
            try:
                c = rd.replace(year=y)
            except ValueError:
                c = rd.replace(year=y, day=28)
            q = dict(base, start_date=(c - timedelta(days=7)).isoformat(), end_date=(c + timedelta(days=7)).isoformat())
            url = "https://archive-api.open-meteo.com/v1/archive?" + urllib.parse.urlencode(q)
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "garmin-run-coach/1.3"}), timeout=90) as r:
                h = json.loads(r.read().decode())["hourly"]
            byday = defaultdict(list)
            for tt, t, rh in zip(h["time"], h["temperature_2m"], h["relative_humidity_2m"]):
                if int(tt[11:13]) in hrs and t is not None and rh is not None:
                    byday[tt[:10]].append(wbgt_abm(t, rh))
            vals += [st.mean(v) for v in byday.values() if v]
        src = "近 %d 年同期（比赛日前后 7 天）的历史天气" % a.years
    if not vals:
        print("没有取到天气数据。")
        return 1
    vals.sort()
    q = lambda p: vals[min(len(vals) - 1, int(p * len(vals)))]
    rows = [("典型（中位数）", st.median(vals))] if len(vals) > 1 else [("预报", vals[0])]
    if len(vals) > 5:
        rows += [("偏热（75%% 分位）" % (), q(0.75)), ("很热（90% 分位）", q(0.90))]
    print("比赛日 %s，%02d:00 起约 %d 小时；数据：%s；城市坐标 %.2f, %.2f。" % (rd, a.hour, len(hrs), src, a.lat, a.lon))
    print("| 情形 | WBGT 粗估 | 超出最佳区间 | 成绩约慢 | %s" % ("调整后目标 |" if goal else ""))
    print("|---|---|---|---|%s" % ("---|" if goal else ""))
    for label, w in rows:
        ex = max(0.0, w - 15) + max(0.0, 7.5 - w)
        lo, hi = 0.003 * ex, 0.004 * ex
        tail = (" %s–%s |" % (fmt_dur(goal * (1 + lo)), fmt_dur(goal * (1 + hi)))) if goal else ""
        print("| %s | %.1f °C | %.1f °C | %.1f–%.1f%% |%s" % (label, w, ex, lo * 100, hi * 100, tail))
    print("说明：WBGT 7.5–15 °C 时最容易跑出好成绩，之外每差 1 °C 约慢 0.3–0.4%（1258 场比赛的分析，[HEAT-4]）；"
          "慢一些的跑者受热影响更大 [HEAT-1]。这里的 WBGT 是不含日晒和风的粗估，晴天无风时实际更高。"
          "WBGT 超过约 20 °C 时，赛前 1–2 周安排热适应 [HEAT-3]，目标按上表放慢，前半程更保守。")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--race-date", help="比赛日 YYYY-MM-DD：改为比赛日天气与成绩修正模式")
    ap.add_argument("--hour", type=int, default=8, help="比赛开始的小时（当地时间）")
    ap.add_argument("--hours", type=int, default=4, help="没有 --goal 时，按几个小时统计")
    ap.add_argument("--goal", help="目标成绩，如 3:30:00")
    ap.add_argument("--months", default="12,1,2", help="要统计的月份，例如 12,1,2")
    ap.add_argument("--years", type=int, default=3, help="统计最近几年（默认 3）")
    ap.add_argument("--hot", action="store_true", help="夏季：按炎热判断")
    a = ap.parse_args()
    if a.race_date:
        return race_mode(a)
    months = [int(x) for x in a.months.split(",")]
    y1 = date.today().year - 1
    j = fetch(a.lat, a.lon, "%d-01-01" % (y1 - a.years + 1), "%d-12-31" % y1)
    h = j["hourly"]
    days = defaultdict(lambda: defaultdict(list))  # (月) -> 日期 -> [(小时, 体感, 降水, 阵风)]
    for t, at, pr, gu in zip(h["time"], h["apparent_temperature"], h["precipitation"], h["wind_gusts_10m"]):
        m = int(t[5:7])
        if m in months and at is not None:
            days[m][t[:10]].append((int(t[11:13]), at, pr or 0, gu or 0))
    print("城市坐标 %.2f, %.2f；%d–%d 年；%s" % (a.lat, a.lon, y1 - a.years + 1, y1, "炎热标准" if a.hot else "寒冷/雨/风标准"))
    print("| 月份 | " + " | ".join(s[0] for s in SLOTS) + " |")
    print("|---|" + "---|" * len(SLOTS))
    for m in months:
        cells = []
        for name, h0, h1 in SLOTS:
            bad = tot = 0
            temps = []
            for d, rows in days[m].items():
                rs = [r for r in rows if h0 <= r[0] < h1]
                if not rs:
                    continue
                tot += 1
                temps += [r[1] for r in rs]
                if a.hot:
                    b = any(r[1] >= 30 for r in rs)
                else:
                    b = any(r[1] < -5 for r in rs) or sum(r[2] for r in rs) >= 2 or any(r[3] >= 50 for r in rs)
                bad += b
            cells.append("%d%%（体感 %+.1f°C）" % (round(100 * bad / tot), sum(temps) / len(temps)) if tot else "-")
        print("| %d 月 | %s |" % (m, " | ".join(cells)))
    print("百分比 = 该时段不适合跑步的天数占比。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

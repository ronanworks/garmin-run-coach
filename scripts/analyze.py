#!/usr/bin/env python3
"""把本地佳明数据算成一份“事实报告”：训练量、强度分布、负荷变化、恢复指标、作息、能力基准、成绩预测。

  python3 analyze.py                 # 默认分析最近 26 周
  python3 analyze.py --weeks 16 --asof 2026-11-01

输出（在 ~/garmin-coach/reports/）：
  analysis_YYYY-MM-DD.md    给人和智能体读的报告（只有数据和计算，不含建议）
  metrics_YYYY-MM-DD.json   全部数字，供 plan_tool.py / today.py 使用

读取 ~/garmin-coach/athlete.json（可选）里的 hrmax、lthr、lt1、lt2、rhr、sex、races；
没有时依次退回到佳明心率区间设置、观测值，并在报告里注明来源。
"""
import argparse
import math
import statistics as st
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gclib  # noqa: E402
import fitlite  # noqa: E402
import predict  # noqa: E402
import zones  # noqa: E402
import hrpace  # noqa: E402
from gclib import fmt_dur, fmt_pace  # noqa: E402

CACHE_VERSION = 6
DIST_EFFORTS = [400, 800, 1000, 3000, 5000, 10000, 21097.5]
DUR_EFFORTS = [180, 360, 720, 1200, 1800, 3600]
WD = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


# ---------------------------------------------------------------- 小工具

def med(xs):
    xs = [x for x in xs if x is not None]
    return st.median(xs) if xs else None


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def pct(a, b):
    return 100.0 * a / b if b else None


def f1(x, nd=1):
    return "-" if x is None else ("%.*f" % (nd, x))


def hhmm(minutes):
    if minutes is None:
        return "-"
    m = int(round(minutes))
    return "%d:%02d" % (m // 60, m % 60)


def hrv_cv(xs):
    """lnRMSSD 的变异系数（%），至少 5 晚。"""
    xs = [math.log(x) for x in xs if x and x > 0]
    if len(xs) < 5:
        return None
    return st.pstdev(xs) / st.mean(xs) * 100


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def is_run(a):
    t = a.get("activityType") or {}
    k = t.get("typeKey", "")
    return "run" in k or t.get("parentTypeId") == 1


# ---------------------------------------------------------------- 每次跑步的 FIT 指标

def best_by_distance(t, d, target):
    best, i = None, 0
    for j in range(len(d)):
        while i + 1 < j and d[j] - d[i + 1] >= target:
            i += 1
        if d[j] - d[i] >= target:
            dt = t[j] - t[i]
            if dt > 0 and (best is None or dt < best):
                best = dt
    if best and target / best > 6.5:  # 快于 2:34/km，多半是 GPS 漂移
        return None
    return best


def best_by_duration(t, d, w):
    best, i = None, 0
    for j in range(len(t)):
        while t[j] - t[i] > w:
            i += 1
        span = t[j] - t[i]
        if span >= w - 5 and span > 0:
            v = (d[j] - d[i]) / span
            if v <= 6.5 and (best is None or v > best):
                best = v
    return best


def run_metrics(fit_path, lt1, lt2, hrmax, rhr, sex):
    r = fitlite.load_run(fit_path)
    rc = r["records"]
    t_all, hr_all, d_all = rc["t"], rc["hr"], rc["dist"]
    n = len(t_all)
    out = {"has_fit": n > 0}
    if n < 30:
        return out
    z = [0.0, 0.0, 0.0]
    trimp = 0.0
    for k in range(1, n):
        dt = min(max(t_all[k] - t_all[k - 1], 0), 10)
        h = hr_all[k]
        if h:
            z[zones.zone3(h, lt1, lt2) - 1] += dt
            if hrmax and rhr and hrmax > rhr:
                x = min(max((h - rhr) / (hrmax - rhr), 0), 1)
                trimp += dt / 60 * x * (0.64 * math.exp(1.92 * x) if sex != "F" else 0.86 * math.exp(1.67 * x))
    out["zone_s"] = z
    out["trimp"] = trimp
    # 距离/时长最佳
    pts = [(tt, dd) for tt, dd in zip(t_all, d_all) if dd is not None]
    if len(pts) > 30:
        tt = [p[0] for p in pts]
        dd = [p[1] for p in pts]
        for k in range(1, len(dd)):  # 距离必须单调不减
            if dd[k] < dd[k - 1]:
                dd[k] = dd[k - 1]
        out["best_dist"] = {str(x): best_by_distance(tt, dd, x) for x in DIST_EFFORTS if dd[-1] >= x}
        out["best_dur"] = {str(w): best_by_duration(tt, dd, w) for w in DUR_EFFORTS if tt[-1] - tt[0] >= w}
        # 有氧解耦（只算稳定跑）：去掉前 10 分钟和暂停/走路，前后两半比较 速度/心率
        t0 = tt[0] + 600
        steady = [(x, y, h) for x, y, h in zip(t_all, d_all, hr_all) if x >= t0 and y is not None and h]
        inc = []
        for (a0, b0, _), (a1, b1, h1) in zip(steady, steady[1:]):
            dt_, dd_ = a1 - a0, b1 - b0
            if 0 < dt_ <= 10 and dd_ / dt_ > 1.5:
                inc.append((a1, dt_, dd_, h1))
        moving = sum(x[1] for x in inc)
        if moving >= 40 * 60:
            blocks = defaultdict(lambda: [0.0, 0.0])
            acc = 0.0
            for x in inc:
                acc += x[1]
                bb = blocks[int(acc // 60)]
                bb[0] += x[1]
                bb[1] += x[2]
            sp = [v[1] / v[0] for v in blocks.values() if v[0] >= 30]
            cv = (st.pstdev(sp) / st.mean(sp)) if len(sp) > 5 else 1
            z3_share = z[2] / max(sum(z), 1)
            if cv < 0.10 and z3_share < 0.05:
                half, acc = moving / 2, 0.0
                parts = [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0]]  # 时间、距离、心率×时间
                for x in inc:
                    k = 0 if acc < half else 1
                    acc += x[1]
                    parts[k][0] += x[1]
                    parts[k][1] += x[2]
                    parts[k][2] += x[3] * x[1]
                ef = [(p_[1] / p_[0] * 60) / (p_[2] / p_[0]) for p_ in parts if p_[0] > 0]
                if len(ef) == 2 and ef[0] > 0:
                    out["decoupling_pct"] = (ef[0] - ef[1]) / ef[0] * 100
                    tot_t = parts[0][0] + parts[1][0]
                    out["ef"] = ((parts[0][1] + parts[1][1]) / tot_t * 60) / ((parts[0][2] + parts[1][2]) / tot_t)
    # 心率-配速用的稳定段、心率来源、锁步频比例、标准有氧检查跑（见 hrpace.py）
    out["steady"] = hrpace.steady_blocks(rc, lt2)
    out["hr_source"] = r.get("hr_source")
    out["cad_lock"] = round(r.get("cad_lock") or 0, 3)
    if "有氧检查" in (r.get("workout_name") or ""):
        out["check"] = hrpace.check_run(r)
    return out


# ---------------------------------------------------------------- 读取

def load_runs(raw, since, asof):
    acts = gclib.load_json(raw / "activities.json", []) or []
    runs = []
    for a in acts:
        if not is_run(a):
            continue
        try:
            sl = datetime.strptime(str(a.get("startTimeLocal"))[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if not (since <= sl.date() <= asof):
            continue
        det = gclib.load_json(raw / "details" / ("%s.json" % a["activityId"]), {}) or {}
        sm = det.get("summaryDTO", {}) or {}
        rpe = sm.get("directWorkoutRpe")
        runs.append({
            "id": a["activityId"], "date": sl.date(), "start": sl, "type": (a.get("activityType") or {}).get("typeKey"),
            "dist_km": (a.get("distance") or 0) / 1000.0,
            "dur_s": a.get("movingDuration") or a.get("duration") or 0,
            "avg_hr": a.get("averageHR"), "max_hr": a.get("maxHR"),
            "speed": a.get("averageSpeed"), "gap_speed": a.get("avgGradeAdjustedSpeed"),
            "elev": a.get("elevationGain"), "garmin_load": a.get("activityTrainingLoad"),
            "te_label": a.get("trainingEffectLabel"), "vo2max": a.get("vO2MaxValue"),
            "is_race": ((a.get("eventType") or {}).get("typeKey") == "race"),
            "garmin_zone_s": [a.get("hrTimeInZone_%d" % i) for i in range(1, 6)],
            # 佳明的用力程度按 10–100 存（1–10 分 ×10），小于 10 的值不是用户填的，当作没填
            "rpe": (rpe / 10.0) if isinstance(rpe, (int, float)) and 10 <= rpe <= 100 else None,
            "feel": sm.get("directWorkoutFeel") if sm.get("directWorkoutFeel") in (0, 25, 50, 75, 100) else None,
            "name": a.get("activityName") or "",
        })
    runs.sort(key=lambda r: r["start"])
    return runs


def merge_windows(folder, key=None):
    """把 28 天窗口文件合并成 {calendarDate: 记录}。"""
    out = {}
    for p in sorted(Path(folder).glob("*.json")):
        j = gclib.load_json(p)
        if j is None:
            continue
        rows = j.get(key, []) if (key and isinstance(j, dict)) else j
        if isinstance(rows, dict):
            rows = rows.get("data", [])
        for r in rows or []:
            if isinstance(r, dict) and r.get("calendarDate"):
                out[r["calendarDate"]] = r
    return out


def load_days(raw):
    hrv = merge_windows(raw / "hrv", "hrvSummaries")
    sleep = merge_windows(raw / "sleep")
    daily = merge_windows(raw / "daily", "data")
    days = {}
    for d in set(hrv) | set(sleep) | set(daily):
        rec = {}
        h = hrv.get(d) or {}
        if h:
            b = h.get("baseline") or {}
            rec.update({"hrv": h.get("lastNightAvg"), "hrv_week": h.get("weeklyAvg"),
                        "hrv_status": h.get("status"), "hrv_bal_low": b.get("balancedLow"),
                        "hrv_bal_up": b.get("balancedUpper")})
        s = sleep.get(d) or {}
        if s and s.get("sleepTimeSeconds"):
            sc = ((s.get("sleepScores") or {}).get("overall") or {}).get("value")
            wake = s.get("sleepEndTimestampLocal")
            wake_min = None
            if wake:
                wdt = datetime.fromtimestamp(wake / 1000, timezone.utc)
                wake_min = wdt.hour * 60 + wdt.minute
            rec.update({"sleep_h": s["sleepTimeSeconds"] / 3600.0, "sleep_score": sc, "wake_min": wake_min})
        dl = daily.get(d) or {}
        if dl:
            rec.update({"rhr": (dl.get("heartRate") or {}).get("restingValue"),
                        "stress": (dl.get("stress") or {}).get("avgLevel")})
        days[gclib.parse_date(d)] = rec
    return days


def thresholds(home, raw, runs, days):
    ath = gclib.load_json(home / "athlete.json", {}) or {}
    z = gclib.load_json(raw / "profile" / "hr_zones.json", []) or []
    zr = next((x for x in z if x.get("sport") in ("RUNNING", "DEFAULT")), z[0] if z else {})
    src = {}
    hrmax = ath.get("hrmax")
    src["hrmax"] = "athlete.json" if hrmax else None
    if not hrmax and zr.get("maxHeartRateUsed"):
        hrmax, src["hrmax"] = zr["maxHeartRateUsed"], "佳明心率区间设置"
    obs = sorted([r["max_hr"] for r in runs if r.get("max_hr")])
    if not hrmax and obs:
        hrmax, src["hrmax"] = obs[-1], "观测到的最高心率（可能偏低）"
    lthr = ath.get("lthr")
    src["lthr"] = "athlete.json" if lthr else None
    if not lthr and zr.get("lactateThresholdHeartRateUsed"):
        lthr, src["lthr"] = zr["lactateThresholdHeartRateUsed"], "佳明心率区间设置（手表自动检测或手填）"
    rhr = ath.get("rhr")
    src["rhr"] = "athlete.json" if rhr else None
    if not rhr:
        vals = [v.get("rhr") for k, v in sorted(days.items())[-60:] if v.get("rhr")]
        if vals:
            rhr, src["rhr"] = round(st.median(vals)), "近 60 天静息心率中位数"
    m = zones.hr_model(hrmax, lthr, ath.get("lt1"), ath.get("lt2"), rhr)
    m["src"] = src
    m["sex"] = ath.get("sex")
    m["garmin_zone_floors"] = [zr.get("zone%dFloor" % i) for i in range(1, 6)] if zr else None
    m["garmin_method"] = zr.get("trainingMethod")
    return m, ath


# ---------------------------------------------------------------- 主分析

def analyze(asof: date, weeks: int, hrpace_opts=None):
    home = gclib.coach_home()
    raw = home / "raw"
    if not (raw / "activities.json").exists():
        raise SystemExit("没有找到数据。请先运行 garmin_pull.py。")
    start = monday(asof) - timedelta(weeks=weeks - 1)
    hist_start = start - timedelta(days=365)  # 多读一年做历史对比（历史最高周量等）
    runs_all = load_runs(raw, hist_start, asof)
    strength_acts = []  # 力量训练的日期（佳明活动类型 strength_training）
    for a_ in gclib.load_json(raw / "activities.json", []) or []:
        if "strength" in ((a_.get("activityType") or {}).get("typeKey") or ""):
            try:
                strength_acts.append(datetime.strptime(str(a_.get("startTimeLocal"))[:10], "%Y-%m-%d").date())
            except ValueError:
                pass
    days = load_days(raw)
    th, ath = thresholds(home, raw, runs_all, days)
    if not th.get("ok"):
        raise SystemExit("缺少最大心率/乳酸阈心率，无法分区。请在 athlete.json 写入 hrmax 或 lthr。")
    lt1, lt2, hrmax, rhr, sex = th["lt1"], th["lt2"], th.get("hrmax"), th.get("rhr"), th.get("sex")
    sig = [CACHE_VERSION, hrpace.VERSION, lt1, lt2, hrmax, rhr, sex]

    # 每次跑步的 FIT 指标（有缓存）
    cache_dir = home / "derived" / "runs"
    for r in runs_all:
        cp = cache_dir / ("%s.json" % r["id"])
        c = gclib.load_json(cp)
        if c and c.get("sig") == sig:
            r.update(c["m"])
            continue
        fp = raw / "fit" / ("%s.zip" % r["id"])
        m = {}
        if fp.exists():
            try:
                m = run_metrics(fp, lt1, lt2, hrmax, rhr, sex)
            except Exception as e:  # 个别损坏文件不影响整体
                m = {"has_fit": False, "fit_error": str(e)[:80]}
        r.update(m)
        gclib.save_json(cp, {"sig": sig, "m": m})
    for r in runs_all:
        if "zone_s" not in r and r.get("avg_hr"):  # 没有 FIT：用平均心率粗分
            zi = zones.zone3(r["avg_hr"], lt1, lt2)
            r["zone_s"] = [r["dur_s"] if zi == i else 0 for i in (1, 2, 3)]
            r["zone_from_avg"] = True
        zs = r.get("zone_s") or [0, 0, 0]
        if zs[2] >= 5 * 60:
            r["cls"] = "高"
        elif zs[1] >= 15 * 60 or (sum(zs) and zs[1] / sum(zs) >= 0.25):
            r["cls"] = "中"
        else:
            r["cls"] = "低"
        r["long"] = r["dist_km"] >= 16 or r["dur_s"] >= 90 * 60

    runs = [r for r in runs_all if r["date"] >= start]
    M = {"asof": asof.isoformat(), "window_start": start.isoformat(), "weeks": weeks,
         "thresholds": {k: th.get(k) for k in ("lt1", "lt2", "hrmax", "lthr", "rhr", "source", "uncertainty_bpm",
                                               "bands", "src", "garmin_zone_floors", "garmin_method")}}

    # ---- 0. 数据概况
    n = len(runs)
    M["data"] = {
        "runs": n, "with_hr": sum(1 for r in runs if r.get("avg_hr")),
        "with_fit": sum(1 for r in runs if r.get("has_fit")),
        "with_rpe": sum(1 for r in runs if r.get("rpe") is not None),
        "races": sum(1 for r in runs if r["is_race"]),
        "treadmill": sum(1 for r in runs if r["type"] and "treadmill" in r["type"]),
        "days_hrv": sum(1 for d, v in days.items() if start <= d <= asof and v.get("hrv")),
        "days_sleep": sum(1 for d, v in days.items() if start <= d <= asof and v.get("sleep_h")),
        "days_rhr": sum(1 for d, v in days.items() if start <= d <= asof and v.get("rhr")),
        "days_total": (asof - start).days + 1,
        "first_run_ever": runs_all[0]["date"].isoformat() if runs_all else None,
        # 数据新鲜度：最近一次活动 / 最近一天健康数据，比分析截止日早几天
        "last_activity": max((r["date"] for r in runs_all), default=None).isoformat() if runs_all else None,
        "last_day_data": max((d for d, v in days.items() if d <= asof and (v.get("hrv") or v.get("rhr") or v.get("sleep_h"))),
                             default=None).isoformat() if days else None,
    }

    # ---- 1. 周表
    def week_rows(rs, w0, nweeks):
        rows = []
        for i in range(nweeks):
            ws = w0 + timedelta(weeks=i)
            wr = [r for r in rs if ws <= r["date"] < ws + timedelta(days=7)]
            zs = [sum((r.get("zone_s") or [0, 0, 0])[k] for r in wr) for k in range(3)]
            daily = [sum(r.get("trimp") or 0 for r in wr if r["date"] == ws + timedelta(days=k)) for k in range(7)]
            mono = (st.mean(daily) / st.pstdev(daily)) if st.pstdev(daily) > 0 else None
            rows.append({
                "week": ws.isoformat(), "km": sum(r["dist_km"] for r in wr), "runs": len(wr),
                "hours": sum(r["dur_s"] for r in wr) / 3600,
                "long_km": max([r["dist_km"] for r in wr], default=0),
                "long_min": max([r["dur_s"] for r in wr], default=0) / 60,
                "zone_pct": [pct(z, sum(zs)) for z in zs] if sum(zs) else [None] * 3,
                "cls": {c: sum(1 for r in wr if r["cls"] == c) for c in ("低", "中", "高")},
                "trimp": sum(r.get("trimp") or 0 for r in wr),
                # session RPE（Foster）：用力程度 1–10 × 分钟；只统计填了 RPE 的跑步
                "srpe": sum(r["rpe"] * r["dur_s"] / 60 for r in wr if r.get("rpe")),
                "srpe_n": sum(1 for r in wr if r.get("rpe")),
                "strength": sum(1 for x in strength_acts if ws <= x < ws + timedelta(days=7)),
                "garmin_load": sum(r.get("garmin_load") or 0 for r in wr),
                "monotony": mono, "strain": (sum(daily) * mono) if mono else None,
            })
        for i, w in enumerate(rows):
            prev = rows[i - 1]["km"] if i else None
            w["chg_pct"] = pct(w["km"] - prev, prev) if prev else None
            if i >= 2 and rows[i - 2]["km"]:
                w["chg2_pct"] = pct(w["km"] - rows[i - 2]["km"], rows[i - 2]["km"])
        return rows

    W = week_rows(runs, start, weeks)
    hist_weeks = week_rows(runs_all, monday(runs_all[0]["date"]) if runs_all else start,
                           ((monday(asof) - monday(runs_all[0]["date"])).days // 7 + 1) if runs_all else 0)
    M["weeks_table"] = W
    last4 = W[-4:]
    M["volume"] = {
        "avg4_km": mean([w["km"] for w in last4]), "avg8_km": mean([w["km"] for w in W[-8:]]),
        "avg12_km": mean([w["km"] for w in W[-12:]]), "avg_window_km": mean([w["km"] for w in W]),
        "max_week_km_window": max([w["km"] for w in W], default=0),
        "max_week_km_history": max([w["km"] for w in hist_weeks], default=0),
        "max_week_history_date": max(hist_weeks, key=lambda w: w["km"])["week"] if hist_weeks else None,
        "runs_per_week_avg8": mean([w["runs"] for w in W[-8:]]),
        "weeks_ge3_runs_pct": pct(sum(1 for w in W if w["runs"] >= 3), len(W)),
        "longest_run_km_window": max([r["dist_km"] for r in runs], default=0),
        "longest_run_min_window": max([r["dur_s"] for r in runs], default=0) / 60,
        "longest_run_km_last8w": max([r["dist_km"] for r in runs if r["date"] >= asof - timedelta(weeks=8)], default=0),
        "runs_ge": {str(k): sum(1 for r in runs if r["dist_km"] >= k and r["date"] >= asof - timedelta(weeks=16))
                    for k in (21, 25, 28, 30, 32)},
        "runs_ge_120min_16w": sum(1 for r in runs if r["dur_s"] >= 7200 and r["date"] >= asof - timedelta(weeks=16)),
    }
    gaps = []
    rd = sorted(set(r["date"] for r in runs))
    for a, b in zip(rd, rd[1:]):
        if (b - a).days >= 7:
            gaps.append({"from": a.isoformat(), "to": b.isoformat(), "days": (b - a).days})
    M["volume"]["gaps_ge7d"] = gaps

    # ---- 2. 强度分布
    def tid(rs):
        zs = [sum((r.get("zone_s") or [0, 0, 0])[k] for r in rs) for k in range(3)]
        tot = sum(zs)
        c = {k: sum(1 for r in rs if r["cls"] == k) for k in ("低", "中", "高")}
        return {"time_pct": [pct(z, tot) for z in zs] if tot else None,
                "session_pct": {k: pct(v, len(rs)) for k, v in c.items()} if rs else None, "n": len(rs)}
    r8 = [r for r in runs if r["date"] >= asof - timedelta(weeks=8)]
    easy = [r for r in runs if r["cls"] == "低" and not r["long"] and r.get("avg_hr")]
    longs = [r for r in runs if r["long"] and r.get("avg_hr")]
    M["intensity"] = {
        "last8w": tid(r8), "window": tid(runs),
        "easy_runs_avg_hr_median": med([r["avg_hr"] for r in easy]),
        "nonlong_runs_avg_hr_above_lt1_pct": pct(sum(1 for r in runs if not r["long"] and r.get("avg_hr") and r["avg_hr"] >= lt1),
                                                 sum(1 for r in runs if not r["long"] and r.get("avg_hr"))),
        "long_runs": [{"date": r["date"].isoformat(), "km": r["dist_km"], "min": r["dur_s"] / 60,
                       "pace": fmt_pace(r["speed"]), "avg_hr": r["avg_hr"], "decoupling_pct": r.get("decoupling_pct"),
                       "z_pct": [pct(z, sum(r["zone_s"])) for z in r["zone_s"]] if r.get("zone_s") and sum(r["zone_s"]) else None}
                      for r in longs[-12:]],
        "zone_from_avg_only": sum(1 for r in runs if r.get("zone_from_avg")),
    }

    # ---- 3. 负荷变化
    spikes = []
    for r in runs:
        prev = [x["dist_km"] for x in runs_all if r["date"] - timedelta(days=30) <= x["date"] < r["date"]]
        if prev and max(prev) > 0 and r["dist_km"] >= 5:
            ratio = r["dist_km"] / max(prev) - 1
            if ratio > 0.10:
                band = "小尖峰 >10–30%" if ratio <= 0.30 else ("中尖峰 >30–100%" if ratio <= 1.0 else "大尖峰 >100%")
                spikes.append({"date": r["date"].isoformat(), "km": r["dist_km"], "prev30_max_km": max(prev),
                               "increase_pct": ratio * 100, "band": band})
    # EWMA 急慢性负荷（只做描述）
    dl = defaultdict(float)
    for r in runs_all:
        dl[r["date"]] += r.get("trimp") or 0
    a_l, c_l, acwr = 0.0, 0.0, []
    la, lc = 2 / 8, 2 / 29
    d = runs_all[0]["date"] if runs_all else start
    while d <= asof:
        a_l = la * dl[d] + (1 - la) * a_l
        c_l = lc * dl[d] + (1 - lc) * c_l
        if d >= start and c_l > 0:
            acwr.append((d, a_l / c_l))
        d += timedelta(days=1)
    M["load"] = {
        "spikes_window": spikes, "spikes_last12w": [s for s in spikes if s["date"] >= (asof - timedelta(weeks=12)).isoformat()],
        "weeks_gt10pct": [w["week"] for w in W if w.get("chg_pct") is not None and w["chg_pct"] > 10 and w["km"] > 15],
        "weeks_2w_gt30pct": [w["week"] for w in W if (w.get("chg2_pct") or 0) > 30 and w["km"] > 15],
        "monotony_last4": [w["monotony"] for w in last4], "acwr_ewma_latest": acwr[-1][1] if acwr else None,
        "acwr_ewma_max_window": max((x[1] for x in acwr), default=None),
        "trimp_note": "Banister TRIMP，按 %s 公式；用于相对比较，不是绝对标准" % ("女性" if sex == "F" else "男性"),
    }

    # ---- 4. 恢复指标
    def dvals(key, d0, d1):
        return [days[d][key] for d in sorted(days) if d0 <= d <= d1 and days[d].get(key) is not None]
    hrv7 = dvals("hrv", asof - timedelta(days=6), asof)
    hrv_base = dvals("hrv", asof - timedelta(days=34), asof - timedelta(days=7))
    swc = None
    if len(hrv_base) >= 14:
        mu, sd = st.mean(hrv_base), st.pstdev(hrv_base)
        swc = (mu - 0.5 * sd, mu, mu + 0.5 * sd)
    hrv7m = mean(hrv7) if len(hrv7) >= 4 else None
    status = None
    if swc and hrv7m is not None:
        status = "低于个人正常范围" if hrv7m < swc[0] else ("高于个人正常范围" if hrv7m > swc[2] else "在个人正常范围内")
    rhr7 = dvals("rhr", asof - timedelta(days=6), asof)
    rhr28 = dvals("rhr", asof - timedelta(days=34), asof - timedelta(days=7))
    sl7 = dvals("sleep_h", asof - timedelta(days=6), asof)
    sl28 = dvals("sleep_h", asof - timedelta(days=27), asof)
    sc28 = dvals("sleep_score", asof - timedelta(days=27), asof)
    gst = defaultdict(int)
    for dd, v in days.items():
        if asof - timedelta(days=27) <= dd <= asof and v.get("hrv_status"):
            gst[v["hrv_status"]] += 1
    # 大负荷后的第二天
    resp = {"hi": [], "other": [], "rest": []}
    loads = sorted([v for k, v in dl.items() if start <= k <= asof and v > 0])
    q75 = loads[int(len(loads) * 0.75)] if len(loads) >= 8 else None
    for k in range((asof - start).days):
        d0 = start + timedelta(days=k)
        nxt = days.get(d0 + timedelta(days=1), {})
        base = dvals("hrv", d0 - timedelta(days=7), d0 - timedelta(days=1))
        if nxt.get("hrv") is None or len(base) < 4:
            continue
        grp = "rest" if dl.get(d0, 0) == 0 else ("hi" if q75 and dl[d0] >= q75 else "other")
        resp[grp].append((nxt["hrv"] - st.mean(base), nxt.get("sleep_score")))
    M["recovery"] = {
        "hrv_7d_mean": hrv7m, "hrv_swc": swc, "hrv_status_own": status, "hrv_days_7d": len(hrv7),
        "hrv_latest": next((days[d]["hrv"] for d in sorted(days, reverse=True) if d <= asof and days[d].get("hrv")), None),
        "garmin_hrv_status_28d": dict(gst),
        "garmin_balanced_band": next(((days[d].get("hrv_bal_low"), days[d].get("hrv_bal_up")) for d in sorted(days, reverse=True)
                                      if d <= asof and days[d].get("hrv_bal_low")), None),
        "rhr_7d": mean(rhr7), "rhr_base": mean(rhr28), "rhr_diff": (mean(rhr7) - mean(rhr28)) if rhr7 and rhr28 else None,
        "sleep_7d_h": mean(sl7), "sleep_28d_h": mean(sl28),
        "nights_lt7h_28d_pct": pct(sum(1 for x in sl28 if x < 7), len(sl28)), "sleep_score_28d": mean(sc28),
        "next_day_after_hi_load": {g: {"n": len(v), "hrv_delta": mean([x[0] for x in v]),
                                       "sleep_score": mean([x[1] for x in v])} for g, v in resp.items()},
        "hi_load_threshold_trimp": q75,
        "hrv_cv_7d": hrv_cv(dvals("hrv", asof - timedelta(days=6), asof)),
        "hrv_cv_base": mean([hrv_cv(dvals("hrv", asof - timedelta(days=7 * k + 6), asof - timedelta(days=7 * k)))
                             for k in range(1, 9)]),
    }
    # 主观负荷：每次跑步 sRPE ÷ TRIMP，近 4 周对前 8 周（同样的心率负荷，感觉是不是更累了）
    def srpe_ratio(d0, d1):
        xs = [r["rpe"] * r["dur_s"] / 60 / r["trimp"] for r in runs_all
              if d0 < r["date"] <= d1 and r.get("rpe") and r.get("trimp") and r["trimp"] > 5]
        return (mean(xs), len(xs)) if len(xs) >= 4 else (None, len(xs))
    r4, n4 = srpe_ratio(asof - timedelta(weeks=4), asof)
    r8, n8 = srpe_ratio(asof - timedelta(weeks=12), asof - timedelta(weeks=4))
    M["recovery"]["srpe_trimp"] = {"recent4w": r4, "n_recent": n4, "prev8w": r8, "n_prev": n8,
                                   "change_pct": ((r4 / r8 - 1) * 100) if (r4 and r8) else None,
                                   "rpe_coverage_pct": pct(sum(1 for r in runs if r.get("rpe")), len(runs))}
    # 晨间主观状态（today.py 每次记录到 wellness.json）
    wl = gclib.load_json(home / "wellness.json", {}) or {}
    def wavg(key_, d0, d1):
        xs = [v.get(key_) for k_, v in wl.items() if v.get(key_) is not None and d0 < gclib.parse_date(k_) <= d1]
        return (mean(xs), len(xs))
    M["recovery"]["wellness"] = {k_: {"recent14": wavg(k_, asof - timedelta(days=14), asof),
                                      "prev28": wavg(k_, asof - timedelta(days=42), asof - timedelta(days=14))}
                                 for k_ in ("feel", "soreness", "sleepq", "stress")} if wl else None
    M["volume"]["strength_per_week_8w"] = sum(1 for x in strength_acts if x > asof - timedelta(weeks=8)) / 8

    # ---- 5. 作息
    wk = defaultdict(list)
    for r in runs:
        wk[r["date"].weekday()].append(r["start"].hour * 60 + r["start"].minute)
    nweeks = max(weeks, 1)
    wake = defaultdict(list)
    for dd, v in days.items():
        if start <= dd <= asof and v.get("wake_min") is not None:
            wake[dd.weekday()].append(v["wake_min"])
    w2r = []
    for r in runs:
        v = days.get(r["date"], {})
        if v.get("wake_min") is not None:
            gap = r["start"].hour * 60 + r["start"].minute - v["wake_min"]
            if 0 < gap <= 240:
                w2r.append(gap)
    M["schedule"] = {
        "by_weekday": [{"day": WD[i], "run_weeks_pct": pct(len(set(monday(r["date"]) for r in runs if r["date"].weekday() == i)), nweeks),
                        "start_median": hhmm(med(wk[i])), "wake_median": hhmm(med(wake[i]))} for i in range(7)],
        "wake_to_run_median_min": med(w2r), "wake_to_run_n": len(w2r),
    }

    # ---- 6. 能力基准
    def bests(rs, key, better):
        out = {}
        for r in rs:
            for k, v in (r.get(key) or {}).items():
                if v and (k not in out or better(v, out[k][0])):
                    out[k] = (v, r["date"].isoformat())
        return out
    r16 = [r for r in runs_all if r["date"] >= asof - timedelta(weeks=16)]
    bd = bests(r16, "best_dist", lambda a, b: a < b)
    bu = bests(r16, "best_dur", lambda a, b: a > b)
    cs = None
    pts = [(bd[k][0], float(k)) for k in ("400", "800", "5000") if k in bd]
    if len(pts) == 3:
        f = predict.cs_fit(pts)
        if f and 2.0 < f[0] < 6.5:
            cs = {"cs": f[0], "d_prime": f[1], "r2": f[2], "points": "400/800/5000 m"}
    races, race_list = [], []
    kw = ("马拉松", "半马", "全马", "比赛", "竞赛", "race", "Race", "RACE", "parkrun", "10K", "10k", "5K", "5k", "越野赛")
    for r in runs_all:
        if r["dist_km"] < 3 or r["date"] < asof - timedelta(weeks=52):
            continue
        by_name = any(k in r["name"] for k in kw)
        if r["is_race"] or by_name:
            weeks_ago = (asof - r["date"]).days // 7
            race_list.append({"date": r["date"].isoformat(), "name": r["name"], "km": r["dist_km"], "time": fmt_dur(r["dur_s"]),
                              "avg_hr": r["avg_hr"], "vdot": zones.vdot_from_race(r["dist_km"] * 1000, r["dur_s"]),
                              "weeks_ago": weeks_ago, "marked_race": r["is_race"],
                              "used": bool(r["is_race"] and weeks_ago <= 26)})
            if r["is_race"] and weeks_ago <= 26:
                races.append((r["dist_km"] * 1000, r["dur_s"], "比赛 %s %.1f km %s" % (r["date"], r["dist_km"], fmt_dur(r["dur_s"]))))
    def same_race(date_iso, km):  # 同一天、距离差 5% 以内，算同一场（手表 GPS 距离和官方距离常差几百米）
        for x in race_list:
            if x["date"] == date_iso and abs(x["km"] - km) <= 0.05 * km:
                return x
        return None
    for rr in ath.get("races", []) or []:  # 用户手动补充的比赛：和手表标记的比赛用同一条 26 周规则
        try:
            d_m, t_s = float(rr["distance_m"]), gclib.parse_time(rr["time"])
        except (KeyError, ValueError, TypeError):
            continue
        try:
            rd = gclib.parse_date(rr["date"])
        except (KeyError, ValueError, TypeError):
            rd = None
        weeks_ago = (asof - rd).days // 7 if rd else None
        used = weeks_ago is not None and 0 <= weeks_ago <= 26
        basis = "比赛 %s %s" % (rd.isoformat() if rd else "", rr.get("name", ""))
        dup = same_race(rd.isoformat(), d_m / 1000) if rd else None
        if dup is None:
            race_list.append({"date": rd.isoformat() if rd else "日期缺失", "name": rr.get("name", "用户补充的比赛"),
                              "km": d_m / 1000, "time": fmt_dur(t_s), "avg_hr": rr.get("avg_hr"),
                              "vdot": zones.vdot_from_race(d_m, t_s),
                              "weeks_ago": weeks_ago if weeks_ago is not None else -1, "marked_race": True,
                              "used": used, "user_added": True, "date_missing": rd is None})
            if used:
                races.append((d_m, t_s, basis))
        else:  # 手表里已有这场：用户确认它是比赛；在 26 周内且还没计入时才计入，避免同一场算两次
            dup["marked_race"] = True
            dup["user_added"] = True
            if used and not dup["used"]:
                dup["used"] = True
                races.append((d_m, t_s, basis))
    effort_basis = []
    for k in ("5000", "10000", "21097.5"):
        if k in bd:
            effort_basis.append((float(k), bd[k][0], "训练中最快 %s m（%s）" % (k, bd[k][1])))
    vd = [(zones.vdot_from_race(d_, t_), lab) for d_, t_, lab in races + effort_basis]
    vo2 = [r["vo2max"] for r in runs_all if r.get("vo2max")]
    efm = defaultdict(list)
    for r in runs:
        if r.get("ef"):
            efm[r["date"].strftime("%Y-%m")].append(r["ef"])
    M["fitness"] = {
        "best_dist_16w": {k: {"time": fmt_dur(v[0]), "pace": fmt_pace(float(k) / v[0]), "date": v[1], "s": v[0]} for k, v in bd.items()},
        "best_dur_16w": {k: {"pace": fmt_pace(v[0]), "speed": v[0], "date": v[1]} for k, v in bu.items()},
        "cs": cs, "vdot": [{"vdot": v, "basis": lab} for v, lab in vd], "races_12m": race_list,
        "garmin_vo2max_latest": vo2[-1] if vo2 else None,
        "ef_by_month": {k: mean(v) for k, v in sorted(efm.items())},
        "decoupling_recent": [{"date": r["date"].isoformat(), "km": r["dist_km"], "pct": r["decoupling_pct"]}
                              for r in runs if r.get("decoupling_pct") is not None][-10:],
    }

    # ---- 6b. 心率-配速表现
    hp = dict(hrpace_opts or {})
    hp.setdefault("ref_pace", ath.get("hrpace_ref_pace"))
    hp.setdefault("ref_hr", ath.get("hrpace_ref_hr"))
    hp = {k: v for k, v in hp.items() if v}
    M["hrpace"] = hrpace.build(runs_all, asof, lt1, ath, home, weeks, **hp)

    # ---- 7. 预测（全马、半马、10K）
    w8 = [r for r in runs_all if r["date"] > asof - timedelta(weeks=8)]
    k8 = sum(r["dist_km"] for r in w8) / 8
    p8 = (sum(r["dur_s"] for r in w8) / sum(r["dist_km"] for r in w8)) if w8 and sum(r["dist_km"] for r in w8) else None
    basis = races if races else effort_basis
    preds = {}
    for tgt, name in ((42195, "全马"), (21097.5, "半马"), (10000, "10K")):
        res = predict.predict_all(tgt, basis, k8 if tgt == 42195 else None, p8 if tgt == 42195 else None,
                                  cs["cs"] if (cs and tgt == 42195) else None)
        preds[name] = {"models": res, "summary": predict.summarize(res)}
    M["prediction"] = {"basis": "比赛成绩" if races else "训练中的最快片段（不是全力比赛，偏保守）",
                       "weekly_km_8w": k8, "train_pace_8w": fmt_pace(1000 / p8) if p8 else None,
                       "train_pace_8w_s": p8, "results": preds}
    return M, runs


# ---------------------------------------------------------------- 报告

def render(M):
    L = []
    th, dt, v, it, ld, rc, sc, ft, pr = (M["thresholds"], M["data"], M["volume"], M["intensity"], M["load"],
                                         M["recovery"], M["schedule"], M["fitness"], M["prediction"])
    L.append("# 训练数据分析（自动生成）\n")
    L.append("截至 %s，分析窗口 %s 起 %d 周。本报告只包含数据和计算结果；解读和建议请按 references/analysis-protocol.md 写，并引用证据编号。\n"
             % (M["asof"], M["window_start"], M["weeks"]))
    L.append("## 0. 数据概况与可信度\n")
    lad = dt.get("last_day_data") or dt.get("last_activity")
    if lad:
        lag = (gclib.parse_date(M["asof"]) - gclib.parse_date(lad)).days
        if lag >= 2:
            L.append("- **注意：数据只到 %s，比分析截止日 %s 早 %d 天。** 近 4 周跑量、近 7 天 HRV/静息心率等最近的数字会偏低或缺失。"
                     "先运行 garmin_pull.py 补数据，或者用 --asof %s 重新分析。" % (lad, M["asof"], lag, lad))
    L.append("- 跑步 %d 次；有心率 %d、有 FIT 逐秒数据 %d、有主观用力评分 RPE %d；标记为比赛 %d；跑步机 %d。"
             % (dt["runs"], dt["with_hr"], dt["with_fit"], dt["with_rpe"], dt["races"], dt["treadmill"]))
    L.append("- 健康数据覆盖：HRV %d/%d 天，睡眠 %d 天，静息心率 %d 天。账号最早一次跑步：%s。"
             % (dt["days_hrv"], dt["days_total"], dt["days_sleep"], dt["days_rhr"], dt["first_run_ever"]))
    s = th["src"]
    L.append("- 心率阈值：LT1 ≈ %s，LT2 ≈ %s bpm（%s，误差约 ±%s）。HRmax %s（%s），LTHR %s（%s），静息心率 %s（%s）。"
             % (th["lt1"], th["lt2"], th["source"], th["uncertainty_bpm"], th["hrmax"], s.get("hrmax"),
                th["lthr"], s.get("lthr"), th["rhr"], s.get("rhr")))
    if it["zone_from_avg_only"]:
        L.append("- 有 %d 次跑步没有 FIT，只能按平均心率粗略归区。" % it["zone_from_avg_only"])
    L.append("- 佳明区间设置：方法 %s，各区下限 %s。\n" % (th.get("garmin_method"), th.get("garmin_zone_floors")))

    L.append("## 1. 训练量与规律\n")
    L.append("| 周（周一） | 跑量 km | 次数 | 时长 h | 最长 km | 区1/区2/区3 % | 课次 低/中/高 | TRIMP | sRPE（填了的次数） | 力量 | 环比 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for w in M["weeks_table"][-16:]:
        zp = "/".join(f1(x, 0) for x in w["zone_pct"]) if w["zone_pct"][0] is not None else "-"
        L.append("| %s | %.1f | %d | %.1f | %.1f | %s | %d/%d/%d | %.0f | %s | %d | %s |" % (
            w["week"], w["km"], w["runs"], w["hours"], w["long_km"], zp, w["cls"]["低"], w["cls"]["中"], w["cls"]["高"],
            w["trimp"], ("%.0f（%d）" % (w["srpe"], w["srpe_n"])) if w.get("srpe_n") else "-", w.get("strength", 0),
            ("%+.0f%%" % w["chg_pct"]) if w.get("chg_pct") is not None else "-"))
    L.append("")
    L.append("- 平均周跑量：近 4 周 %s，近 8 周 %s，近 12 周 %s km；窗口内最高 %.1f km；历史最高 %.1f km（%s 那周）。"
             % (f1(v["avg4_km"]), f1(v["avg8_km"]), f1(v["avg12_km"]), v["max_week_km_window"], v["max_week_km_history"],
                v["max_week_history_date"]))
    L.append("- 近 8 周平均每周 %s 次；跑 3 次及以上的周占 %s%%。" % (f1(v["runs_per_week_avg8"]), f1(v["weeks_ge3_runs_pct"], 0)))
    L.append("- 最长一次：窗口内 %.1f km / %.0f 分钟；近 8 周 %.1f km。近 16 周 ≥21/25/28/30/32 km 的次数：%s；≥2 小时 %d 次。"
             % (v["longest_run_km_window"], v["longest_run_min_window"], v["longest_run_km_last8w"],
                "/".join(str(v["runs_ge"][k]) for k in ("21", "25", "28", "30", "32")), v["runs_ge_120min_16w"]))
    if v["gaps_ge7d"]:
        L.append("- 7 天以上没跑：" + "；".join("%s→%s（%d 天）" % (g["from"], g["to"], g["days"]) for g in v["gaps_ge7d"]))
    L.append("")

    L.append("## 2. 强度分布（三区：区1 < LT1 ≤ 区2 < LT2 ≤ 区3）\n")
    for lab, key in (("近 8 周", "last8w"), ("整个窗口", "window")):
        t = it[key]
        if t["time_pct"]:
            L.append("- %s：按时间 %s%%；按课次 低/中/高 = %s%%（%d 次）" % (
                lab, "/".join(f1(x, 0) for x in t["time_pct"]),
                "/".join(f1(t["session_pct"][k], 0) for k in ("低", "中", "高")), t["n"]))
    L.append("- 课次分类规则：区3 ≥5 分钟记“高”；区2 ≥15 分钟或占 25% 以上记“中”；其余“低”。这是按心率对 Seiler“课目标法”的近似。")
    L.append("- 非长距离跑中平均心率 ≥ LT1 的比例：%s%%；低强度轻松跑平均心率中位数：%s。"
             % (f1(it["nonlong_runs_avg_hr_above_lt1_pct"], 0), f1(it["easy_runs_avg_hr_median"], 0)))
    if it["long_runs"]:
        L.append("\n| 长距离日期 | km | 分钟 | 配速 | 平均心率 | 区1/2/3 % | 解耦 % |")
        L.append("|---|---|---|---|---|---|---|")
        for r in it["long_runs"]:
            L.append("| %s | %.1f | %.0f | %s | %s | %s | %s |" % (
                r["date"], r["km"], r["min"], r["pace"], f1(r["avg_hr"], 0),
                "/".join(f1(x, 0) for x in r["z_pct"]) if r["z_pct"] else "-", f1(r["decoupling_pct"])))
    L.append("")

    L.append("## 3. 负荷变化与风险提示（都是提示，不是预测）\n")
    sp = ld["spikes_last12w"]
    L.append("- 近 12 周单次跑量超过“前 30 天最长一次”10%%以上：%d 次。" % len(sp))
    for s_ in sp[-8:]:
        L.append("  - %s：%.1f km，前 30 天最长 %.1f km，+%.0f%%（%s）" % (s_["date"], s_["km"], s_["prev30_max_km"], s_["increase_pct"], s_["band"]))
    L.append("- 周跑量环比 >10%% 的周：%s；比两周前多 30%% 以上的周：%s。"
             % ("、".join(ld["weeks_gt10pct"][-6:]) or "无", "、".join(ld["weeks_2w_gt30pct"][-6:]) or "无"))
    L.append("- 近 4 周单调性（Foster）：%s。EWMA 急慢性负荷比：最新 %s，窗口最高 %s（仅描述，证据不支持用它判断伤病风险）。"
             % ("、".join(f1(x, 2) for x in ld["monotony_last4"]), f1(ld["acwr_ewma_latest"], 2), f1(ld["acwr_ewma_max_window"], 2)))
    L.append("- %s。\n" % ld["trimp_note"])

    L.append("## 4. 恢复指标\n")
    if rc["hrv_swc"]:
        L.append("- HRV：近 7 天均值 %s ms（%d 天有数据）；个人正常范围（前 28 天 均值±0.5SD）%s–%s ms → **%s**；最近一晚 %s ms。"
                 % (f1(rc["hrv_7d_mean"], 0), rc["hrv_days_7d"], f1(rc["hrv_swc"][0], 0), f1(rc["hrv_swc"][2], 0),
                    rc["hrv_status_own"], f1(rc["hrv_latest"], 0)))
    else:
        L.append("- HRV：数据不足以建立个人基线（需要至少 14 晚）。")
    if rc["garmin_hrv_status_28d"]:
        L.append("- 佳明 HRV 状态近 28 天：%s；佳明“平衡”区间 %s。" % (rc["garmin_hrv_status_28d"], rc["garmin_balanced_band"]))
    L.append("- 静息心率：近 7 天 %s，前 28 天 %s，差 %s bpm。" % (f1(rc["rhr_7d"]), f1(rc["rhr_base"]), f1(rc["rhr_diff"])))
    if rc.get("hrv_cv_7d") is not None:
        L.append("- HRV 波动（lnRMSSD 7 天变异系数）：近 7 天 %s%%，前 8 周平均 %s%%。波动明显变大且均值下降，常见于负荷累积或生活压力 [HRV-1]。"
                 % (f1(rc["hrv_cv_7d"]), f1(rc.get("hrv_cv_base"))))
    sr = rc.get("srpe_trimp") or {}
    L.append("- 主观用力：近 26 周 %s%% 的跑步在手表上填了用力程度（RPE）。" % f1(sr.get("rpe_coverage_pct"), 0))
    if sr.get("change_pct") is not None:
        L.append("  - 每次跑步“sRPE ÷ TRIMP”近 4 周比前 8 周 %+.0f%%（%d / %d 次）。明显上升说明同样的心率负荷感觉更累，"
                 "主观和心率负荷不一致本身就是有用的信号 [LOAD-12][SUBJ-1]。" % (sr["change_pct"], sr["n_recent"], sr["n_prev"]))
    if rc.get("wellness"):
        names = {"feel": "整体感觉(1–5)", "soreness": "腿酸(0–10)", "sleepq": "睡眠质量(1–5)", "stress": "压力(1–5)"}
        parts = ["%s 近 14 天 %s（%d 次）/ 之前 %s" % (names[k_], f1(v["recent14"][0]), v["recent14"][1], f1(v["prev28"][0]))
                 for k_, v in rc["wellness"].items() if v["recent14"][1]]
        if parts:
            L.append("- 晨间主观状态：" + "；".join(parts) + "。")
    L.append("- 睡眠：近 7 天平均 %s h，近 28 天 %s h，不足 7 小时的夜晚占 %s%%，睡眠分均值 %s。"
             % (f1(rc["sleep_7d_h"]), f1(rc["sleep_28d_h"]), f1(rc["nights_lt7h_28d_pct"], 0), f1(rc["sleep_score_28d"], 0)))
    nd = rc["next_day_after_hi_load"]
    L.append("- 第二天早上的反应（HRV 相对前 7 天均值的变化 / 睡眠分）：大负荷日后 %s ms / %s（%d 次）；其他跑步日后 %s / %s（%d 次）；休息日后 %s / %s（%d 次）。"
             % (f1(nd["hi"]["hrv_delta"]), f1(nd["hi"]["sleep_score"], 0), nd["hi"]["n"], f1(nd["other"]["hrv_delta"]),
                f1(nd["other"]["sleep_score"], 0), nd["other"]["n"], f1(nd["rest"]["hrv_delta"]), f1(nd["rest"]["sleep_score"], 0), nd["rest"]["n"]))
    L.append("")

    L.append("## 5. 作息与可训练时间\n")
    L.append("| 星期 | 有跑步的周占比 | 开跑时间中位数 | 起床时间中位数 |")
    L.append("|---|---|---|---|")
    for x in sc["by_weekday"]:
        L.append("| %s | %s%% | %s | %s |" % (x["day"], f1(x["run_weeks_pct"], 0), x["start_median"], x["wake_median"]))
    L.append("\n- 起床到开跑中位数 %s 分钟（%d 次）。\n" % (f1(sc["wake_to_run_median_min"], 0), sc["wake_to_run_n"]))

    L.append("## 6. 能力基准（近 16 周训练数据）\n")
    if ft["best_dist_16w"]:
        L.append("- 最快片段：" + "；".join("%s m %s（%s/km，%s）" % (k, x["time"], x["pace"], x["date"])
                                       for k, x in sorted(ft["best_dist_16w"].items(), key=lambda kv: float(kv[0]))))
    if ft["best_dur_16w"]:
        L.append("- 最佳时长平均配速：" + "；".join("%d 分钟 %s/km" % (int(k) // 60, x["pace"]) for k, x in sorted(ft["best_dur_16w"].items(), key=lambda kv: int(kv[0]))))
    if ft["cs"]:
        L.append("- 临界速度 CS ≈ %.2f m/s（%s/km），D′ ≈ %.0f m，拟合 R² %.3f（用 %s）。"
                 % (ft["cs"]["cs"], fmt_pace(ft["cs"]["cs"]), ft["cs"]["d_prime"], ft["cs"]["r2"], ft["cs"]["points"]))
    for x in ft["vdot"]:
        L.append("- VDOT %.1f（%s）" % (x["vdot"], x["basis"]))
    if ft.get("races_12m"):
        L.append("- 比赛记录（近 12 个月手表标记或名称像比赛的，加上 athlete.json 里用户补充的）：")
        for x in ft["races_12m"]:
            if x["used"]:
                tag = "已用于预测"
            elif x.get("date_missing"):
                tag = "用户补充，缺日期，未用于预测，请向用户确认日期"
            elif x["weeks_ago"] < 0:
                tag = "日期在截止日之后，未用于预测，请向用户确认日期"
            elif x["marked_race"]:
                tag = "超过 26 周，只作参考，不进预测和目标"
            else:
                tag = "名称像比赛，未标记，请向用户确认"
            ago = "日期缺失" if x.get("date_missing") else ("%d 周前" % x["weeks_ago"] if x["weeks_ago"] >= 0 else "截止日之后")
            L.append("  - %s %s：%.2f km %s，平均心率 %s，VDOT %.1f，%s（%s）" % (
                x["date"], x["name"], x["km"], x["time"], f1(x["avg_hr"], 0), x["vdot"], ago, tag))
    if ft["garmin_vo2max_latest"]:
        L.append("- 佳明 VO2max 估计 %s（手表算法，未经独立验证，仅看趋势）。" % ft["garmin_vo2max_latest"])
    if ft["ef_by_month"]:
        L.append("- 有氧效率 EF（稳定跑，米/分 ÷ 心率），按月：" + "；".join("%s %.2f" % (k, x) for k, x in ft["ef_by_month"].items()))
    L.append("- 注意：训练片段多半不是全力，按它算的 VDOT/CS 会偏低；如有近期比赛成绩，请写进 athlete.json 的 races。\n")

    L.append(hrpace.render(M.get("hrpace", {})))
    L.append("## 7. 成绩预测（多模型，单位 时:分:秒）\n")
    L.append("依据：%s；赛前 8 周平均周跑量 %s km，平均训练配速 %s/km。\n" % (pr["basis"], f1(pr["weekly_km_8w"]), pr["train_pace_8w"]))
    for name, x in pr["results"].items():
        if not x["models"]:
            continue
        L.append("**%s**：区间 %s – %s，中位数 %s" % (name, fmt_dur(x["summary"]["min"]), fmt_dur(x["summary"]["max"]), fmt_dur(x["summary"]["median"])))
        for r in x["models"]:
            L.append("- %s｜%s｜%s %s" % (r["model"], r["basis"], fmt_dur(r["time_s"]), ("（" + r["note"] + "）") if r["note"] else ""))
        L.append("")
    L.append("## 8. 需要向用户确认的信息\n")
    q = []
    if not s.get("hrmax") or "观测" in str(s.get("hrmax")):
        q.append("最大心率是否测过（比赛冲刺或专门测试的最高值）")
    if not th["lthr"]:
        q.append("是否做过乳酸阈测试或 30 分钟全力测试")
    if not dt["races"]:
        q.append("近 6 个月有没有正式比赛成绩（距离、用时、日期）")
    q += ["伤病史和当前是否有疼痛", "每周哪几天、几点能训练，每次最多多长时间", "目标比赛、日期、目标成绩"]
    L.extend("- " + x for x in q)
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--weeks", type=int, default=26)
    ap.add_argument("--asof", help="截止日期 YYYY-MM-DD，默认今天")
    a = ap.parse_args()
    asof = gclib.parse_date(a.asof) if a.asof else date.today()
    M, _ = analyze(asof, a.weeks)
    out = gclib.coach_home() / "reports"
    md = out / ("analysis_%s.md" % asof)
    js = out / ("metrics_%s.json" % asof)
    gclib.write_private(md, render(M))
    gclib.save_json(js, M)
    gclib.save_json(out / "metrics_latest.json", M)
    print("报告：%s\n数据：%s" % (md, js))


if __name__ == "__main__":
    main()

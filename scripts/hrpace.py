#!/usr/bin/env python3
"""心率-配速表现：同样配速下心率怎么变、同样心率下配速怎么变（间歇课、热身、坡道、停顿都排除）。

  python3 hrpace.py                       # 打印最近 26 周的心率-配速趋势（需要先运行 analyze.py 生成缓存）
  python3 hrpace.py --ref-pace 5:30 --ref-hr 145
  python3 hrpace.py --run 123456789       # 某一次跑步和此前 8 周的模型相比

方法（详见 references/analysis-protocol.md 第 2b 节和 evidence.md）：
1. 稳定段：每次跑步切成 60 秒一段，只保留“移动时间第 10–60 分钟、本段配速波动小、前 3 分钟配速也接近、
   坡度不超过 2%、心率低于 LT2”的段落。心率在配速改变后约 3 分钟才基本稳定 [HRP-2]，所以要求前 3 分钟也稳定；
   高于 LT2 心率不会进入稳态，所以排除；60 分钟以后的心率漂移另用解耦率看。
2. 共同斜率：用每次跑步内部的心率-速度变化，求一个对所有跑步通用的斜率 β（bpm / (m/s)），
   这样不同日子之间的体能、天气差异不会混进斜率。
3. 每次跑步的“同配速心率”= 该次稳定段平均心率 − β ×（该次平均速度 − 参考速度）。
   “同心率配速”由同一模型反推。按月汇总，给出次数、标准误和气温（有城市坐标时）。

客观性说明：
- 下面这些门槛是工程上的经验参数，不是研究给出的数值：段长 60 秒、跳过前 10 分钟、只用前 60 分钟、
  10 秒小段速度变异系数 ≤8%、前 3 段速度差 ≤7%、坡度 ≤2%、锁步频比例 >30%、保底斜率 40。
  有研究依据的只有：心率约 3 分钟进入稳态 [HRP-2]、最小有意义变化约 1% [HRP-1]。
- 用户的数据本身可能不准（手腕心率、GPS、手表里的阈值设置），所以报告会列出用了多少、排除了多少，
  结论用“可能变好 / 不明确 / 可能变差”三档，证据不够时就是“不明确”。
"""
import argparse
import math
import statistics as st
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gclib  # noqa: E402
from gclib import fmt_pace, parse_pace  # noqa: E402

VERSION = 5         # 改了稳定段算法就加 1，analyze.py 的缓存会自动重算
BLOCK = 60          # 秒
SKIP = 600          # 跳过前 10 分钟移动时间
MAX_T = 3600        # 只用前 60 分钟
CV_MAX = 0.08       # 段内速度变异系数上限（按 10 秒小段计算，单秒 GPS 速度噪声太大）
PRE_TOL = 0.07      # 前 2 段平均速度与本段相差不超过 7%
GRADE_MAX = 0.02    # 坡度上限
MIN_V = 1.8         # m/s，低于约 9:15/km 视为走路
DEFAULT_SLOPE = 40.0  # 保底斜率（bpm per m/s），约等于每快 10 秒/km 心率高 2–3 bpm；只在数据不足时用，并在报告中注明


def steady_blocks(rec, lt2=None):
    """rec: fitlite.load_run()['records']。返回 [[分钟, 速度m/s, 心率, 坡度], ...]。"""
    t, d, hr, alt = rec["t"], rec["dist"], rec["hr"], rec["alt"]
    n = len(t)
    if n < 60:
        return []
    moving = 0.0
    cur = None
    blocks = []
    for k in range(1, n):
        if d[k] is None or d[k - 1] is None:
            continue
        dt = t[k] - t[k - 1]
        dd = d[k] - d[k - 1]
        if not (0 < dt <= 10) or dd < 0:
            if cur is not None:
                cur["broken"] = True  # 停顿或数据缺口：这一段作废
            continue
        v = dd / dt
        if v < MIN_V:
            if cur is not None:
                cur["broken"] = True
            continue
        moving += dt
        if moving < SKIP or moving > MAX_T:
            continue
        idx = int((moving - SKIP) // BLOCK)
        if cur is None or cur["idx"] != idx:
            if cur is not None:
                blocks.append(cur)
            cur = {"idx": idx, "T": 0.0, "D": 0.0, "hrT": 0.0, "hrN": 0.0, "ch": defaultdict(lambda: [0.0, 0.0]),
                   "alt0": alt[k], "alt1": alt[k], "broken": False}
        cur["T"] += dt
        cur["D"] += dd
        c10 = cur["ch"][int((moving - SKIP) // 10)]
        c10[0] += dt
        c10[1] += dd
        if hr[k]:
            cur["hrT"] += hr[k] * dt
            cur["hrN"] += dt
        if alt[k] is not None:
            if cur["alt0"] is None:
                cur["alt0"] = alt[k]
            cur["alt1"] = alt[k]
    if cur is not None:
        blocks.append(cur)
    out, prev = [], {}
    for b in blocks:
        ok = not b["broken"] and b["T"] >= 0.8 * BLOCK and b["hrN"] >= 0.8 * b["T"] and b["D"] > 0
        v = b["D"] / b["T"] if b["T"] else 0
        if ok:
            sp = [dd_ / tt_ for tt_, dd_ in b["ch"].values() if tt_ >= 8]
            ok = len(sp) >= 4 and st.pstdev(sp) / st.mean(sp) <= CV_MAX
        g = None
        if ok and b["alt0"] is not None and b["alt1"] is not None:
            g = (b["alt1"] - b["alt0"]) / b["D"]
            ok = abs(g) <= GRADE_MAX
        h = b["hrT"] / b["hrN"] if b["hrN"] else None
        if ok and lt2 and h and h >= lt2:
            ok = False
        # 前 3 分钟配速也要稳定：心率对配速变化的时间常数约 1 分钟，3 分钟后才基本进入稳态
        pv = [prev.get(b["idx"] - j) for j in (1, 2, 3)]
        steady_before = all(x is not None for x in pv) and all(abs(x - v) / v <= PRE_TOL for x in pv)
        prev[b["idx"]] = v if (not b["broken"] and b["T"] >= 0.8 * BLOCK) else None
        if ok and steady_before and h:
            out.append([round((b["idx"] * BLOCK + SKIP) / 60, 1), round(v, 3), round(h, 1),
                        round(g, 4) if g is not None else None])
    return out


# ---------------------------------------------------------------- 模型

def common_slope(runs):
    """共同斜率 β（bpm per m/s）：同一个月内，不同跑步的平均速度和平均心率之间的关系。
    同一个月里体能接近，轻松跑和节奏跑的速度差又足够大；跑步内部的速度变化太小、
    还混着心率漂移和 GPS 噪声，会把斜率压低，所以不用。"""
    by_m = defaultdict(list)
    for r in runs:
        bl = r.get("blocks") or []
        if len(bl) >= 3:
            by_m[r["date"].strftime("%Y-%m")].append((st.mean(b[1] for b in bl), st.mean(b[2] for b in bl)))
    sxy = sxx = 0.0
    months = 0
    for pts in by_m.values():
        if len(pts) < 4:
            continue
        mv = st.mean(p[0] for p in pts)
        mh = st.mean(p[1] for p in pts)
        xx = sum((p[0] - mv) ** 2 for p in pts)
        if xx < 0.05:  # 这个月的跑步配速都差不多，提供不了斜率信息
            continue
        sxy += sum((p[0] - mv) * (p[1] - mh) for p in pts)
        sxx += xx
        months += 1
    if months >= 2 and sxx > 0:
        beta = sxy / sxx
        if 20 <= beta <= 80:
            return beta, "由 %d 个月里不同跑步之间的配速差估计" % months
    return DEFAULT_SLOPE, "数据不足以可靠估计，使用保底斜率 %.0f bpm/(m/s)，结果只看相对变化" % DEFAULT_SLOPE


def run_offsets(runs, beta, v_ref):
    """每次跑步折算到参考速度的心率。"""
    out = []
    for r in runs:
        bl = r.get("blocks") or []
        if len(bl) < 3:
            continue
        v = st.mean(b[1] for b in bl)
        h = st.mean(b[2] for b in bl)
        w = r.get("wx") or {}
        out.append({"id": r["id"], "date": r["date"], "n": len(bl), "v": v, "hr": h,
                    "hr_at_ref": h - beta * (v - v_ref), "extrap": abs(v - v_ref) > 0.5,
                    "temp": w.get("t"), "app": w.get("at"), "dew": w.get("dp"),
                    "src": r.get("hr_source") or "wrist", "title": r.get("title", "")})
    return out


def summarize(offsets, beta, v_ref, hr_refs, key=lambda o: o["date"].strftime("%Y-%m")):
    groups = defaultdict(list)
    for o in offsets:
        if not o["extrap"]:
            groups[key(o)].append(o)
    rows = []
    for k in sorted(groups):
        g = groups[k]
        vals = [o["hr_at_ref"] for o in g]
        m = st.mean(vals)
        se = (st.stdev(vals) / math.sqrt(len(vals))) if len(vals) > 1 else None

        def med(key_):
            xs = [o[key_] for o in g if o.get(key_) is not None]
            return st.median(xs) if xs else None
        rows.append({"period": k, "runs": len(g), "blocks": sum(o["n"] for o in g), "hr_at_ref": m, "se": se,
                     "pace_at_hr": {str(h): v_ref + (h - m) / beta for h in hr_refs},
                     "temp_median": med("temp"), "app_median": med("app"), "dew_median": med("dew")})
    return rows


def personal_noise(offsets):
    """同一个月内，单次跑步“同配速心率”的个人波动（合并组内标准差）。"""
    groups = defaultdict(list)
    for o in offsets:
        if not o["extrap"]:
            groups[o["date"].strftime("%Y-%m")].append(o["hr_at_ref"])
    ss = df = 0.0
    for v in groups.values():
        if len(v) >= 3:
            m = st.mean(v)
            ss += sum((x - m) ** 2 for x in v)
            df += len(v) - 1
    return math.sqrt(ss / df) if df >= 5 else None


def verdict(diff, se, ref_hr):
    """三档判定：最小有意义变化取 1%（[HRP-1]），用 90% 置信区间判断方向。"""
    swc = 0.01 * ref_hr
    if diff < -swc and diff + 1.645 * se < 0:
        return "可能变好"
    if diff > swc and diff - 1.645 * se > 0:
        return "可能变差"
    return "不明确"


def wbgt_abm(t, rh):
    """WBGT 简化估算（澳大利亚气象局公式，假设中等日照、微风；不含日晒和风的影响，只作粗略参考）。"""
    if t is None or rh is None:
        return None
    e = rh / 100.0 * 6.105 * math.exp(17.27 * t / (237.7 + t))
    return 0.567 * t + 0.393 * e + 3.94


def check_run(run, min_s=600):
    """标准有氧检查跑：固定配速段（≥10 分钟、有配速目标）最后 5 分钟的心率和配速，以及其后 60 秒的心率恢复。"""
    steps = {s_["idx"]: s_ for s_ in run.get("steps", [])}
    rec = run["records"]
    seg = None
    for l in run["laps"]:
        s_ = steps.get(l.get("step"))
        if s_ and s_.get("target") and s_["target"].get("kind") == "speed" and (s_.get("dur") or 0) >= min_s:
            if seg is None or seg["step"] != l.get("step"):
                seg = {"step": l.get("step"), "start": l["start"], "end": l["start"]}
            seg["end"] = l["start"] + (l.get("elapsed_s") or 0)
    if not seg:
        return None
    t1 = seg["end"]
    pts = [(t, h, d) for t, h, d in zip(rec["t"], rec["hr"], rec["dist"]) if t1 - 300 <= t < t1 and h and d is not None]
    if len(pts) < 60:
        return None
    hr5 = st.mean(p[1] for p in pts)
    v5 = (pts[-1][2] - pts[0][2]) / (pts[-1][0] - pts[0][0]) if pts[-1][0] > pts[0][0] else None
    after = [h for t, h in zip(rec["t"], rec["hr"]) if t1 <= t <= t1 + 60 and h]
    hrr = (after[0] - after[-1]) if len(after) > 20 else None
    return {"hr_last5": hr5, "v_last5": v5, "hrr60": hrr}


# ---------------------------------------------------------------- 天气（可选，只用城市级坐标）

WX_VARS = "temperature_2m,apparent_temperature,dew_point_2m,relative_humidity_2m"


def hourly_weather(coords, d0, d1, cache_path):
    """Open-Meteo 逐小时天气：{'YYYY-MM-DDTHH': {'t': 气温, 'at': 体感温度, 'dp': 露点, 'rh': 相对湿度}}。失败返回 {}。"""
    import json
    import urllib.parse
    import urllib.request
    cache = gclib.load_json(cache_path, {}) or {}
    key = "%.2f,%.2f" % (coords[0], coords[1])
    data = cache.get(key, {})
    need = [d0 + timedelta(days=i) for i in range((d1 - d0).days + 1)]
    if all(("%sT12" % x.isoformat()) in data for x in need if x < date.today() - timedelta(days=6)):
        return data
    try:
        base = {"latitude": "%.2f" % coords[0], "longitude": "%.2f" % coords[1], "hourly": WX_VARS, "timezone": "auto"}
        urls = ["https://archive-api.open-meteo.com/v1/archive?" + urllib.parse.urlencode(
            dict(base, start_date=d0.isoformat(), end_date=min(d1, date.today()).isoformat()))]
        if d1 >= date.today() - timedelta(days=7):  # 历史库有几天延迟，最近的用预报库的过去数据
            urls.append("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(
                dict(base, past_days=10, forecast_days=1)))
        for url in urls:
            req = urllib.request.Request(url, headers={"User-Agent": "garmin-run-coach/1.3"})
            with urllib.request.urlopen(req, timeout=60) as r:
                j = json.loads(r.read().decode())
            h = j["hourly"]
            for i, tt in enumerate(h["time"]):
                if h["temperature_2m"][i] is not None:
                    data[tt[:13]] = {"t": h["temperature_2m"][i], "at": h["apparent_temperature"][i],
                                     "dp": h["dew_point_2m"][i], "rh": h["relative_humidity_2m"][i]}
        cache[key] = data
        gclib.save_json(cache_path, cache)
    except Exception:
        pass
    return data


def coords_for(ath, d):
    """city_coords，搬过家时按 city_history 里的日期切换（只用城市级坐标）。"""
    c = ath.get("city_coords")
    for h_ in sorted(ath.get("city_history") or [], key=lambda x: x.get("from", "")):
        if h_.get("from", "") <= d.isoformat():
            c = h_.get("coords") or c
    return c


def weather_at(ath, home, when):
    c = coords_for(ath, when.date())
    if not c or home is None:
        return None
    wx = hourly_weather(c, when.date(), when.date(), home / "derived" / "weather_hourly_v2.json")
    return wx.get(when.strftime("%Y-%m-%dT%H"))


# ---------------------------------------------------------------- 汇总入口（analyze.py 调用）

def _band_diff(ra, rb, key_):
    diffs, wts, bands, ses = [], [], [], []
    for band in sorted({int(o[key_] // 5) * 5 for o in ra} & {int(o[key_] // 5) * 5 for o in rb}):
        xa = [o["hr_at_ref"] for o in ra if int(o[key_] // 5) * 5 == band]
        xb = [o["hr_at_ref"] for o in rb if int(o[key_] // 5) * 5 == band]
        if len(xa) >= 3 and len(xb) >= 3:
            w = min(len(xa), len(xb))
            diffs.append((st.mean(xa) - st.mean(xb)) * w)
            ses.append((st.variance(xa) / len(xa) + st.variance(xb) / len(xb)) * w * w)
            wts.append(w)
            bands.append("%d–%d °C" % (band, band + 5))
    if not wts:
        return None
    return {"diff": sum(diffs) / sum(wts), "se": math.sqrt(sum(ses)) / sum(wts), "bands": bands, "n": sum(wts)}


def build(runs, asof, lt1, ath=None, home=None, weeks=26, ref_pace=None, ref_hr=None):
    """runs: analyze.py 里带 'steady' 的跑步列表。返回 metrics 字典。"""
    ath = ath or {}
    start = asof - timedelta(weeks=weeks)
    in_window = [r for r in runs if r["date"] >= start]
    pool = [r for r in in_window if "treadmill" not in (r.get("type") or "")]
    locked = [r for r in pool if (r.get("cad_lock") or 0) > 0.3]
    quality = {"window_runs": len(in_window), "treadmill": len(in_window) - len(pool), "cad_lock": len(locked),
               "no_steady": sum(1 for r in pool if len(r.get("steady") or []) < 3 and (r.get("cad_lock") or 0) <= 0.3)}
    rs = [{"id": r["id"], "date": r["date"], "blocks": r.get("steady") or [], "title": r.get("name", ""),
           "start": r["start"], "hr_source": r.get("hr_source")}
          for r in pool if r.get("steady") and (r.get("cad_lock") or 0) <= 0.3]
    if len([r for r in rs if len(r["blocks"]) >= 3]) < 4:
        return {"ok": False, "reason": "稳定段太少（需要至少 4 次各有 3 分钟以上稳定跑的记录）"}
    # 心率来源：手腕和心率带混用时，只用次数多的那一种，避免把换传感器当成体能变化
    srcs = defaultdict(int)
    for r in rs:
        srcs[r.get("hr_source") or "wrist"] += 1
    main_src = max(srcs, key=srcs.get)
    mixed = len(srcs) > 1
    if mixed:
        quality["other_source"] = len([r for r in rs if (r.get("hr_source") or "wrist") != main_src])
        rs = [r for r in rs if (r.get("hr_source") or "wrist") == main_src]
    if (ath.get("city_coords") or ath.get("city_history")) and home is not None:
        cache = {}
        for r in rs:
            c = coords_for(ath, r["date"])
            if not c:
                continue
            ck = tuple(c)
            if ck not in cache:
                cache[ck] = hourly_weather(c, start, asof, home / "derived" / "weather_hourly_v2.json")
            r["wx"] = cache[ck].get(r["start"].strftime("%Y-%m-%dT%H"))
    beta, beta_src = common_slope(rs)
    recent = [b[1] for r in rs if r["date"] >= asof - timedelta(weeks=8) for b in r["blocks"]] or \
             [b[1] for r in rs for b in r["blocks"]]
    v_ref = (1000.0 / parse_pace(ref_pace)) if ref_pace else 1000.0 / (round(1000.0 / st.median(recent) / 5) * 5)
    hr_refs = [int(ref_hr)] if ref_hr else sorted({int(round((lt1 - 10) / 5.0) * 5), int(round(lt1 / 5.0) * 5)}) \
        if lt1 else [140, 150]
    offs = run_offsets(rs, beta, v_ref)
    rows = summarize(offs, beta, v_ref, hr_refs)
    noise = personal_noise(offs)
    ok_offs = [o["hr_at_ref"] for o in offs if not o["extrap"]]
    ref_level = st.mean(ok_offs) if ok_offs else 150
    # 最近 8 周相对前 8–16 周
    a = [o for o in offs if not o["extrap"] and o["date"] > asof - timedelta(weeks=8)]
    b = [o for o in offs if not o["extrap"] and asof - timedelta(weeks=16) < o["date"] <= asof - timedelta(weeks=8)]
    change = None
    if len(a) >= 4 and len(b) >= 4:
        xa, xb = [o["hr_at_ref"] for o in a], [o["hr_at_ref"] for o in b]
        diff = st.mean(xa) - st.mean(xb)
        se = math.sqrt(st.variance(xa) / len(xa) + st.variance(xb) / len(xb))
        change = {"recent_8w": st.mean(xa), "prev_8w": st.mean(xb), "diff_bpm": diff, "se": se,
                  "n_recent": len(xa), "n_prev": len(xb), "verdict_all": verdict(diff, se, ref_level)}
        # 同一体感温度段（5 °C 一档，体感温度已包含湿度和风）内比较；没有体感温度时用气温
        key_ = "app" if any(o.get("app") is not None for o in a + b) else "temp"
        bd = _band_diff([o for o in a if o.get(key_) is not None], [o for o in b if o.get(key_) is not None], key_)
        if bd:
            change.update({"same_temp_diff": bd["diff"], "same_temp_se": bd["se"], "same_temp_bands": bd["bands"],
                           "same_temp_n": bd["n"], "band_key": "体感温度" if key_ == "app" else "气温",
                           "verdict": verdict(bd["diff"], bd["se"], ref_level)})
    checks = [{"date": r["date"].isoformat(), **r["check"]} for r in runs
              if r.get("check") and r["date"] >= start]
    return {"ok": True, "beta": beta, "beta_src": beta_src, "v_ref": v_ref, "ref_pace": fmt_pace(v_ref),
            "mean_hr_at_ref": ref_level, "noise_bpm": noise, "hr_refs": hr_refs, "months": rows, "change": change,
            "runs": [dict(o, date=o["date"].isoformat()) for o in offs[-12:]],
            "n_runs": len(offs), "n_blocks": sum(o["n"] for o in offs), "excluded_cad_lock": len(locked),
            "hr_sources": dict(srcs), "mixed_sources": mixed, "main_source": main_src, "checks": checks,
            "quality": quality,
            "temps_available": any(o.get("temp") is not None for o in offs)}


def expected_hr(model, v):
    """用模型的均值估计某速度下的“正常”心率（单次跑步对比用）。"""
    if not model.get("ok") or model.get("mean_hr_at_ref") is None:
        return None
    return model["mean_hr_at_ref"] + model["beta"] * (v - model["v_ref"])


def _f(x, fmt="%.0f", unit=""):
    return (fmt % x + unit) if x is not None else "-"


def render(H):
    """返回事实报告里的 Markdown 段落。"""
    L = ["## 6b. 心率-配速表现（稳定跑，间歇、热身、坡道、停顿已排除）\n"]
    if not H.get("ok"):
        L.append("- " + H.get("reason", "数据不足") + "\n")
        return "\n".join(L)
    src_cn = {"wrist": "手腕光电心率", "strap": "心率带"}
    L.append("- 参考配速 %s/km，参考心率 %s bpm；共 %d 次跑步、%d 个 1 分钟稳定段（心率来源：%s）。斜率：每快 0.1 m/s 心率约高 %.1f bpm（%s）。"
             % (H["ref_pace"], "、".join(map(str, H["hr_refs"])), H["n_runs"], H["n_blocks"],
                src_cn.get(H["main_source"], H["main_source"]), H["beta"] / 10, H["beta_src"]))
    q = H.get("quality") or {}
    if q:
        L.append("- 数据质量：窗口内 %d 次跑步，用上 %d 次；排除 跑步机 %d、稳定段不足 3 分钟 %d（间歇、太短、停顿多、坡道多）、"
                 "心率疑似锁步频 %d%s。用上的比例低时，结论更不可靠。" % (
                     q.get("window_runs", 0), H["n_runs"], q.get("treadmill", 0), q.get("no_steady", 0), q.get("cad_lock", 0),
                     ("、心率来源不同 %d" % q["other_source"]) if q.get("other_source") else ""))
    if H.get("noise_bpm"):
        L.append("- 你的个人波动：同一个月里，单次跑步的同配速心率标准差约 %.1f bpm（约 %.1f%%）。单次跑步偏离在这个范围内属于正常。"
                 % (H["noise_bpm"], H["noise_bpm"] / H["mean_hr_at_ref"] * 100))
    if H.get("mixed_sources"):
        L.append("- 手腕心率和心率带混用（%s），趋势只用次数多的那一种。" % H["hr_sources"])
    head = "| 月份 | 跑步次数 | 稳定段 | 同配速心率 @%s | " % H["ref_pace"] + \
        " | ".join("同心率配速 @%s" % h for h in H["hr_refs"]) + " | 中位气温 | 中位体感 | 中位露点 |"
    L.append("\n" + head)
    L.append("|" + "---|" * (4 + len(H["hr_refs"]) + 3))
    for r in H["months"]:
        L.append("| %s%s | %d | %d | %.0f%s | %s | %s | %s | %s |" % (
            r["period"], "（样本少）" if r["runs"] < 3 else "", r["runs"], r["blocks"], r["hr_at_ref"],
            (" ±%.1f" % r["se"]) if r["se"] else "",
            " | ".join(fmt_pace(r["pace_at_hr"][str(h)]) for h in H["hr_refs"]),
            _f(r["temp_median"], unit=" °C"), _f(r["app_median"], unit=" °C"), _f(r["dew_median"], unit=" °C")))
    c = H.get("change")
    if c:
        L.append("\n- 近 8 周的同配速心率 %.1f bpm（%d 次），前 8 周 %.1f bpm（%d 次），差 %+.1f bpm（标准误 %.1f）→ 不分天气的判定：**%s**。"
                 % (c["recent_8w"], c["n_recent"], c["prev_8w"], c["n_prev"], c["diff_bpm"], c["se"], c["verdict_all"]))
        if c.get("same_temp_diff") is not None:
            L.append("- 只比同一%s段（%s）的跑步：差 %+.1f bpm（标准误 %.1f）→ **%s**（这是首选结论）。"
                     % (c["band_key"], "、".join(c["same_temp_bands"]), c["same_temp_diff"], c["same_temp_se"], c["verdict"]))
        elif H["temps_available"]:
            L.append("- 两个时期没有足够的同温度段跑步，无法排除季节影响，结论只能参考上一行。")
        L.append("- 判定规则：最小有意义变化取 1%%（约 %.1f bpm）；变化超过它、并且 90%% 置信区间不跨 0，才判“可能变好/变差” [HRP-1]。"
                 % (0.01 * H["mean_hr_at_ref"]))
    if H["runs"]:
        L.append("\n| 最近的稳定跑 | 稳定段 | 平均配速 | 平均心率 | 同配速心率 | 气温 | 体感 | 露点 |")
        L.append("|---|---|---|---|---|---|---|---|")
        for o in H["runs"][-8:]:
            L.append("| %s | %d | %s | %.0f | %.0f%s | %s | %s | %s |" % (
                o["date"], o["n"], fmt_pace(o["v"]), o["hr"], o["hr_at_ref"], "（外推）" if o["extrap"] else "",
                _f(o.get("temp"), unit=" °C"), _f(o.get("app"), unit=" °C"), _f(o.get("dew"), unit=" °C")))
    if H.get("checks"):
        L.append("\n| 标准有氧检查跑 | 最后 5 分钟配速 | 最后 5 分钟心率 | 60 秒心率恢复 |")
        L.append("|---|---|---|---|")
        for x in H["checks"]:
            L.append("| %s | %s | %s | %s |" % (x["date"], fmt_pace(x.get("v_last5")), _f(x.get("hr_last5")),
                                              _f(x.get("hrr60"), unit=" bpm")))
        L.append("- 检查跑的条件最一致（同路线、同配速），比自由跑步更可靠；心率恢复变快不一定是好事 [HRP-4]。")
    L.append("")
    if not H["temps_available"]:
        L.append("- 没有天气数据（athlete.json 里没有 city_coords）。气温和湿度会明显影响同配速心率，比较不同季节时要谨慎。")
    L.append("- 解读规则见 references/analysis-protocol.md 第 2b 节：先看判定和样本量，再排除天气、疲劳、心率测量误差，并结合 HRV、静息心率、主观感受和成绩交叉判断。\n")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--asof")
    ap.add_argument("--weeks", type=int, default=26)
    ap.add_argument("--ref-pace", help="参考配速，如 5:30")
    ap.add_argument("--ref-hr", type=int, help="参考心率，如 145")
    ap.add_argument("--run", help="单次跑步的 activityId：和此前 8 周的模型相比")
    a = ap.parse_args()
    import analyze  # 复用 analyze.py 的读取和缓存
    asof = gclib.parse_date(a.asof) if a.asof else date.today()
    M, runs = analyze.analyze(asof, a.weeks, hrpace_opts={"ref_pace": a.ref_pace, "ref_hr": a.ref_hr})
    H = M.get("hrpace", {})
    if not a.run:
        print(render(H))
        return 0
    r = next((x for x in runs if str(x["id"]) == str(a.run)), None)
    if not r:
        print("没找到这次跑步：确认活动 ID，并把 --asof 设成这次跑步当天或之后。")
        return 1
    if not r.get("steady"):
        print("这次跑步没有足够的稳定段（间歇课、太短或停顿多时正常）。")
        return 1
    prior = [x for x in runs if x["date"] < r["date"] and x["date"] >= r["date"] - timedelta(weeks=8)]
    home = gclib.coach_home()
    ath = gclib.load_json(home / "athlete.json", {}) or {}
    base = build(prior, r["date"] - timedelta(days=1), M["thresholds"]["lt1"], ath, home, weeks=8,
                 ref_pace=a.ref_pace, ref_hr=a.ref_hr)
    v = st.mean(b[1] for b in r["steady"])
    h = st.mean(b[2] for b in r["steady"])
    e = expected_hr(base, v)
    print("这次稳定段 %d 分钟：平均配速 %s/km，平均心率 %.0f。" % (len(r["steady"]), fmt_pace(v), h))
    if e is not None:
        nz = base.get("noise_bpm")
        print("按此前 8 周的心率-配速关系，这个配速的心率通常约 %.0f，这次 %+.0f bpm%s。" % (
            e, h - e, ("（你单次跑步的正常波动约 ±%.0f bpm，%s）" % (nz, "在波动范围内" if abs(h - e) <= nz else "超出波动范围，先看天气、睡眠、疲劳和心率测量"))
            if nz else "（样本不足以估计你的正常波动）"))
    else:
        print("此前 8 周的稳定跑太少，无法比较。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

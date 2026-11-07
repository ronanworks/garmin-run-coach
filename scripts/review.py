#!/usr/bin/env python3
"""单次课复盘：这次跑步和计划比，配速、心率是否达标，组间恢复怎样，心率数据可不可信，和自己平时的心率-配速关系比如何。

  python3 review.py                    # 最近一次跑步
  python3 review.py --date 2026-11-05  # 那天的最后一次跑步
  python3 review.py --id 658880388

计划目标优先读 FIT 文件里手表实际执行的课程步骤（佳明课程、佳明教练课、本技能写入的课都有）；
没有结构化课程时，再看 ~/garmin-coach/plan/plan.json 里当天的课。
输出：~/garmin-coach/reports/review_日期_活动ID.md（同时打印）。只陈述数据，解读按 references/analysis-protocol.md 第 7 节。
"""
import argparse
import statistics as st
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gclib  # noqa: E402
import fitlite  # noqa: E402
import hrpace  # noqa: E402
from gclib import fmt_dur, fmt_pace  # noqa: E402

KIND = {0: "训练", 1: "休息", 2: "热身", 3: "放松", 4: "恢复", 5: "训练", 6: "其他"}


def pick_activity(raw, aid=None, day=None):
    acts = [a for a in (gclib.load_json(raw / "activities.json", []) or [])
            if "run" in ((a.get("activityType") or {}).get("typeKey") or "")]
    acts.sort(key=lambda a: str(a.get("startTimeLocal")))
    if aid:
        return next((a for a in acts if str(a["activityId"]) == str(aid)), None)
    if day:
        same = [a for a in acts if str(a.get("startTimeLocal", ""))[:10] == day]
        return same[-1] if same else None
    return acts[-1] if acts else None


def segments(run):
    """把连续、同一课程步骤的圈合并成一段。没有课程步骤时，每圈一段。"""
    laps = run["laps"]
    segs = []
    for i, l in enumerate(laps):
        key = ("s", l.get("step"), l.get("intensity")) if l.get("step") is not None else ("l", i)
        if segs and segs[-1]["key"] == key and key[0] == "s":
            s = segs[-1]
        else:
            s = {"key": key, "step": l.get("step"), "intensity": l.get("intensity"), "start": l.get("start"),
                 "T": 0.0, "E": 0.0, "D": 0.0, "hrT": 0.0, "hrN": 0.0, "max_hr": 0, "cadT": 0.0, "cadN": 0.0, "laps": 0}
            segs.append(s)
        tt = l.get("timer_s") or 0
        s["T"] += tt
        s["E"] += l.get("elapsed_s") or tt
        s["D"] += l.get("dist_m") or 0
        if l.get("avg_hr"):
            s["hrT"] += l["avg_hr"] * tt
            s["hrN"] += tt
        s["max_hr"] = max(s["max_hr"], l.get("max_hr") or 0)
        if l.get("cad"):
            s["cadT"] += l["cad"] * tt
            s["cadN"] += tt
        s["laps"] += 1
    for s in segs:
        s["v"] = s["D"] / s["T"] if s["T"] else None
        s["hr"] = s["hrT"] / s["hrN"] if s["hrN"] else None
        s["cad"] = s["cadT"] / s["cadN"] if s["cadN"] else None
        s["end"] = (s["start"] or 0) + s["E"]
    return segs


def hr_series(rec, t0, t1):
    return [(t, h) for t, h in zip(rec["t"], rec["hr"]) if t0 <= t < t1 and h]


def suspicious_hr(rec, s):
    """稳定配速的训练段里，后段心率反而比前段低 5 bpm 以上：常见于手腕光电心率读数偏低。"""
    if not s["start"] or s["T"] < 240:
        return None
    pts = [(t, d) for t, d in zip(rec["t"], rec["dist"]) if s["start"] <= t < s["end"] and d is not None]
    ch = defaultdict(lambda: [None, None])
    for t, d in pts:
        c = ch[int((t - s["start"]) // 10)]
        c[0] = d if c[0] is None else c[0]
        c[1] = d
    sp = [(b - a) / 10 for a, b in ch.values() if a is not None and b is not None and b > a]
    if len(sp) < 12 or st.mean(sp) <= 0 or st.pstdev(sp) / st.mean(sp) > 0.08:
        return None
    early = [h for t, h in hr_series(rec, s["start"] + 90, s["start"] + 150)]
    late = [h for t, h in hr_series(rec, s["end"] - 60, s["end"])]
    if early and late and st.mean(late) < st.mean(early) - 5:
        return "配速稳定，心率却从约 %.0f 降到 %.0f" % (st.mean(early), st.mean(late))
    return None


def judge_pace(v, tgt):
    if not v or not tgt or tgt.get("kind") != "speed":
        return ""
    fast, slow = tgt["high"], tgt["low"]
    if v > fast * 1.005:
        return "快 %d 秒/km" % round(1000 / v * -1 + 1000 / fast)
    if v < slow * 0.995:
        return "慢 %d 秒/km" % round(1000 / v - 1000 / slow)
    return "在区间内"


def judge_hr(h, tgt):
    if not h or not tgt or tgt.get("kind") != "hr" or tgt.get("pct"):
        return ""
    lo, hi = tgt.get("low"), tgt.get("high")
    if hi and h > hi:
        return "高于上限 %d" % hi
    if lo and lo > 100 and h < lo:
        return "低于下限 %d" % lo
    return "在区间内"


def tgt_text(tgt):
    if not tgt:
        return "-"
    if tgt["kind"] == "speed":
        return "%s–%s" % (fmt_pace(tgt["high"]), fmt_pace(tgt["low"]))
    if tgt["kind"] == "hr" and not tgt.get("pct"):
        return ("≤%d" % tgt["high"]) if (tgt.get("low") or 0) <= 100 else "%d–%d" % (tgt["low"], tgt["high"])
    return "-"


def plan_targets(home, day):
    """没有 FIT 课程步骤时，从 plan.json 当天的课里取目标：热身/放松用 hr，训练段用 pace 或 hr。"""
    plan = gclib.load_json(home / "plan" / "plan.json")
    if not plan:
        return None, {}
    s = next((x for w in plan.get("weeks", []) for x in w.get("sessions", []) if x.get("date") == day), None)
    if not s:
        return None, {}
    out = {}

    def conv(st_):
        if st_.get("pace"):
            a, b = gclib.parse_pace(st_["pace"][0]), gclib.parse_pace(st_["pace"][1])
            return {"kind": "speed", "high": 1000 / min(a, b), "low": 1000 / max(a, b)}
        if st_.get("hr"):
            return {"kind": "hr", "low": st_["hr"][0], "high": st_["hr"][1], "pct": False}
        return None
    for st_ in s.get("steps", []):
        if st_.get("kind") == "repeat":
            for c in st_["steps"]:
                key = {"work": "训练", "recover": "恢复"}.get(c.get("kind"), "训练")
                out.setdefault(key, conv(c))
        else:
            key = {"warmup": "热身", "cooldown": "放松", "work": "训练", "run": "训练"}.get(st_.get("kind"), "训练")
            out.setdefault(key, conv(st_))
    return s, out


def km_splits(rec):
    """每公里分段：[(第几公里, 用时秒, 平均心率, 平均步频, 坡度), ...]。"""
    pts = [(t, d, h, c, al) for t, d, h, c, al in zip(rec["t"], rec["dist"], rec["hr"], rec["cad"], rec["alt"])
           if d is not None]
    out, k, i0 = [], 1, 0
    for i, p in enumerate(pts):
        if p[1] >= k * 1000:
            seg = pts[i0:i + 1]
            hs = [x[2] for x in seg if x[2]]
            cs = [x[3] for x in seg if x[3]]
            g = ((seg[-1][4] - seg[0][4]) / (seg[-1][1] - seg[0][1])) if (seg[-1][4] is not None and seg[0][4] is not None
                                                                         and seg[-1][1] > seg[0][1]) else None
            out.append((k, seg[-1][0] - seg[0][0], st.mean(hs) if hs else None, st.mean(cs) if cs else None, g))
            k += 1
            i0 = i
    return out


def durability(splits, ref):
    """Smyth 式“心率/速度”比（[HRP-3]）：以参考段为 1，返回 (最后 5 km 的比值, 开始脱钩的公里)。
    开始点 = 参考段之后第一次超过 1.025、且后面连续 2 km 也超过的那一公里。"""
    rows = [(k, sec, h) for k, sec, h, _, _ in splits if h and sec]
    base = [h * sec / 1000.0 for k, sec, h in rows if ref[0] < k <= ref[1]]
    if len(base) < 3 or len(rows) < ref[1] + 5:
        return None, None
    b = st.mean(base)
    ratio = [(k, (h * sec / 1000.0) / b) for k, sec, h in rows]
    onset = None
    for i, (k, r) in enumerate(ratio):
        if k > ref[1] and r > 1.025 and all(x[1] > 1.025 for x in ratio[i + 1:i + 3]) and len(ratio[i + 1:i + 3]) == 2:
            onset = k
            break
    return st.mean(r for _, r in ratio[-5:]), onset


def race_section(rec, a, cs=None):
    """比赛或长距离的配速分析。"""
    L = []
    sp = km_splits(rec)
    if len(sp) < 5:
        return L
    dist_km = (a.get("distance") or 0) / 1000
    is_race = ((a.get("eventType") or {}).get("typeKey") == "race")
    L.append("\n## %s\n" % ("比赛配速分析" if is_race else "长距离配速与耐久性"))
    rows5 = []
    for j in range(0, len(sp), 5):
        g = sp[j:j + 5]
        tt = sum(x[1] for x in g)
        hs = [x[2] for x in g if x[2]]
        rows5.append("| %d–%d km | %s | %s | %s |" % (g[0][0] - 1, g[-1][0], fmt_dur(tt), fmt_pace(len(g) * 1000 / tt),
                                                    ("%.0f" % st.mean(hs)) if hs else "-"))
    L.append("| 区段 | 用时 | 配速 | 平均心率 |\n|---|---|---|---|\n" + "\n".join(rows5))
    half = len(sp) // 2
    t1 = sum(x[1] for x in sp[:half])
    t2 = sum(x[1] for x in sp[half:2 * half])
    paces = [x[1] for x in sp]
    cv = st.pstdev(paces) / st.mean(paces) * 100
    L.append("\n- 前后半程（各 %d km）：%s vs %s，后半程%s %.1f%%；每公里配速变异系数 %.1f%%。" % (
        half, fmt_pace(half * 1000 / t1), fmt_pace(half * 1000 / t2), "慢" if t2 > t1 else "快", abs(t2 / t1 - 1) * 100, cv))
    if is_race and dist_km >= 40:
        L.append("- 参考：14 场美国马拉松的 91,929 个成绩中，后半程平均比前半程慢 15.6%（男）和 11.7%（女）[PACING-2]。")
    if dist_km >= 40:
        base = [x for x in sp if 2 < x[0] <= 16]
        last = sp[-12:]
        if base and last:
            vb = len(base) * 1000 / sum(x[1] for x in base)
            vl = len(last) * 1000 / sum(x[1] for x in last)
            slow = (1 - vl / vb) * 100
            L.append("- 撞墙检测：最后约 12 km 比第 2–16 km 慢 %.0f%%%s [PACING-1]。" % (
                slow, "，达到“撞墙”标准（>25%）" if slow > 25 else "，没有达到“撞墙”标准（>25%）"))
            if cs:
                L.append("- 第 2–16 km 平均速度是临界速度的 %.0f%%（超过 94%% 的人后程大幅掉速的概率更高）[PRED-5]。" % (vb / cs * 100))
    ref = (5, 10) if dist_km >= 30 else (3, 8)
    ratio, onset = durability(sp, ref)
    if ratio:
        L.append("- 耐久性：以第 %d–%d km 为基准，最后 5 km 的“心率/速度”比为 %.2f（1.00 表示没有脱钩）%s。全马人群平均约 1.16，脱钩越小完赛越快 [HRP-3]。" % (
            ref[0] + 1, ref[1], ratio, ("，从第 %d km 开始脱钩（>1.025）" % onset) if onset else "，全程没有明显脱钩"))
    cad = [x[3] for x in sp if x[3]]
    if len(cad) >= 10:
        c1 = st.mean(cad[2:7])
        c2 = st.mean(cad[-5:])
        L.append("- 步频：前段约 %.0f，最后 5 km 约 %.0f 步/分（%+.0f）。" % (c1, c2, c2 - c1))
    hilly = sum(1 for x in sp if x[4] is not None and abs(x[4]) > 0.03)
    if hilly >= 3:
        L.append("- 有 %d 公里坡度超过 3%%，分段配速和脱钩会受坡道影响，要结合路线看。" % hilly)
    return L


def review(aid=None, day=None):
    home = gclib.coach_home()
    raw = home / "raw"
    a = pick_activity(raw, aid, day)
    if not a:
        return "没有找到这次跑步。先运行 garmin_pull.py 拉最新数据。", None
    fp = raw / "fit" / ("%s.zip" % a["activityId"])
    if not fp.exists():
        return "没有这次跑步的 FIT 文件。先运行 garmin_pull.py（不要加 --no-fit）。", None
    run = fitlite.load_run(fp)
    rec = run["records"]
    segs = segments(run)
    steps = {s_["idx"]: s_ for s_ in run.get("steps", [])}
    dstr = str(a.get("startTimeLocal"))[:10]
    plan_s, plan_t = plan_targets(home, dstr)
    det = gclib.load_json(raw / "details" / ("%s.json" % a["activityId"]), {}) or {}
    sm = det.get("summaryDTO") or {}
    L = ["# 单次课复盘：%s %s\n" % (dstr, a.get("activityName") or "")]
    L.append("- 课程：%s%s" % (run.get("workout_name") or "（手表上没有选结构化课程）",
                             ("；课表当天：%s" % plan_s.get("title")) if plan_s else ""))
    L.append("- 全程 %.2f km，%s，平均心率 %s，最高 %s；佳明训练负荷 %s，有氧训练效果 %s。" % (
        (a.get("distance") or 0) / 1000, fmt_dur(a.get("movingDuration") or a.get("duration")),
        "%.0f" % a["averageHR"] if a.get("averageHR") else "-", "%.0f" % a["maxHR"] if a.get("maxHR") else "-",
        round(a.get("activityTrainingLoad") or 0),
        "%.1f" % a["aerobicTrainingEffect"] if a.get("aerobicTrainingEffect") else "-"))
    rpe = sm.get("directWorkoutRpe")
    if rpe and 10 <= rpe <= 100:
        feel = sm.get("directWorkoutFeel")
        L.append("- 你在手表上填的用力程度 %d/10%s。" % (round(rpe / 10), ("，感受 %s（0 很差 … 100 很好）" % feel)
                                                    if feel in (0, 25, 50, 75, 100) else ""))
    table_at = len(L)
    rep_n = 0
    work, notes = [], []
    prev_work = None
    # 有分组结构：FIT 里有课程步骤并且至少有两种步骤，或者圈的强度标记里有恢复/休息
    structured = (len({s_["step"] for s_ in segs if s_["step"] is not None}) >= 2) or \
        any(KIND.get(s_["intensity"]) in ("恢复", "休息") for s_ in segs)
    unstructured_laps = 0
    for s in segs:
        if s["T"] < 20:  # 结束时多按出来的零碎圈
            continue
        if not structured and len(segs) > 8:  # 没有课程结构的自动分圈：不逐圈列表，看下面的分段
            unstructured_laps += 1
            continue
        st_ = steps.get(s["step"]) if s["step"] is not None else None
        kind = KIND.get((st_ or {}).get("intensity", s["intensity"]), "训练")
        tgt = (st_ or {}).get("target") or (plan_t.get(kind) if plan_t else None)
        label = kind
        if kind == "训练" and structured and len(segs) > 2:
            rep_n += 1
            label = "第 %d 组" % rep_n
            work.append(s)
        if kind == "恢复" and prev_work is not None:
            hs = hr_series(rec, s["start"], s["end"])
            if hs:
                notes.append("%s 后恢复 %s：心率 %d → %d（降 %d）" % (
                    "第 %d 组" % rep_n, fmt_dur(s["T"]), hs[0][1], hs[-1][1], hs[0][1] - hs[-1][1]))
        if kind == "训练":
            prev_work = s
            sus = suspicious_hr(rec, s)
            if sus:
                notes.append("%s 心率数据可疑：%s。手腕光电心率在较快配速时可能读数偏低 [DEV-2]，这段以配速和主观感受为准。" % (label, sus))
        L.append("| %s | %s | %.2f km | %s | %s | %s | %s | %s | %s | %s |" % (
            label, fmt_dur(s["T"]), s["D"] / 1000, fmt_pace(s["v"]), ("%.0f" % s["hr"]) if s["hr"] else "-",
            s["max_hr"] or "-", ("%.0f" % s["cad"]) if s["cad"] else "-", tgt_text(tgt), judge_pace(s["v"], tgt),
            judge_hr(s["hr"], tgt)))
    if len(L) > table_at:
        L[table_at:table_at] = ["\n| 段 | 时长 | 距离 | 配速 | 平均心率 | 最高心率 | 步频 | 目标 | 配速 | 心率 |",
                                "|---|---|---|---|---|---|---|---|---|---|"]
    if unstructured_laps:
        L.append("- 没有课程结构（自动分圈 %d 个），分段见下文。" % unstructured_laps)
    L.append("")
    if len(work) >= 2:
        vs = [w["v"] for w in work if w["v"]]
        hs = [w["hr"] for w in work if w["hr"]]
        L.append("- 训练段 %d 组：配速 %s；平均心率 %s。最后一组比第一组配速 %+d 秒/km、心率 %+d bpm。" % (
            len(work), "、".join(fmt_pace(v) for v in vs), "、".join("%.0f" % h for h in hs),
            round(1000 / vs[-1] - 1000 / vs[0]), round(hs[-1] - hs[0])))
    for n_ in notes:
        L.append("- " + n_)
    # 稳定跑：和自己的心率-配速关系比（间歇课没有足够的稳定段，会自动跳过）
    M = None
    try:
        import analyze
        d = gclib.parse_date(dstr)
        M, runs = analyze.analyze(d - timedelta(days=1), 8)
        lt2 = M["thresholds"]["lt2"]
        bl = hrpace.steady_blocks(rec, lt2)
        if len(bl) >= 5 and len(work) < 2:  # 间歇/分组课不做这项比较
            v = st.mean(b[1] for b in bl)
            h = st.mean(b[2] for b in bl)
            e = hrpace.expected_hr(M.get("hrpace", {}), v)
            if e:
                nz = (M.get("hrpace") or {}).get("noise_bpm")
                L.append("- 稳定段 %d 分钟，平均 %s/km、心率 %.0f；按此前 8 周的心率-配速关系，这个配速通常约 %.0f，这次 %+.0f bpm%s。"
                         % (len(bl), fmt_pace(v), h, e, h - e,
                            ("（你单次跑步的正常波动约 ±%.0f bpm，%s）" % (nz, "在波动范围内，不必解读" if abs(h - e) <= nz
                                                                     else "超出波动范围，先看天气、睡眠、疲劳和心率测量"))
                            if nz else ""))
    except SystemExit:
        pass
    dur_s = a.get("movingDuration") or a.get("duration") or 0
    if ((a.get("eventType") or {}).get("typeKey") == "race" or (a.get("distance") or 0) >= 18000 or dur_s >= 5400) \
            and len(work) < 2:
        cs = ((M or {}).get("fitness") or {}).get("cs") or {}
        L.extend(race_section(rec, a, cs.get("cs")))
    ath = gclib.load_json(home / "athlete.json", {}) or {}
    start_dt = datetime.strptime(str(a.get("startTimeLocal"))[:19], "%Y-%m-%d %H:%M:%S")
    wx = hrpace.weather_at(ath, home, start_dt)
    if wx:
        wb = hrpace.wbgt_abm(wx.get("t"), wx.get("rh"))
        L.append("- 开跑时城市天气：气温 %.0f °C，体感 %.0f °C，露点 %.0f °C，湿度 %.0f%%%s。" % (
            wx["t"], wx.get("at") or wx["t"], wx.get("dp") or 0, wx.get("rh") or 0,
            ("，WBGT 粗估 %.0f °C" % wb) if wb is not None else ""))
    L.append("\n解读：按 references/analysis-protocol.md 第 7 节，先看是否完成、配速是否在区间，再看心率；心率数据可疑时以配速和感受为准。")
    text = "\n".join(L) + "\n"
    out = home / "reports" / ("review_%s_%s.md" % (dstr, a["activityId"]))
    gclib.write_private(out, text)
    return text, out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--id")
    ap.add_argument("--date")
    a = ap.parse_args()
    text, out = review(a.id, a.date)
    print(text)
    if out:
        print("已保存：%s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())

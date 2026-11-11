#!/usr/bin/env python3
"""课表工具：生成周期化课表草稿、按证据规则检查课表、导出给人看的 Markdown。

  python3 plan_tool.py build 请求.json  --out plan.json     # 由请求生成草稿（见 assets/plan_request.example.json）
  python3 plan_tool.py check plan.json  [--metrics metrics_latest.json] [--strict]
  python3 plan_tool.py md    plan.json  --out plan.md

生成器只给“合规的起点”，课表要和用户一起改；改完一定再跑 check。
规则编号和证据见 references/plan-design.md 与 references/evidence.md。
"""
import argparse
from collections import defaultdict
import json
import math
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gclib  # noqa: E402
import zones  # noqa: E402
from gclib import fmt_dur, fmt_pace, parse_date, parse_pace, parse_time  # noqa: E402

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
HARD = {"threshold", "interval", "mp", "long_mp", "test", "race"}
LONG = {"long", "long_mp"}
RUN_TYPES = {"easy", "recovery", "long", "long_mp", "threshold", "interval", "mp", "test", "race", "shakeout", "check"}
DEFAULT_SKELETON = {"mon": "easy_q2", "tue": "rest", "wed": "easy", "thu": "quality", "fri": "strength",
                    "sat": "long", "sun": "recovery"}


def monday(d):
    return d - timedelta(days=d.weekday())


def r1(x):
    return round(x * 2) / 2  # 取到 0.5 km


def fl(x):
    return math.floor(x * 2) / 2  # 向下取到 0.5 km（长距离增幅不因取整超标）


# ---------------------------------------------------------------- 结构化步骤（供导入手表）

def st_run(km=None, sec=None, hr=None, pace=None, kind="run", note=None):
    s = {"kind": kind}
    if km is not None:
        s["km"] = round(km, 2)
    if sec is not None:
        s["sec"] = int(sec)
    if hr:
        s["hr"] = list(hr)
    if pace:
        s["pace"] = list(pace)
    if note:
        s["note"] = note
    return s


def st_repeat(times, steps):
    return {"kind": "repeat", "times": times, "steps": steps}


class Paces:
    def __init__(self, z):
        self.z = z

    def p(self, key):
        return tuple(self.z["pace"][key])

    def hr(self, key):
        return tuple(self.z["hr"][key])

    def sec(self, key):  # 中值秒/公里
        a, b = self.z["pace"][key]
        return (parse_pace(a) + parse_pace(b)) / 2


def s_easy(P, km, strides=0, recovery=False):
    key = "recovery" if recovery else "easy"
    steps = [st_run(km=km, hr=P.hr(key), note="能完整说话，不追配速")]
    title = ("恢复 %g km" if recovery else "轻松 %g km") % km
    if strides:
        steps.append(st_repeat(strides, [st_run(sec=20, kind="work", note="快而放松，不冲刺"),
                                         st_run(sec=60, kind="recover", note="走或慢跑")]))
        title += " + %d×20″加速" % strides
    return {"type": "recovery" if recovery else "easy", "title": title, "km": km, "steps": steps}


def s_check(P, pace_s):
    """标准有氧检查跑：固定配速 15 分钟，最后 5 分钟的心率就是“同配速心率”最可靠的版本 [HRP-1]。"""
    pace = (fmt_pace(1000 / (pace_s - 3)), fmt_pace(1000 / (pace_s + 3)))
    steps = [st_run(sec=600, hr=P.hr("easy"), kind="warmup", note="轻松热身"),
             st_run(sec=900, pace=pace, kind="work", note="平路、同一路线、尽量同一时段；只按配速跑，不追心率"),
             st_run(sec=60, kind="rest", note="停下站着或慢走，看心率降了多少"),
             st_run(sec=300, hr=P.hr("easy"), kind="cooldown")]
    km = 15 * 60 / P.sec("E") + 900 / pace_s
    return {"type": "check", "title": "有氧检查跑 15′@%s" % fmt_pace(1000 / pace_s), "km": r1(km), "steps": steps,
            "note": "每次都用同一配速、同一路线；能戴心率带更好。结果看事实报告 6b 的“标准有氧检查跑”表"}


def s_strength(ph, is_rec):
    """力量训练：大重量和“大重量+跳跃”的组合对中长跑成绩效果最好 [STR-4]；每周 2–3 次能改善跑步经济性 [STR-1]。"""
    if ph == "基础":
        title, note = "力量（一般）", "分腿蹲、罗马尼亚硬拉、提踵、臀桥、平板支撑，各 3×8–12，每组留 2–3 次余力；约 30 分钟"
    elif ph in ("强化", "专项"):
        title, note = "力量（大重量+跳）", ("深蹲或硬拉 3–4×4–6（较重，留 1–2 次余力）+ 跳箱或单腿跳 3×6 + 提踵 3×10；约 35 分钟。"
                                       "动作还不熟练时，继续做一般力量")
    else:
        title, note = "力量（维持）", "大重量 2×4–5 + 少量跳跃；比赛前 10 天停"
    if is_rec:
        note += "；减量周组数减三分之一"
    return {"type": "strength", "title": title, "note": note}


def s_long(P, km, mp_km=0):
    if mp_km:
        base = km - mp_km
        steps = [st_run(km=base, hr=P.hr("long2"), note="稳住心率"),
                 st_run(km=mp_km, pace=P.p("M"), kind="work", note="马拉松配速，心率不超过马配上限")]
        return {"type": "long_mp", "title": "长距离 %g km（后 %g km 马配）" % (km, mp_km), "km": km, "steps": steps}
    a = r1(km * 2 / 3)
    steps = [st_run(km=a, hr=P.hr("long1")), st_run(km=km - a, hr=P.hr("long2"))]
    return {"type": "long", "title": "长距离 %g km" % km, "km": km, "steps": steps,
            "note": "按心率跑，到预计时长上限就停，不凑公里"}


def _wu_cd(P, wu, cd, main_steps, main_km):
    steps = [st_run(km=wu, hr=P.hr("easy"), kind="warmup")] + main_steps + [st_run(km=cd, hr=P.hr("easy"), kind="cooldown")]
    return steps, wu + cd + main_km


def s_threshold(P, reps, minutes=None, rep_km=None, rec_s=120, wu=2, cd=2):
    t = P.sec("T")
    if rep_km:
        work = st_run(km=rep_km, pace=P.p("T"), kind="work", note="吃力但可控，能说短句")
        wkm = reps * rep_km
        title = "阈值 %d×%g km" % (reps, rep_km)
    else:
        work = st_run(sec=minutes * 60, pace=P.p("T"), kind="work", note="吃力但可控，能说短句")
        wkm = reps * minutes * 60 / t
        title = "阈值 %d×%d′" % (reps, minutes)
    rec = st_run(sec=rec_s, kind="recover", note="慢跑，不要停")
    main = [st_repeat(reps, [work, rec])] if reps > 1 else [work]
    rkm = (reps - 1) * rec_s / P.sec("E")
    steps, km = _wu_cd(P, wu, cd, main, wkm + rkm)
    return {"type": "threshold", "title": title, "km": r1(km), "steps": steps}


def s_interval(P, reps, rep_km, rec_s=150, wu=2, cd=2):
    main = [st_repeat(reps, [st_run(km=rep_km, pace=P.p("I"), kind="work", note="均匀，最后一组不冲"),
                             st_run(sec=rec_s, kind="recover", note="慢跑")])]
    steps, km = _wu_cd(P, wu, cd, main, reps * rep_km + reps * rec_s / P.sec("E"))
    return {"type": "interval", "title": "间歇 %d×%g km" % (reps, rep_km), "km": r1(km), "steps": steps}


def s_mp(P, reps, rep_km, rec_s=120, wu=2, cd=2, key="M", label="马配"):
    work = st_run(km=rep_km, pace=P.p(key), kind="work", note="平稳，不提速")
    main = [st_repeat(reps, [work, st_run(sec=rec_s, kind="recover", note="慢跑")])] if reps > 1 else [work]
    steps, km = _wu_cd(P, wu, cd, main, reps * rep_km + (reps - 1) * rec_s / P.sec("E"))
    return {"type": "mp", "title": "%s %d×%g km" % (label, reps, rep_km) if reps > 1 else "%s %g km" % (label, rep_km),
            "km": r1(km), "steps": steps}


def s_test(P, dist_km, label, wu=2, cd=2):
    steps, km = _wu_cd(P, wu, cd, [st_run(km=dist_km, kind="work", note="均匀全力，前段别冲")], dist_km)
    return {"type": "test", "title": label, "km": r1(km), "steps": steps}


# ---------------------------------------------------------------- 生成

def make_zones(req):
    """返回 {"hr": {...}, "pace": {...}}，pace 都是 'm:ss' 字符串区间（快, 慢）。"""
    z = req.get("zones") or {}
    cur = req.get("current", {})
    hm = zones.hr_model(cur.get("hrmax"), cur.get("lthr"), cur.get("lt1"), cur.get("lt2"))
    if not hm.get("ok"):
        raise SystemExit("请求里缺少 current.hrmax 或 current.lthr（或 zones.hr）")
    lt1 = hm["lt1"]
    ref = cur.get("lthr") or hm["lt2"]
    hr = {"recovery": [100, min(lt1 - 12, round(0.85 * ref))], "easy": [100, lt1 - 5],
          "long1": [100, lt1 - 5], "long2": [100, lt1], "M": [round(0.90 * ref), round(0.95 * ref)],
          "T": [round(0.95 * ref), ref + 2]}
    hr.update(z.get("hr", {}))
    vdot = cur.get("vdot")
    if not vdot and cur.get("race"):
        rdate = cur["race"].get("date")
        if not rdate:
            raise SystemExit("current.race 缺少 date。只有近 26 周的比赛能定配速，请补上比赛日期，"
                             "或改用事实报告里的 VDOT（metrics_latest.json 的 fitness.vdot）。")
        if rdate:
            start = gclib.parse_date(str(req["start"])[:10])
            age_w = (start - gclib.parse_date(str(rdate)[:10])).days // 7
            if age_w > 26:
                raise SystemExit("current.race 是 %d 周前的比赛，超过 26 周，不能用来定配速。"
                                 "请改用事实报告里的 VDOT（metrics_latest.json 的 fitness.vdot，已排除旧比赛）。" % age_w)
        vdot = zones.vdot_from_race(float(cur["race"]["distance_m"]), parse_time(cur["race"]["time"]))
    if not vdot:
        raise SystemExit("请求里需要 current.vdot 或 current.race（近期比赛/测试成绩）来算配速")
    pt = zones.pace_table(vdot)
    pace = {k: [fmt_pace(1000 / a), fmt_pace(1000 / b)] for k, (a, b) in pt.items()}
    notes = []
    goal = req["race"].get("goal_time")
    dist = float(req["race"]["distance_m"])
    if goal and dist >= 40000:
        gp = parse_time(goal) / (dist / 1000)
        cur_m = (pt["M"][0] + pt["M"][1]) / 2
        if gp >= cur_m * 0.98:
            pace["M"] = [fmt_pace(1000 / (gp - 3)), fmt_pace(1000 / (gp + 3))]
            notes.append("马配按目标配速 %s/km。" % fmt_pace(1000 / gp))
        else:
            notes.append("目标配速 %s/km 比当前能力（%s/km）快 %.1f%%，马配先按当前能力练，检查点达标后再调。"
                         % (fmt_pace(1000 / gp), fmt_pace(1000 / cur_m), (cur_m / gp - 1) * 100))
    pace.update(z.get("pace", {}))
    return {"hr": hr, "pace": pace, "vdot": round(vdot, 1), "lt1": lt1, "lt2": hm["lt2"], "notes": notes}


def build(req):
    start = monday(parse_date(req["start"]))
    race_d = parse_date(req["race"]["date"])
    dist = float(req["race"]["distance_m"])
    kind = "M" if dist >= 40000 else ("HM" if dist >= 20000 else ("10K" if dist >= 9000 else "5K"))
    n = (monday(race_d) - start).days // 7 + 1
    if n < 6:
        raise SystemExit("离比赛不到 6 周，生成器不适合；请手写短周期课表并用 check 检查。")
    Z = make_zones(req)
    P = Paces(Z)
    cur = req["current"]
    v0 = float(cur["avg4_km"])
    vmax = float(cur.get("max_week_km") or v0)
    long30 = float(cur.get("longest_30d_km") or v0 * 0.3)
    taper = int(req.get("taper_weeks") or {"M": 3, "HM": 2, "10K": 2, "5K": 1}[kind])
    rec_every = int(req.get("recovery_every") or 4)
    peak = float(req.get("peak_km") or round(min(max(v0, vmax) * 1.15, v0 * 1.45)))
    inc = float(req.get("weekly_increase") or 0.08)
    check_every = int(req.get("aerobic_check_every", 4) or 0)  # 0 表示不安排
    # 检查跑配速固定不变（以后重新生成课表也沿用 meta.check_pace），默认取轻松配速的快端
    check_pace = parse_pace(req["check_pace"]) if req.get("check_pace") else parse_pace(Z["pace"]["E"][0])
    sk = dict(DEFAULT_SKELETON)
    sk.update(req.get("skeleton") or {})
    easy_cap_min = float(req.get("easy_max_min") or 70)
    long_cap_min = float(req.get("long_max_min") or (180 if kind == "M" else 120))
    long_cap = {"M": 32, "HM": 20, "10K": 16, "5K": 14}[kind]
    long_cap = min(long_cap, long_cap_min / (P.sec("E") / 60))
    pre = n - taper
    if kind in ("M", "HM"):
        nb, nbu = round(pre * 0.35), round(pre * 0.35)
    else:
        nb, nbu = round(pre * 0.40), round(pre * 0.30)
    phases = ["基础"] * nb + ["强化"] * nbu + ["专项"] * (pre - nb - nbu) + ["减量"] * (taper - 1) + ["比赛周"]

    # 检查点测试放在减量周（附近没有减量周时，把测试那周变成减量周）
    R = [i for i in range(1, pre - 1) if (i + 1) % rec_every == 0]

    def nearest(target, exclude=(), lo=-1, hi=10 ** 6, tol=2):
        c = [r for r in R if r not in exclude and lo < r < hi]
        if c:
            best = min(c, key=lambda r: (abs(r - target), r))
            if abs(best - target) <= tol:
                return best
        return target if lo < target < hi else None
    # 先定离比赛近、更关键的测试，再定前一个（两次测试至少隔 3 周）
    if kind == "M":
        t2 = nearest(pre - 3)
        t1 = nearest(nb + nbu - 1, {t2}, hi=(t2 - 2) if t2 is not None else 10 ** 6, tol=4)
        tests = [(t1, ("10K", 10, "10K 测试")), (t2, ("HM", 21.1, "半马测试"))]
    elif kind == "HM":
        t2 = nearest(nb + nbu)
        t1 = nearest(nb - 1, {t2}, hi=(t2 - 2) if t2 is not None else 10 ** 6, tol=4)
        tests = [(t1, ("5K", 5, "5K 测试")), (t2, ("10K", 10, "10K 测试"))]
    else:
        t2 = nearest(pre - 2)
        t1 = nearest(nb - 1, {t2}, hi=(t2 - 2) if t2 is not None else 10 ** 6, tol=4)
        tests = [(t1, ("5K", 5, "5K 测试")), (t2, ("5K", 5, "5K 测试"))]
    test_weeks = {w: t for w, t in tests if w is not None and 0 <= w < pre}

    # 周跑量与长距离
    vols, longs, recs = [], [], []
    last_full = min(v0, peak)
    prev_rec = False
    long_day = DAYS.index(next((d for d in DAYS if sk[d] == "long"), "sat"))
    long_hist = [(start - timedelta(days=7), long30)]
    last_full_long = long30
    phase_cap = {"基础": 0.75, "强化": 0.90, "专项": 1.0}
    for i in range(n):
        ph = phases[i]
        is_rec = ph not in ("减量", "比赛周") and i > 0 and (((i + 1) % rec_every == 0 and i < pre - 1) or i in test_weeks)
        if ph in ("减量", "比赛周"):
            frac = {3: [0.80, 0.65, 0.42], 2: [0.72, 0.45], 1: [0.50]}[min(taper, 3)][i - pre]
            vol = peak * frac
        elif is_rec:
            vol = last_full * 0.75
        elif i == 0:
            vol = min(v0 * 1.05, peak)
            last_full = vol
        elif prev_rec:
            vol = last_full  # 减量周之后回到减量前的水平，不在这一周加量
        else:
            step = inc if last_full < vmax else min(inc, 0.05)
            vol = min(peak, last_full * (1 + step))
            last_full = vol
        prev_rec = is_rec
        vols.append(round(vol))
        recs.append(is_rec)
        wd = start + timedelta(weeks=i, days=long_day)
        prev30 = max([k for dd, k in long_hist if wd - timedelta(days=30) <= dd < wd] or [long30])
        if ph == "比赛周":
            L = 0
        elif ph == "减量":
            L = fl(({3: [0.70, 0.50], 2: [0.55]}.get(min(taper, 3), [0.5]) + [0.5])[i - pre] * long_cap)
        elif is_rec:
            L = fl(last_full_long * 0.65)
            last_full_long -= 2  # 减量周后从稍低处重新爬升
        else:
            share = 0.55 if (kind == "M" and peak < 50) else 0.5  # 跑量低的全马计划，长距离占比放宽一点
            L = fl(min(long_cap * phase_cap.get(ph, 1.0), prev30 * 1.10, last_full_long + 2.5, vol * share))
            L = max(L, min(last_full_long, fl(prev30 * 1.10)))
            last_full_long = L
        longs.append(L)
        long_hist.append((wd, L))

    # 课程进阶表
    thr_seq = [(3, 8, None), (3, 10, None), (2, 15, None), (4, 8, None), (3, 12, None), (2, 18, None), (2, 20, None), (3, None, 3.0)]
    int_seq = [(5, 1.0), (6, 1.0), (5, 1.2), (6, 1.2)]
    mp_seq = {"M": [(3, 3.0), (2, 5.0), (2, 6.0), (3, 4.0)], "HM": [(3, 3.0), (2, 5.0), (4, 2.0), (2, 6.0)],
              "10K": [(5, 1.0), (4, 1.6), (3, 2.0), (6, 1.0)], "5K": [(6, 0.8), (5, 1.0), (8, 0.4), (4, 1.0)]}[kind]
    mp_key, mp_label = {"M": ("M", "马配"), "HM": ("HM", "半马配速"), "10K": ("I", "10K 配速"), "5K": ("I", "5K 配速")}[kind]
    ti = ii = mi = 0
    lmp = 0

    weeks = []
    for i in range(n):
        ws = start + timedelta(weeks=i)
        ph, is_rec = phases[i], recs[i]
        sessions = {}
        # 主课
        q = None
        if ph == "比赛周":
            q = s_easy(P, 5)
            q["steps"].insert(1, st_repeat(2, [st_run(km=1, pace=P.p(mp_key), kind="work"), st_run(sec=120, kind="recover")]))
            q["title"] = "轻松 5 km + 2×1 km %s" % mp_label
            q["type"] = "mp"
        elif i in test_weeks and test_weeks[i][0] != "HM":
            q = s_test(P, test_weeks[i][1], test_weeks[i][2])
        elif ph == "减量":
            reps, km_ = (2, 3.0) if i - pre == 0 else (1, 3.0)
            q = s_mp(P, reps, km_, key=mp_key, label=mp_label)
        elif is_rec:
            q = s_threshold(P, 1, 20, wu=2.5, cd=2) if kind in ("M", "HM") else s_interval(P, 4, 1.0)
        elif ph == "专项":
            r, k = mp_seq[mi % len(mp_seq)]
            q = s_mp(P, r, k, key=mp_key, label=mp_label) if kind in ("M", "HM") else s_interval(P, r, k)
            mi += 1
        else:
            r, m, k = thr_seq[min(ti, len(thr_seq) - 1)]
            q = s_threshold(P, r, m, rep_km=k)
            ti += 1
        # 第二课（周一类“easy_q2”）
        q2 = None
        q2_day = next((d for d in DAYS if sk[d] == "easy_q2"), None)
        q2_ok = q2_day is not None and all(sk[DAYS[(DAYS.index(q2_day) + k) % 7]] not in ("quality", "long") for k in (-1, 1))
        if ph in ("强化", "专项") and not is_rec and i % 2 == 1 and q2_ok:
            r, k = int_seq[min(ii, len(int_seq) - 1)]
            q2 = s_interval(P, r, k) if kind != "M" or ph == "强化" else None
            ii += 1 if q2 else 0
        # 长距离
        L = longs[i]
        lsess = None
        if ph == "比赛周":
            lsess = {"type": "race", "title": "比赛 %s" % req["race"].get("name", ""), "km": dist / 1000,
                     "date": race_d.isoformat(), "steps": []}
        elif i in test_weeks and test_weeks[i][0] == "HM":
            lsess = s_test(P, 21.1, "半马测试", wu=2, cd=1)
        elif L:
            mp_km = 0
            if kind == "M" and ph == "专项" and not is_rec:
                lmp += 1
                if lmp % 2 == 1:
                    mp_km = min(10, 4 + 2 * ((lmp + 1) // 2))
            elif kind == "M" and ph == "减量" and i == pre:
                mp_km = 6
            lsess = s_long(P, L, mp_km)
        # 跑量低的周长距离已含马配，或者长距离日是测试时，主课改成轻松 + 加速，避免一周两节重课
        if lsess and ((lsess.get("type") == "long_mp" and vols[i] < 50) or lsess.get("type") == "test") \
                and q and q.get("type") in HARD:
            q = s_easy(P, r1(min(8.0, max(5.0, vols[i] * 0.15))), strides=6)
        # 填轻松跑
        fixed = (q["km"] if q else 0) + (q2["km"] if q2 else 0) + (lsess["km"] if lsess and lsess["type"] != "race" else 0)
        roles = {d: sk[d].split("+")[0] for d in DAYS}
        plus_strength = {d for d in DAYS if sk[d].endswith("+strength")}
        easy_days = [d for d in DAYS if roles[d] in ("easy", "recovery") or (roles[d] == "easy_q2" and not q2)]
        rest_km = max(vols[i] - fixed, 0)
        min_easy = 3 if peak < 40 else 4
        dropped = []
        while len(easy_days) > 1 and rest_km / len(easy_days) < min_easy and ph != "比赛周":
            d_ = next((d for d in easy_days if roles[d] == "recovery"), easy_days[-1])
            easy_days.remove(d_)
            dropped.append(d_)
        wts = {d: (0.8 if roles[d] == "recovery" else 1.0) for d in easy_days}
        tot_w = sum(wts.values()) or 1
        day_cap = req.get("day_max_min") or {}
        # 标准有氧检查跑：放在减量周（不含测试周、减量期）里第一个普通轻松日
        check_day = None
        if check_every and is_rec and i not in test_weeks and ph not in ("减量", "比赛周"):
            check_day = next((d for d in DAYS if roles[d] == "easy" and d in easy_days), None)
        for d in DAYS:
            role = roles[d]
            day = ws + timedelta(days=DAYS.index(d))
            if ph == "比赛周" and day > race_d:
                continue
            if role == "rest" or d in dropped:
                sessions[d] = {"type": "rest", "title": "休息"}
            elif role == "strength":
                sessions[d] = s_strength(ph, is_rec) if (race_d - day).days > 10 else {"type": "rest", "title": "休息"}
            elif role == "quality":
                sessions[d] = q or {"type": "rest", "title": "休息"}
            elif role == "long":
                sessions[d] = lsess or {"type": "rest", "title": "休息"}
            elif role == "easy_q2" and q2:
                sessions[d] = q2
            elif d in easy_days:
                cap_min = day_cap.get(d) or (45 if role == "recovery" else easy_cap_min)
                km = r1(min(max(rest_km * wts[d] / tot_w, 4), cap_min / (P.sec("E") / 60)))
                if ph == "比赛周":
                    km = min(km, 6)
                strides = 6 if (role == "easy_q2" and ph != "比赛周" and not is_rec) else 0
                sessions[d] = s_easy(P, km, strides=strides, recovery=(role == "recovery"))
                if d == check_day:
                    sessions[d] = s_check(P, check_pace)
            if d in plus_strength and d in sessions and sessions[d].get("type") not in ("rest", "strength") \
                    and (race_d - day).days > 10:
                sessions[d] = dict(sessions[d], strength=s_strength(ph, is_rec))
        if ph == "比赛周":  # 比赛放在比赛日，赛前一天改为短的激活跑
            rd = DAYS[race_d.weekday()]
            sessions[rd] = lsess
            pd = DAYS[(race_d - timedelta(days=1)).weekday()]
            if pd in sessions and sessions[pd]["type"] not in ("rest", "strength"):
                sessions[pd] = s_easy(P, 3, strides=4)
                sessions[pd]["type"] = "shakeout"
                sessions[pd]["title"] = "激活 3 km + 4×20″加速"
        out = []
        for d in DAYS:
            if d in sessions:
                s_ = dict(sessions[d])
                s_["date"] = (ws + timedelta(days=DAYS.index(d))).isoformat()
                out.append(s_)
        weeks.append({"week": i + 1, "start": ws.isoformat(), "phase": ph, "recovery": is_rec,
                      "target_km": vols[i], "sessions": out})

    meta = {
        "created": date.today().isoformat(), "race": req["race"], "start": start.isoformat(), "kind": kind,
        "basis": cur, "zones": Z, "skeleton": sk, "backup": req.get("backup") or {"thu": "fri", "sat": "sun"},
        "name_prefix": req.get("name_prefix", "GRC·"), "peak_km": peak, "taper_weeks": taper,
        "checkpoints": [{"week": w + 1, "test": t[2]} for w, t in sorted(test_weeks.items())],
        "check_pace": fmt_pace(1000 / check_pace), "aerobic_check_every": check_every,
        "generator": "plan_tool.py build v1.4",
    }
    return {"meta": meta, "weeks": weeks}


# ---------------------------------------------------------------- 检查

def week_km(w):
    return sum(s.get("km", 0) for s in w["sessions"] if s.get("type") in RUN_TYPES and s.get("type") != "race")


def check(plan, metrics=None):
    F = []

    def add(level, rule, msg):
        F.append({"level": level, "rule": rule, "msg": msg})

    W = plan["weeks"]
    meta = plan.get("meta", {})
    hist_max = (metrics or {}).get("volume", {}).get("max_week_km_history") or meta.get("basis", {}).get("max_week_km")
    runs = []  # (date, km, type)
    if metrics:
        for r in metrics.get("intensity", {}).get("long_runs", []):
            runs.append((parse_date(r["date"]), r["km"], "hist"))
    base_long = meta.get("basis", {}).get("longest_30d_km")
    start = parse_date(W[0]["start"]) if W else None
    if base_long and start:
        runs.append((start - timedelta(days=7), float(base_long), "hist"))
    for w in W:
        for s in w["sessions"]:
            if s.get("type") in RUN_TYPES and s.get("type") != "race" and s.get("km"):
                runs.append((parse_date(s["date"]), float(s["km"]), s["type"]))
    runs.sort()
    # P1 单次跑量尖峰
    for d, km, t in runs:
        if t == "hist" or km < 8:
            continue
        prev = [k for dd, k, _ in runs if d - timedelta(days=30) <= dd < d]
        if prev and km > max(prev) * 1.10 + 0.05:
            add("WARN", "P1", "%s %.1f km 比前 30 天最长 %.1f km 多 %.0f%%（单次跑量超过 10%% 与过度使用伤风险升高相关）"
                % (d, km, max(prev), (km / max(prev) - 1) * 100))
    # P2 周跑量增幅
    kms = [week_km(w) for w in W]
    for i in range(1, len(W)):
        ref = next((kms[j] for j in range(i - 1, -1, -1) if not W[j].get("recovery")), kms[i - 1])
        if kms[i] > ref * 1.10 + 1 and kms[i] > 20:
            add("INFO", "P2", "第 %d 周 %.0f km，比上一个正常周 %.0f km 多 %.0f%%" % (W[i]["week"], kms[i], ref, (kms[i] / ref - 1) * 100))
        ref3 = max(kms[max(0, i - 3):i])  # 最近三周里最高的一周代表“已适应的量”，减量周后回升不算激进加量
        if i >= 2 and ref3 and kms[i] > ref3 * 1.30 and kms[i] > 20:
            add("WARN", "P2", "第 %d 周比前三周最高还多 %.0f%%（两周内增加 >30%%）" % (W[i]["week"], (kms[i] / ref3 - 1) * 100))
    # P3 超过历史最高
    if hist_max:
        over = [w["week"] for w, k in zip(W, kms) if k > hist_max]
        if over:
            add("INFO", "P3", "第 %s 周超过历史最高周量 %.0f km，这几周多留意身体反应" % ("、".join(map(str, over)), hist_max))
        big = [w["week"] for w, k in zip(W, kms) if k > hist_max * 1.25]
        if big:
            add("WARN", "P3", "第 %s 周超过历史最高的 1.25 倍" % "、".join(map(str, big)))
    # P4 恢复周
    last = 0
    for i, w in enumerate(W):
        if w.get("recovery") or w.get("phase") in ("减量", "比赛周"):
            last = i
        elif i - last >= 5:
            add("WARN", "P4", "第 %d 周之前已连续 5 周没有减量周" % w["week"])
            last = i
    # P5 强度分布、每周高强度次数
    for w in W:
        rs = [s for s in w["sessions"] if s.get("type") in RUN_TYPES]
        hard = [s for s in rs if s["type"] in HARD]
        if len(hard) > 2:
            add("WARN", "P5", "第 %d 周有 %d 次高强度课（含马配长跑/测试），通常每周不超过 2 次" % (w["week"], len(hard)))
        if len(rs) >= 5 and len([s for s in rs if s["type"] not in HARD]) / len(rs) < 0.6 and w.get("phase") != "比赛周":
            add("WARN", "P5", "第 %d 周低强度课次不到 60%%" % w["week"])
    tot = [s for w in W for s in w["sessions"] if s.get("type") in RUN_TYPES]
    if tot:
        low = sum(1 for s in tot if s["type"] not in HARD) / len(tot)
        add("INFO", "P5", "全计划低强度课次占 %.0f%%（多数研究中的耐力运动员约 75–80%%）" % (low * 100))
    # P6 连续两天高强度/长距离
    seq = sorted([(parse_date(s["date"]), s) for w in W for s in w["sessions"]], key=lambda x: x[0])
    for (d1, a), (d2, b) in zip(seq, seq[1:]):
        if (d2 - d1).days == 1 and (a.get("type") in HARD | LONG) and (b.get("type") in HARD | LONG) and b.get("type") != "race":
            add("WARN", "P6", "%s %s 和 %s %s 连着两天都是重课" % (d1, a.get("title"), d2, b.get("title")))
    # P7 长距离占比与时长
    E = None
    try:
        E = (parse_pace(meta["zones"]["pace"]["E"][0]) + parse_pace(meta["zones"]["pace"]["E"][1])) / 2
    except (KeyError, TypeError, ValueError):
        pass
    for w, k in zip(W, kms):
        for s in w["sessions"]:
            if s.get("type") in LONG and k:
                if s["km"] / k > 0.5 and k >= 30:
                    add("INFO", "P7", "第 %d 周长距离占周量 %.0f%%" % (w["week"], s["km"] / k * 100))
                if E and s["km"] * E / 60 > 210:
                    add("WARN", "P7", "%s 长距离预计超过 3.5 小时" % s["date"])
    # P8 减量
    if len(W) >= 3 and meta.get("race"):
        peak = max(kms[:-2]) if len(kms) > 2 else max(kms)
        last2 = (kms[-2] + kms[-1]) / 2 if len(kms) >= 2 else kms[-1]
        red = 1 - last2 / peak if peak else 0
        lvl = "INFO" if 0.35 <= red <= 0.65 else "WARN"
        add(lvl, "P8", "最后两周平均比最高周少 %.0f%%（研究中最有效的是约 2 周、逐步减 41–60%%，保持强度和频率）" % (red * 100))
        taper_w = [w for w in W if w.get("phase") in ("减量", "比赛周")]
        if taper_w and not any(s.get("type") in HARD - {"race"} for w in taper_w for s in w["sessions"]):
            add("WARN", "P8", "减量期没有任何强度课，研究建议减量时保持强度")
        tk = [week_km(w) for w in taper_w]
        if any(b > a for a, b in zip(tk, tk[1:])):
            add("WARN", "P8", "减量期周量中途回升（有纪律的减量是逐周连续下降）")
    # P9 检查点与长距离准备
    if meta.get("race") and len(W) >= 10:
        if not any(s.get("type") == "test" for w in W for s in w["sessions"]):
            add("WARN", "P9", "整个计划没有测试/检查点，目标无法按实际进展调整")
        if float(meta["race"].get("distance_m", 0)) >= 40000:
            n28 = sum(1 for w in W for s in w["sessions"] if s.get("type") in LONG and s.get("km", 0) >= 28)
            if n28 < 2:
                add("INFO", "P9", "28 km 以上长距离只有 %d 次（群体研究中更长的长跑与更快完赛相关；最长低于 25 km 者明显更慢）" % n28)
    # P10 休息日
    for w in W:
        if w.get("phase") == "比赛周":
            continue
        if not any(s.get("type") in ("rest", "strength") for s in w["sessions"]) and len(w["sessions"]) >= 7:
            add("WARN", "P10", "第 %d 周没有休息日" % w["week"])
    # P11 比赛日
    if meta.get("race", {}).get("date"):
        rd = meta["race"]["date"]
        if not any(s.get("type") == "race" and s.get("date") == rd for w in W for s in w["sessions"]):
            add("WARN", "P11", "比赛日 %s 没有排比赛" % rd)
        rdd = parse_date(rd)
        for w in W:
            for s in w["sessions"]:
                if s.get("type") in HARD - {"race"} and 0 < (rdd - parse_date(s["date"])).days < 3 and s.get("km", 0) > 8:
                    add("WARN", "P11", "%s 离比赛不到 3 天还有较重的课" % s["date"])
    # P12 骨架稳定
    pat = {}
    for w in W:
        if w.get("phase") in ("比赛周",):
            continue
        for s in w["sessions"]:
            dd = parse_date(s["date"]).weekday()
            key = "long" if s.get("type") in LONG else ("hard" if s.get("type") in HARD else "other")
            pat.setdefault(dd, {}).setdefault(key, 0)
            pat[dd][key] += 1
    moves = [DAY_CN[d] for d, c in pat.items() if c.get("long") and sum(c.values()) and c["long"] / sum(c.values()) < 0.7]
    if moves:
        add("INFO", "P12", "长距离不总在同一天（%s），固定骨架更容易坚持" % "、".join(moves))
    # P14 力量课的第二天是主课或长距离（按星期汇总成一条）
    by_date = {s_["date"]: s_ for w in W for s_ in w["sessions"]}
    n_str = 0
    pairs = defaultdict(int)
    for w in W:
        for s_ in w["sessions"]:
            if not (s_.get("type") == "strength" or s_.get("strength")):
                continue
            n_str += 1
            nxt = by_date.get((parse_date(s_["date"]) + timedelta(days=1)).isoformat())
            if nxt and nxt.get("type") in HARD | LONG and nxt.get("type") != "race":
                d0 = parse_date(s_["date"]).weekday()
                pairs[(d0, "长距离" if nxt.get("type") in LONG else "主课")] += 1
    if pairs:
        add("INFO", "P14", "力量课的第二天是重课：%s。这些力量课做轻一点，或改到主课当天跑完后再做（经验做法）"
            % "；".join("%s力量 → 第二天%s（%d 次）" % (DAY_CN[d0], k, c) for (d0, k), c in sorted(pairs.items())))
    if len(W) >= 8 and n_str < len(W) * 0.75:
        add("INFO", "P14", "力量课平均每周 %.1f 次；研究里有效的方案多为每周 2–3 次 [STR-1][STR-4]" % (n_str / len(W)))
    # P13 备用日
    sk = meta.get("skeleton") or {}
    for src, dst in (meta.get("backup") or {}).items():
        if src in DAYS and dst in DAYS:
            gap = (DAYS.index(dst) - DAYS.index(src)) % 7
            if gap not in (1, 2, 3):
                add("WARN", "P13", "备用日 %s→%s 不在原定日之后 1–3 天，漏课时无法按规则挪" % (DAY_CN[DAYS.index(src)], DAY_CN[DAYS.index(dst)]))
            elif sk.get(dst) in ("quality", "long"):
                add("WARN", "P13", "备用日 %s 本身是主课或长距离日" % DAY_CN[DAYS.index(dst)])
    if not any(f["level"] == "WARN" for f in F):
        add("OK", "-", "没有发现需要修改的问题")
    return F


# ---------------------------------------------------------------- Markdown

def md(plan):
    m = plan["meta"]
    Z = m["zones"]
    L = ["# 训练课表：%s %s\n" % (m["race"].get("name", ""), m["race"]["date"])]
    goal = m["race"].get("goal_time")
    L.append("从 %s 开始，共 %d 周。目标：%s。VDOT %.1f。\n" % (m["start"], len(plan["weeks"]), goal or "完赛", Z["vdot"]))
    for n_ in Z.get("notes", []):
        L.append("- " + n_)
    L.append("\n| 周次 | 阶段 | " + " | ".join(DAY_CN) + " | 合计 |")
    L.append("|---|---|" + "---|" * 7 + "---|")
    for w in plan["weeks"]:
        cells = {parse_date(s["date"]).weekday(): s.get("title", s.get("type")) + ("＋" + s["strength"]["title"] if s.get("strength") else "")
                 for s in w["sessions"]}
        ph = w["phase"] + ("·减量周" if w.get("recovery") else "")
        L.append("| 第 %d 周 %s | %s | %s | %.0f km |" % (w["week"], w["start"][5:], ph,
                                                       " | ".join(cells.get(i, "") for i in range(7)), week_km(w)))
    L.append("\n## 每种课怎么跑\n")
    L.append("| 类型 | 心率 | 配速 | 说明 |")
    L.append("|---|---|---|---|")
    h, p = Z["hr"], Z["pace"]
    L.append("| 恢复 | ≤%d | 不看 | 长距离后的第二天，跑不动就休息 |" % h["recovery"][1])
    L.append("| 轻松 | ≤%d | 约 %s–%s | 能完整说话 |" % (h["easy"][1], p["E"][0], p["E"][1]))
    L.append("| 长距离 | 前 2/3 ≤%d，之后 ≤%d | 约 %s–%s | 到时长上限就停 |" % (h["long1"][1], h["long2"][1], p["E"][0], p["E"][1]))
    L.append("| 马拉松配速 | %d–%d | %s–%s | 平稳，不提速 |" % (h["M"][0], h["M"][1], p["M"][0], p["M"][1]))
    L.append("| 阈值 | %d–%d | %s–%s | 吃力但可控，组间慢跑 |" % (h["T"][0], h["T"][1], p["T"][0], p["T"][1]))
    L.append("| 间歇 | 后半组接近最大 | %s–%s | 每组 3–5 分钟 |" % (p["I"][0], p["I"][1]))
    L.append("| 加速 | 不看 | 快而放松 | 每次 20 秒，走 60 秒 |")
    if m.get("check_pace") and m.get("aerobic_check_every"):
        L.append("| 有氧检查跑 | 不看，只记录 | 固定 %s | 热身 10 分钟 → 固定配速 15 分钟 → 停 60 秒 → 放松 5 分钟；每次同路线、同配速 |"
                 % m["check_pace"])
    L.append("\n力量课：基础期一般力量（3×8–12），强化和专项期大重量+跳跃（3–4×4–6 + 跳 3×6），减量期维持，比赛前 10 天停。"
             "动作不熟练时先做一般力量。")
    if m.get("checkpoints"):
        L.append("\n## 检查点\n")
        for c in m["checkpoints"]:
            L.append("- 第 %d 周：%s" % (c["week"], c["test"]))
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("request")
    b.add_argument("--out")
    c = sub.add_parser("check")
    c.add_argument("plan")
    c.add_argument("--metrics")
    c.add_argument("--strict", action="store_true")
    m = sub.add_parser("md")
    m.add_argument("plan")
    m.add_argument("--out")
    a = ap.parse_args()
    if a.cmd == "build":
        req = json.load(open(a.request, encoding="utf-8"))
        plan = build(req)
        out = Path(a.out) if a.out else gclib.coach_home() / "plan" / "plan.json"
        gclib.save_json(out, plan)
        print("课表草稿：%s（%d 周）" % (out, len(plan["weeks"])))
        for f in check(plan):
            print("[%s] %s %s" % (f["level"], f["rule"], f["msg"]))
    elif a.cmd == "check":
        plan = json.load(open(a.plan, encoding="utf-8"))
        met = json.load(open(a.metrics, encoding="utf-8")) if a.metrics else None
        F = check(plan, met)
        for f in F:
            print("[%s] %s %s" % (f["level"], f["rule"], f["msg"]))
        if a.strict and any(f["level"] == "WARN" for f in F):
            sys.exit(1)
    else:
        plan = json.load(open(a.plan, encoding="utf-8"))
        text = md(plan)
        if a.out:
            gclib.write_private(Path(a.out), text)
            print("已写入 %s" % a.out)
        else:
            print(text)


if __name__ == "__main__":
    main()

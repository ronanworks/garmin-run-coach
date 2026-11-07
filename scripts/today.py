#!/usr/bin/env python3
"""每日决策：今天按课表跑，还是只改今天。原则：一项指标差不改；只改今天，后面的课不动，漏掉的不补。

  python3 today.py                                  # 今天，读取最新数据
  python3 today.py --feel 2 --soreness 6            # 加上主观感受（强烈建议先问用户）
  python3 today.py --pain 4 --gait                  # 疼痛 0–10、是否影响跑姿
  python3 today.py --symptoms above_neck|below_neck|fever
  python3 today.py --date 2026-11-12 --plan 某计划.json

需要：~/garmin-coach/plan/plan.json 和 garmin_pull.py 拉到的最新健康数据（建议先运行 garmin_pull.py --days 40）。
规则编号见 references/daily-adjust.md。
"""
import argparse
import statistics as st
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gclib  # noqa: E402
from analyze import load_days  # noqa: E402

HARD = {"threshold", "interval", "mp", "long_mp", "test"}
LONG = {"long", "long_mp"}
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WD = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def sessions_by_date(plan):
    out = {}
    for w in plan.get("weeks", []):
        for s in w.get("sessions", []):
            out[s["date"]] = s
    return out


def ran_on(raw, d: date):
    acts = gclib.load_json(raw / "activities.json", []) or []
    km = 0.0
    for a in acts:
        t = (a.get("activityType") or {}).get("typeKey", "")
        if ("run" in t) and str(a.get("startTimeLocal", ""))[:10] == d.isoformat():
            km += (a.get("distance") or 0) / 1000
    return km


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date")
    ap.add_argument("--plan")
    ap.add_argument("--feel", type=int, help="整体感觉 1（很差）–5（很好）")
    ap.add_argument("--soreness", type=int, help="肌肉酸痛 0–10")
    ap.add_argument("--sleepq", type=int, help="昨晚睡眠质量 1（很差）–5（很好），自己的感觉，不是手表分")
    ap.add_argument("--stress", type=int, help="最近的生活/工作压力 1（很小）–5（很大）")
    ap.add_argument("--pain", type=int, help="某处疼痛 0–10（不是普通酸痛）")
    ap.add_argument("--gait", action="store_true", help="疼痛影响跑姿/让你一瘸一拐")
    ap.add_argument("--symptoms", choices=["none", "above_neck", "below_neck", "fever"], default="none")
    ap.add_argument("--red-flag", action="store_true", help="胸痛、晕厥、心悸、异常气短")
    a = ap.parse_args()

    home = gclib.coach_home()
    raw = home / "raw"
    today = gclib.parse_date(a.date) if a.date else date.today()
    plan_path = Path(a.plan) if a.plan else home / "plan" / "plan.json"
    plan = gclib.load_json(plan_path)
    if not plan:
        print("没有找到课表 %s。先用 plan_tool.py build 生成。" % plan_path)
        return 2
    S = sessions_by_date(plan)
    zones = plan.get("meta", {}).get("zones", {})
    easy_hr = (zones.get("hr", {}).get("easy") or [None, None])[1]
    s = S.get(today.isoformat(), {"type": "rest", "title": "课表里没有安排"})
    lines = ["今天 %s（%s）课表：%s%s" % (today, WD[today.weekday()], s.get("title", s.get("type")),
                                    ("（%g km）" % s["km"]) if s.get("km") else "")]
    reasons, verdict, rules = [], "照常", []

    # ---- 红旗与疼痛、症状（最先判断）
    if a.red_flag:
        print("\n".join(lines + ["结论：今天不跑，尽快就医（胸痛、晕厥、心悸、异常气短都不要硬撑）。[D1]"]))
        return 0
    if a.symptoms in ("below_neck", "fever"):
        print("\n".join(lines + ["结论：今天不跑。发热或颈部以下症状（咳嗽加重、胸闷、全身酸痛、乏力）时休息，"
                                 "症状消失后再休 1 天，回来先跑轻松。[D2]（经验法则，证据有限）"]))
        return 0
    if a.pain is not None and (a.pain >= 4 or a.gait):
        print("\n".join(lines + ["结论：今天不跑。疼痛 ≥4/10 或影响跑姿时停跑 48 小时，没有好转就去看医生；"
                                 "不要用止痛药硬跑。[D3]"]))
        return 0

    # ---- 客观指标
    days = load_days(raw)
    def vals(key, d0, d1):
        return [days[d][key] for d in sorted(days) if d0 <= d <= d1 and days[d].get(key) is not None]
    signals = []
    hrv7 = vals("hrv", today - timedelta(days=6), today)
    base = vals("hrv", today - timedelta(days=34), today - timedelta(days=7))
    hrv_low = False
    if len(hrv7) >= 4 and len(base) >= 14:
        mu, sd = st.mean(base), st.pstdev(base)
        lo, hi = mu - 0.5 * sd, mu + 0.5 * sd
        m7 = st.mean(hrv7)
        state = "低于" if m7 < lo else ("高于" if m7 > hi else "在")
        reasons.append("HRV 7 天均值 %.0f ms，个人正常范围 %.0f–%.0f → %s范围内" % (m7, lo, hi, state)
                       if state == "在" else "HRV 7 天均值 %.0f ms，%s个人正常范围 %.0f–%.0f" % (m7, state, lo, hi))
        hrv_low = m7 < lo
        if m7 > hi:
            reasons.append("  （偏高通常不是问题；如果同时很累，按低强度处理）")
    else:
        reasons.append("HRV 数据不足（需要近 7 天至少 4 晚、此前至少 14 晚）")
    rhr_t = vals("rhr", today - timedelta(days=1), today)
    rhr_b = vals("rhr", today - timedelta(days=34), today - timedelta(days=7))
    if rhr_t and len(rhr_b) >= 10:
        diff = rhr_t[-1] - st.mean(rhr_b)
        reasons.append("静息心率 %d，比基线 %.0f 高 %+.0f" % (rhr_t[-1], st.mean(rhr_b), diff))
        if diff >= 5:
            signals.append("静息心率偏高 ≥5")
    sl = days.get(today, {})
    if sl.get("sleep_h"):
        reasons.append("昨晚睡眠 %.1f 小时，睡眠分 %s" % (sl["sleep_h"], sl.get("sleep_score") or "-"))
        if sl["sleep_h"] < 6 or (sl.get("sleep_score") or 100) < 60:
            signals.append("睡眠差（<6 小时或睡眠分 <60）")
    if a.feel is not None:
        reasons.append("整体感觉 %d/5" % a.feel)
        if a.feel <= 2:
            signals.append("主观感觉差")
    if a.soreness is not None:
        reasons.append("肌肉酸痛 %d/10" % a.soreness)
        if a.soreness >= 6:
            signals.append("明显酸痛")
    if a.sleepq is not None:
        reasons.append("自评睡眠质量 %d/5" % a.sleepq)
        if a.sleepq <= 2:
            signals.append("自评睡得差")
    if a.stress is not None:
        reasons.append("压力 %d/5" % a.stress)
        if a.stress >= 4:
            signals.append("压力大")
    # 记下每天的主观状态，analyze.py 会和之前比较（主观自评比常用客观指标更敏感 [SUBJ-1]）
    if any(x is not None for x in (a.feel, a.soreness, a.sleepq, a.stress, a.pain)):
        wl = gclib.load_json(home / "wellness.json", {}) or {}
        wl[today.isoformat()] = {k: v for k, v in (("feel", a.feel), ("soreness", a.soreness), ("sleepq", a.sleepq),
                                                   ("stress", a.stress), ("pain", a.pain)) if v is not None}
        gclib.save_json(home / "wellness.json", wl)
    if a.feel is None and a.soreness is None:
        reasons.append("没有主观感受（建议先问：整体感觉 1–5？腿酸 0–10？）")
    if a.pain is not None and 0 < a.pain < 4:
        signals.append("轻微疼痛 %d/10" % a.pain)

    t = s.get("type")
    very_bad = (a.feel is not None and a.feel <= 1) or (a.soreness is not None and a.soreness >= 8)
    # ---- 漏课：最近 1–3 天的重课没做，今天是它的备用日
    moved = None
    backup = plan.get("meta", {}).get("backup", {})
    for back in (1, 2, 3):
        d0 = today - timedelta(days=back)
        s0 = S.get(d0.isoformat())
        if s0 and s0.get("type") in HARD | LONG and backup.get(DAYS[d0.weekday()]) == DAYS[today.weekday()] \
                and ran_on(raw, d0) < 0.5 * (s0.get("km") or 1) and t in ("rest", "strength", "easy", "recovery", None):
            moved = s0
            break

    if very_bad:
        verdict, rules = "休息，或只跑 20–30 分钟很轻松的跑", ["D5"]
    elif a.symptoms == "above_neck":
        verdict, rules = "只跑轻松（心率 ≤%s），缩短到平时的 50–70%%，不做强度" % (easy_hr or "轻松上限"), ["D2"]
    elif t in HARD and (hrv_low or len(signals) >= 2):
        km = s.get("km") or 8
        verdict = "主课改成轻松跑 %g km（心率 ≤%s）" % (max(5, round(km * 0.8)), easy_hr or "轻松上限")
        rules = ["D4"] if hrv_low else ["D5"]
    elif t in LONG and (hrv_low or len(signals) >= 2):
        verdict = "长距离照跑但缩短 20–30%%，全程心率不超过轻松上限 %s" % (easy_hr or "")
        rules = ["D4" if hrv_low else "D5"]
    elif moved and not hrv_low and len(signals) < 2:
        verdict = "把 %s 没做的「%s」挪到今天做（只挪这一次）" % (moved["date"], moved.get("title"))
        rules = ["D7"]
    elif t in ("easy", "recovery") and len(signals) >= 2:
        verdict, rules = "轻松跑缩短到 30–40 分钟，或休息", ["D5"]
    else:
        verdict = "照常"
        rules = ["D6"] if signals else []
    lines.append("结论：%s %s" % (verdict, ("[" + "][".join(rules) + "]") if rules else ""))
    lines.append("依据：")
    lines.extend("- " + r for r in reasons)
    if signals:
        lines.append("- 疲劳信号：%s（只有 1 项时照常训练）" % "、".join(signals))
    lines.append("之后：后面的课表不变；今天改掉的课不补。[D6]")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())

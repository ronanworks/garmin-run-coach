#!/usr/bin/env python3
"""每周复盘：计划和实际对一次账，问一次疼痛，看下周最重要的课和 P1 上限，写一条“如果……就……”预案。

  python3 weekly.py --pain 0                          # 上一个完整周（周一到周日）；疼痛 0–10 每周都要问
  python3 weekly.py --pain 3 --where 左膝 --note "周三加班没跑"
  python3 weekly.py --ifthen "如果周三加班，就把阈值课挪到周四"
  python3 weekly.py --week-of 2026-11-09              # 指定那一周（那周里的任意一天）
  python3 weekly.py --light                           # 少看数：只给 5 行
  python3 weekly.py --stamp "模型名"                  # 报告末尾写上技能版本、模型和日期

依据（references/report-templates.md 第 5 节）：
- 自我监测再加反馈、回顾目标的组合，效应约 0.42，单一技巧约 0.26 [BCT-1]；活动监测反馈的效应约 0.42 [BCT-2]；
  “如果……就……”预案另有约 0.25 的小效应 [BCT-3]。这些研究几乎都来自不怎么运动的人群，对已经很自律的跑者没被检验过。
- 干预一停，效应就下降 [BCT-4]，所以用每周一次、轻量的节奏。
- 新手跑者放弃跑步的人里，48% 是因为伤病 [BCT-5]，所以每周都问一次疼痛。
- 推送式激励消息没有带来更多运动 [BCT-6]，所以复盘只对账、写预案，不做激励。
措辞规矩：没做的课写“没做（不补）”，不写“落下了”“要补回来”这类话。
跑者说的话（疼痛、备注、预案）追加记进 ~/garmin-coach/diary.jsonl。输出：~/garmin-coach/reports/weekly_周一日期.md。
"""
import argparse
import statistics as st
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gclib  # noqa: E402
from analyze import load_days  # noqa: E402
from resume import completion  # noqa: E402

HARD = {"threshold", "interval", "mp", "long_mp", "test"}
LONG = {"long", "long_mp"}
WD = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def mean(xs):
    xs = [x for x in xs if x is not None]
    return st.mean(xs) if xs else None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--week-of", help="要复盘的那一周里的任意一天（默认上一个完整周）")
    ap.add_argument("--pain", type=int, help="这周最明显的疼痛 0–10（不是普通肌肉酸）")
    ap.add_argument("--where", help="疼痛部位")
    ap.add_argument("--gait", action="store_true", help="疼痛影响跑姿")
    ap.add_argument("--note", help="跑者对这周的一句话")
    ap.add_argument("--ifthen", help="下周的“如果……就……”预案")
    ap.add_argument("--light", action="store_true", help="少看数模式")
    ap.add_argument("--plan")
    ap.add_argument("--stamp", help="模型名称，写进报告末尾")
    ap.add_argument("--today", help="把这一天当作今天（默认系统日期）")
    ap.add_argument("--no-log", action="store_true", help="不记日记（试算、演示时用；跑者本人说的情况不要加）")
    a = ap.parse_args()

    home = gclib.coach_home()
    raw = home / "raw"
    today = gclib.parse_date(a.today) if a.today else date.today()
    if a.week_of:
        d = gclib.parse_date(a.week_of)
        mon = d - timedelta(days=d.weekday())
    else:
        mon = today - timedelta(days=today.weekday() + 7)
    sun = mon + timedelta(days=6)
    runs = gclib.list_runs(raw)
    wk_runs = [r for r in runs if mon <= r["date"] <= sun]
    plan = gclib.load_json(Path(a.plan) if a.plan else home / "plan" / "plan.json")
    diary = gclib.read_diary()
    if not a.no_log and any(x is not None for x in (a.pain, a.note, a.ifthen)):
        gclib.append_diary({"date": today.isoformat(), "kind": "weekly", "week": mon.isoformat(), "pain": a.pain,
                            "where": a.where, "gait": a.gait or None, "note": a.note, "ifthen": a.ifthen})

    L = ["# 周复盘：%s – %s\n" % (mon, sun)]
    # ---- 1. 计划 vs 实际
    planned = []
    if plan:
        for w in plan.get("weeks", []):
            for s in w.get("sessions", []):
                if mon <= gclib.parse_date(s["date"]) <= sun and s.get("km") and s.get("type") not in ("rest", "strength"):
                    planned.append(s)
    by_day = {}
    for r in wk_runs:
        by_day.setdefault(r["date"], []).append(r)
    rows, done_n, key_done, key_n = [], 0, 0, 0
    last_data = runs[-1]["date"] if runs else None
    planned_past = [s for s in planned if gclib.parse_date(s["date"]) < today
                    and not (last_data and gclib.parse_date(s["date"]) > last_data)]
    pdays = set()
    for s in sorted(planned, key=lambda x: x["date"]):
        d = gclib.parse_date(s["date"])
        pdays.add(d)
        km = sum(r["km"] for r in by_day.get(d, []))
        if d >= today or (last_data and d > last_data):
            rows.append("| %s %s | %s | %.1f | - | 还没到或还没同步 |" % (d.strftime("%m-%d"), WD[d.weekday()],
                                                                 s.get("title", s.get("type")), s["km"]))
            continue
        ratio = km / s["km"] if s["km"] else 0
        status = "完成" if ratio >= 0.8 else ("部分（%.0f%%）" % (ratio * 100) if ratio >= 0.3 else "没做（不补）")
        if ratio >= 0.8:
            done_n += 1
        if s.get("type") in HARD | LONG:
            key_n += 1
            key_done += ratio >= 0.8
        rows.append("| %s %s | %s | %.1f | %s | %s |" % (d.strftime("%m-%d"), WD[d.weekday()], s.get("title", s.get("type")),
                                                       s["km"], ("%.1f" % km) if km else "-", status))
    extra = [r for r in wk_runs if r["date"] not in pdays]
    for r in extra:
        rows.append("| %s %s | %s%s | - | %.1f | %s |" % (r["date"].strftime("%m-%d"), WD[r["date"].weekday()],
                                                       "（计划外）" if planned else "", r["name"][:20], r["km"],
                                                       "计划外" if planned else "-"))
    p_km = sum(s["km"] for s in planned_past)
    a_km = sum(r["km"] for r in wk_runs)
    if planned:
        summary = "这周%s计划 %.0f km、%d 节跑步课；实际跑了 %.1f km（%.0f%%），按计划完成 %d 节；关键课（强度和长距离）完成 %d/%d。" % (
            "到目前为止" if len(planned_past) < len(planned) else "", p_km, len(planned_past), a_km,
            a_km / p_km * 100 if p_km else 0, done_n, key_done, key_n)
    else:
        summary = "这周没有课表（或课表不覆盖这周）；实际跑了 %d 次、%.1f km。" % (len(wk_runs), a_km)
    # ---- 2. P1
    p1 = []
    for r in wk_runs:
        base = gclib.longest_before(runs, r["date"])
        if base and r["km"] >= 8 and r["km"] > base * 1.1 + 0.05:
            p1.append("%s %.1f km，比前 30 天最长 %.1f km 多 %.0f%%" % (r["date"], r["km"], base, (r["km"] / base - 1) * 100))
    # ---- 3. 疼痛
    pain_lines = []
    if a.pain is None:
        pain_lines.append("还没问疼痛：这周有没有哪里疼（不是普通肌肉酸）？0–10 分，在哪里？问完用 --pain 重新运行。")
    elif a.pain >= 4 or a.gait:
        pain_lines.append("疼痛 %d/10%s%s：停跑 48 小时，没有好转就去看医生或物理治疗师；不要吃止痛药硬跑 [D3]。"
                          "在这之前不往手表写新课。" % (a.pain, ("（%s）" % a.where) if a.where else "", "，影响跑姿" if a.gait else ""))
    elif a.pain > 0:
        pain_lines.append("疼痛 %d/10%s：可以继续，但要盯三件事——跑时不超过轻中度、第二天早上能恢复、一周比一周轻；"
                          "有一条不满足就停跑，找医生或物理治疗师。这是经验做法：疼痛监测的思路来自跟腱病研究 [INJ-5]，"
                          "不能当作其他部位的研究结论。骨头上的点状疼（按压痛、夜里也疼）直接看医生。"
                          % (a.pain, ("（%s）" % a.where) if a.where else ""))
    else:
        pain_lines.append("这周没有疼痛。")
    # ---- 4. 恢复与主观
    days = load_days(raw)
    wk = [days.get(mon + timedelta(days=i), {}) for i in range(7)]
    base_days = [days.get(mon - timedelta(days=i), {}) for i in range(1, 29)]
    rec_lines = []
    hv, hb = mean(x.get("hrv") for x in wk), mean(x.get("hrv") for x in base_days)
    if hv and hb:
        rec_lines.append("HRV 周均 %.0f ms（前 4 周 %.0f）" % (hv, hb))
    rv, rb = mean(x.get("rhr") for x in wk), mean(x.get("rhr") for x in base_days)
    if rv and rb:
        rec_lines.append("静息心率周均 %.0f（前 4 周 %.0f）" % (rv, rb))
    sv = mean(x.get("sleep_h") for x in wk)
    if sv:
        rec_lines.append("睡眠平均 %.1f 小时" % sv)
    wl = gclib.load_json(home / "wellness.json", {}) or {}
    wv = [v for k, v in wl.items() if mon <= gclib.parse_date(k) <= sun]
    if wv:
        parts = []
        for key, name in (("feel", "整体感觉"), ("soreness", "腿酸"), ("sleepq", "自评睡眠"), ("stress", "压力")):
            m = mean(v.get(key) for v in wv)
            if m is not None:
                parts.append("%s %.1f" % (name, m))
        if parts:
            rec_lines.append("自评（%d 天）：%s" % (len(wv), "，".join(parts)))
    # ---- 5. 下周
    nmon, nsun = sun + timedelta(days=1), sun + timedelta(days=7)
    nxt, cap_lines = [], []
    if plan:
        for w in plan.get("weeks", []):
            for s in w.get("sessions", []):
                d = gclib.parse_date(s["date"])
                if nmon <= d <= nsun and s.get("type") not in ("rest",):
                    nxt.append(s)
                    if s.get("km") and s.get("type") != "race":
                        base = gclib.longest_before(runs, d)
                        if base and s["km"] > base * 1.1 + 0.05:
                            cap_lines.append("%s %s 计划 %.1f km，超过 P1 上限约 %.1f km（前 30 天最长 %.1f km × 110%%）。"
                                             "建议这一次缩到上限以内，改完运行 plan_tool.py check [LOAD-3]。"
                                             % (d, s.get("title"), s["km"], base * 1.1, base))
    key_next = next((s for s in nxt if s.get("type") in LONG | HARD), None)
    # ---- 6. 完成度趋势
    comp = completion(plan, runs, sun + timedelta(days=1)) if plan else []
    # ---- 7. 上周的预案
    prev_if = [e for e in diary if e.get("kind") == "weekly" and e.get("ifthen") and e.get("week") != mon.isoformat()]
    last_if = prev_if[-1]["ifthen"] if prev_if else None
    gap = (today - runs[-1]["date"]).days if runs else None

    if a.light:
        out = ["周复盘（%s – %s，少看数模式）" % (mon, sun),
               "- " + summary,
               "- 疼痛：" + pain_lines[0],
               "- 下周最重要的课：%s" % (("%s %s" % (key_next["date"], key_next.get("title"))) if key_next else "没有课表"),
               "- 预案：%s" % (a.ifthen or "还没写。想一个下周最可能打乱计划的情况：“如果……就……”。"),
               "后面的骨架不变；没做的课不补，也不挪到下周。"]
        if cap_lines or p1:
            out.insert(4, "- 注意：" + (cap_lines[0] if cap_lines else p1[0]))
        print("\n".join(out))
        return 0

    L.append("## 计划和实际\n")
    if sun >= today or (last_data and last_data < sun):
        L.append("注意：%s（最新跑步记录 %s）。还没到或没同步的课不算进完成度；要完整对账，先运行 garmin_pull.py，等这周结束再复盘。\n"
                 % ("这周还没结束" if sun >= today else "数据可能还没同步完", last_data))
    L.append(summary)
    if rows:
        L.append("\n| 日期 | 课 | 计划 km | 实际 km | 状态 |\n|---|---|---|---|---|\n" + "\n".join(rows))
    if gap is not None and gap >= 8:
        L.append("\n已经 %d 天没有跑步记录：先运行 `resume.py`（中断后复训），不要直接接着课表跑。" % gap)
    L.append("\n## 单次跑量（P1）\n")
    L.append("\n".join("- " + x for x in p1) if p1 else "- 这周没有单次跑量超过前 30 天最长 110% 的跑 [LOAD-3]。")
    L.append("\n## 疼痛\n")
    L.extend("- " + x for x in pain_lines)
    if rec_lines:
        L.append("\n## 恢复（只记录，一项指标差不改课 [HRV-1]）\n")
        L.append("- " + "；".join(rec_lines) + "。")
    if comp:
        L.append("\n## 最近几周的完成度\n")
        L.append("- " + "、".join("第 %s 周 %.0f%%" % (r["week"], c * 100) for r, _, c in comp))
        good = sum(1 for _, _, c in comp if c >= 0.8)
        if len(comp) >= 4 and good / len(comp) < 0.8:
            L.append("- 完成计划跑量 ≥80% 的周只有 %d/%d。完成度长期偏低时，到下一个检查点按实际情况重估目标，而不是靠补课追 [ILL-3]。"
                     % (good, len(comp)))
    L.append("\n## 下周\n")
    if nxt:
        L.append("- " + "；".join("%s %s" % (WD[gclib.parse_date(s["date"]).weekday()], s.get("title", s.get("type"))) for s in nxt) + "。")
    if key_next:
        L.append("- 最重要的一节：%s %s。" % (key_next["date"], key_next.get("title")))
    L.extend("- " + x for x in cap_lines)
    L.append("\n## 预案\n")
    if last_if:
        L.append("- 上次的预案：“%s”。用上了吗？有用就留着，没用就换一个。" % last_if)
    L.append("- 下周：%s" % (("“%s”" % a.ifthen) if a.ifthen else "还没写。想一个下周最可能打乱计划的情况，写成“如果……就……”"
                                                          "（例如：如果周四加班，就把阈值课挪到周五备用日）[BCT-3]。"))
    if a.note:
        L.append("- 你的备注：%s" % a.note)
    L.append("\n后面的骨架不变；没做的课不补，也不挪到下周。")
    text = "\n".join(L) + "\n"
    if a.stamp:
        text += "\n—— 由 garmin-run-coach 技能版本 v%s 生成；模型：%s；日期：%s。\n" % (gclib.SKILL_VERSION, a.stamp, today)
    out = home / "reports" / ("weekly_%s.md" % mon)
    gclib.write_private(out, text)
    print(text)
    print("已保存：%s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())

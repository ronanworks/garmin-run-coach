#!/usr/bin/env python3
"""中断后复训：停跑多久、该怎么接回课表、生病后先过哪几关、临时想跑长一点行不行。

  python3 resume.py                                   # 自动发现停跑天数，给出复训档位和接下来两周的上限
  python3 resume.py --reason illness --severity mild  # 生病：mild 只有鼻咽症状 / moderate 有咳嗽等胸部症状 / severe 发热、全身症状
  python3 resume.py --reason illness --severity mild --checklist clear   # 静息症状清单全部为“没有”
  python3 resume.py --challenge ok                    # 自测运动后 24 小时一切正常（bad = 出现症状）
  python3 resume.py --covid below_neck                # 新冠：below_neck / above_neck
  python3 resume.py --want-km 30 [--on 2026-11-20]    # 临时想跑 30 km：按规则 P1 检查
  python3 resume.py --red-flag                        # 胸痛、晕厥、心悸、静息气促：停，就医

依据（references/daily-adjust.md “中断后复训”）：
- 停训损失有时间线：14 天约 −4% 最大摄氧量、同配速心率约 +11 bpm [DETR-1]；21 天约 −7%，56 天稳定在约 −16% [DETR-2]。
  这些是小样本研究的平均数，用来分档，个人损失要用检查跑实测。
- 生病后：IOC 2022 共识的静息症状清单 → 自测运动（平时强度的 60–70%，10–20 分钟）→ 观察 24 小时 [ILL-3]。
- 加量护栏：单次跑量不超过前 30 天最长一次的 110% [LOAD-3]（规则 P1）。停跑时 30 天窗口自然变小，长距离会被迫逐步重建。
- 各档回来跑多少量、30 天内没跑过时首周的上限，都是本技能的经验值，没有研究给出。
跑者说的情况会追加记进 ~/garmin-coach/diary.jsonl。
"""
import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gclib  # noqa: E402

FIRST_WEEK_MIN = 40  # 前 30 天一次都没跑时，首周单次最长约 40 分钟轻松跑（经验值）

CHECKLIST = ["发热或寒战", "过度疲劳、全身肌肉酸痛", "呼吸困难或气促", "胸痛、胸闷或胸部压迫感",
             "头晕，或静息时心悸、心跳过快", "中度以上咳嗽", "严重头痛", "鼻咽症状严重，或超过 3 天没有好转", "持续的肠胃症状",
             "就是觉得“状态不够去运动”"]

TIERS = [  # (最长停跑天数, 名称, 生理参考, 回来怎么跑（经验值）)
    (7, "短暂中断（≤7 天）", None,
     ["直接回到课表，漏掉的课不补 [D6]。", "回来第一次跑轻松；单次跑量按 P1 上限。"]),
    (14, "中断 8–14 天", "停训 14 天的研究中，最大摄氧量约降 4%，同配速心率约高 11 bpm [DETR-1]。",
     ["第 1 周：只跑轻松，跑量约为原计划的 50–70%；第 4 天起可以加几次 20 秒加速。",
      "第 2 周：没有不适就回到课表，先恢复一节短的强度课。",
      "这两周的长距离取“原计划长度 × 当周比例”和 P1 上限中较小的那个，不追原计划长度。",
      "第 2 周安排一次有氧检查跑，实测掉了多少。"]),
    (28, "中断 15–28 天", "停训 21 天的研究中，最大摄氧量约降 7% [DETR-2]。另外，综述认为保持一些强度、只减量，损失更小（专家意见）[DETR-3]。",
     ["第 1 周：只跑轻松，约原计划的 50%。", "第 2 周：约 70%，加一节短的强度课（例如阈值总时长减半）。",
      "第 3 周：约 85–90%；没有不适再回到课表。",
      "这几周的长距离取“原计划长度 × 当周比例”和 P1 上限中较小的那个，逐步加长。",
      "第 2 周做有氧检查跑；检查点前重新评估目标。"]),
    (10 ** 6, "长期停训（>28 天）", "停训 56 天时，最大摄氧量稳定在比训练时低约 16% 的水平 [DETR-2]。",
     ["不要接着旧课表跑。先 2–4 周轻松跑重建基础（每周 3–4 次，单次按 P1 上限）。",
      "然后重新运行 analyze.py，用新的基线重新定目标、重新生成课表（第 5、6 步）。",
      "回来第 2 周做一次有氧检查跑，第 4 周左右再做测试。"]),
]


def tier_for(days):
    for lim, name, phys, todo in TIERS:
        if days <= lim:
            return name, phys, todo
    return TIERS[-1][1:]


def completion(plan, runs, today, weeks=8):
    """最近 weeks 个已结束的课表周：每周实际跑量 / 计划跑量。"""
    rows = [r for r in gclib.plan_week_rows(plan, today) if r["end"] < today and r["planned_km"] > 0][-weeks:]
    out = []
    for r in rows:
        done = sum(x["km"] for x in runs if r["start"] <= x["date"] <= r["end"])
        out.append((r, done, min(1.0, done / r["planned_km"])))
    return out


def want_check(runs, want_km, on, today):
    """临时想跑多长：按 P1 检查 [LOAD-3]。"""
    on = gclib.parse_date(on) if on else today
    base = gclib.longest_before(runs, on)
    if not base:
        return "\n想在 %s 跑 %.1f km：前 30 天没有跑步记录，P1 没法算；首周每次最多约 %d 分钟（经验值）。" % (on, want_km, FIRST_WEEK_MIN)
    if want_km > base * 1.1 + 0.05:
        return ("\n想在 %s 跑 %.1f km：超过上限 %.1f km（前 30 天最长 %.1f km × 110%%）。研究中单次比前 30 天最长多 10%% 以上，"
                "过度使用伤风险约高 1.5–2.3 倍（群体关联，不是对你的预测）[LOAD-3]。建议改成 %.1f km 以内。"
                "生病还没过完三关的话，先不谈长距离。" % (on, want_km, base * 1.1, base, base * 1.1))
    return "\n想在 %s 跑 %.1f km：在上限 %.1f km 以内 ✓（生病的话，先过完三关）。" % (on, want_km, base * 1.1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", help="今天的日期（默认今天）")
    ap.add_argument("--reason", choices=["illness", "injury", "travel", "life"], help="停跑原因")
    ap.add_argument("--severity", choices=["mild", "moderate", "severe"], help="生病的严重程度")
    ap.add_argument("--checklist", choices=["clear", "some"], help="静息症状清单：全部没有 / 有任何一项")
    ap.add_argument("--challenge", choices=["ok", "bad"], help="自测运动 24 小时后的结果")
    ap.add_argument("--covid", choices=["above_neck", "below_neck"], help="新冠：只有颈部以上症状 / 有颈部以下症状")
    ap.add_argument("--want-km", type=float, help="临时想跑的距离（km），按 P1 检查")
    ap.add_argument("--on", help="想跑的那天（默认今天）")
    ap.add_argument("--red-flag", action="store_true", help="胸痛、晕厥、心悸、静息气促")
    ap.add_argument("--plan")
    ap.add_argument("--no-log", action="store_true", help="不记日记（试算、演示时用；跑者本人说的情况不要加）")
    a = ap.parse_args()

    home = gclib.coach_home()
    today = gclib.parse_date(a.date) if a.date else date.today()
    runs = [r for r in gclib.list_runs(home / "raw") if r["date"] <= today]
    plan = gclib.load_json(Path(a.plan) if a.plan else home / "plan" / "plan.json")
    L = []
    if not a.no_log and any((a.reason, a.severity, a.checklist, a.challenge, a.covid, a.red_flag)):
        gclib.append_diary({"date": today.isoformat(), "kind": "resume", "reason": a.reason, "severity": a.severity,
                            "checklist": a.checklist, "challenge": a.challenge, "covid": a.covid,
                            "red_flag": a.red_flag or None})

    # ---- 1. 红旗：最先判断
    if a.red_flag or a.severity == "severe" or a.challenge == "bad":
        why = ("胸痛、晕厥、心悸或静息气促" if a.red_flag else
               "发热、寒战、全身酸痛或过度疲劳这类全身症状" if a.severity == "severe" else "自测运动后出现了症状")
        L.append("结论：停止训练，先看医生（最好是运动医学科）。原因：%s。[ILL-3]" % why)
        L.append("- 全身症状要卧床休息到症状消失，再由医生决定什么时候回来；自测运动也要等医生同意。")
        L.append("- 在医生同意之前，不往手表写新课（不运行写入步骤），已经排好的课先不跑。")
        L.append("- 本技能不做诊断，也不推荐药物。")
        print("\n".join(L))
        return 0

    # ---- 2. 停跑多久
    last = runs[-1]["date"] if runs else None
    off = (today - last).days if last else None
    if off is None:
        L.append("没有跑步记录。先运行 garmin_pull.py。")
    else:
        name, phys, todo = tier_for(off)
        L.append("最后一次跑步：%s（%d 天前）。复训档位：%s。" % (last, off, name))
        L.append("- 数据显示：前 30 天%s。" % (
            ("最长一次 %.1f km" % gclib.longest_before(runs, today)) if gclib.longest_before(runs, today) else "一次都没有跑"))
        L.append(("- 研究表明：%s" % phys) if phys else "- 7 天以内的中断没有专门研究；按本技能的经验做法，直接接回课表。")

    # ---- 3. 生病后的关卡（IOC 2022 共识 [ILL-3]）
    if a.reason == "illness" or a.severity or a.covid:
        L.append("\n生病后回来，按顺序过三关（IOC 2022 共识；具体步骤是专家意见，不是试验结论）[ILL-3]：")
        if a.covid == "below_neck":
            L.append("- 新冠有颈部以下症状（发热、气促、胸痛、心悸）：至少完全休息 5 天，满 10 天后才恢复正常训练 [ILL-4]（经验做法）。")
        elif a.covid == "above_neck":
            L.append("- 新冠只有颈部以上症状：至少 5 天后再开始逐步恢复 [ILL-4]（经验做法）。")
        if a.checklist != "clear":
            L.append("1. 每天早上先过静息症状清单（安静时有没有下面任何一项）：")
            L.extend("   - " + x for x in CHECKLIST)
            if a.checklist == "some":
                L.append("结论：今天不跑。症状消失前只做日常活动；一周内没有好转，或症状好了又复发，去看医生 [ILL-3]。")
            else:
                L.append("   有任何一项就今天不练；全部没有，再用 --checklist clear 重新运行，进入第 2 关（自测运动）。")
            if a.want_km:
                L.append(want_check(runs, a.want_km, a.on, today))
            print("\n".join(L))
            return 0
        elif a.challenge is None:
            L.append("1. 静息症状清单：全部没有 ✓")
            L.append("2. 今天做一次自测运动：热身后快走或慢跑 10–20 分钟，用力程度约为平时的 60–70%。"
                     "出现明显疲劳、气促或心跳异常就停。")
            L.append("3. 之后 24 小时留意：症状有没有回来、心率恢复是不是变慢、尿色有没有变深。"
                     "明天用 --challenge ok 或 --challenge bad 再运行。")
            if a.want_km:
                L.append(want_check(runs, a.want_km, a.on, today))
            print("\n".join(L))
            return 0
        else:
            L.append("1. 静息症状清单：全部没有 ✓\n2. 自测运动 24 小时后正常 ✓\n3. 下面按档位逐步加量。")

    # ---- 4. 回来怎么跑（各档的量是经验值）
    if off is not None:
        L.append("\n接下来怎么跑（各档回来跑多少量没有研究给出，下面是本技能的经验做法）：")
        L.extend("- " + x for x in todo)
        if a.reason == "injury":
            L.append("- 伤后回来：先由医生或物理治疗师评估，按他们给的进度来。常见的经验做法是盯三件事：跑时疼痛不超过轻中度、"
                     "第二天早上恢复、一周比一周轻（这个思路来自跟腱病研究 [INJ-5]，不是其他伤病的研究结论）。"
                     "骨应力损伤要求跑时、跑后和第二天都没有症状，不能套用这条。")

    # ---- 5. P1 上限：接下来 14 天的课
    cap_base = gclib.longest_before(runs, today + timedelta(days=1))
    if cap_base:
        L.append("\n单次跑量上限（P1）：现在约 %.1f km（前 30 天最长 %.1f km × 110%%）[LOAD-3]。这个上限会随你回来后跑的距离逐步放开。"
                 % (cap_base * 1.1, cap_base))
    else:
        L.append("\n前 30 天没有跑步，P1 没有参照：首周每次最多约 %d 分钟轻松跑（本技能的经验值），之后按 P1 逐步加长。"
                 % FIRST_WEEK_MIN)
    if plan and cap_base:
        over = []
        for w in plan.get("weeks", []):
            for s in w.get("sessions", []):
                d = gclib.parse_date(s["date"])
                if today <= d <= today + timedelta(days=13) and s.get("km") and s.get("type") not in ("race",) \
                        and s["km"] > cap_base * 1.1 + 0.05:
                    over.append("%s %s（%.1f km）→ 建议不超过 %.1f km" % (d, s.get("title", s.get("type")), s["km"], cap_base * 1.1))
        if over:
            L.append("课表里接下来 14 天超过上限的课（只改这几次，其他课不动）：")
            L.extend("- " + x for x in over)
    if plan and off and off > 7 and (plan.get("meta") or {}).get("check_pace"):
        L.append("有氧检查跑：用原来的固定配速 %s、同一路线；跑完运行 `hrpace.py --run 活动ID`，看同配速心率比停跑前高多少"
                 "（和个人波动比，波动以内就说“不明确”）[HRP-1]。" % plan["meta"]["check_pace"])

    if a.want_km:
        L.append(want_check(runs, a.want_km, a.on, today))

    # ---- 7. 课表完成度与目标
    if plan and runs:
        comp = completion(plan, runs, today)
        if len(comp) >= 3:
            good = sum(1 for _, _, c in comp if c >= 0.8)
            L.append("\n最近 %d 个课表周：完成计划跑量 ≥80%% 的有 %d 周（%s）。" % (
                len(comp), good, "、".join("%.0f%%" % (c * 100) for _, _, c in comp)))
            if good / len(comp) < 0.8:
                L.append("- 完成度偏低。IOC 共识引用的一项运动员队列里，完成 80% 以上计划周的人达成目标的可能性高约 7 倍 [ILL-3]；"
                         "这是群体关联，不是对你的预测。建议在下一个检查点重新评估目标，而不是靠补课追回来。")
        race = ((plan.get("meta") or {}).get("race") or {}).get("date")
        if race and (a.reason == "illness" or a.severity):
            dd = (gclib.parse_date(race) - today).days
            if 0 <= dd <= 14:
                L.append("- 离比赛 %d 天。共识提到，赛前 8–12 天内的感染会降低按时起跑的可能 [ILL-3]；要不要参赛，等症状和自测结果出来再定，有疑问先问医生。" % dd)
    L.append("\n原则：漏掉的课不补，后面的骨架不动；只调整回来这几周的量。")
    print("\n".join(L))
    return 0


if __name__ == "__main__":
    sys.exit(main())

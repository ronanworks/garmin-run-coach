#!/usr/bin/env python3
"""补给与补水：出汗率测试、比赛补给计划、肠道训练进阶。

  python3 fuel.py sweat --pre 64.2 --post 63.3 --drink 0.4 --minutes 70 [--urine 0] [--temp 18]
      # 称重法出汗率：跑前跑后都在排尿后、不穿衣服（或同一身干衣服）称重；结果记到 ~/garmin-coach/sweat_tests.json
  python3 fuel.py plan --goal 3:30:00 --km 42.195 --weight 64 [--carb 60] [--gel 25] [--sweat 1.0]
      # 比赛补给计划：每小时碳水、能量胶时间点和公里点、每小时补水、赛前碳水
  python3 fuel.py gut --target 60 --long-runs 8
      # 肠道训练：长距离里逐步把每小时碳水加到比赛目标

依据（references/evidence.md）：
- 每小时碳水：比赛 1–2.5 小时约 30–60 g/h；2–3 小时单一来源最高约 60 g/h；超过约 2.5 小时、练过的人可到 90 g/h（葡萄糖+果糖）[FUEL-1][FUEL-2]。
- 按计划补给（约 60 g/h 碳水 + 约 0.75 L/h 水）的业余全马跑者，比自选补给快约 11 分钟 [FUEL-5]。
- 两周肠道训练让运动中胃肠症状减少约 60% [FUEL-6]。耐力项目运动员的出汗率平均 1.28 ± 0.57 L/h，个体差异很大 [FUEL-7]。
- 补水：口渴就喝，不要喝到体重比赛前还重；体重丢失尽量不超过约 2%（两份建议之间有张力，见 [FUEL-4]）。
- 赛前 36–48 小时碳水 10–12 g/kg/天；赛前 1–4 小时吃 1–4 g/kg [FUEL-2]。
有糖尿病、胃肠疾病或肾病的人，先问医生。
"""
import argparse
import math
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gclib  # noqa: E402
from gclib import fmt_dur, parse_time  # noqa: E402


def sweat(a):
    hours = a.minutes / 60.0
    loss = a.pre - a.post + a.drink - (a.urine or 0)  # kg ≈ L
    rate = loss / hours
    pct = (a.pre - a.post) / a.pre * 100
    print("出汗率约 %.2f L/h（%d 分钟共出汗约 %.2f L）；这次体重下降 %.1f%%。" % (rate, a.minutes, loss, pct))
    print("参考：耐力项目运动员平均约 1.28 ± 0.57 L/h，个体差异很大 [FUEL-7]。")
    if pct > 2:
        print("体重下降超过 2%：这种天气下这次喝少了，比赛时可以按 plan 命令算的量补水 [FUEL-4]。")
    elif a.post > a.pre:
        print("跑后比跑前还重：喝多了。长时间比赛里喝水过多有低钠风险，口渴再喝 [FUEL-4]。")
    home = gclib.coach_home()
    log = gclib.load_json(home / "sweat_tests.json", []) or []
    log.append({"date": date.today().isoformat(), "rate_l_h": round(rate, 2), "minutes": a.minutes,
                "mass_loss_pct": round(pct, 2), "temp_c": a.temp})
    gclib.save_json(home / "sweat_tests.json", log)
    print("已记录（共 %d 次）。不同气温下多测几次，比赛时用最接近比赛天气的那次。" % len(log))


def carb_target(hours, trained):
    if hours < 1.25:
        return 0, "1 小时左右的比赛：可以不吃，或用含糖饮料漱口 [FUEL-1]"
    if hours <= 2.5:
        return 45, "1–2.5 小时：每小时 30–60 g [FUEL-2]"
    if trained:
        return 75, "超过 2.5 小时、肠胃练过：每小时 60–90 g，需要葡萄糖+果糖的配方 [FUEL-1]"
    return 60, "超过 2.5 小时：先按每小时约 60 g；练过之后才往 90 g 加 [FUEL-1][FUEL-5]"


def plan(a):
    goal_s = parse_time(a.goal)
    hours = goal_s / 3600.0
    g_h, why = (a.carb, "你指定的目标") if a.carb else carb_target(hours, a.trained)
    print("比赛 %.1f km，目标 %s（约 %.1f 小时）。体重 %.1f kg。" % (a.km, a.goal, hours, a.weight))
    print("\n1. 每小时碳水：%d g（%s）。" % (g_h, why))
    if g_h:
        every = max(15, int(60 * a.gel / g_h))  # 分钟
        times = list(range(a.first, int(goal_s / 60) - 10, every))
        pace_s = goal_s / a.km
        print("   每支能量胶约 %d g 碳水 → 约每 %d 分钟一支，共 %d 支：" % (a.gel, every, len(times)))
        print("   " + "、".join("%s（约 %.0f km）" % (fmt_dur(m * 60), m * 60 / pace_s) for m in times))
        print("   第一支在约 %d 分钟吃，不要等饿了再吃；用训练里试过的同款 [FUEL-1]。" % a.first)
    allow = 0.02 * a.weight / hours  # 2% 体重摊到每小时
    if a.sweat:
        need = max(0.0, a.sweat - allow)
        drink = min(need, 0.8)
        print("\n2. 补水：出汗率 %.2f L/h，允许体重下降约 2%%（%.1f kg）→ 每小时约喝 %.2f L（每个补给站约 %.0f ml，按 %d km 一个站）。"
              % (a.sweat, 0.02 * a.weight, drink, drink * 1000 / max(1.0, a.km / a.aid / hours), a.aid))
        print("   口渴就喝、不渴别硬灌；比赛中体重不应该比赛前还重 [FUEL-4]。")
    else:
        print("\n2. 补水：没有出汗率数据。先用 sweat 命令在接近比赛天气的长距离里测一次；在那之前，口渴就喝 [FUEL-4]。")
    print("\n3. 赛前：")
    if hours >= 1.5:
        print("   - 赛前 36–48 小时：每天碳水约 %d–%d g（10–12 g/kg），用平时吃惯的低纤维食物 [FUEL-2]。"
              % (10 * a.weight, 12 * a.weight))
    if hours >= 1.0:
        print("   - 赛前 1–4 小时：碳水约 %d–%d g（1–4 g/kg，离比赛越近吃得越少）[FUEL-2]。" % (a.weight, 4 * a.weight))
    else:
        print("   - 比赛不到 1 小时：研究里的赛前碳水建议针对 1 小时以上的运动 [FUEL-2]；赛前 2–3 小时正常吃一顿熟悉、好消化的饭即可（经验做法）。")
    if a.km >= 21:
        print("   - 按计划补给的业余全马跑者比自选补给快约 11 分钟（3:38 vs 3:49）[FUEL-5]，但前提是比赛前练过同样的吃法。")


def gut(a):
    start = 30 if a.target <= 60 else 45
    steps = max(1, a.long_runs - 2)  # 赛前最后 2 次长距离按目标量演练
    print("肠道训练：%d 次长距离（90 分钟以上）里，把每小时碳水从 %d g 逐步加到 %d g。加量节奏是经验做法；"
          "反复练习本身能减少胃肠症状有研究支持 [FUEL-6]。" % (a.long_runs, start, a.target))
    for i in range(a.long_runs):
        g = start + (a.target - start) * min(1.0, i / steps)
        print("  第 %d 次：每小时约 %d g%s" % (i + 1, round(g / 5) * 5, "（比赛演练：同款补给、同样时间点）" if i >= steps else ""))
    print("两周反复练习让运动中的胃肠症状减少约 60%（25 名耐力跑者的随机研究）[FUEL-6]。有腹痛、腹泻就退回上一档。")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sweat")
    s.add_argument("--pre", type=float, required=True, help="跑前体重 kg")
    s.add_argument("--post", type=float, required=True, help="跑后体重 kg")
    s.add_argument("--drink", type=float, default=0.0, help="跑步中喝了多少升")
    s.add_argument("--urine", type=float, default=0.0, help="跑步中排尿多少升（没有填 0）")
    s.add_argument("--minutes", type=float, required=True)
    s.add_argument("--temp", type=float, help="当时气温 °C（方便以后挑接近比赛天气的那次）")
    p = sub.add_parser("plan")
    p.add_argument("--goal", required=True)
    p.add_argument("--km", type=float, default=42.195)
    p.add_argument("--weight", type=float, required=True)
    p.add_argument("--carb", type=int, help="每小时碳水 g（不填按比赛时长自动选）")
    p.add_argument("--trained", action="store_true", help="肠胃已经练过高碳水")
    p.add_argument("--gel", type=float, default=25, help="每支能量胶的碳水 g")
    p.add_argument("--first", type=int, default=35, help="第一支胶在第几分钟")
    p.add_argument("--sweat", type=float, help="出汗率 L/h（sweat 命令测得）")
    p.add_argument("--aid", type=float, default=5, help="补给站间隔 km")
    g = sub.add_parser("gut")
    g.add_argument("--target", type=int, default=60)
    g.add_argument("--long-runs", type=int, default=8)
    a = ap.parse_args()
    {"sweat": sweat, "plan": plan, "gut": gut}[a.cmd](a)
    return 0


if __name__ == "__main__":
    sys.exit(main())

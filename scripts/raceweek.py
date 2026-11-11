#!/usr/bin/env python3
"""比赛周清单：咖啡因用量、按距离的热身课、赛前睡眠、止痛药和心脏提醒。

  python3 raceweek.py --weight 62 --distance 42.195 --start 08:00
  python3 raceweek.py --weight 62 --distance 10 --start 19:30 --goal 45:00 --write   # 同时生成热身课，写进手表用
  python3 raceweek.py --weight 62 --distance 21.1 --caffeine 7                         # 超过 6 mg/kg 会被拒绝

依据（references/goal-setting.md 第 7 节）：
- 咖啡因：3–6 mg/kg 稳定有效，最低可以到约 2 mg/kg；>6 mg/kg 没有必要；常用在开跑前约 60 分钟 [CAF-1][CAF-2]。
  低剂量（约 200 mg 以内）几乎没有副作用 [CAF-3]。任何补剂都先在训练里试过，并注意兴奋剂污染 [SUPP-1]。
- 热身：5K/10K 做几段短的比赛配速比低强度热身更好 [WARM-1]；动态拉伸改善跑步经济性，60 秒静态拉伸没有 [WARM-2]；
  热身让体温升得太多会伤耐力表现 [WARM-3]。半马、全马的短热身没有试验，是经验做法。
- 睡眠：重要比赛前睡不好很常见 [SLEEP-4]；一晚睡不好主要伤“比平时早起很多”和通宵，上午的比赛影响小 [SLEEP-5]；
  赛前几晚多睡有帮助 [SLEEP-6]。
- 止痛药：赛前吃止痛药的跑者不良事件约高 5 倍 [MED-1]。心脏骤停罕见（每 10 万完赛者约 0.6 例）[MED-2]。
本脚本不推荐任何药物；孕期、心律失常、高血压、焦虑症或正在用药的人，咖啡因先问医生。
"""
import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gclib  # noqa: E402
from gclib import fmt_pace, parse_time  # noqa: E402

CAF_LO, CAF_HI, CAF_MAX = 3.0, 6.0, 6.0


def warmup(distance_km, goal_pace_s=None, hot=False):
    """按距离返回 (说明文字, 手表步骤)。步骤格式和 plan_tool 一致。"""
    def st(sec, kind, note, pace=None):
        s = {"kind": kind, "sec": int(sec), "note": note}
        if pace:
            s["pace"] = list(pace)
        return s
    if distance_km <= 12:
        rp = None
        if goal_pace_s:
            rp = (fmt_pace(1000 / (goal_pace_s - 5)), fmt_pace(1000 / (goal_pace_s + 5)))
        easy_min = 6 if hot else 10
        steps = [st(easy_min * 60, "warmup", "轻松慢跑"),
                 st(300, "rest", "动态拉伸：摆腿、弓步走、高抬腿，不做长时间静态拉伸"),
                 {"kind": "repeat", "times": 3, "steps": [st(45, "work", "比赛配速，放松", rp), st(60, "recover", "走或慢跑")]},
                 st(180, "cooldown", "慢走到起点，离开跑还有 5–10 分钟时结束")]
        text = ["约 %d 分钟轻松跑 → 5 分钟动态拉伸 → 3 段约 45 秒（约 250 m）比赛配速 → 走到起点；开跑前 5–10 分钟结束 [WARM-1][WARM-2]。"
                % easy_min]
        if hot:
            text.append("天热：轻松跑缩短，热身做完别在太阳下等 [WARM-3]。")
        return text, steps
    jog = 5 if (hot or distance_km > 30) else 8
    steps = [st(jog * 60, "warmup", "很轻松地慢跑或快走"),
             st(300, "rest", "动态活动：摆腿、弓步走、高抬腿"),
             {"kind": "repeat", "times": 2, "steps": [st(15, "work", "轻快几步，不冲刺"), st(45, "recover", "走")]}]
    text = ["%d 分钟很轻松的慢跑或快走 → 5 分钟动态活动 → 2 次 15 秒轻快跑；前 1–2 km 本身就是热身，按计划配速的慢端起跑。"
            "这部分没有试验支持，是经验做法；热身让体温升太多反而伤耐力表现 [WARM-3]。" % jog]
    if hot:
        text.append("天热：热身减到最少，在阴凉处等。")
    return text, steps


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--weight", type=float, required=True, help="体重 kg")
    ap.add_argument("--distance", type=float, required=True, help="比赛距离 km")
    ap.add_argument("--start", default="08:00", help="开跑时间 HH:MM")
    ap.add_argument("--date", help="比赛日期 YYYY-MM-DD（写手表时需要；默认读课表里的比赛日）")
    ap.add_argument("--goal", help="目标成绩，例如 45:00；5K/10K 的热身配速用")
    ap.add_argument("--caffeine", type=float, help="你打算用的咖啡因 mg/kg（不填就给 3–6 mg/kg 的范围）")
    ap.add_argument("--hot", action="store_true", help="预计比赛时 WBGT ≥ 20 °C（weather.py --race-date 能查）")
    ap.add_argument("--write", action="store_true", help="生成热身课 ~/garmin-coach/plan/warmup_plan.json")
    a = ap.parse_args()

    home = gclib.coach_home()
    L = ["# 比赛周清单（%.1f km，开跑 %s，体重 %.1f kg）\n" % (a.distance, a.start, a.weight)]

    # 1. 咖啡因
    L.append("## 1. 咖啡因（可选）")
    if a.caffeine is not None and a.caffeine > CAF_MAX:
        L.append("- 你填的 %.1f mg/kg 超过 6 mg/kg：研究里更高的剂量没有额外好处，副作用更多 [CAF-1]。本清单不给这个剂量，请改在 6 mg/kg 以内。"
                 % a.caffeine)
    else:
        lo, hi = CAF_LO * a.weight, CAF_HI * a.weight
        try:
            t0 = datetime.strptime(a.start, "%H:%M")
            when = (t0 - timedelta(minutes=60)).strftime("%H:%M")
        except ValueError:
            when = "开跑前约 60 分钟"
        if a.caffeine is not None:
            L.append("- 你的计划：%.1f mg/kg ≈ %.0f mg，%s 左右服用。" % (a.caffeine, a.caffeine * a.weight, when))
        L.append("- 研究里稳定有效的范围：3–6 mg/kg，也就是 %.0f–%.0f mg；常用在开跑前约 60 分钟（%s 左右）[CAF-1]。"
                 "4–6 mg/kg 的效果约是 ≤3 mg/kg 的两倍 [CAF-2]。" % (lo, hi, when))
        L.append("- 低剂量选项：2–3 mg/kg（%.0f–%.0f mg，约 200 mg 以内），几乎没有副作用 [CAF-3]。" % (2 * a.weight, 3 * a.weight))
        L.append("- 必须先在一次长距离或测试里用同样的剂量和时间试过；比赛日不尝试新东西 [SUPP-1]。")
        L.append("- 把能量胶里的咖啡因算进去（看包装）。全马途中要不要再追加、追加多少：没有验证过的方案，不明确。")
        L.append("- 孕期、心律失常、高血压、焦虑症，或正在吃药的人，先问医生。下午或晚上的比赛，留意它对当晚睡眠的影响。")

    # 2. 热身
    goal_pace = None
    if a.goal:
        try:
            goal_pace = parse_time(a.goal) / a.distance
        except ValueError:
            pass
    text, steps = warmup(a.distance, goal_pace, a.hot)
    L.append("\n## 2. 热身")
    L.extend("- " + t for t in text)

    # 3. 睡眠
    L.append("\n## 3. 睡眠")
    L.append("- 赛前一晚睡不好很常见：283 名精英运动员中 64% 在重要比赛前至少有一次睡不好，多是入睡困难 [SLEEP-4]。不用慌。")
    L.append("- 一晚睡不好，主要伤在通宵不睡和“比平时早起很多”；上午进行的项目基本不受影响 [SLEEP-5]。")
    L.append("- 真正能做的在前面：赛前 3–5 晚每晚多躺约 1 小时（小样本研究里多睡 3 晚计时赛更快）[SLEEP-6]；"
             "比赛日起床时间往前挪的话，这一周就逐步早睡早起。")
    L.append("- 不要为了睡着临时尝试安眠药或没用过的助眠产品；有长期失眠的找医生。")

    # 4. 止痛药与心脏
    L.append("\n## 4. 安全提醒")
    L.append("- 赛前和比赛中不要吃布洛芬、阿司匹林这类止痛药（医生开的除外）：一项 3,913 人的马拉松队列中，赛前吃止痛药的人不良事件约高 5 倍，"
             "9 例住院全在服药者中 [MED-1]。医生开的处方药不要自己停。")
    L.append("- 心脏骤停很少见（2010–2023 年每 10 万完赛者约 0.6 例，全马约 1.0 例），旁观者心肺复苏是存活关键 [MED-2]。"
             "运动时出现过胸痛、不明原因晕厥、心悸或异常气促，或者有心脏病、家族里有人年轻时猝死：赛前先看医生。")
    L.append("- 比赛中胸痛、胸闷、头晕、心慌：立即停下，找最近的医疗点。记下赛道上医疗站和 AED 的位置。")

    # 5. 其他
    L.append("\n## 5. 别忘了")
    L.append("- 补给：`fuel.py plan`（碳水、补水、赛前碳水）；天气：`weather.py --race-date`（按 WBGT 放慢目标）[HEAT-4]。")
    L.append("- 比赛周不加新训练，不换新鞋、新胶；只要没有 D1–D3 的情况，不按 HRV 或睡眠改课 [D6]。")
    L.append("- 以上剂量和做法是群体研究或经验，不是对你个人的保证。")

    if a.write:
        rd = a.date
        if not rd:
            plan = gclib.load_json(home / "plan" / "plan.json", {}) or {}
            rd = ((plan.get("meta") or {}).get("race") or {}).get("date")
        if not rd:
            L.append("\n没有比赛日期：加 --date 再运行 --write。")
        else:
            d = gclib.parse_date(rd)
            mon = d - timedelta(days=d.weekday())
            title = "赛前热身 %s" % ("5K/10K" if a.distance <= 12 else ("半马" if a.distance <= 25 else "全马"))
            wp = {"meta": {"name_prefix": "GRC·"},
                  "weeks": [{"week": 1, "start": mon.isoformat(), "phase": "比赛周",
                             "sessions": [{"date": d.isoformat(), "type": "shakeout", "title": title, "km": 2, "steps": steps}]}]}
            out = home / "plan" / "warmup_plan.json"
            gclib.save_json(out, wp)
            L.append("\n已生成热身课 %s（%s）。写进手表：`plan_to_garmin.py --plan %s`，然后按第 7 步预览、确认、写入。"
                     "比赛日开跑前在手表上先跑这节课，再切换到比赛。" % (out, d, out))
    print("\n".join(L))
    return 0


if __name__ == "__main__":
    sys.exit(main())

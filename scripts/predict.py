#!/usr/bin/env python3
"""多模型比赛成绩预测。每个模型都标明出处和适用范围，结果给区间，不给单一“承诺值”。

  python3 predict.py --race 10000 44:00 --target 42195
  python3 predict.py --race 21097.5 1:35:00 --weekly-km 60 --train-pace 5:15 --target 42195
  python3 predict.py --cs 4.30 --target 42195
  python3 predict.py --goal 3:20:00 --train-pace 5:15     # 反推：Tanda 模型需要多少周跑量

模型（详见 references/evidence.md）：
- Riegel：T2 = T1·(D2/D1)^k。马拉松默认 k=1.07（Vickers & Vertosick 2016 用 1.07 作基准，
  并发现 1.06 让约一半业余跑者的马拉松预测快 10 分钟以上）。[PRED-1][PRED-2]
- VDOT：Daniels & Gilbert 方程的等效成绩。[PRED-3]
- Tanda 2011：Pm(s/km) = 17.1 + 140·exp(−0.0053·K) + 0.55·P；K=赛前 8 周平均周跑量，
  P=同期平均训练配速。样本为完赛 2:47–3:36、周跑量 40–111 km 的跑者，SEE ≈ 4 分钟。[PRED-4]
- 临界速度 CS：由训练中最快的 400/800/5000 m 拟合 D = CS·t + D′；业余跑者马拉松平均约为
  CS 的 84.8%（完赛约 150 分钟者 93.0%，300–360 分钟者 78.9%），中间按完赛时间线性插值（插值是本工具的近似）。[PRED-5]
"""
import argparse
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gclib import fmt_dur, fmt_pace, parse_pace, parse_time  # noqa: E402
import zones  # noqa: E402

MARATHON = 42195.0


def riegel(t1, d1, d2, k=1.06):
    return t1 * (d2 / d1) ** k


def vdot_equiv(d1, t1, d2):
    return zones.race_time_from_vdot(zones.vdot_from_race(d1, t1), d2)


def tanda(weekly_km, train_pace_s):
    """返回 (马拉松用时秒, 是否在原研究样本范围内)。"""
    pm = 17.1 + 140.0 * math.exp(-0.0053 * weekly_km) + 0.55 * train_pace_s
    in_range = 40.4 <= weekly_km <= 110.7 and 253.3 <= train_pace_s <= 330.6
    return pm * 42.195, in_range


def tanda_required_km(goal_s, train_pace_s):
    """Tanda 反推：在给定平均训练配速下，需要多少周跑量。不可能时返回 None。"""
    pm = goal_s / 42.195
    x = (pm - 17.1 - 0.55 * train_pace_s) / 140.0
    if x <= 0:
        return None
    return -math.log(x) / 0.0053


def cs_fit(points):
    """points: [(用时秒, 距离米), ...]，至少 2 个。返回 (CS m/s, D′ m, R²)。"""
    pts = [(t, d) for t, d in points if t and d]
    if len(pts) < 2:
        return None
    n = len(pts)
    mt = sum(t for t, _ in pts) / n
    md = sum(d for _, d in pts) / n
    sxx = sum((t - mt) ** 2 for t, _ in pts)
    if sxx == 0:
        return None
    cs = sum((t - mt) * (d - md) for t, d in pts) / sxx
    dp = md - cs * mt
    ss_tot = sum((d - md) ** 2 for _, d in pts)
    ss_res = sum((d - (cs * t + dp)) ** 2 for t, d in pts)
    r2 = 1 - ss_res / ss_tot if ss_tot else 1.0
    return cs, dp, r2


def cs_marathon(cs):
    """按完赛时间在 150 分钟（93.0%）与 330 分钟（78.9%）之间插值，迭代求解。"""
    t = MARATHON / (0.85 * cs) / 60
    for _ in range(50):
        frac = 0.930 + (min(max(t, 150), 330) - 150) * (0.789 - 0.930) / 180
        t_new = MARATHON / (frac * cs) / 60
        if abs(t_new - t) < 0.01:
            break
        t = t_new
    return t * 60, frac


def predict_all(target_m, races=(), weekly_km=None, train_pace_s=None, cs=None):
    """races: [(距离米, 用时秒, 说明)]。返回模型结果列表。"""
    out = []
    is_mar = abs(target_m - MARATHON) < 500
    for d, t, label in races:
        if d >= target_m * 0.98:
            continue
        k = 1.07 if is_mar else 1.06
        out.append({"model": "Riegel k=%.2f" % k, "basis": label, "time_s": riegel(t, d, target_m, k),
                    "note": "马拉松用 1.07；短距离成绩推全马通常偏乐观" if is_mar else ""})
        out.append({"model": "VDOT 等效", "basis": label, "time_s": vdot_equiv(d, t, target_m),
                    "note": "需要足够的长距离训练才能兑现" if is_mar else ""})
    if is_mar and weekly_km and train_pace_s:
        ts, ok = tanda(weekly_km, train_pace_s)
        out.append({"model": "Tanda 2011（训练量）", "basis": "周跑量 %.0f km，训练配速 %s" % (weekly_km, fmt_pace(1000 / train_pace_s)),
                    "time_s": ts, "note": ("" if ok else "超出原研究范围，仅供参考；") + "误差约 ±4 分钟"})
    if is_mar and cs:
        ts, frac = cs_marathon(cs)
        out.append({"model": "临界速度 CS", "basis": "CS %.2f m/s（%s/km）" % (cs, fmt_pace(cs)), "time_s": ts,
                    "note": "按 %.0f%% CS；前 16 km 不宜快于 94%% CS（%s/km）" % (frac * 100, fmt_pace(0.94 * cs))})
    return out


def summarize(results):
    ts = sorted(r["time_s"] for r in results)
    if not ts:
        return None
    mid = ts[len(ts) // 2] if len(ts) % 2 else (ts[len(ts) // 2 - 1] + ts[len(ts) // 2]) / 2
    return {"min": ts[0], "median": mid, "max": ts[-1], "n": len(ts)}


def main():
    ap = argparse.ArgumentParser(description="多模型成绩预测")
    ap.add_argument("--race", nargs=2, action="append", metavar=("距离米", "用时"), default=[])
    ap.add_argument("--target", type=float, default=MARATHON)
    ap.add_argument("--weekly-km", type=float)
    ap.add_argument("--train-pace", help="赛前 8 周平均训练配速，如 5:15")
    ap.add_argument("--cs", type=float, help="临界速度 m/s")
    ap.add_argument("--goal", help="反推 Tanda 需要的周跑量，如 3:20:00")
    a = ap.parse_args()
    tp = parse_pace(a.train_pace) if a.train_pace else None
    if a.goal and tp:
        k = tanda_required_km(parse_time(a.goal), tp)
        print("Tanda 反推：训练配速 %s 时，%s 大约需要赛前 8 周平均 %s km/周" %
              (a.train_pace, a.goal, ("%.0f" % k) if k else "（这个配速下达不到）"))
    races = [(float(d), parse_time(t), "%s m 用时 %s" % (d, t)) for d, t in a.race]
    res = predict_all(a.target, races, a.weekly_km, tp, a.cs)
    for r in res:
        print("%-18s %-28s %s  %s" % (r["model"], r["basis"], fmt_dur(r["time_s"]), r["note"]))
    s = summarize(res)
    if s:
        print("区间 %s – %s，中位数 %s（%d 个模型）" % (fmt_dur(s["min"]), fmt_dur(s["max"]), fmt_dur(s["median"]), s["n"]))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""心率区间、强度阈值与 VDOT 配速表。

  python3 zones.py --hrmax 186 --lthr 168
  python3 zones.py --race 10000 44:00        # 由比赛成绩算 VDOT 和各强度配速
  python3 zones.py --hrmax 188               # 只有最大心率时的粗略估计

依据（详见 references/evidence.md）：
- 三区模型：区 1 低于第一乳酸/通气阈（LT1），区 2 在两阈之间，区 3 高于第二阈（LT2）。[TID-1]
- 只有 LTHR 时：LT2 = LTHR；LT1 取 Friel 跑步区间 Z2 上限 = 89% LTHR（教练实践，C 级）。[ZONE-2]
- 只有最大心率时：用挪威奥委会 2024 强度量表 I1–I5（55/72/82/87/92/100 %HRmax），
  LT1≈I2/I3 交界 82%，LT2≈I3/I4 交界 87%。个体差异大，只能当起点。[ZONE-1]
- VDOT：Daniels & Gilbert (1979) 的两条方程；E/M/T/I/R 配速的百分比取值用来复现 Daniels 公开的配速表（C 级）。[PACE-1]
"""
import argparse
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gclib import fmt_dur, fmt_pace, parse_time  # noqa: E402

OLT_SCALE = [0.55, 0.72, 0.82, 0.87, 0.92, 1.00]      # 挪威奥委会 I1–I5 的 %HRmax 边界
FRIEL_LTHR = [0.85, 0.90, 0.95, 1.00, 1.03, 1.06]     # Friel Z1|Z2|Z3|Z4|Z5a|Z5b|Z5c 的边界（%LTHR）


# ---------------------------------------------------------------- VDOT（Daniels & Gilbert 1979）

def vo2_of_speed(v_m_min: float) -> float:
    return -4.60 + 0.182258 * v_m_min + 0.000104 * v_m_min ** 2


def frac_of_duration(t_min: float) -> float:
    return 0.8 + 0.1894393 * math.exp(-0.012778 * t_min) + 0.2989558 * math.exp(-0.1932605 * t_min)


def vdot_from_race(dist_m: float, time_s: float) -> float:
    t = time_s / 60.0
    return vo2_of_speed(dist_m / t) / frac_of_duration(t)


def speed_for_vo2(vo2: float) -> float:
    """解 0.000104 v² + 0.182258 v − (4.60 + VO2) = 0，返回 m/min。"""
    a, b, c = 0.000104, 0.182258, -(4.60 + vo2)
    return (-b + math.sqrt(b * b - 4 * a * c)) / (2 * a)


def race_time_from_vdot(vdot: float, dist_m: float) -> float:
    lo, hi = dist_m / 600.0 * 60, dist_m / 60.0 * 60  # 10 m/s 到 1 m/s 之间二分
    for _ in range(100):
        mid = (lo + hi) / 2
        if vdot_from_race(dist_m, mid) > vdot:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def pace_table(vdot: float) -> dict:
    """返回各强度的配速（秒/公里）。E 给范围，其余给中心值和 ±范围。"""
    def p(frac):
        return 60000.0 / speed_for_vo2(frac * vdot)
    mar = race_time_from_vdot(vdot, 42195) / 42.195
    half = race_time_from_vdot(vdot, 21097.5) / 21.0975
    return {
        "E": (p(0.70), p(0.62)),          # 轻松/长距离：快端–慢端；实际以心率上限为准
        "M": (mar - 3, mar + 3),          # 马拉松配速（同 VDOT 的全马等效）
        "HM": (half - 3, half + 3),
        "T": (p(0.88) - 3, p(0.88) + 3),  # 阈值：“舒服的吃力”
        "I": (p(1.00), p(0.95)),          # 间歇：3–5 分钟一组
        "R": (p(1.07), p(1.04)),          # 重复跑：200–400 米，练速度和经济性
    }


# ---------------------------------------------------------------- 心率阈值

def hr_model(hrmax=None, lthr=None, lt1=None, lt2=None, rhr=None) -> dict:
    """估计 LT1/LT2 并给出处方心率带。来源越靠前越可信。"""
    if lt1 and lt2:
        src, err = "实测阈值（实验室或场地测试）", 3
    elif lthr:
        lt2, lt1 = lthr, round(0.89 * lthr)
        src, err = "由 LTHR 推算（LT2 = LTHR，LT1 = 89% LTHR）", 6
    elif hrmax:
        lt1, lt2 = round(0.82 * hrmax), round(0.87 * hrmax)
        src, err = "只由最大心率推算（82% / 87% HRmax），个体差异大", 10
    else:
        return {"ok": False, "reason": "缺少最大心率或乳酸阈心率"}
    ref = lthr or lt2
    bands = {
        "恢复": (None, min(lt1 - 12, round(0.85 * ref))),
        "轻松": (None, lt1 - 5),
        "长距离": (None, lt1),
        "马拉松配速": (round(0.90 * ref), round(0.95 * ref)),
        "阈值": (round(0.95 * ref), ref + 2),
        "间歇": (ref, hrmax or None),
    }
    out = {"ok": True, "lt1": lt1, "lt2": lt2, "source": src, "uncertainty_bpm": err,
           "hrmax": hrmax, "lthr": lthr, "rhr": rhr, "bands": bands}
    if hrmax:
        out["olt_zones"] = [(round(OLT_SCALE[i] * hrmax), round(OLT_SCALE[i + 1] * hrmax)) for i in range(5)]
    if lthr:
        b = [round(x * lthr) for x in FRIEL_LTHR]
        out["friel_zones"] = {"Z1": (None, b[0] - 1), "Z2": (b[0], b[1] - 1), "Z3": (b[1], b[2] - 1),
                              "Z4": (b[2], b[3] - 1), "Z5a": (b[3], b[4] - 1), "Z5b": (b[4], b[5] - 1),
                              "Z5c": (b[5], None)}
    return out


def zone3(hr, lt1, lt2) -> int:
    if hr is None:
        return 0
    return 1 if hr < lt1 else (2 if hr < lt2 else 3)


def main():
    ap = argparse.ArgumentParser(description="心率与配速区间")
    ap.add_argument("--hrmax", type=int)
    ap.add_argument("--lthr", type=int)
    ap.add_argument("--lt1", type=int)
    ap.add_argument("--lt2", type=int)
    ap.add_argument("--race", nargs=2, metavar=("距离米", "用时"), help="例如 10000 44:00")
    a = ap.parse_args()
    if a.hrmax or a.lthr or (a.lt1 and a.lt2):
        m = hr_model(a.hrmax, a.lthr, a.lt1, a.lt2)
        if m["ok"]:
            print("LT1 ≈ %d，LT2 ≈ %d bpm（%s，误差约 ±%d）" % (m["lt1"], m["lt2"], m["source"], m["uncertainty_bpm"]))
            for k, (lo, hi) in m["bands"].items():
                print("  %-6s %s" % (k, ("≤%d" % hi) if lo is None else ("%d–%s" % (lo, hi if hi else "最大"))))
    if a.race:
        d, t = float(a.race[0]), parse_time(a.race[1])
        v = vdot_from_race(d, t)
        print("VDOT ≈ %.1f" % v)
        for name, (fast, slow) in pace_table(v).items():
            print("  %-2s %s–%s /km" % (name, fmt_pace(1000 / fast), fmt_pace(1000 / slow)))
        for dist, label in ((5000, "5K"), (10000, "10K"), (21097.5, "半马"), (42195, "全马")):
            print("  等效 %s：%s" % (label, fmt_dur(race_time_from_vdot(v, dist))))


if __name__ == "__main__":
    main()

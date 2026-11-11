#!/usr/bin/env python3
"""坡度校正配速（GAP）：把上下坡的配速折算成“平路上同样费力的配速”。

  python3 gap.py                        # 最近一次跑步：每公里的实际配速、坡度和坡度校正配速
  python3 gap.py --date 2026-11-07
  python3 gap.py --id 123456789
  python3 gap.py --selftest             # 核对公式和参考表（维护用）

方法（解读见 references/analysis-protocol.md 第 7 节）：
- 上坡：Minetti 2002 跑步能耗多项式 Cr(i)（J/kg/m，坡度 i 是小数，有效范围 ±0.45），
  校正系数 f = Cr(i) / Cr(0) [GAP-1]。研究得出。
- 下坡：“省力”封顶。−9% 处系数最低 0.88，到 −18% 回到 1.00，更陡不再算省力。
  形状模仿 Strava 2017 年按心率拟合的曲线（厂商说法 [GAP-2]，完整曲线未公开）→ 本技能的经验值。
  只要代谢上的数（例如研究对照），用 --mode metabolic。
- 海拔先平滑再求坡度：31 点滑动中值 → 每 10 m 重采样 → 11 点移动平均（约 100 m）→ 前后各 50 m 求坡度。经验值。
- 置信度：坡度绝对值 >10% 的距离超过 20% → “低”；任意 200 m 平均坡度绝对值 >15% → “很低”。经验值。
  超过 ±10% 的坡上，个人能耗和平路能耗的关系明显变弱，校正只能当参考。
- 坡后的配速和心率会滞后约 24–78 秒 [GAP-3]，所以只在 ≥1 km 的窗口里看，不逐秒比较。
- 跑步机课不校正。手表海拔来自气压计或 GPS，GPS 海拔误差更大，本脚本无法分辨。
"""
import argparse
import bisect
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gclib  # noqa: E402
from gclib import fmt_pace  # noqa: E402

GRID = 10.0          # 重采样间隔，米（经验值）
MED_W = 31           # 滑动中值窗口，点（经验值）
AVG_W = 11           # 移动平均窗口，格（约 100 m，经验值）
HALF_BASE = 5        # 求坡度时前后各取几格（5 × 10 m = 50 m，经验值）
DOWN_KNOTS = ((0.0, 1.00), (-0.09, 0.88), (-0.18, 1.00))   # 下坡“省力”封顶曲线（经验值，仿 [GAP-2]）


def cost(i):
    """Minetti 2002 跑步能耗（J/kg/m）。i 超出 ±0.45 时按边界算。"""
    i = max(-0.45, min(0.45, i))
    return 155.4 * i ** 5 - 30.4 * i ** 4 - 43.3 * i ** 3 + 46.3 * i ** 2 + 19.5 * i + 3.6


C0 = cost(0.0)


def factor(i, mode="effort"):
    """坡度 i 时，1 米相当于平路上多少米的努力。"""
    if i is None:
        return 1.0
    if mode == "metabolic" or i >= 0:
        return cost(i) / C0
    if i <= DOWN_KNOTS[-1][0]:
        return DOWN_KNOTS[-1][1]
    for (x0, y0), (x1, y1) in zip(DOWN_KNOTS, DOWN_KNOTS[1:]):
        if x1 <= i <= x0:
            return y0 + (y1 - y0) * (i - x0) / (x1 - x0)
    return 1.0


def _median(xs, w):
    h = w // 2
    out = []
    for j in range(len(xs)):
        win = sorted(xs[max(0, j - h):j + h + 1])
        out.append(win[len(win) // 2])
    return out


def grade_profile(dist, alt):
    """返回 (格点距离, 格点坡度)；海拔数据不够时返回 None。"""
    pts = [(d, a) for d, a in zip(dist, alt) if d is not None and a is not None]
    if len(pts) < 60 or len(pts) < 0.5 * len(dist):
        return None
    alts = _median([a for _, a in pts], MED_W)
    ds, al = [], []
    for (d, _), a in zip(pts, alts):
        if not ds or d > ds[-1]:
            ds.append(d)
            al.append(a)
    if len(ds) < 20 or ds[-1] - ds[0] < 500:
        return None
    grid = []
    g = ds[0]
    while g <= ds[-1]:
        grid.append(g)
        g += GRID
    ga, j = [], 0
    for g in grid:
        while j + 1 < len(ds) and ds[j + 1] < g:
            j += 1
        if j + 1 < len(ds) and ds[j + 1] > ds[j]:
            ga.append(al[j] + (al[j + 1] - al[j]) * (g - ds[j]) / (ds[j + 1] - ds[j]))
        else:
            ga.append(al[j])
    h = AVG_W // 2
    sm = [st.mean(ga[max(0, j - h):j + h + 1]) for j in range(len(ga))]
    gr = []
    for j in range(len(sm)):
        lo, hi = max(0, j - HALF_BASE), min(len(sm) - 1, j + HALF_BASE)
        run_m = (hi - lo) * GRID
        gr.append(max(-0.45, min(0.45, (sm[hi] - sm[lo]) / run_m)) if run_m > 0 else 0.0)
    return grid, gr


def confidence(gr):
    if not gr:
        return "没有数据"
    steep = sum(1 for x in gr if abs(x) > 0.10) / len(gr)
    w = int(200 / GRID)
    very = any(st.mean(abs(x) for x in gr[j:j + w]) > 0.15 for j in range(0, max(1, len(gr) - w)))
    if very:
        return "很低"
    if steep > 0.20:
        return "低"
    return "正常"


def km_gap(rec, mode="effort"):
    """每公里：[(第几公里, 用时秒, 坡度校正后的用时秒, 平均坡度)]，以及置信度。
    公里边界和 review.km_splits 一致；校正后用时 = 用时 × 实际距离 / Σ(Δd × f)。"""
    prof = grade_profile(rec["dist"], rec["alt"])
    if not prof:
        return None, "没有可用的海拔数据"
    grid, gr = prof

    def g_at(d):
        k = bisect.bisect_right(grid, d) - 1
        return gr[max(0, min(len(gr) - 1, k))]

    pts = [(t, d) for t, d in zip(rec["t"], rec["dist"]) if d is not None]
    out, k, i0 = [], 1, 0
    for i, (t, d) in enumerate(pts):
        if d >= k * 1000:
            seg = pts[i0:i + 1]
            real, eq, gsum = 0.0, 0.0, 0.0
            for (t0, d0), (t1, d1) in zip(seg, seg[1:]):
                dd = d1 - d0
                if dd <= 0:
                    continue
                gi = g_at((d0 + d1) / 2)
                real += dd
                eq += dd * factor(gi, mode)
                gsum += dd * gi
            sec = seg[-1][0] - seg[0][0]
            if real > 0 and eq > 0:
                out.append((k, sec, sec * real / eq, gsum / real))
            k += 1
            i0 = i
    return out, confidence(gr)


def selftest():
    ok = True
    checks = [(0.0, 3.60), (-0.20, 1.80), (0.45, 19.43)]
    for i, want in checks:
        got = cost(i)
        flag = abs(got - want) < 0.01
        ok &= flag
        print("Cr(%+.2f) = %.2f（应为 %.2f）%s" % (i, got, want, "" if flag else "  ✗"))
    table = [(-0.20, 0.50), (-0.10, 0.60), (-0.05, 0.76), (0.0, 1.00), (0.04, 1.24), (0.10, 1.66), (0.20, 2.50)]
    for i, want in table:
        got = factor(i, "metabolic")
        flag = abs(got - want) < 0.01
        ok &= flag
        print("代谢系数 f(%+.2f) = %.2f（参考表 %.2f）%s" % (i, got, want, "" if flag else "  ✗"))
    xs = [x / 1000.0 for x in range(-300, 0)]
    imin = min(xs, key=cost)
    flag = abs(imin + 0.181) < 0.002
    ok &= flag
    print("多项式最低点 i = %.3f（应约为 −0.181）%s" % (imin, "" if flag else "  ✗"))
    for i, want in ((-0.09, 0.88), (-0.18, 1.00), (-0.30, 1.00), (-0.045, 0.94)):
        got = factor(i)
        flag = abs(got - want) < 0.005
        ok &= flag
        print("努力系数 f(%+.3f) = %.3f（应为 %.2f）%s" % (i, got, want, "" if flag else "  ✗"))
    # 平路合成数据：校正前后应一致
    n = 1200
    rec = {"t": list(range(n)), "dist": [i * 3.0 for i in range(n)], "alt": [100.0] * n}
    rows, conf = km_gap(rec)
    flat = rows and all(abs(g - s) < 0.5 for _, s, g, _ in rows) and conf == "正常"
    ok &= bool(flat)
    print("平路合成数据：校正前后一致 %s" % ("" if flat else "  ✗"))
    # 3% 匀速上坡：校正后应更快
    rec["alt"] = [100.0 + 0.03 * d for d in rec["dist"]]
    rows, conf = km_gap(rec)
    up = rows and all(g < s for _, s, g, _ in rows[1:-1]) and abs(rows[1][3] - 0.03) < 0.003
    ok &= bool(up)
    print("3%% 上坡合成数据：坡度 %.3f，校正后配速 %s（实际 %s）%s" % (
        rows[1][3], fmt_pace(1000 / rows[1][2]), fmt_pace(1000 / rows[1][1]), "" if up else "  ✗"))
    print("全部通过" if ok else "有检查没通过")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--id")
    ap.add_argument("--date")
    ap.add_argument("--mode", choices=["effort", "metabolic"], default="effort")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    import fitlite
    from review import pick_activity
    raw = gclib.coach_home() / "raw"
    act = pick_activity(raw, a.id, a.date)
    if not act:
        print("没有找到这次跑步。先运行 garmin_pull.py。")
        return 2
    if "treadmill" in ((act.get("activityType") or {}).get("typeKey") or ""):
        print("跑步机课不做坡度校正。")
        return 0
    fp = raw / "fit" / ("%s.zip" % act["activityId"])
    if not fp.exists():
        print("没有这次跑步的 FIT 文件。先运行 garmin_pull.py（不要加 --no-fit）。")
        return 2
    rows, conf = km_gap(fitlite.load_run(fp)["records"], a.mode)
    if not rows:
        print(conf)
        return 1
    print("%s %s：坡度校正置信度 %s（%s 模式）" % (str(act.get("startTimeLocal"))[:10], act.get("activityName") or "",
                                              conf, "努力" if a.mode == "effort" else "代谢"))
    print("| 公里 | 实际配速 | 平均坡度 | 坡度校正配速 |\n|---|---|---|---|")
    for k, sec, gsec, g in rows:
        print("| %d | %s | %+.1f%% | %s |" % (k, fmt_pace(1000 / sec), g * 100, fmt_pace(1000 / gsec)))
    tot, gtot = sum(r[1] for r in rows), sum(r[2] for r in rows)
    print("\n整段：实际 %s，坡度校正 %s。下坡封顶和海拔平滑是本技能的经验参数 [GAP-1][GAP-2]。"
          % (fmt_pace(len(rows) * 1000 / tot), fmt_pace(len(rows) * 1000 / gtot)))
    if act.get("avgGradeAdjustedSpeed"):
        print("佳明自己算的整段坡度校正配速：%s（算法未公开，只作对照）。" % fmt_pace(act["avgGradeAdjustedSpeed"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())

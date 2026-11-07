#!/usr/bin/env python3
"""从佳明个人 API 拉取数据到本地（增量，可反复运行）。

  python3 garmin_pull.py                 # 默认拉最近 365 天
  python3 garmin_pull.py --days 180
  python3 garmin_pull.py --since 2025-01-01
  python3 garmin_pull.py --no-fit        # 不下载 FIT 原始文件（快，但分析会少很多指标）

数据保存在 ~/garmin-coach/raw/（可用环境变量 GARMIN_COACH_HOME 改）。
只读取，不会修改佳明账号里的任何东西。
"""
import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gclib  # noqa: E402

RUN_TYPES = {"running", "treadmill_running", "trail_running", "track_running", "indoor_running",
             "street_running", "virtual_run", "ultra_run", "obstacle_run"}
PAGE = 20  # 中国区 limit=50 会报 500，20 比较稳


def is_run(a: dict) -> bool:
    t = (a.get("activityType") or {})
    k = t.get("typeKey", "")
    return k in RUN_TYPES or "run" in k or t.get("parentTypeId") == 1


def pull_activity_list(since: date, until: date, log):
    out, start, use_dates = [], 0, True
    for _ in range(300):
        params = {"start": start, "limit": PAGE}
        if use_dates:
            params.update({"startDate": since.isoformat(), "endDate": until.isoformat()})
        try:
            page = gclib.api_get("/activitylist-service/activities/search/activities", params)
        except gclib.GarminError:
            if use_dates and start == 0:
                use_dates = False  # 个别地区不认日期参数：改成不带日期、从新往旧翻页
                continue
            raise
        if not page:
            break
        out.extend(page)
        oldest = min(str(a.get("startTimeLocal", "9999"))[:10] for a in page)
        log("  活动列表：已取 %d 条（最早 %s）" % (len(out), oldest))
        if len(page) < PAGE or oldest < since.isoformat():
            break
        start += PAGE
    return [a for a in out if since.isoformat() <= str(a.get("startTimeLocal", ""))[:10] <= until.isoformat()]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=365, help="拉最近多少天（默认 365）")
    ap.add_argument("--since", help="从哪天开始，YYYY-MM-DD（优先于 --days）")
    ap.add_argument("--no-fit", action="store_true", help="不下载 FIT 文件")
    ap.add_argument("--refresh-days", type=int, default=21, help="最近多少天的健康数据每次都重新下载")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    log = (lambda *a: None) if args.quiet else print

    today = date.today()
    since = gclib.parse_date(args.since) if args.since else today - timedelta(days=args.days)
    raw = gclib.raw_dir()
    man = gclib.load_json(raw / "manifest.json", {}) or {}
    fails = []

    try:
        host, _ = gclib.get_token()
    except gclib.GarminError as e:
        print("无法连接：%s" % e)
        return 2
    region = "cn" if host.endswith(".cn") else "global"
    log("地区：%s；数据目录：%s" % ("中国区" if region == "cn" else "国际区", raw))

    # 1. 资料：心率区间、用户设置
    for name, path in (("hr_zones", "/biometric-service/heartRateZones/v2"),
                       ("user_settings", "/userprofile-service/userprofile/settings")):
        try:
            j = gclib.api_get(path)
            if j is not None:
                gclib.save_json(raw / "profile" / (name + ".json"), j)
        except gclib.GarminError as e:
            fails.append({"what": name, "error": str(e)})

    # 2. 活动列表（增量）
    acts_path = raw / "activities.json"
    acts = {a["activityId"]: a for a in (gclib.load_json(acts_path, []) or []) if "activityId" in a}
    covered_from = gclib.parse_date(man["activities_from"]) if man.get("activities_from") else None
    last_pull = gclib.parse_date(man["pulled_at"]) if man.get("pulled_at") else None
    ranges = []
    if covered_from and last_pull and acts:
        if since < covered_from:
            ranges.append((since, covered_from))
        ranges.append((max(since, last_pull - timedelta(days=3)), today))
    else:
        ranges.append((since, today))
    for s, e in ranges:
        log("拉取活动列表 %s → %s" % (s, e))
        try:
            for a in pull_activity_list(s, e, log):
                acts[a["activityId"]] = a
        except gclib.GarminError as e2:
            fails.append({"what": "activity_list", "error": str(e2)})
    acts_list = sorted(acts.values(), key=lambda a: str(a.get("startTimeGMT", "")))
    gclib.save_json(acts_path, acts_list)
    runs = [a for a in acts_list if is_run(a) and str(a.get("startTimeLocal", ""))[:10] >= since.isoformat()]
    log("活动共 %d 条，其中跑步 %d 条（%s 之后）" % (len(acts_list), len(runs), since))

    # 3. 每次跑步的详情（含主观感受 RPE）和 FIT 原始文件
    n_det = n_fit = 0
    for i, a in enumerate(runs, 1):
        aid = a["activityId"]
        dp = raw / "details" / ("%s.json" % aid)
        if not dp.exists():
            try:
                j = gclib.api_get("/activity-service/activity/%s" % aid)
                if j is not None:
                    gclib.save_json(dp, j)
                    n_det += 1
            except gclib.GarminError as e:
                fails.append({"what": "detail %s" % aid, "error": str(e)})
        fp = raw / "fit" / ("%s.zip" % aid)
        if not args.no_fit and not fp.exists():
            try:
                b = gclib.api_get("/download-service/files/activity/%s" % aid, raw=True, timeout=180)
                if b:
                    gclib.ensure_private_dir(fp.parent)
                    fp.write_bytes(b)
                    n_fit += 1
            except gclib.GarminError as e:
                fails.append({"what": "fit %s" % aid, "error": str(e)})
        if i % 20 == 0:
            log("  跑步明细 %d/%d" % (i, len(runs)))
    log("新下载：详情 %d 个，FIT %d 个" % (n_det, n_fit))

    # 4. 健康数据（28 天一个窗口；最近的窗口每次刷新）
    endpoints = {
        "hrv": lambda s, e: gclib.api_get("/hrv-service/hrv/daily/%s/%s" % (s, e)),
        "sleep": lambda s, e: gclib.api_get("/sleep-service/sleep/dailySleepSummariesByDate",
                                            {"startDate": s.isoformat(), "endDate": e.isoformat()}),
        "daily": lambda s, e: gclib.api_get("/usersummary-service/v2/dailysummaries",
                                            {"startDate": s.isoformat(), "endDate": e.isoformat()}),
    }
    stable_before = today - timedelta(days=args.refresh_days)
    for name, fn in endpoints.items():
        got = 0
        for s, e in gclib.windows(since, today):
            p = raw / name / ("%s_%s.json" % (s, e))
            if p.exists() and e < stable_before:
                continue
            try:
                j = fn(s, min(e, today))
                gclib.save_json(p, j if j is not None else [])
                got += 1
            except gclib.GarminError as e2:
                fails.append({"what": "%s %s" % (name, s), "error": str(e2)})
        log("健康数据 %s：更新 %d 个窗口" % (name, got))

    man.update({
        "pulled_at": today.isoformat(), "region": region,
        "activities_from": min(since, covered_from).isoformat() if covered_from else since.isoformat(),
        "n_activities": len(acts_list), "n_runs_in_window": len(runs),
        "n_fit": len(list((raw / "fit").glob("*.zip"))) if (raw / "fit").exists() else 0,
        "failures": fails[-50:],
    })
    gclib.save_json(raw / "manifest.json", man)
    if fails:
        log("有 %d 项没取到（详见 manifest.json）。可以稍后再运行一次，已下载的不会重复下载。" % len(fails))
    log("完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

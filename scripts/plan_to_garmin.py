#!/usr/bin/env python3
"""把 plan.json 里的跑步课转换成佳明“训练课程”格式，生成三种导入方式需要的文件。

  python3 plan_to_garmin.py --from 2026-11-05 --to 2026-12-31
  python3 plan_to_garmin.py --weeks 1-8
  python3 plan_to_garmin.py --mcp-ids ids.json      # garmin_mcp 第二步：用上传返回的 ID 生成排课数据

输出到 ~/garmin-coach/plan/garmin/：
  workouts.json          每种课一个佳明训练课程（同名课只建一次，按日期重复使用）
  schedule.json          [{"date":…, "name":…}]
  connect_upload.js      路线 A：在已登录的 Connect 网页里运行（先 DRY_RUN 预览，再正式写入）
  mcp_upload.json        路线 B：garmin_mcp 的 upload_workouts 参数
  manual.md              路线 C：手动在 Connect App/网页里建课的清单
步骤格式：佳明 workout-service 的 ExecutableStepDTO / RepeatGroupDTO。
配速目标用米/秒，targetValueOne 放较快的速度；心率目标 targetValueOne 放下限。
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gclib  # noqa: E402
from gclib import parse_date, parse_pace  # noqa: E402

RUN_TYPES = {"easy", "recovery", "long", "long_mp", "threshold", "interval", "mp", "test", "shakeout", "check"}
STEP = {"warmup": (1, "warmup"), "cooldown": (2, "cooldown"), "work": (3, "interval"), "run": (3, "interval"),
        "recover": (4, "recovery"), "rest": (5, "rest")}
SPORT = {"sportTypeId": 1, "sportTypeKey": "running"}


def speed(p):
    return round(1000.0 / parse_pace(p), 7)


class Builder:
    def __init__(self):
        self.order = 0
        self.grp = 0

    def exe(self, s, child=None):
        self.order += 1
        sid, skey = STEP.get(s.get("kind", "run"), STEP["run"])
        if s.get("km") is not None:
            cond, val = (3, "distance"), round(float(s["km"]) * 1000)
        elif s.get("sec") is not None:
            cond, val = (2, "time"), int(s["sec"])
        else:
            cond, val = (1, "lap.button"), None
        v1 = v2 = None
        if s.get("hr"):
            tgt = (4, "heart.rate.zone")
            v1, v2 = int(s["hr"][0]), int(s["hr"][1])
        elif s.get("pace"):
            tgt = (6, "pace.zone")
            a, b = speed(s["pace"][0]), speed(s["pace"][1])
            v1, v2 = max(a, b), min(a, b)  # 快的速度放 targetValueOne
        else:
            tgt = (1, "no.target")
        o = {"type": "ExecutableStepDTO", "stepOrder": self.order,
             "stepType": {"stepTypeId": sid, "stepTypeKey": skey},
             "endCondition": {"conditionTypeId": cond[0], "conditionTypeKey": cond[1]}, "endConditionValue": val,
             "targetType": {"workoutTargetTypeId": tgt[0], "workoutTargetTypeKey": tgt[1]},
             "targetValueOne": v1, "targetValueTwo": v2, "zoneNumber": None, "description": s.get("note")}
        if cond[1] == "distance":
            o["preferredEndConditionUnit"] = {"unitKey": "kilometer"}
        if child:
            o["childStepId"] = child
        return o

    def steps(self, steps):
        out = []
        for s in steps:
            if s.get("kind") == "repeat":
                self.order += 1
                self.grp += 1
                g = self.grp
                rep = {"type": "RepeatGroupDTO", "stepOrder": self.order,
                       "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat"}, "childStepId": g,
                       "numberOfIterations": int(s["times"]), "smartRepeat": False,
                       "endCondition": {"conditionTypeId": 7, "conditionTypeKey": "iterations"},
                       "endConditionValue": int(s["times"])}
                rep["workoutSteps"] = [self.exe(c, g) for c in s["steps"]]
                out.append(rep)
            else:
                out.append(self.exe(s))
        return out


def describe(s):
    """给手表和网页看的简短说明（佳明描述字段有长度限制，控制在 500 字内）。"""
    parts = []
    for st_ in s.get("steps", []):
        parts.append(step_text(st_))
    txt = "；".join(parts)
    if s.get("note"):
        txt = s["note"] + "。" + txt
    return txt[:480]


def step_text(st_):
    if st_.get("kind") == "repeat":
        return "%d×（%s）" % (st_["times"], " + ".join(step_text(c) for c in st_["steps"]))
    name = {"warmup": "热身", "cooldown": "放松", "work": "", "run": "", "recover": "恢复"}.get(st_.get("kind"), "")
    amt = ("%g km" % st_["km"]) if st_.get("km") is not None else (("%d 秒" % st_["sec"]) if st_.get("sec", 0) < 120
                                                                  else "%g 分钟" % (st_["sec"] / 60)) if st_.get("sec") else "按圈键结束"
    tgt = ""
    if st_.get("hr"):
        tgt = " 心率≤%d" % st_["hr"][1] if st_["hr"][0] <= 100 else " 心率%d–%d" % tuple(st_["hr"])
    elif st_.get("pace"):
        tgt = " 配速%s–%s" % tuple(st_["pace"])
    return (name + amt + tgt).strip()


def workout_json(name, s):
    b = Builder()
    return {"workoutName": name, "description": describe(s), "sportType": SPORT,
            "workoutSegments": [{"segmentOrder": 1, "sportType": SPORT, "workoutSteps": b.steps(s.get("steps", []))}]}


JS_TEMPLATE = r"""// 在已登录的佳明 Connect 网页（connect.garmin.com 或 connect.garmin.cn）里运行。
// 第一次保持 DRY_RUN = true：只读取、不写入，返回将要新建/排入的数量和日历冲突。
// 用户确认后把 DRY_RUN 改成 false 再运行一次。脚本可重复运行：同名课程会复用，已排的日期会跳过。
const DRY_RUN = __DRY__;
const WORKOUTS = __WORKOUTS__;
const SCHEDULE = __SCHEDULE__;
const csrf = document.querySelector('meta[name="csrf-token"]')?.content;
const H = {'connect-csrf-token': csrf, 'Content-Type': 'application/json'};
const api = (p, o = {}) => fetch('/gc-api' + p, {credentials: 'include', headers: H, ...o});
const sleep = ms => new Promise(r => setTimeout(r, ms));
const out = {dryRun: DRY_RUN, csrf: !!csrf, fail: []};
// 1. 已有课程（按名字复用）
const ids = {};
for (let start = 1; start < 3000; start += 100) {
  const r = await api(`/workout-service/workouts?start=${start}&limit=100&myWorkoutsOnly=true&sharedWorkoutsOnly=false&orderBy=UPDATE_DATE&orderSeq=DESC`);
  if (!r.ok) { out.fail.push('list workouts ' + r.status); break; }
  const j = await r.json(); for (const w of j) ids[w.workoutName] = w.workoutId;
  if (j.length < 100) break;
}
const names = [...new Set(SCHEDULE.map(s => s[1]))];
out.toCreate = names.filter(n => !ids[n]);
out.reuse = names.filter(n => ids[n]).length;
// 2. 日历里已有的内容（月份从 0 开始；相邻月份会重叠，按 id 去重）
const months = [...new Set(SCHEDULE.map(s => s[0].slice(0, 7)))];
const have = new Set(), others = {}, seen = new Set();
for (const ym of months) {
  const [y, m] = ym.split('-').map(Number);
  const r = await api(`/calendar-service/year/${y}/month/${m - 1}`);
  if (!r.ok) { out.fail.push('calendar ' + ym + ' ' + r.status); continue; }
  const c = await r.json();
  for (const i of (c.calendarItems || [])) {
    if (seen.has(i.id)) continue; seen.add(i.id);
    if (i.itemType === 'workout') {
      have.add(i.date + '|' + i.title);
      if (!names.includes(i.title)) (others[i.date] = others[i.date] || []).push(i.title);
    }
  }
}
out.alreadyScheduled = SCHEDULE.filter(([d, n]) => have.has(d + '|' + n)).length;
out.toSchedule = SCHEDULE.length - out.alreadyScheduled;
out.otherWorkoutsOnSameDays = Object.fromEntries(SCHEDULE.filter(([d]) => others[d]).map(([d]) => [d, others[d]]));
// 3. 正在进行的佳明教练/训练计划（只报告，不改）
try {
  const r = await api('/trainingplan-service/trainingplan/plans?limit=50');
  if (r.ok) {
    const j = await r.json(); const list = Array.isArray(j) ? j : (j.trainingPlanList || j.plans || []);
    out.trainingPlans = list.map(p => ({name: p.name || p.trainingPlanName, status: p.trainingStatus?.statusKey || p.status || p.trainingPlanStatus})).slice(0, 10);
  }
} catch (e) { out.trainingPlans = 'unknown'; }
if (!DRY_RUN) {
  out.created = [];
  for (const n of out.toCreate) {
    const r = await api('/workout-service/workout', {method: 'POST', body: JSON.stringify(WORKOUTS[n])});
    const j = await r.json().catch(() => null);
    if (r.ok && j?.workoutId) { ids[n] = j.workoutId; out.created.push(n); } else out.fail.push('create ' + n + ' ' + r.status);
    await sleep(300);
  }
  out.scheduled = 0;
  for (const [d, n] of SCHEDULE) {
    if (have.has(d + '|' + n)) continue;
    if (!ids[n]) { out.fail.push('no id ' + n); continue; }
    const r = await api('/workout-service/schedule/' + ids[n], {method: 'POST', body: JSON.stringify({date: d})});
    if (r.ok) out.scheduled++; else out.fail.push('schedule ' + d + ' ' + r.status);
    await sleep(300);
  }
}
JSON.stringify(out);
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plan")
    ap.add_argument("--from", dest="d0")
    ap.add_argument("--to", dest="d1")
    ap.add_argument("--weeks", help="例如 1-8")
    ap.add_argument("--prefix", help="课程名前缀，默认用 plan.meta.name_prefix")
    ap.add_argument("--mcp-ids", help="garmin_mcp upload_workouts 返回结果整理成的 {课程名: workout_id} JSON")
    a = ap.parse_args()
    home = gclib.coach_home()
    plan = gclib.load_json(Path(a.plan) if a.plan else home / "plan" / "plan.json")
    if not plan:
        print("没有找到课表。")
        return 2
    outd = home / "plan" / "garmin"
    if a.mcp_ids:
        ids = json.load(open(a.mcp_ids, encoding="utf-8"))
        sched = gclib.load_json(outd / "schedule.json", [])
        payload = [{"workout_id": int(ids[x["name"]]), "calendar_date": x["date"]} for x in sched if x["name"] in ids]
        gclib.save_json(outd / "mcp_schedule.json", payload)
        missing = sorted({x["name"] for x in sched if x["name"] not in ids})
        print("mcp_schedule.json：%d 条（作为 schedule_workouts 的 schedules 参数）%s" %
              (len(payload), ("；缺少 ID：" + "、".join(missing)) if missing else ""))
        return 0
    prefix = a.prefix if a.prefix is not None else plan.get("meta", {}).get("name_prefix", "GRC·")
    w0, w1 = (int(x) for x in a.weeks.split("-")) if a.weeks else (None, None)
    d0 = parse_date(a.d0) if a.d0 else None
    d1 = parse_date(a.d1) if a.d1 else None
    W, sched = {}, []
    for w in plan["weeks"]:
        if w0 and not (w0 <= w["week"] <= w1):
            continue
        for s in w["sessions"]:
            if s.get("type") not in RUN_TYPES or not s.get("steps"):
                continue
            d = parse_date(s["date"])
            if (d0 and d < d0) or (d1 and d > d1):
                continue
            name = (prefix + s["title"])[:80]
            if name not in W:
                W[name] = workout_json(name, s)
            sched.append({"date": s["date"], "name": name})
    if not sched:
        print("所选范围内没有可导入的跑步课。")
        return 1
    gclib.save_json(outd / "workouts.json", W)
    gclib.save_json(outd / "schedule.json", sched)
    gclib.save_json(outd / "mcp_upload.json", list(W.values()))
    js = (JS_TEMPLATE.replace("__WORKOUTS__", json.dumps(W, ensure_ascii=False))
          .replace("__SCHEDULE__", json.dumps([[x["date"], x["name"]] for x in sched], ensure_ascii=False)))
    gclib.write_private(outd / "connect_upload.js", js.replace("__DRY__", "true"))
    gclib.write_private(outd / "connect_upload_write.js", js.replace("__DRY__", "false"))
    L = ["# 手动导入清单\n", "在佳明 Connect App：训练与计划 → 训练课程 → 创建训练 → 跑步；按下面的步骤添加，保存后点“添加到日历”选日期。\n"]
    for name, wj in W.items():
        dates = [x["date"] for x in sched if x["name"] == name]
        src = next(s for w in plan["weeks"] for s in w["sessions"] if (prefix + s.get("title", ""))[:80] == name)
        L.append("## %s\n" % name)
        L.append("- 日期：" + "、".join(dates))
        for st_ in src.get("steps", []):
            L.append("- " + step_text(st_))
        L.append("")
    gclib.write_private(outd / "manual.md", "\n".join(L))
    print("已生成 %d 种课程、%d 次排课 → %s" % (len(W), len(sched), outd))
    print("路线 A：在已登录的 Connect 网页运行 connect_upload.js（预览）→ 用户确认 → connect_upload_write.js（写入）")
    print("路线 B：garmin_mcp upload_workouts(mcp_upload.json) → 记下 ID → plan_to_garmin.py --mcp-ids → schedule_workouts")
    print("路线 C：按 manual.md 手动建课")
    return 0


if __name__ == "__main__":
    sys.exit(main())

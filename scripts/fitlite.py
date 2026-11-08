#!/usr/bin/env python3
"""极简 FIT 解码器（只用标准库）。读取佳明 FIT 文件（或下载得到的 ZIP）中的
session / lap / record 消息，换算成常用单位。默认不保留 GPS 坐标（隐私）。

  python3 fitlite.py 某次跑步.zip      # 打印摘要，用于自检

主要函数：load_run(path) -> dict
  {"session": {...}, "laps": [...], "records": {"t": [...], "dist": [...], "speed": [...],
   "hr": [...], "cad": [...], "power": [...], "alt": [...]}, "start": unix 秒}
"""
import io
import struct
import sys
import zipfile
from collections import defaultdict

FIT_EPOCH = 631065600  # 1989-12-31 00:00:00 UTC 与 Unix 时间的差

# 基本类型编号 -> (struct 格式, 字节数, 无效值)
BASE = {
    0: ("B", 1, 0xFF), 1: ("b", 1, 0x7F), 2: ("B", 1, 0xFF), 3: ("h", 2, 0x7FFF),
    4: ("H", 2, 0xFFFF), 5: ("i", 4, 0x7FFFFFFF), 6: ("I", 4, 0xFFFFFFFF), 7: ("s", 1, None),
    8: ("f", 4, None), 9: ("d", 8, None), 10: ("B", 1, 0x00), 11: ("H", 2, 0x0000),
    12: ("I", 4, 0x00000000), 13: ("B", 1, None), 14: ("q", 8, 0x7FFFFFFFFFFFFFFF),
    15: ("Q", 8, 0xFFFFFFFFFFFFFFFF), 16: ("Q", 8, 0),
}

WANTED = {0: "file_id", 18: "session", 19: "lap", 20: "record", 21: "event", 23: "device_info", 26: "workout",
          27: "workout_step"}


def _read_field(data, pos, size, bt, endian):
    num = bt & 0x1F
    fmt, bsize, invalid = BASE.get(num, ("B", 1, None))
    raw = data[pos:pos + size]
    if num == 7:
        return raw.split(b"\x00", 1)[0].decode("utf-8", "replace") or None
    if num == 13 or size % bsize:
        return raw
    n = size // bsize
    vals = struct.unpack(endian + fmt * n, raw)
    vals = [None if (invalid is not None and v == invalid) else v for v in vals]
    if num in (8, 9):
        vals = [None if (v is not None and v != v) else v for v in vals]  # NaN
    return vals[0] if n == 1 else vals


def decode(data: bytes, wanted=WANTED):
    """返回 {消息名: [ {字段号: 值}, ... ]}。只解码 wanted 中的消息，其余跳过。"""
    out = defaultdict(list)
    pos = 0
    while pos + 12 <= len(data):
        hsize = data[pos]
        if hsize not in (12, 14) or data[pos + 8:pos + 12] != b".FIT":
            break
        dsize = struct.unpack_from("<I", data, pos + 4)[0]
        p, end = pos + hsize, min(pos + hsize + dsize, len(data))
        defs, last_ts = {}, None
        while p < end:
            h = data[p]
            p += 1
            if h & 0x80:  # 压缩时间戳头
                lmt, off = (h >> 5) & 0x3, h & 0x1F
                d = defs.get(lmt)
                if d is None:
                    break
                if last_ts is not None:
                    ts = (last_ts & ~0x1F) + off
                    if off < (last_ts & 0x1F):
                        ts += 0x20
                    last_ts = ts
                msg, p = _read_msg(data, p, d)
                if msg is not None:
                    msg[253] = last_ts
                    out[wanted[d[0]]].append(msg)
            elif h & 0x40:  # 定义消息
                lmt, has_dev = h & 0x0F, h & 0x20
                endian = ">" if data[p + 1] == 1 else "<"
                gnum = struct.unpack_from(endian + "H", data, p + 2)[0]
                nf = data[p + 4]
                p += 5
                fields = [(data[p + 3 * i], data[p + 3 * i + 1], data[p + 3 * i + 2]) for i in range(nf)]
                p += 3 * nf
                dev_size = 0
                if has_dev:
                    nd = data[p]
                    p += 1
                    dev_size = sum(data[p + 3 * i + 1] for i in range(nd))
                    p += 3 * nd
                defs[lmt] = (gnum, endian, fields, dev_size)
            else:  # 数据消息
                d = defs.get(h & 0x0F)
                if d is None:
                    break
                msg, p = _read_msg(data, p, d)
                if msg is not None:
                    if msg.get(253) is not None:
                        last_ts = msg[253]
                    out[wanted[d[0]]].append(msg)
        pos = end + 2  # 跳过 CRC，继续读可能串接的下一个 FIT
    return out


def _read_msg(data, p, d):
    gnum, endian, fields, dev_size = d
    if gnum not in WANTED:
        return None, p + sum(f[1] for f in fields) + dev_size
    msg = {}
    for fnum, size, bt in fields:
        msg[fnum] = _read_field(data, p, size, bt, endian)
        p += size
    return msg, p + dev_size


def read_bytes(path) -> bytes:
    path = str(path)
    if path.lower().endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".fit")] or z.namelist()
            return z.read(names[0])
    with open(path, "rb") as f:
        return f.read()


def _s(v, scale=1.0, offset=0.0):
    if v is None or isinstance(v, (list, bytes)):
        return None
    return v / scale - offset


def load_run(path, keep_gps=False):
    m = decode(read_bytes(path))
    rec = {k: [] for k in ("t", "dist", "speed", "hr", "cad", "power", "alt")}
    if keep_gps:
        rec["lat"], rec["lon"] = [], []
    for r in m.get("record", []):
        ts = r.get(253)
        if ts is None:
            continue
        spd = r.get(73) if r.get(73) is not None else r.get(6)
        alt = r.get(78) if r.get(78) is not None else r.get(2)
        cad = r.get(4)
        if cad is not None:
            frac = r.get(53)
            cad = (cad + (frac / 128.0 if isinstance(frac, int) else 0)) * 2  # 步/分
        rec["t"].append(ts + FIT_EPOCH)
        rec["dist"].append(_s(r.get(5), 100))
        rec["speed"].append(_s(spd, 1000))
        rec["hr"].append(r.get(3))
        rec["cad"].append(cad)
        rec["power"].append(r.get(7))
        rec["alt"].append(_s(alt, 5, 500))
        if keep_gps:
            sc = 180.0 / 2 ** 31
            rec["lat"].append(r.get(0) * sc if r.get(0) is not None else None)
            rec["lon"].append(r.get(1) * sc if r.get(1) is not None else None)
    laps = []
    for l in m.get("lap", []):
        laps.append({
            "start": (l[2] + FIT_EPOCH) if l.get(2) is not None else None,
            "elapsed_s": _s(l.get(7), 1000), "timer_s": _s(l.get(8), 1000),
            "dist_m": _s(l.get(9), 100),
            "avg_speed": _s(l.get(110) if l.get(110) is not None else l.get(13), 1000),
            "avg_hr": l.get(15), "max_hr": l.get(16), "intensity": l.get(23),
            "step": l.get(71), "cad": (l.get(17) * 2) if isinstance(l.get(17), int) else None,
            "power": l.get(19), "ascent": l.get(21), "descent": l.get(22),
        })
    ses = {}
    if m.get("session"):
        s = m["session"][0]
        ses = {
            "start": (s[2] + FIT_EPOCH) if s.get(2) is not None else None,
            "sport": s.get(5), "sub_sport": s.get(6),
            "elapsed_s": _s(s.get(7), 1000), "timer_s": _s(s.get(8), 1000),
            "dist_m": _s(s.get(9), 100),
            "avg_speed": _s(s.get(124) if s.get(124) is not None else s.get(14), 1000),
            "avg_hr": s.get(16), "max_hr": s.get(17), "ascent_m": s.get(22),
            "aerobic_te": _s(s.get(24), 10),
        }
    start = ses.get("start") or (rec["t"][0] if rec["t"] else None)
    # 手表执行的结构化课程（如果这次跑的是课程）：步骤、目标
    steps = []
    for w in m.get("workout_step", []):
        lo, hi, tt = w.get(5), w.get(6), w.get(3)
        tgt = None
        if tt == 0 and lo and hi:      # 速度，mm/s
            tgt = {"kind": "speed", "low": lo / 1000.0, "high": hi / 1000.0}
        elif tt == 1 and lo is not None and hi:
            # 心率目标有两种存法：FIT 规范是“≤100 = %HRmax，>100 = bpm+100”；
            # 但 Connect 网页/本技能写入的课程，手表实测直接存 bpm（例如 100 和 135，2026-11 观察）。
            # 判断：上限 ≥200 按规范减 100；上限在 101–199 按原值 bpm；上限 ≤100 才是 %HRmax。
            if hi >= 200:
                tgt = {"kind": "hr", "low": (lo - 100) if lo > 100 else None, "high": hi - 100, "pct": False}
            elif hi > 100:
                tgt = {"kind": "hr", "low": lo if lo > 0 else None, "high": hi, "pct": False}
            else:
                tgt = {"kind": "hr", "low": lo or None, "high": hi, "pct": True}
        dtp, dv = w.get(1), w.get(2)
        steps.append({"idx": w.get(254), "name": w.get(0) if isinstance(w.get(0), str) else None,
                      "dur_type": dtp,  # 0 时间 1 距离 6 重复
                      "dur": (dv / 1000.0 if dtp == 0 else (dv / 100.0 if dtp == 1 else dv)) if dv is not None else None,
                      "repeat": w.get(4) if dtp == 6 else None, "target": tgt, "intensity": w.get(7),
                      "notes": w.get(8) if isinstance(w.get(8), str) else None})
    # 心率来源：device_info 里有通过 ANT+（1）或蓝牙（3）连接、类型为心率（120）的设备，就是外接心率带
    hr_source = "wrist"
    for d_ in m.get("device_info", []):
        if d_.get(25) in (1, 3) and d_.get(1) == 120:
            hr_source = "strap"
    # 心率“锁步频”：心率读数和步频几乎相同的时间占比高，说明光电心率误把步频当成心率
    pairs = [(h, c) for h, c in zip(rec["hr"], rec["cad"]) if h and c and h > 130]
    cad_lock = (sum(1 for h, c in pairs if abs(h - c) <= 2) / len(pairs)) if len(pairs) > 120 else 0.0
    wname = None
    if m.get("workout"):
        wname = m["workout"][0].get(8) if isinstance(m["workout"][0].get(8), str) else None
    return {"session": ses, "laps": laps, "records": rec, "start": start, "steps": steps, "workout_name": wname,
            "hr_source": hr_source, "cad_lock": cad_lock}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    r = load_run(sys.argv[1])
    rc = r["records"]
    n = len(rc["t"])
    print("记录点 %d 个，时长 %.0f 秒，圈数 %d" % (n, (rc["t"][-1] - rc["t"][0]) if n else 0, len(r["laps"])))
    print("session:", {k: v for k, v in r["session"].items()})
    hrs = [h for h in rc["hr"] if h]
    if hrs:
        print("心率 %d–%d，均值 %.0f" % (min(hrs), max(hrs), sum(hrs) / len(hrs)))

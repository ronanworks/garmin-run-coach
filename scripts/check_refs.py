#!/usr/bin/env python3
"""引用与措辞自检：在把报告或课表说明发给用户之前运行，拦住编造的证据编号和手写的 PMID/DOI。

  python3 check_refs.py report 报告.md                 # 检查一份要发给用户的文字（也可以用 - 从标准输入读）
  python3 check_refs.py report 报告.md --stamp "模型名"  # 检查通过后在文末加上“技能版本、模型、日期”
  python3 check_refs.py library                        # 维护者：证据库自身是否一致（速查表和详细条目对得上）
  python3 check_refs.py library --online               # 维护者：用 PubMed E-utilities 重新核对每个 PMID 的第一作者和年份

为什么要有它（references/evidence.md 的 AI-1 到 AI-5）：
- 大模型写的参考文献里，编造的比例在审计中是 18–55%，最常错的就是 PMID [AI-1][AI-2]；2025 年测试的聊天机器人没有一个
  能给出完全准确的参考文献列表 [AI-3]。把回答锚定在固定的权威文本上，准确率明显提高 [AI-4]。
- 所以本技能的规矩是：正文只写证据编号，不手写 PMID/DOI；编号必须在证据库里；“研究表明”后面的依据不能全是 C 级或未核实。
- 自检能减少多少错误没有被直接测量过，这是推论。它也查不出“编号存在但用错了地方”，这一点仍要靠读 evidence.md。
退出码：report 模式有 ERROR 时为 1。
"""
import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gclib  # noqa: E402

EVIDENCE = Path(__file__).resolve().parent.parent / "references" / "evidence.md"
ID_RE = re.compile(r"\[([A-Z][A-Z0-9]{0,7}-\d{1,3})\]")
RULE_RE = re.compile(r"^(D[1-9]|P\d{1,2})$")
PMID_RE = re.compile(r"PMID\s*[:：]?\s*\d{5,9}|pubmed\.ncbi\.nlm\.nih\.gov/\d+", re.I)
DOI_RE = re.compile(r"(?:doi\.org/|doi\s*[:：]\s*|\b)10\.\d{4,9}/[^\s)\]，。；]+", re.I)
CLAIM_RE = re.compile(r"研究(表明|显示|发现|证明|证实)")


def load_library(path=EVIDENCE):
    """{ID: {"grade": 速查表等级, "title": 详细条目标题, "verify": 核验行, "cite": 引用行}}"""
    lib, cur = {}, None
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\|\s*([A-Z][A-Z0-9]{0,7}-\d{1,3})\s*\|(.*)\|\s*([^|]*)\|\s*$", line)
        if m:
            lib.setdefault(m.group(1), {})["grade"] = m.group(3).strip()
            lib[m.group(1)]["quick"] = True
            continue
        m = re.match(r"^###\s*\[([A-Z][A-Z0-9]{0,7}-\d{1,3})\]\s*(.*)$", line)
        if m:
            cur = m.group(1)
            e = lib.setdefault(cur, {})
            e["title"] = m.group(2).strip()
            e["detail"] = e.get("detail", 0) + 1
            continue
        if line.startswith("## ") or line.startswith("### "):
            cur = None
        if cur and line.startswith("- 引用："):
            lib[cur]["cite"] = line
        if cur and line.startswith("- 核验："):
            lib[cur]["verify"] = line
    return lib


def weak(e):
    """C 级、证据缺口，或核验写着 UNVERIFIED → 不能单独撑起“研究表明”。"""
    g = (e.get("grade") or "").strip()
    v = e.get("verify") or ""
    return g.startswith("C") or g.startswith("—") or g == "" or "未核实" in g or ("UNVERIFIED" in v and "VERIFIED（" not in v)


def check_report(text, lib):
    out = []
    for m in PMID_RE.finditer(text):
        out.append(("ERROR", "正文手写了 PMID：%s。只写证据编号，例如 [LOAD-3]；文献细节留在 evidence.md。" % m.group(0)))
    for m in DOI_RE.finditer(text):
        out.append(("ERROR", "正文手写了 DOI：%s。只写证据编号。" % m.group(0)))
    used = set()
    for m in ID_RE.finditer(text):
        i = m.group(1)
        used.add(i)
        if i not in lib and not RULE_RE.match(i):
            out.append(("ERROR", "证据编号 [%s] 不在 evidence.md 里：要么删掉，要么换成库里真实存在的编号，没有就写“经验做法”。" % i))
    for m in re.finditer(r"\[(D[1-9]|P\d{1,2})\]", text):
        used.add(m.group(1))
    for sent in re.split(r"[。；\n]", text):
        if not CLAIM_RE.search(sent):
            continue
        ids = [i for i in ID_RE.findall(sent) if i in lib]
        if not ids:
            out.append(("WARN", "“%s…” 写了“研究表明/显示”，但这句没有证据编号。补上编号，或改成“经验做法/不明确”。" % sent.strip()[:40]))
        elif all(weak(lib[i]) for i in ids):
            out.append(("WARN", "“%s…” 的依据 %s 全是 C 级或未核实：按 evidence.md 规则，改成“经验做法/专家意见/不明确”。"
                        % (sent.strip()[:40], "".join("[%s]" % i for i in ids))))
    rules = sorted(i for i in used if RULE_RE.match(i))
    if rules and not [i for i in used if i in lib]:
        out.append(("WARN", "只引用了规则编号（%s），没有证据编号。规则编号是检查项，不是证据。" % "、".join(rules)))
    if "技能版本" not in text:
        out.append(("INFO", "文末还没有生成信息。加 --stamp \"模型名\"，会写上技能版本、模型和日期。"))
    return out, used


def check_library(lib):
    out = []
    for i, e in sorted(lib.items()):
        if e.get("quick") and not e.get("detail"):
            out.append(("WARN", "[%s] 在速查表里，但没有详细条目" % i))
        if e.get("detail") and not e.get("quick"):
            out.append(("INFO", "[%s] 有详细条目，但速查表里没有" % i))
        if (e.get("detail") or 0) > 1:
            out.append(("ERROR", "[%s] 的详细条目重复了 %d 次" % (i, e["detail"])))
        if e.get("detail") and not e.get("verify"):
            out.append(("WARN", "[%s] 没有“核验”行" % i))
    return out


def online(lib):
    """用 PubMed esummary 核对每个条目引用行里的 PMID：第一作者姓氏和年份要出现在引用行里。"""
    pairs = []
    for i, e in lib.items():
        for p in re.findall(r"PMID\s*(\d{5,9})", e.get("cite") or ""):
            pairs.append((i, p))
    ids = sorted({p for _, p in pairs})
    meta = {}
    for k in range(0, len(ids), 150):
        q = urllib.parse.urlencode({"db": "pubmed", "id": ",".join(ids[k:k + 150]), "retmode": "json"})
        req = urllib.request.Request("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?" + q,
                                     headers={"User-Agent": "garmin-run-coach-evidence-check"})
        j = json.load(urllib.request.urlopen(req, timeout=60))
        meta.update({x: j["result"].get(x) for x in ids[k:k + 150]})
        time.sleep(0.4)
    out = []
    for i, p in pairs:
        r = meta.get(p)
        cite = lib[i].get("cite") or ""
        if not r or r.get("error"):
            out.append(("ERROR", "[%s] PMID %s 在 PubMed 查不到" % (i, p)))
            continue
        au = (r.get("authors") or [{}])[0].get("name", "")
        sur = au.split(" ")[0] if au else ""
        yr = (r.get("pubdate") or "")[:4]
        bad = []
        if sur and len(au.split()) <= 3 and sur.lower() not in cite.lower():  # 机构作者（如 ACSM）不比
            bad.append("第一作者应为 %s" % au)
        if yr and yr not in cite:
            bad.append("年份应为 %s" % yr)
        if bad:
            out.append(("WARN", "[%s] PMID %s：%s（PubMed 标题：%s）" % (i, p, "，".join(bad), (r.get("title") or "")[:70])))
    out.append(("INFO", "核对了 %d 个 PMID" % len(ids)))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report")
    r.add_argument("file")
    r.add_argument("--stamp", help="模型名称，例如 Claude Opus 5.5；检查没有 ERROR 时在文末加生成信息")
    lb = sub.add_parser("library")
    lb.add_argument("--online", action="store_true")
    a = ap.parse_args()
    lib = load_library()
    if a.cmd == "library":
        F = check_library(lib)
        if a.online:
            F += online(lib)
        n = sum(1 for e in lib.values() if e.get("detail"))
        for lvl, msg in F:
            print("[%s] %s" % (lvl, msg))
        print("证据库共 %d 条详细条目。" % n)
        return 1 if any(l_ == "ERROR" for l_, _ in F) else 0
    text = sys.stdin.read() if a.file == "-" else Path(a.file).read_text(encoding="utf-8")
    F, used = check_report(text, lib)
    for lvl, msg in F:
        print("[%s] %s" % (lvl, msg))
    errs = any(l_ == "ERROR" for l_, _ in F)
    n_ev = len([i for i in used if i in lib])
    print("用到 %d 个证据编号；%s" % (n_ev, "有 ERROR，先改再发给用户。" if errs else "没有 ERROR。"))
    if a.stamp and not errs and a.file != "-":
        line = "\n\n—— 由 garmin-run-coach 技能版本 v%s 生成；模型：%s；日期：%s。数字来自脚本，结论按 references/ 的规则。\n" % (
            gclib.SKILL_VERSION, a.stamp, date.today().isoformat())
        if "技能版本" not in text:
            Path(a.file).write_text(text.rstrip() + line, encoding="utf-8")
            print("已在文末加上生成信息。")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())

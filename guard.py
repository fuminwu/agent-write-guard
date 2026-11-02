#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agent-write-guard —— 装在 agent 落盘缝上的「不可绕过」守卫。

【解决什么问题】
  agent 改文件有四种典型事故，都发生在一瞬间、事后才发现：
    1. 整篇重写：只想改两行，结果 400 行全被重写，diff 变成一片红，看不出改了什么
    2. 行尾翻转：CRLF → LF，git 里整个文件都变了（Windows 上尤其常见）
    3. 编码/BOM 丢失：.ps1 / .csv 里的中文变 ANSI，文件当场语法错
    4. 顺手删：改一处却吞掉一行（Obsidian 的 [[双链]] 最常被吞）
  这些靠「记得检查」是防不住的。本脚本把它们挂到 harness 的工具拦截缝上：
    写之前 —— 会踩坑的写法直接否决（exit 2，理由回给模型）
    写之后 —— 行尾/BOM/双链/整篇同样断言，坏了当场报

【协议】
  兼容 Claude Code 的 hook 方言（DSH 的 dsh-hooks-claude-code 用的就是它）：
    stdin : {"session_id","hook_event_name","tool_name","tool_input","tool_response","cwd",...}
    exit 0 : 放行
    exit 2 : 拦截 —— stderr 作为理由回给模型（PreToolUse = 否决；PostToolUse = 结果被 block）

【例外通道（可审计，不可静默）】
  python guard.py allow --tag "原因" --minutes 15
  → 生成限时 override.json；**每次被使用都写日志**，事后可数。

【自测】
  python guard.py selftest      # 0 = 全过，3 = 有失败

【环境变量】
  GUARD_STATE   状态目录（默认 <脚本目录>/.guard_state）
  GUARD_LOG     日志路径（默认 <脚本目录>/guard.log）
  GUARD_ROOTS   监控根，os.pathsep 分隔（默认 = 会话 cwd）
  GUARD_SKIP    额外跳过片段，os.pathsep 分隔
  GUARD_DISABLE 设为 1 则整体放行（应急开关）
"""
import argparse
import datetime
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import traceback

VERSION = "0.1.0"

_HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.environ.get("GUARD_STATE") or os.path.join(_HERE, ".guard_state")
LOG_PATH = os.environ.get("GUARD_LOG") or os.path.join(_HERE, "guard.log")
OVERRIDE_PATH = os.path.join(STATE_DIR, "override.json")

MUT_TOOLS = ("write", "edit", "str_replace_editor")
SHELL_TOOLS = ("bash", "pwsh", "shell")
BOM_UTF8 = b"\xef\xbb\xbf"

# 通用跳过项：备份、依赖、缓存、VCS
SKIP_PARTS = ["_backup_", "__pycache__", "node_modules", os.sep + ".git" + os.sep, ".dsh-uploads"]
# shell 直写只保护这些扩展名（其余交给 shell 自身）
PROTECT_EXT = (".md", ".json", ".jsonc", ".yml", ".yaml", ".toml", ".ini", ".ps1", ".sh", ".py", ".txt")

WIKILINK_RE = re.compile(r"(?<![\w\]])\[\[([^\[\]\n'\"]+?)\]\]")
WHOLE_DIFF_MIN_LINES = 40   # 基准文件 ≥ 这个行数才判「整篇重写」
STALE_STATE_HOURS = 6       # 快照过期即视为无基准（防拿旧基准误伤）


# ────────────────────────── 基础工具 ──────────────────────────
def _log(line):
    os.makedirs(STATE_DIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(LOG_PATH, "ab") as f:
            f.write(("%s | %s\n" % (stamp, line)).encode("utf-8"))
    except OSError:
        pass


def _norm(p):
    return os.path.normcase(os.path.abspath(str(p).replace("/", os.sep)))


def watched_roots(payload_cwd=None):
    env = [r for r in (os.environ.get("GUARD_ROOTS") or "").split(os.pathsep) if r.strip()]
    if env:
        return env
    return [payload_cwd or os.getcwd()]


def in_watch(p, roots):
    n = _norm(p)
    return any(n.startswith(_norm(r)) for r in roots)


def should_skip(p):
    n = _norm(p)
    if os.path.basename(n).startswith("_backup_") or n.endswith(".bak"):
        return True
    extra = [x for x in (os.environ.get("GUARD_SKIP") or "").split(os.pathsep) if x.strip()]
    return any(part in n for part in SKIP_PARTS + extra)


def probe(path):
    """文件格式指纹：编码/BOM/行尾/行数/sha256/内容。"""
    with open(path, "rb") as f:
        raw = f.read()
    crlf = raw.count(b"\r\n")
    lf = raw.count(b"\n") - crlf
    if crlf and lf:
        eol = "MIXED"
    elif crlf:
        eol = "CRLF"
    else:
        eol = "LF"
    bom = raw[:3] == BOM_UTF8
    try:
        text = raw.decode("utf-8-sig")
        decodable = True
    except UnicodeDecodeError:
        text = raw.decode("utf-8", "replace")
        decodable = False
    return {
        "eol": eol,
        "bom": bom,
        "lines": text.count("\n") + (0 if text.endswith("\n") or not text else 1),
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "utf8_ok": decodable,
        "text": text,
    }


def links_count(text):
    return len(WIKILINK_RE.findall(text or ""))


def eol_of(text):
    # errors="replace" 只为保命：入参异常（孤立代理字符等）时也要能算出行尾，而不是抛错
    raw = (text or "").encode("utf-8", "replace")
    crlf = raw.count(b"\r\n")
    lf = raw.count(b"\n") - crlf
    if crlf and lf:
        return "MIXED"
    return "CRLF" if crlf else "LF"


def diff_ops(old_lines, new_lines):
    """改 / 增 / 删 三分（替换算「改」，多出来的才算增删）"""
    m = a = d = 0
    sm = difflib.SequenceMatcher(None, old_lines, new_lines)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            continue
        if op == "insert":
            a += j2 - j1
        elif op == "delete":
            d += i2 - i1
        else:
            k = min(i2 - i1, j2 - j1)
            m += k
            d += (i2 - i1) - k
            a += (j2 - j1) - k
    return {"modified": m, "added": a, "deleted": d}


def whole_rewrite(ops, old_lines_n, new_lines_n):
    """(改+删) 与 (改+增) 双阈值 —— 纯替换时 deleted 恒为 0，单阈值会得到一条死断言。"""
    return (ops["modified"] + ops["deleted"]) >= 0.9 * old_lines_n \
        and (ops["modified"] + ops["added"]) >= 0.9 * max(1, new_lines_n)


# ────────────────────────── 快照 / 例外通道 ──────────────────────────
def state_file(session, path):
    key = hashlib.sha1(("%s|%s" % (session, _norm(path))).encode("utf-8")).hexdigest()[:16]
    return os.path.join(STATE_DIR, "ss_%s.json" % key)


def snap_as_state(snap):
    """把 probe 结果转成 state 同形字典（PRE 时文件还没有 state 文件，用这个当基准）。"""
    if snap is None:
        return {"absent": True}
    return {"absent": False, "eol": snap["eol"], "bom": snap["bom"], "lines": snap["lines"],
            "sha256": snap["sha256"], "links": links_count(snap["text"]), "text": snap["text"]}


def save_state(session, path, snap):
    os.makedirs(STATE_DIR, exist_ok=True)
    data = {"session": session, "path": os.path.abspath(path),
            "at": datetime.datetime.now().isoformat(timespec="seconds")}
    if snap is None:
        data["absent"] = True
    else:
        data["absent"] = False
        data["eol"] = snap["eol"]
        data["bom"] = snap["bom"]
        data["lines"] = snap["lines"]
        data["sha256"] = snap["sha256"]
        data["links"] = links_count(snap["text"])
        data["text"] = snap["text"] if len(snap["text"]) < 3_000_000 else None
    with open(state_file(session, path), "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False)


def load_state(session, path):
    fp = state_file(session, path)
    if not os.path.exists(fp):
        return None
    try:
        with open(fp, "rb") as f:
            data = json.loads(f.read().decode("utf-8-sig", "replace"))
    except (OSError, ValueError):
        return None
    try:
        age = (datetime.datetime.now() - datetime.datetime.fromisoformat(data.get("at", ""))).total_seconds()
    except ValueError:
        return None
    if age > STALE_STATE_HOURS * 3600:
        return None
    return data


def clear_state(session, path):
    fp = state_file(session, path)
    if os.path.exists(fp):
        try:
            os.remove(fp)
        except OSError:
            pass


def override_active():
    if not os.path.exists(OVERRIDE_PATH):
        return None
    try:
        with open(OVERRIDE_PATH, "rb") as f:
            data = json.loads(f.read().decode("utf-8-sig", "replace"))
    except (OSError, ValueError):
        return None
    try:
        until = datetime.datetime.fromisoformat(data.get("until", ""))
    except ValueError:
        return None
    if datetime.datetime.now() > until:
        return None
    return data


def _safe_probe(path):
    """文件当前指纹；不存在 → None。探针失败不拦路（放行 + 由调用方记日志）。"""
    if not os.path.exists(path):
        return None
    try:
        return probe(path)
    except OSError:
        return None


# ────────────────────────── 目标解析 ──────────────────────────
def mutation_targets(tool, args):
    """→ [(path, new_full_text_or_None, op_label)]；None 表示局部替换（全文未知）"""
    out = []
    if tool == "write":
        p = args.get("file_path") or args.get("path")
        if p:
            out.append((p, args.get("content"), "write"))
    elif tool == "edit":
        p = args.get("file_path") or args.get("path")
        if p:
            out.append((p, None, "edit"))
    elif tool == "str_replace_editor":
        cmd = str(args.get("command") or "").lower()
        p = args.get("path") or args.get("file_path")
        if not p or cmd == "view":
            return []
        if cmd == "create":
            out.append((p, args.get("file_text") or args.get("content"), "create"))
        else:
            out.append((p, None, cmd or "edit"))
    return out


_REDIRECT_RE = re.compile(
    r"(?:(?<!>)>(?!>)\s*[\"']?([^\s;|&\"'<>]+)"                   # > path   （>> 追加不算）
    r"|Out-File\s+(?:-\w+\s+\S+\s+)*[\"']?([^\s;|&\"'<>]+)"       # Out-File -Path x
    r"|Set-Content\s+(?:-\w+\s+\S+\s+)*[\"']?([^\s;|&\"'<>]+))",  # Set-Content x
    re.I)


def shell_targets(args, roots):
    """bash/pwsh 用重定向直写受保护文件（覆盖式）—— 粗筛，只命中文档类扩展名。"""
    cmd = str(args.get("command") or "")
    if not cmd:
        return []
    hits = []
    for m in _REDIRECT_RE.finditer(cmd):
        p = next((g for g in m.groups() if g), None)
        if not p:
            continue
        if not os.path.isabs(p):
            continue
        if not p.lower().endswith(PROTECT_EXT):
            continue
        if not in_watch(p, roots) or should_skip(p):
            continue
        if not os.path.exists(p):
            continue
        hits.append(p)
    return hits


# ────────────────────────── PRE：写之前拦 ──────────────────────────
def pre_rule_violations(path, old, new_text, op, tool):
    """返回问题清单。old=None 表示新建：无基准，不判格式类。"""
    problems = []
    if new_text is None:
        return problems                      # 局部替换：全文未知，改由 POST 断言实际结果
    if old is None or old.get("absent"):
        return problems
    o_eol, o_bom = old.get("eol"), old.get("bom")
    o_text = old.get("text") or ""
    o_links = old.get("links", 0)
    n_eol = eol_of(new_text)

    if o_eol == "CRLF" and n_eol == "LF":
        problems.append("行尾 CRLF→LF（会造成整篇 diff，等于看不见改了什么）")
    if o_eol != "MIXED" and n_eol == "MIXED":
        problems.append("会写入 MIXED 行尾（文件原本是 %s）" % o_eol)
    if o_bom and not new_text.startswith("﻿"):
        problems.append("BOM 丢失（.ps1/.csv 的中文会变 ANSI，出现语法错）")

    n_links = links_count(new_text)
    if o_links and n_links < o_links:        # 只对本来就有 [[双链]] 的文件生效
        problems.append("[[双链]] 计数下降 %d → %d（吞行嫌疑）" % (o_links, n_links))

    if o_text and old.get("lines", 0) >= WHOLE_DIFF_MIN_LINES:
        ops = diff_ops(o_text.splitlines(), (new_text or "").splitlines())
        n_lines = len((new_text or "").splitlines())
        if whole_rewrite(ops, old["lines"], n_lines):
            problems.append("【整篇重写】基准 %d 行里 ≥90%% 被替换（应走 %s 定点改，或显式声明例外）"
                            % (old["lines"], "edit" if tool == "write" else tool))
    return problems


def _payload(p):
    args = p.get("tool_input") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {}
    return str(p.get("tool_name") or ""), args, str(p.get("session_id") or "nosession"), \
        p.get("cwd") or os.getcwd()


def handle_pre(payload):
    tool, args, session, cwd = _payload(payload)
    roots = watched_roots(cwd)

    if tool in SHELL_TOOLS:
        hits = shell_targets(args, roots)
        if hits and not override_active():
            return 2, ("[write-guard·PRE] 疑似用 %s 覆盖式直写受保护文件：%s\n"
                       "→ 改用 edit 定点改；确需整篇重写请先声明："
                       "python guard.py allow --tag \"原因\" --minutes 15（会被记日志）"
                       % (tool, hits[0]))
        return 0, ""

    if tool not in MUT_TOOLS:
        return 0, ""

    blocked = []
    for path, new_text, op in mutation_targets(tool, args):
        ap = path if os.path.isabs(path) else os.path.join(cwd, path)
        if not in_watch(ap, roots) or should_skip(ap):
            continue
        snap_now = _safe_probe(ap)
        before = load_state(session, ap) or snap_as_state(snap_now)
        probs = pre_rule_violations(ap, before, new_text, op, tool)
        save_state(session, ap, snap_now)    # 存「改前」快照供 POST 比对
        if probs:
            ov = override_active()
            if ov:
                _log("OVERRIDE-USED event=PRE tag=%s tool=%s file=%s probs=%s"
                     % (ov.get("tag"), tool, os.path.basename(ap), "；".join(probs)))
            else:
                blocked.append("%s：%s" % (os.path.basename(ap), "；".join(probs)))
    if blocked:
        _log("PRE-BLOCK tool=%s n=%d :: %s" % (tool, len(blocked), " | ".join(blocked)))
        return 2, ("[write-guard·PRE] 拦下 %d 处落盘：\n  - %s\n"
                   "→ 定点改（edit）或先声明例外：python guard.py allow --tag \"原因\" --minutes 15"
                   % (len(blocked), "\n  - ".join(blocked)))
    return 0, ""


# ────────────────────────── POST：写之后验 ──────────────────────────
def handle_post(payload):
    tool, args, session, cwd = _payload(payload)
    if tool not in MUT_TOOLS:
        return 0, ""
    roots = watched_roots(cwd)

    fails = []
    for path, _new, _op in mutation_targets(tool, args):
        ap = path if os.path.isabs(path) else os.path.join(cwd, path)
        if not in_watch(ap, roots) or should_skip(ap):
            continue
        before = load_state(session, ap)
        after = _safe_probe(ap)
        name = os.path.basename(ap)
        if before is None:
            clear_state(session, ap)
            continue
        if before.get("absent"):
            _log("POST-NEW file=%s lines=%s" % (name, after["lines"] if after else "?"))
            clear_state(session, ap)
            continue
        if after is None:
            _log("POST-GONE file=%s（原本存在，现在读不到）" % name)
            fails.append("%s：文件读不到了（被删/改名？）" % name)
            clear_state(session, ap)
            continue

        probs = []
        if after["eol"] != before["eol"]:
            probs.append("行尾 %s → %s" % (before["eol"], after["eol"]))
        if after["bom"] != before["bom"]:
            probs.append("BOM %s → %s" % (before["bom"], after["bom"]))
        if not after["utf8_ok"]:
            probs.append("不再是合法 UTF-8")
        o_links = before.get("links", 0)
        n_links = links_count(after["text"])
        if o_links and n_links < o_links:
            probs.append("[[双链]] %d → %d（吞行嫌疑）" % (o_links, n_links))
        old_text = before.get("text")
        if old_text:
            ol, nl = old_text.splitlines(), after["text"].splitlines()
            ops = diff_ops(ol, nl)
            if len(ol) > 20 and whole_rewrite(ops, len(ol), len(nl)):
                probs.append("【整篇 diff】%d 行里 ≥90%% 被替换（改%d 增%d 删%d）"
                             % (len(ol), ops["modified"], ops["added"], ops["deleted"]))
        if probs:
            ov = override_active()
            if ov:
                _log("OVERRIDE-USED event=POST tag=%s tool=%s file=%s probs=%s"
                     % (ov.get("tag"), tool, name, "；".join(probs)))
            else:
                fails.append("%s：%s" % (name, "；".join(probs)))
        else:
            _log("POST-PASS file=%s eol=%s lines=%s" % (name, after["eol"], after["lines"]))
        clear_state(session, ap)

    if fails:
        _log("POST-FAIL tool=%s n=%d :: %s" % (tool, len(fails), " | ".join(fails)))
        return 2, ("[write-guard·POST] 刚写坏 %d 处，立即回滚/修正：\n  - %s\n"
                   "→ 回滚从备份取；修正格式：保持原行尾/编码再写"
                   % (len(fails), "\n  - ".join(fails)))
    return 0, ""


# ────────────────────────── CLI ──────────────────────────
def run_stdin():
    # stdin 必须**按字节读 + 显式 UTF-8 解码**：Windows 下 sys.stdin 默认用本机 ANSI(cp936)
    # + surrogateescape → 中文变孤立代理字符，之后任何 encode("utf-8") 直接抛 UnicodeEncodeError，
    # 钩子一抛错，桥通常按「非阻断失败」放行 —— 上了膛也哑火。
    raw = sys.stdin.buffer.read().decode("utf-8", "replace")
    if not raw.strip():
        return 0
    try:
        payload = json.loads(raw)
    except ValueError:
        _log("ERROR stdin 不是合法 JSON（前 120 字符）: %r" % raw[:120])
        return 0
    if os.environ.get("GUARD_DISABLE") == "1":
        return 0
    try:
        event = str(payload.get("hook_event_name") or "")
        if event == "PreToolUse":
            code, msg = handle_pre(payload)
        elif event == "PostToolUse":
            code, msg = handle_post(payload)
        else:
            return 0
    except Exception:                      # noqa: BLE001 —— 守卫自身出错绝不许静默
        _log("ERROR 守卫内部异常（放行但留痕）:\n" + traceback.format_exc())
        return 0
    if code == 2 and msg:
        sys.stderr.write(msg + "\n")
    return code


def cmd_allow(args):
    os.makedirs(STATE_DIR, exist_ok=True)
    until = datetime.datetime.now() + datetime.timedelta(minutes=args.minutes)
    with open(OVERRIDE_PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"until": until.isoformat(timespec="seconds"), "tag": args.tag,
                   "created": datetime.datetime.now().isoformat(timespec="seconds")},
                  f, ensure_ascii=False)
    _log("OVERRIDE-GRANT tag=%s minutes=%d until=%s"
         % (args.tag, args.minutes, until.isoformat(timespec="seconds")))
    print("[write-guard] 例外通道已开：%d 分钟内不拦（tag=%s，直到 %s）\n"
          "  每次使用都会写进 %s" % (args.minutes, args.tag, until.strftime("%H:%M:%S"), LOG_PATH))
    return 0


def cmd_status(_args):
    ov = override_active()
    print("[write-guard] v%s" % VERSION)
    print("  状态目录   : %s" % STATE_DIR)
    print("  日志       : %s" % LOG_PATH)
    print("  监控根     : %s" % " | ".join(watched_roots()))
    print("  例外通道   : %s" % ("开启中 tag=%s 到 %s" % (ov.get("tag"), ov.get("until")) if ov else "关闭"))
    pend = os.listdir(STATE_DIR) if os.path.isdir(STATE_DIR) else []
    print("  未闭合快照 : %d 个" % len([f for f in pend if f.startswith("ss_")]))
    return 0


def cmd_selftest(_args):
    """合成用例：每条断言一个反例 + 一个正例。用临时目录，不碰真实工作区。"""
    tmp = tempfile.mkdtemp(prefix="guard_selftest_")
    globals()["STATE_DIR"] = os.path.join(tmp, "_state")
    globals()["LOG_PATH"] = os.path.join(tmp, "guard.log")
    globals()["OVERRIDE_PATH"] = os.path.join(STATE_DIR, "override.json")
    os.environ["GUARD_ROOTS"] = tmp          # 让默认监控根 = 临时目录

    def mk(name, text, encoding="utf-8", eol="\n"):
        p = os.path.join(tmp, name)
        with open(p, "wb") as f:
            f.write(text.replace("\n", eol).encode(encoding))
        return p

    results = []

    def case(name, want, got, extra=""):
        ok = want == got
        results.append((name, ok, want, got, extra))

    def pre(sid, tool, args):
        return handle_pre({"hook_event_name": "PreToolUse", "session_id": sid,
                           "tool_name": tool, "tool_input": args, "cwd": tmp})[0]

    def post(sid, tool, args):
        return handle_post({"hook_event_name": "PostToolUse", "session_id": sid,
                            "tool_name": tool, "tool_input": args, "cwd": tmp})[0]

    # 1) CRLF → LF
    p = mk("a.md", "行1\n行2\n行3\n", eol="\r\n")
    case("CRLF→LF 应拦", 2, pre("t1", "write", {"file_path": p, "content": "行1\n行2\n行3\n"}))

    # 2) BOM 丢失
    p = mk("b.ps1", "Write-Host 中\n", encoding="utf-8-sig")
    case("BOM 丢失应拦", 2, pre("t2", "write", {"file_path": p, "content": "Write-Host 中\n"}))

    # 3) [[双链]] 下降
    p = mk("c.md", "- [[A]]\n- [[B]]\n")
    case("双链下降应拦", 2, pre("t3", "write", {"file_path": p, "content": "- [[A]]\n"}))

    # 4) 本来没有双链的文件不误伤
    p = mk("c2.md", "纯文本\n第二行\n")
    case("无双链文件不误伤", 0, pre("t3b", "write", {"file_path": p, "content": "纯文本\n第二行改\n"}))

    # 5) 整篇重写（60 行全换）
    p = mk("d.md", "".join("旧%d\n" % i for i in range(60)))
    case("整篇重写应拦", 2, pre("t4", "write",
                               {"file_path": p, "content": "".join("新%d\n" % i for i in range(60))}))

    # 6) 定点改：PRE 放行 + POST 通过
    p = mk("e.md", "行1\n行2\n行3\n", eol="\r\n")
    case("定点改 PRE 放行", 0, pre("t5", "write", {"file_path": p, "content": "行1\r\n行2改\r\n行3\r\n"}))
    mk("e.md", "行1\n行2改\n行3\n", eol="\r\n")
    case("定点改 POST 通过", 0, post("t5", "write", {"file_path": p, "content": "行1\r\n行2改\r\n行3\r\n"}))

    # 7) 局部替换（edit）后被写坏：PRE 放行（全文未知）→ POST 逮住
    p = mk("f.md", "- [[A]]\n- [[B]]\n- [[C]]\n", eol="\r\n")
    case("局部替换 PRE 放行（POST 兜）", 0, pre("t6", "edit",
                                             {"file_path": p, "old_string": "[[A]]", "new_string": "[[A2]]"}))
    mk("f.md", "- [[A2]]\n- [[C]]\n", eol="\n")
    case("写坏后 POST 必须拦", 2, post("t6", "edit",
                                    {"file_path": p, "old_string": "[[A]]", "new_string": "[[A2]]"}))

    # 8) 新建文件：PRE 与 POST 都不算违规
    p = os.path.join(tmp, "g.md")
    ok1 = pre("t7", "write", {"file_path": p, "content": "新文件\n"}) == 0
    with open(p, "wb") as f:
        f.write("新文件\n".encode("utf-8"))
    ok2 = post("t7", "write", {"file_path": p, "content": "新文件\n"}) == 0
    case("新建文件不误伤", True, ok1 and ok2)

    # 9) 监控根之外的路径不越权拦
    outside = os.path.join(os.path.dirname(tmp), "guard_outside_probe.md")
    case("监控根之外不拦", 0, pre("t8", "write", {"file_path": outside, "content": "x\n"}))

    # 10) shell 覆盖式直写受保护文件 → 拦；追加不算
    p = mk("h.md", "旧内容\n")
    case("shell 覆盖直写应拦", 2, pre("t9", "bash", {"command": 'echo hi > "%s"' % p}))
    case("shell 追加不拦", 0, pre("t9b", "bash", {"command": 'echo hi >> "%s"' % p}))

    # 11) 例外通道：开启后放行，且日志留 OVERRIDE-USED
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(OVERRIDE_PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"until": (datetime.datetime.now()
                             + datetime.timedelta(minutes=5)).isoformat(timespec="seconds"),
                   "tag": "selftest"}, f, ensure_ascii=False)
    p = mk("i.md", "行1\n行2\n", eol="\r\n")
    code = pre("t10", "write", {"file_path": p, "content": "行1\n行2\n"})
    mk("i.md", "行1\n行2\n", eol="\n")
    code2 = post("t10", "write", {"file_path": p, "content": "行1\n行2\n"})
    log_text = open(LOG_PATH, "rb").read().decode("utf-8", "replace") if os.path.exists(LOG_PATH) else ""
    case("allow 通道生效且留痕", True, code == 0 and code2 == 0 and "OVERRIDE-USED" in log_text)
    os.remove(OVERRIDE_PATH)

    # 12) 端到端：真子进程 + stdin JSON + exit 2 + stderr 有理由
    #     必须去掉 PYTHONIOENCODING：生产环境通常没有它（2026-09 实测：自测带它全绿，生产不带就崩）
    p = mk("j.md", "甲\n乙\n", eol="\r\n")
    payload = json.dumps({"hook_event_name": "PreToolUse", "session_id": "t11", "tool_name": "write",
                          "tool_input": {"file_path": p, "content": "甲\n乙\n"}}).encode("utf-8")
    env = dict(os.environ, GUARD_STATE=STATE_DIR, GUARD_LOG=LOG_PATH, GUARD_ROOTS=tmp)
    env.pop("PYTHONIOENCODING", None)
    proc = subprocess.run([sys.executable, os.path.abspath(__file__)], input=payload,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    case("子进程接线：exit2 + stderr 理由", True,
         proc.returncode == 2 and b"PRE" in proc.stderr,
         "rc=%s" % proc.returncode)

    ok_n = sum(1 for r in results if r[1])
    print("── write-guard selftest ──")
    for name, ok, want, got, extra in results:
        print("  [%s] %-30s want=%s got=%s %s"
              % ("PASS" if ok else "FAIL", name, want, got, extra))
    print("SELFTEST %s %d/%d" % ("PASS" if ok_n == len(results) else "FAIL", ok_n, len(results)))
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if ok_n == len(results) else 3


def main():
    # 中文理由必须完整送达模型：Windows 控制台默认 cp936 会把理由变成乱码，等于防线「喊了但听不清」
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(
        description="agent-write-guard：hook 入口（PreToolUse 拦 / PostToolUse 验）")
    ap.add_argument("--version", action="version", version="agent-write-guard " + VERSION)
    sub = ap.add_subparsers(dest="cmd")
    a = sub.add_parser("allow", help="限时开例外通道")
    a.add_argument("--tag", required=True, help="原因（会写进日志）")
    a.add_argument("--minutes", type=int, default=15)
    sub.add_parser("status", help="看状态")
    sub.add_parser("selftest", help="跑自测")
    args = ap.parse_args()
    if args.cmd == "allow":
        return cmd_allow(args)
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "selftest":
        return cmd_selftest(args)
    return run_stdin()


if __name__ == "__main__":
    sys.exit(main())

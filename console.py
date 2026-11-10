#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Claude Code 控制台 — 本地图形面板
启动/停止 magpie 网关、切换模型、调思考档位、进终端、查余额、发一句话跑一轮。

零依赖：只用 Python 标准库（http.server + json + subprocess）。
"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import collections
import glob
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("CCP_PORT", "7788"))
BASE = os.path.expanduser("~")
# 所有路径都可用环境变量覆盖，默认走常见的 Claude Code 目录
BIN = os.environ.get("CLAUDE_BIN_DIR") or os.path.join(BASE, ".local", "bin")
MAGPIE = os.environ.get("MAGPIE_BIN") or os.path.join(BIN, "magpie")
CLAUDE = os.environ.get("CLAUDE_BIN") or os.path.join(BIN, "claude")
CLAUDE_CONFIG_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(BASE, ".claude")
SETTINGS = os.path.join(CLAUDE_CONFIG_DIR, "settings.json")
SESSIONS_DIR = os.path.join(CLAUDE_CONFIG_DIR, "projects")
DEFAULT_WS = (os.environ.get("CLAUDE_PANEL_WORKSPACE")
              or os.path.join(BASE, "claude-workspace"))
GATEWAY = "http://127.0.0.1:3425"
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
MODES = ["default", "acceptEdits", "auto", "plan", "dontAsk", "bypassPermissions"]
# 可选：用 CCP_BLOCKED_GROUPS 屏蔽 magpie 预设分组（逗号分隔），默认屏蔽 relays
BLOCKED_PRESET_GROUPS = {
    g.strip() for g in os.environ.get("CCP_BLOCKED_GROUPS", "relays").split(",") if g.strip()
}
WORKSPACE = DEFAULT_WS
ROOT = os.environ.get("CLAUDE_PANEL_ROOT") or DEFAULT_WS

PERM_ALLOW = [
    "Read(//Users/*/Documents/**)",
    "Edit(//Users/*/Documents/**)",
    "Glob", "Grep", "LS", "NotebookRead",
    "Bash(ls *)", "Bash(cat *)", "Bash(head *)", "Bash(tail *)", "Bash(wc *)",
    "Bash(find *)", "Bash(rg *)", "Bash(grep *)", "Bash(sed -n *)", "Bash(awk *)",
    "Bash(pwd)", "Bash(whoami)", "Bash(date *)", "Bash(env)", "Bash(echo *)",
    "Bash(which *)", "Bash(type *)", "Bash(file *)", "Bash(du *)", "Bash(df *)",
    "Bash(git status *)", "Bash(git log *)", "Bash(git diff *)", "Bash(git show *)",
    "Bash(git branch *)", "Bash(git add *)", "Bash(git commit *)", "Bash(git stash *)",
    "Bash(git checkout *)", "Bash(git switch *)", "Bash(git restore *)",
    "Bash(gh pr view *)", "Bash(gh pr list *)", "Bash(gh issue list *)",
    "Bash(npm run *)", "Bash(npm test *)", "Bash(npm ls *)", "Bash(npx *)",
    "Bash(pnpm *)", "Bash(yarn *)", "Bash(node *)", "Bash(bun *)", "Bash(deno *)",
    "Bash(python3 *)", "Bash(pip3 list)", "Bash(uv *)", "Bash(pytest *)",
    "Bash(make *)", "Bash(cargo *)", "Bash(go build *)", "Bash(go test *)",
    "Bash(jq *)", "Bash(yq *)", "Bash(sort *)", "Bash(uniq *)", "Bash(cut *)",
    "Bash(tr *)", "Bash(diff *)", "Bash(mkdir -p *)", "Bash(touch *)",
    "Bash(cp *)", "Bash(mv *)", "Bash(open *)", "Bash(plutil *)",
    "Bash(sw_vers *)", "Bash(uname *)", "Bash(ps *)", "Bash(lsof -nP -iTCP:*)",
]

PERM_DENY = [
    "Read(~/.ssh/**)", "Read(~/.aws/**)", "Read(~/.gnupg/**)",
    "Read(**/.env)", "Read(**/*.pem)", "Read(**/*.p12)", "Read(**/id_rsa*)",
    "Bash(sudo *)", "Bash(su *)", "Bash(doas *)",
    "Bash(rm -rf /)", "Bash(rm -rf /*)", "Bash(rm -rf ~)", "Bash(rm -rf ~/*)",
    "Bash(rm -rf $HOME*)", "Bash(mkfs*)", "Bash(dd if=*of=/dev/*)",
    "Bash(diskutil *)", "Bash(shutdown *)", "Bash(reboot *)", "Bash(halt *)",
    "Bash(chmod -R 777 /*)", "Bash(chown -R * /*)",
    "Bash(git push --force *)", "Bash(git push -f *)",
    "Bash(:(){ :|:& };:)", "Bash(curl * | sh)", "Bash(curl * | bash)",
    "Bash(wget * | sh)", "Bash(history *)", "Bash(security find-generic-password*)",
]

PERM_ASK = [
    "Bash(git push *)", "Bash(git reset --hard *)", "Bash(git clean -fd *)",
    "Bash(rm *)", "Bash(npm publish *)", "Bash(pip install *)",
    "Bash(brew install *)", "Bash(brew uninstall *)", "Bash(kill *)",
    "Bash(pkill *)", "Bash(curl -X POST *)", "Bash(curl -X PUT *)",
    "Bash(curl -X DELETE *)",
]

ENV = dict(os.environ)
ENV["PATH"] = BIN + os.pathsep + ENV.get("PATH", "")
ENV["CLAUDE_CONFIG_DIR"] = CLAUDE_CONFIG_DIR


# ─────────────────────────── helpers ───────────────────────────

def run(cmd, timeout=40, env=None, cwd=None):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           env=env or ENV, cwd=cwd)
        out = (p.stdout or "") + (p.stderr or "")
        return p.returncode, out.strip()
    except subprocess.TimeoutExpired:
        return 124, "超时"
    except FileNotFoundError:
        return 127, "命令不存在: " + cmd[0]


def version_of(path):
    """取命令版本；命令不存在或输出为空时返回占位文案，避免 state 崩溃。"""
    if not os.path.exists(path):
        return "未安装"
    _, out = run([path, "--version"])
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    return lines[0] if lines else "未知"


def gateway_up():
    try:
        req = urllib.request.Request(GATEWAY + "/v1/models",
                                     headers={"Authorization": "Bearer magpie"})
        with urllib.request.urlopen(req, timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def read_settings():
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def write_settings(d):
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    tmp = SETTINGS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SETTINGS)


def models():
    """返回 [{'id': 'provider/model[1m]', 'label': ..., 'provider': ...}]"""
    if not gateway_up():
        return []
    try:
        req = urllib.request.Request(GATEWAY + "/v1/models",
                                     headers={"Authorization": "Bearer magpie"})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read().decode())
    except Exception:
        return []
    out, seen = [], set()
    for m in data.get("data", []):
        mid = m.get("id", "")
        if not mid or mid in seen:
            continue
        seen.add(mid)
        label = m.get("magpie_label") or m.get("display_name") or mid
        out.append({"id": mid, "label": label,
                    "provider": mid.split("/")[0] if "/" in mid else ""})
    return out


def providers():
    code, out = run([MAGPIE, "providers"])
    rows = []
    if code != 0:
        return rows, out
    for line in out.splitlines():
        line = line.rstrip()
        if not line.strip() or line.startswith("  %"):
            continue
        rows.append(line.strip())
    return rows, ""


_PRESETS_CACHE = {"at": 0.0, "rows": []}
_PRESETS_LOCK = threading.Lock()


def presets(force=False):
    """解析 magpie presets：名称、id、分组、是否已添加、是否只需要 key。

    magpie presets 单次约 3 秒，缓存 10 分钟；新增/替换成功后失效。
    """
    with _PRESETS_LOCK:
        now = time.time()
        if not force and _PRESETS_CACHE["rows"] and now - _PRESETS_CACHE["at"] < 600:
            return _PRESETS_CACHE["rows"]
        code, out = run([MAGPIE, "presets"])
        if code != 0:
            return _PRESETS_CACHE["rows"] or []
        rows = []
        group = ""
        for line in out.splitlines():
            raw = line.rstrip()
            if not raw.strip() or raw.lstrip().startswith("%"):
                continue
            if re.match(r"^\d{4}/\d{2}/\d{2}", raw.strip()):
                continue
            m = re.match(r"^\s{2,}(.+?)\s{2,}(\S+)\s{2,}(.+)$", raw)
            if m:
                name, pid, rest = (x.strip() for x in m.groups())
                rows.append({
                    "name": name,
                    "id": pid,
                    "group": group,
                    "added": "✓" in rest,
                    "needs_key": "<key>" in rest,
                    "blocked": group in BLOCKED_PRESET_GROUPS,
                })
            elif not re.search(r"\s{2,}", raw.strip()):
                group = raw.strip()
        _PRESETS_CACHE["rows"] = rows
        _PRESETS_CACHE["at"] = time.time()
        return rows


def set_provider_key(target, key):
    """新增预设或替换已有 provider 的 key；key 只传给 magpie，不回显。"""
    target = target if isinstance(target, str) else ""
    key = key.strip() if isinstance(key, str) else ""
    if not key:
        return False, "key 不能为空"
    if target.startswith("add:"):
        pid = target[4:]
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", pid):
            return False, "非法预设 id"
        p = next((x for x in presets() if x["id"] == pid), None)
        if not p:
            return False, "未知预设: " + pid
        if p.get("blocked"):
            return False, "该供应商属于中转站，已屏蔽"
        if p["added"]:
            return False, "该预设已存在，请选择「替换已有」"
        if not p["needs_key"]:
            return False, "该预设不接受纯 key，请用 magpie provider add 配置"
        code, out = run([MAGPIE, "provider", "add", pid, key], timeout=120)
        okmsg = "已添加 " + p["name"] + "（" + pid + "）"
    elif target.startswith("replace:"):
        pid = target[8:]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", pid):
            return False, "非法 provider id"
        if any(x.get("blocked") and x["id"] == pid for x in presets()):
            return False, "该 provider 属于中转站，已屏蔽"
        code, out = run([MAGPIE, "provider", "key", pid, key], timeout=120)
        okmsg = "已更新 " + pid + " 的 key"
    else:
        return False, "请选择要添加或替换的目标"
    out = (out or "").replace(key, "***").strip()
    if len(out) > 400:
        out = out[:400] + "…"
    if code != 0:
        return False, out or ("保存失败，退出码 " + str(code))
    if target.startswith("add:"):
        _PRESETS_CACHE["at"] = 0.0
    return True, okmsg


def provider_ids():
    """从 magpie providers 输出里取已有 provider id。"""
    rows, _ = providers()
    ids = set()
    for line in rows:
        if re.match(r"^\d{4}/\d{2}/\d{2}", line):
            continue
        m = re.match(r"^(.+?)\s{2,}(\S+)\s{2,}", line)
        if m:
            ids.add(m.group(2))
    return ids


def remove_provider(pid):
    """删除已有 provider，会一并移除它的 key；不可撤销。"""
    pid = pid.strip() if isinstance(pid, str) else ""
    if not pid:
        return False, "缺少 provider id"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", pid):
        return False, "非法 provider id"
    if any(x.get("blocked") and x["id"] == pid for x in presets()):
        return False, "该 provider 属于中转站，已屏蔽"
    if pid not in provider_ids():
        return False, "未找到 provider: " + pid
    code, out = run([MAGPIE, "provider", "rm", pid], timeout=60)
    out = (out or "").strip()
    if len(out) > 400:
        out = out[:400] + "…"
    if code != 0:
        return False, out or ("删除失败，退出码 " + str(code))
    _PRESETS_CACHE["at"] = 0.0
    return True, "已删除 " + pid


SESSION_LINE_RE = re.compile(
    rb'"type"\s*:\s*"(?:user|assistant|ai-title|last-prompt)"')
SESSION_TAG_RE = re.compile(
    r"<(system-reminder|command-name|command-message|command-args|"
    r"local-command-stdout|ide_opened_file|user-prompt-submit-hook)>"
    r".*?</\1>", re.S)


def clean_session_text(text):
    if not isinstance(text, str):
        return ""
    text = SESSION_TAG_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def extract_session_message(msg):
    if not isinstance(msg, dict):
        return "", []
    content = msg.get("content")
    texts, tools = [], []
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            typ = block.get("type")
            if typ == "text":
                texts.append(str(block.get("text") or ""))
            elif typ == "tool_use":
                tools.append(str(block.get("name") or "工具"))
    return clean_session_text("\n".join(t for t in texts if t)), tools


def session_files():
    files = []
    if not os.path.isdir(SESSIONS_DIR):
        return files
    try:
        projects = list(os.scandir(SESSIONS_DIR))
    except OSError:
        return files
    for project in projects:
        try:
            if not project.is_dir():
                continue
            entries = os.scandir(project.path)
        except OSError:
            continue
        with entries:
            for ent in entries:
                if not ent.name.endswith(".jsonl"):
                    continue
                sid = ent.name[:-6]
                if not re.fullmatch(r"[0-9a-fA-F-]{8,64}", sid):
                    continue
                try:
                    if not ent.is_file():
                        continue
                    mtime = ent.stat().st_mtime
                except OSError:
                    continue
                files.append((mtime, sid, ent.path))
    files.sort(key=lambda x: x[0], reverse=True)
    return files


def find_session_file(sid):
    if not isinstance(sid, str) or not re.fullmatch(r"[0-9a-fA-F-]{8,64}", sid):
        return None
    hits = glob.glob(os.path.join(SESSIONS_DIR, "*", sid + ".jsonl"))
    return hits[0] if hits else None


def session_summary(path, sid, mtime):
    cwd = ""
    first_prompt = ""
    first_title = ""
    try:
        with open(path, "rb") as f:
            for i, raw in enumerate(f):
                if i >= 300:
                    break
                if not SESSION_LINE_RE.search(raw[:65536]):
                    continue
                try:
                    e = json.loads(raw.decode("utf-8", "replace"))
                except Exception:
                    continue
                if not isinstance(e, dict) or e.get("isSidechain"):
                    continue
                if not cwd:
                    cwd = str(e.get("cwd") or "")
                typ = e.get("type")
                if typ == "ai-title" and not first_title:
                    first_title = clean_session_text(str(e.get("aiTitle") or ""))
                elif typ == "user" and not first_prompt:
                    text, _ = extract_session_message(e.get("message") or {})
                    if text:
                        first_prompt = " ".join(text.split())
                if cwd and first_prompt:
                    break
    except OSError:
        pass

    last_title = ""
    last_prompt = ""
    try:
        size = os.path.getsize(path)
        start = max(0, size - 256 * 1024)
        with open(path, "rb") as f:
            if start:
                f.seek(start)
                f.readline()
            for raw in f:
                if not SESSION_LINE_RE.search(raw[:65536]):
                    continue
                try:
                    e = json.loads(raw.decode("utf-8", "replace"))
                except Exception:
                    continue
                if not isinstance(e, dict):
                    continue
                typ = e.get("type")
                if typ == "ai-title":
                    t = clean_session_text(str(e.get("aiTitle") or ""))
                    if t:
                        last_title = t
                elif typ == "last-prompt":
                    t = clean_session_text(str(e.get("lastPrompt") or ""))
                    if t:
                        last_prompt = t
    except OSError:
        pass

    title = last_title or first_title or first_prompt or "（无标题会话）"
    return {
        "id": sid,
        "cwd": cwd,
        "project": os.path.basename(cwd.rstrip("/")) or cwd or "未知项目",
        "title": " ".join(title.split())[:120],
        "last": " ".join(last_prompt.split())[:160],
        "mtime": int(mtime),
    }


def sessions_payload(limit=40):
    rows = []
    for mtime, sid, path in session_files()[:limit]:
        try:
            rows.append(session_summary(path, sid, mtime))
        except Exception:
            continue
    return rows


def session_detail(sid):
    path = find_session_file(sid)
    if not path:
        return {"ok": False, "msg": "会话不存在"}
    try:
        size = os.path.getsize(path)
    except OSError as e:
        return {"ok": False, "msg": "读取失败: " + str(e)}
    window = 4 * 1024 * 1024
    cut = size > window
    messages = collections.deque(maxlen=200)
    total = 0
    cwd = ""
    title = ""
    try:
        with open(path, "rb") as f:
            if cut:
                f.seek(size - window)
                f.readline()
            for raw in f:
                if not SESSION_LINE_RE.search(raw[:65536]):
                    continue
                try:
                    e = json.loads(raw.decode("utf-8", "replace"))
                except Exception:
                    continue
                if not isinstance(e, dict) or e.get("isSidechain"):
                    continue
                typ = e.get("type")
                if typ == "ai-title":
                    t = clean_session_text(str(e.get("aiTitle") or ""))
                    if t:
                        title = t
                    continue
                if typ not in ("user", "assistant"):
                    continue
                if not cwd:
                    cwd = str(e.get("cwd") or "")
                text, tools = extract_session_message(e.get("message") or {})
                if not text and not tools:
                    continue
                total += 1
                messages.append({
                    "role": typ,
                    "text": text[:3000] + ("…" if len(text) > 3000 else ""),
                    "tools": [str(t)[:40] for t in tools[:8]],
                })
    except OSError as e:
        return {"ok": False, "msg": "读取失败: " + str(e)}

    if not cwd:
        try:
            cwd = session_summary(path, sid, 0)["cwd"]
        except Exception:
            pass
    try:
        mtime = int(os.path.getmtime(path))
    except OSError:
        mtime = 0
    return {
        "ok": True,
        "id": sid,
        "cwd": cwd,
        "project": os.path.basename(cwd.rstrip("/")) or cwd,
        "title": " ".join(title.split())[:120],
        "mtime": mtime,
        "messages": list(messages),
        "total": total,
        "truncated": cut or total > len(messages),
    }


def quota():
    code, out = run([MAGPIE, "quota"])
    return out.strip() if code == 0 else (out.strip() or "读取失败")


def start_gateway():
    if gateway_up():
        return True, "网关已在运行"
    log = open(os.path.join(BASE, ".magpie-gateway.log"), "a")
    subprocess.Popen([MAGPIE, "serve"], stdout=log, stderr=log,
                     env=ENV, start_new_session=True)
    for _ in range(60):
        time.sleep(0.5)
        if gateway_up():
            return True, "网关已启动"
    return False, "启动超时，看 ~/.magpie-gateway.log"


def stop_gateway():
    run(["pkill", "-f", "magpie serve"])
    for _ in range(20):
        time.sleep(0.25)
        if not gateway_up():
            return True, "网关已停止"
    return False, "仍在运行"


def state():
    s = read_settings()
    s0 = s
    provs, prov_err = providers()
    return {
        "gateway": gateway_up(),
        "model": s.get("model", ""),
        "effort": s.get("effortLevel", "default"),
        "subagent_effort": s.get("subagentEffort", "default"),
        "models": models(),
        "providers": provs,
        "providers_err": prov_err,
        "quota": quota(),
        "efforts": EFFORTS,
        "perms": {
            "defaultMode": (s0.get("permissions") or {}).get("defaultMode", "default"),
            "allow": (s0.get("permissions") or {}).get("allow", []),
            "deny": (s0.get("permissions") or {}).get("deny", []),
            "ask": (s0.get("permissions") or {}).get("ask", []),
            "additionalDirectories":
                (s0.get("permissions") or {}).get("additionalDirectories", []),
        },
        "modes": MODES,
        "perm_presets": {
            "workspace": {"allow": PERM_ALLOW, "deny": PERM_DENY,
                          "ask": PERM_ASK, "mode": "acceptEdits"},
        },
        "thinking": {
            "disabled": (s0.get("env", {}) or {}).get("CLAUDE_CODE_DISABLE_THINKING") == "1",
            "adaptive_off": (s0.get("env", {}) or {}).get("CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING") == "1",
            "budget": (s0.get("env", {}) or {}).get("MAX_THINKING_TOKENS", ""),
        },
        "settings_path": SETTINGS,
        "workspace": DEFAULT_WS,
        "config_dir": CLAUDE_CONFIG_DIR,
        "claude_version": version_of(CLAUDE),
        "magpie_version": version_of(MAGPIE),
    }


def set_model(mid):
    if "[1m]" not in mid:
        mid = mid + "[1m]"
    code, out = run([MAGPIE, "claude", mid])
    return code == 0, out


def set_effort(level, field="effort"):
    if level not in EFFORTS and level != "default":
        return False, "非法档位: " + level
    if field == "effort":
        s = read_settings()
        if level == "default":
            s.pop("effortLevel", None)
        else:
            s["effortLevel"] = level
        write_settings(s)
        return True, "effort → " + level
    code, out = run([MAGPIE, "claude", "subagent_effort", level])
    return code == 0, out


def set_perms(mode=None, allow=None, deny=None, ask=None, dirs=None):
    s = read_settings()
    perms = s.setdefault("permissions", {})
    if mode is not None:
        if mode not in MODES:
            return False, "非法权限模式: " + mode
        perms["defaultMode"] = mode
    for key, val in (("allow", allow), ("deny", deny), ("ask", ask),
                     ("additionalDirectories", dirs)):
        if val is None:
            continue
        if isinstance(val, str):
            val = [x.strip() for x in val.splitlines() if x.strip()]
        perms[key] = val
    if not perms.get("allow"):
        perms.pop("allow", None)
    if not perms.get("deny"):
        perms.pop("deny", None)
    if not perms.get("ask"):
        perms.pop("ask", None)
    if not perms.get("additionalDirectories"):
        perms.pop("additionalDirectories", None)
    write_settings(s)
    return True, "权限已保存（新会话生效）"


def apply_preset(name):
    if name != "workspace":
        return False, "未知预设: " + str(name)
    dirs = list(dict.fromkeys([WORKSPACE, ROOT]))
    ok, msg = set_perms(mode="acceptEdits", allow=PERM_ALLOW, deny=PERM_DENY,
                        ask=PERM_ASK, dirs=dirs)
    return ok, "已套用「工作区全放开」预设：allow %d 条 / deny %d 条 / ask %d 条" % (
        len(PERM_ALLOW), len(PERM_DENY), len(PERM_ASK))


def set_thinking(disabled=None, adaptive_off=None, budget=None):
    s = read_settings()
    env = s.setdefault("env", {})
    if disabled is not None:
        if disabled:
            env["CLAUDE_CODE_DISABLE_THINKING"] = "1"
        else:
            env.pop("CLAUDE_CODE_DISABLE_THINKING", None)
    if adaptive_off is not None:
        if adaptive_off:
            env["CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING"] = "1"
        else:
            env.pop("CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING", None)
    if budget is not None:
        b = str(budget).strip()
        if b in ("", "0"):
            env.pop("MAX_THINKING_TOKENS", None)
        elif b.isdigit():
            env["MAX_THINKING_TOKENS"] = b
        else:
            return False, "预算必须是数字: " + b
    if not env:
        s.pop("env", None)
    write_settings(s)
    return True, "思考设置已保存"


def run_prompt(prompt, cwd, timeout=300, verbose=False):
    if not cwd or not os.path.isdir(cwd):
        return False, "工作目录不存在: " + str(cwd)
    cmd = [CLAUDE, "-p", prompt, "--output-format", "text"]
    if verbose:
        cmd += ["--verbose"]
    code, out = run(cmd, timeout=timeout, env={**ENV}, cwd=cwd)
    out = "\n".join(l for l in out.splitlines()
                     if "claude-code:unrecognized_model" not in l).strip()
    return code == 0, out or "(无输出)"


# ─────────────────────────── HTTP ───────────────────────────

HTML = r"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Claude Code 控制台</title>
<link rel="icon" type="image/png" href="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAAAAXNSR0IArs4c6QAAAERlWElmTU0AKgAAAAgAAYdpAAQAAAABAAAAGgAAAAAAA6ABAAMAAAABAAEAAKACAAQAAAABAAAAQKADAAQAAAABAAAAQAAAAABGUUKwAAAHd0lEQVR4Ae1bbWxTVRh+29uvdevYWtgYG2zYbTCGfA1xIxgNI4gKxvBDkUQlMRINovjDv4Tw2x8IEpFATDQhSCIxKmIgI8YsMqYLXzJgsDG2wWCs27pu6/o53+dipd7bz9216z5O0q3n9pxz3+d53/N+nNsSTbdpBqYZmMoMqOIFX129ymJ76Kh0ez2rfF6flWgkc2RkRB3vOmMxXqVS+YlU/YJGaNZptPWWXFNdTU29LZ61YyagsnJZkaOvf+ewy/O61+svYNCEVyo0JoLw0mjUHQa99oQpK/NAXd2l1lhki0oAY1QtKy/ZPuB07fV4vDl+P5Oewk2tVpNWq+nKSNPvvnTt1mHmJaKWIprunj0vaMrLrPvtA0OHXC53yoOHXqAgyAqZITswRNJXWAtg81YtWmA9MOR07fD5fJHWSNnPBEEgY5r+YOPN5p28RUJaQlgLWLJ4wXvOCQweWoHigAFYwmkppAWsqaiY19nT1eByeWaGmziRruv12u48c05FbUNDm1TukBZgc/Tu8nh8kwI8AAMLMEnBoy8j4MWqKrPL7dmS6t4+FJhw10THyJiATTpGRsBDR1+V3+fPkw6c6H1gAjYpDhkBLo/7WX+KJDhSYZX0gQnYpGvICPD7fNZUyfCkwirpAxOwSdeQEcAJbqZ00GTph8ImI4ATx5ChcVKQEAKbnIBJgTR2EFOegIiFQqw8en0j5PM/TrUFNZelwsTZRYoJAPjivHR6a20hub1+Ov57O93ocJBBK3CNHiuF4zdOEQFIFwxaNe1+cxGtKM4SraB6aQ4dPXOHTtR2cNz1k06T2rtMkXSIrUaDhvLMBnK6uPLiV0aahj7dvIAOvr+cFhdm0rDbR6mcWCkiQM37vcfhppN/3COB9z32vp99wbDHR8+UZtNXO1bQjo1WcTvAGlKxCVKhLOasrV6vr1R6PVxfxSTUN/VSc+cgLSww0awZenErwDdoBTVVLbTQqlIzddic1No1KJ7dqcfBOeDMUKsRbtl67ceCsSgngFeDJcDx1Vx+RHqdmsoKMnFA+R8RedkGWr8il8wZOrrW5iCH05v0SJEwAgJsaljbQ+wDfrvaTdfbHVQyJ4MAHOERL+aIKoqzac2imdRld1Hzg0FxarKsIeEEAA3AwA+0MLgzFx/yIwOi8nmZpOeQCBKwLSwmPa1fnktzzGnUyNbQN+Bm/6FOeP6dFAKeWIOK3Oz0ahu76WJLHxXlptPcWUZ2kHxOx0SAmKeLZtDaJbPIPuShpvsD4lQImaiWVAIAAjfEtoDzgzVge5RzWDTqn1hDplFL65bmUr4ljepu9IgWkigOwhGgKAzGoi0kQsgQD//aQtv2/Unnb9gI6TIatoSHP9tclU/Vy3LI5U3+8XvCCQgmiR9gRX5MEzw4Se8VpcKxyAjtG3UCbasuoneqC8lk1Ij+AXPhMOE4T56/RzWXukivkUXlWG6haEzCCEBGCPArS7Lp41dLaLk1SwQO54h9jmKpvXuIDp5qodMND8QwmayQGMxYQghA2jsjXUsfriukLc/PFcGiJkBDdoja4HtOn7/8pYU6e5ycPHHlGCxVEt+PKQGBWP9cuYU+Yq2XcWqMugCWEND6LQ55+3+6zQnTI9EZGhj8eLYxIwAazs0y0PaX5tNrlXPEEOj8V+uBSPDNubt05Ewr2biAQhkd3GAVPo4KnCLE1AJJV0yDIwxSTADOBKD5DRWzaecmKxXlpItaR3iDkHoGeqXVTp//eJvqbvbwFlDJwGM+yujivAzRKqIdyyOmO5wegjXhvZLto5gACP/By0/R9g3zRSICWgfwAS56jp69Q9+eaxMLIKnWoRg4S4tJR5+9u0Q8PwCh0awAgGExX5+9S4dOt4jRJIKSI36kiAAIb2bhN6/OF5MakIHKUMeOrr6ph/b9eIuu3LGTjskAIaEa/MPqMgut5EJpYNgbakjIawihuO93te1kH/SI1hZyYJSLigiA+Q25vOzJh8VTIRBg63f/70gsmpNDVnif58NZYmw08w/g0XKG2dnbT0PDPt4Co98ECgkgPvLy097jjfQ2H4oi/AUfisZyHgggDbd7ac+x62JxxJxGbSAeWodTxT1hDaNtspml1sKfncPuV2LVBG6MnB7aRxvtsTgSJChSJpC4quQPD0JlGc+9QFqaQXeqqfnuxuDVFFlAYCFoQIkWsA78RFxtjNKHOO8al4gTYvA0ATI1pV7FKhNx1BdCYJNZAIeU/lHfIMUnhsImI0AtCM3wmJOtAROwSXHJCNBrdRfGoy6XCjbW/cd1ie6CdF0ZAbmmrPNqQd0pHTjR+8AEbFIcsmja3NHhnJ0zM58fj1XFkwxJF06lPr5BnmbQH6m7fPUHqVwyC8AAiyl7n1YrdEsHT9Q+sABTKPllFoBBbZ2d9oL82Xavx7splR9thwIkvabhb4xnZBg/+evy3+ekn6Ef1t2z+U/tr8tz2Bh5Y+u8Xenphi8043BcHUpb8VyDzJAdGIAl3NywFhCYwAcvk/onM1EJCBAxZX80FSAg8H+y/WwugGv6/zQD0wxMTQb+ASkFV3fEIieIAAAAAElFTkSuQmCC">
<script>try{var t=localStorage.getItem('ccpanel-theme');document.documentElement.dataset.theme=t==='light'?'light':'dark'}catch(e){document.documentElement.dataset.theme='dark'}</script>
<style>
:root{
--bg:#161513;--rail:#12110f;--panel:#1c1a18;--field:#100f0d;--raised:#262421;
--raised-hover:#2e2b27;
--line:rgba(239,233,222,.08);--line-strong:rgba(239,233,222,.16);
--fg:#efe9de;--fg2:#aaa398;--muted:#8a847a;
--ok:#4cc38a;--bad:#e5484d;--accent:#e0a45c;
--pri-bg:#efe9de;--pri-fg:#1a1713;--pri-bg-hover:#fff;
--focus-ring:rgba(224,164,92,.12);--hover-tint:rgba(239,233,222,.04);
--thumb-highlight:rgba(255,255,255,.06);
--lamp-off:#3c3830;--lamp-ring:rgba(0,0,0,.4);
--panel-shadow:none;--toast-shadow:none;
--mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,monospace;
--ease:cubic-bezier(.22,1,.36,1)}
html[data-theme="light"]{
color-scheme:light;
--bg:#f6f2e9;--rail:#f0ebe1;--panel:#fffdf8;--field:#fffefa;--raised:#ece5d8;
--raised-hover:#e4dccd;
--line:rgba(31,27,22,.10);--line-strong:rgba(31,27,22,.18);
--fg:#1f1b16;--fg2:#5e554a;--muted:#7b7266;
--ok:#16a34a;--bad:#dc2626;--accent:#8a5a1e;
--pri-bg:#1f1b16;--pri-fg:#fffdf8;--pri-bg-hover:#000;
--focus-ring:rgba(138,90,30,.16);--hover-tint:rgba(31,27,22,.04);
--thumb-highlight:rgba(255,255,255,.7);
--lamp-off:#c9c1b2;--lamp-ring:rgba(31,27,22,.25);
--panel-shadow:0 1px 2px rgba(31,27,22,.04);
--toast-shadow:0 8px 24px rgba(31,27,22,.10)}
*{box-sizing:border-box}
html{color-scheme:dark}
body{margin:0;min-height:100vh;background:var(--bg);color:var(--fg);
font:14px/1.55 -apple-system,BlinkMacSystemFont,"PingFang SC","Helvetica Neue",sans-serif;
-webkit-font-smoothing:antialiased}
.app{display:grid;grid-template-columns:280px minmax(0,1fr);max-width:1440px;margin:0 auto;
min-height:100vh;border-left:1px solid var(--line);border-right:1px solid var(--line)}
.rail{position:sticky;top:0;align-self:start;height:100vh;display:flex;flex-direction:column;
gap:22px;padding:28px 24px;background:var(--rail);border-right:1px solid var(--line);
overflow:auto}
.brand h1{margin:0;font-size:16px;line-height:1.3;font-weight:500;letter-spacing:-.005em}
.brand-note{display:block;margin-top:4px;font:10.5px/1.5 var(--mono);color:var(--muted)}
.rail-block{padding-top:20px;border-top:1px solid var(--line)}
.k{font-size:11px;line-height:16px;color:var(--muted);letter-spacing:.01em}
.k-row{display:flex;align-items:baseline;justify-content:space-between;gap:8px;
margin-bottom:10px}
.link{border:0;background:none;padding:0;height:auto;font-size:11px;line-height:16px;
color:var(--muted);cursor:pointer;font-family:inherit;
transition:color 150ms var(--ease)}
.link:hover{color:var(--fg);background:none;border:0}
.status{display:flex;align-items:center;gap:8px;margin-top:6px;font-size:15px;
line-height:20px;font-weight:500}
.status.off{color:var(--fg2)}
.lamp{width:8px;height:8px;border-radius:2px;flex:0 0 auto;background:var(--lamp-off);
box-shadow:inset 0 0 0 1px var(--lamp-ring),0 0 0 1px var(--line-strong)}
.lamp.on{background:var(--ok)}
.rail-line{margin-top:6px}
.mono{font:12px/1.5 var(--mono);color:var(--fg2);font-variant-numeric:tabular-nums;
word-break:break-all}
.rail-model{margin-top:6px}
.ledger-row{display:flex;align-items:baseline;justify-content:space-between;gap:12px;
padding:9px 0;border-top:1px solid var(--line)}
.ledger-row:first-child{border-top:0;padding-top:0}
.ledger-row .name{font:11px/1.4 var(--mono);color:var(--fg2);word-break:break-all}
.ledger-row .amount{font:17px/1 var(--mono);font-weight:500;color:var(--fg);
font-variant-numeric:tabular-nums;white-space:nowrap}
.ledger-row .cur{color:var(--accent);font-size:11px;font-weight:400;margin-right:1px}
details.raw{margin-top:10px}
details.raw summary{font-size:10.5px;line-height:16px;color:var(--muted);cursor:pointer;
list-style:none;user-select:none}
details.raw summary::-webkit-details-marker{display:none}
details.raw summary::before{content:"+";display:inline-block;width:10px;font-family:var(--mono)}
details.raw[open] summary::before{content:"−"}
details.raw[open] summary{margin-bottom:8px}
details.raw pre{max-height:150px;font-size:10px;line-height:16px;padding:8px}
.rail-foot{margin-top:auto;padding-top:20px;border-top:1px solid var(--line);
font:10px/1.6 var(--mono);color:var(--muted);word-break:break-all}
.main{min-width:0;padding:28px 32px 48px}
.launch{background:var(--panel);border:1px solid var(--line);border-radius:12px;
padding:18px 20px;box-shadow:var(--panel-shadow)}
.launch .k{margin-bottom:8px}
.launch-row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.launch-row input{flex:1 1 300px;height:38px;font:12px var(--mono)}
.launch-row .pri{height:38px;padding:0 20px}
.launch-sub{margin-top:10px}
.launch+.sec{margin-top:28px}
.sec{display:grid;grid-template-columns:168px minmax(0,1fr);gap:32px;
padding:22px 0;border-top:1px solid var(--line)}
.sec-head h2{margin:0;font-size:13px;line-height:18px;font-weight:500;color:var(--fg)}
.sec-head p{margin:4px 0 0;font-size:11px;line-height:16px;color:var(--muted);max-width:20ch}
.sec-body{min-width:0}
label{display:block;margin:0 0 6px;font-size:11.5px;line-height:16px;color:var(--fg2)}
.lh{color:var(--muted);font-size:11px;margin-left:6px}
input,select,textarea{width:100%;min-width:0;background:var(--field);color:var(--fg);
border:1px solid var(--line);border-radius:8px;padding:0 10px;font-family:inherit;
font-size:13px;line-height:1.5;
transition:border-color 150ms var(--ease),background-color 150ms var(--ease)}
input,select{height:36px}
textarea{min-height:92px;padding:9px 10px;resize:vertical;font:12px/18px var(--mono);
caret-color:var(--accent)}
textarea.ta-lg{min-height:116px}
input:hover,select:hover,textarea:hover{border-color:var(--line-strong)}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--accent);
box-shadow:0 0 0 3px var(--focus-ring)}
button{height:34px;padding:0 13px;border-radius:8px;border:1px solid var(--line);
background:var(--raised);color:var(--fg);font-family:inherit;font-size:12.5px;line-height:1;
font-weight:400;cursor:pointer;
transition:background-color 150ms var(--ease),border-color 150ms var(--ease),color 150ms var(--ease)}
button:hover{border-color:var(--line-strong);background:var(--raised-hover)}
button:active{transform:translateY(1px)}
button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
button.pri{background:var(--pri-bg);border-color:transparent;color:var(--pri-fg);
font-weight:500}
button.pri:hover{background:var(--pri-bg-hover)}
button.danger{color:var(--fg2)}
button.danger:hover{background:var(--raised);border-color:rgba(229,72,77,.5);color:var(--bad)}
button:disabled{opacity:.4;cursor:not-allowed;transform:none}
.row{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.grow{flex:1 1 auto;width:auto;min-width:0}
.mt-10{margin-top:10px}.mt-12{margin-top:12px}.mt-16{margin-top:16px}
.seg{display:flex;gap:2px;padding:3px;background:var(--field);
border:1px solid var(--line);border-radius:9px;position:relative}
.seg button{flex:1;min-width:0;height:28px;padding:0 8px;border:0;border-radius:6px;
background:transparent;color:var(--fg2);font:11.5px/1 var(--mono);white-space:nowrap;
overflow:hidden;text-overflow:ellipsis;position:relative;z-index:1}
.seg button:hover{background:var(--hover-tint);color:var(--fg)}
.seg button.act{color:var(--fg)}
.seg button.act:hover{background:transparent}
.seg-thumb{position:absolute;left:0;top:0;z-index:0;border-radius:6px;
background:var(--raised);box-shadow:inset 0 1px 0 var(--thumb-highlight);
pointer-events:none;transition:transform 220ms var(--ease)}
.seg.toggles button.act{background:var(--raised);color:var(--fg);
box-shadow:inset 0 1px 0 var(--thumb-highlight)}
.seg3{display:grid;grid-template-columns:repeat(3,minmax(0,1fr))}
.seg6{display:grid;grid-template-columns:repeat(6,minmax(0,1fr))}
.two{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px}
.three{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:24px}
pre{margin:0;padding:10px 12px;background:var(--field);border:1px solid var(--line);
border-radius:8px;color:var(--fg2);font:11px/17px var(--mono);white-space:pre-wrap;
word-break:break-word;max-height:240px;overflow:auto;font-variant-numeric:tabular-nums}
.hint{margin:12px 0 0;font-size:11.5px;line-height:18px;color:var(--muted);max-width:72ch}
.answer{margin-top:16px;max-height:340px}
.check{display:flex;align-items:center;gap:6px;margin:0;color:var(--fg2);font-size:12.5px;
cursor:pointer;user-select:none}
.check input{width:auto;height:auto;accent-color:var(--accent);margin:0}
.meta .k{font-size:11px;color:var(--muted);margin-bottom:4px}
.meta .v{font:12px/1.5 var(--mono);color:var(--fg2);word-break:break-all}
.prov-row{display:flex;align-items:baseline;justify-content:space-between;gap:20px;
padding:10px 0;border-top:1px solid var(--line)}
.prov-row:first-child{border-top:0;padding-top:0}
.prov-name{font-size:13px;line-height:18px;color:var(--fg)}
.prov-sub{font-size:11px;line-height:16px;color:var(--muted);margin-top:2px}
.prov-meta{font-size:11px;line-height:16px;color:var(--muted);text-align:right;
white-space:nowrap}
.key-form{margin-top:16px;padding-top:16px;border-top:1px solid var(--line)}
.key-head{display:flex;align-items:center;justify-content:space-between;
gap:12px;flex-wrap:wrap;margin-bottom:8px}
.key-head .k{margin:0}
.key-head .seg{flex:0 0 auto;min-width:210px}
.key-row{display:grid;grid-template-columns:minmax(0,1.1fr) minmax(0,1.3fr) auto;
gap:8px;align-items:center}
.key-row.deleting{grid-template-columns:minmax(0,1fr) auto}
.key-row.deleting #keyValue{display:none}
.key-row input{font:12px var(--mono)}
.key-row button{height:36px}
#histSec{scroll-margin-top:16px}
.hist-bar{display:flex;align-items:center;justify-content:space-between;gap:12px;
margin-bottom:10px}
.hist-wrap{display:grid;grid-template-columns:minmax(220px,280px) minmax(0,1fr);
gap:18px;align-items:start}
.hist-list{max-height:520px;overflow:auto;padding-right:14px;
border-right:1px solid var(--line)}
.hist-row{display:flex;align-items:flex-start;gap:10px;padding:10px;
border-radius:8px;cursor:pointer;transition:background-color 140ms var(--ease)}
.hist-row:hover{background:var(--hover-tint)}
.hist-row.act{background:var(--raised)}
.hist-row:focus-visible{outline:2px solid var(--accent);outline-offset:-2px}
.hist-main{min-width:0;flex:1}
.hist-title{font-size:12.5px;line-height:18px;font-weight:500;color:var(--fg);
display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.hist-last{margin-top:3px;font-size:11px;line-height:16px;color:var(--muted);
white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.hist-meta{margin-top:3px;font:10.5px/15px var(--mono);color:var(--muted);
white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.hist-enter{flex:0 0 auto;height:28px;padding:0 9px;font-size:11.5px}
.hist-detail{min-width:0;min-height:260px}
.hist-placeholder{display:flex;align-items:center;justify-content:center;
min-height:240px;padding:24px;background:var(--field);border:1px solid var(--line);
border-radius:10px;color:var(--muted);font-size:12.5px;text-align:center}
.hist-dhead{display:flex;align-items:flex-start;justify-content:space-between;
gap:16px;flex-wrap:wrap;padding-bottom:12px;border-bottom:1px solid var(--line)}
.hist-dmain{min-width:0;flex:1}
.hist-dtitle{font-size:13.5px;line-height:19px;font-weight:500;color:var(--fg);
display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.hist-dmeta{margin-top:4px;font:10.5px/15px var(--mono);color:var(--muted)}
.hist-dcwd{margin-top:3px;font:10.5px/15px var(--mono);color:var(--muted);
word-break:break-all}
.hist-dhead .pri{height:32px;flex:0 0 auto}
.hist-thread{max-height:430px;overflow:auto;padding:4px 8px 0 0}
.hist-msg{display:grid;grid-template-columns:40px minmax(0,1fr);gap:10px;
padding:12px 0;border-top:1px solid var(--line)}
.hist-msg:first-child{border-top:0;padding-top:8px}
.hist-role{font:10px/16px var(--mono);color:var(--muted);padding-top:2px}
.hist-text{font-size:12.5px;line-height:20px;color:var(--fg);
white-space:pre-wrap;word-break:break-word}
.hist-text.assistant{color:var(--fg2)}
.hist-tools{margin-top:5px;font:10px/15px var(--mono);color:var(--muted)}
.tag{font-size:10px;line-height:14px;color:var(--fg2);border:1px solid var(--line);
border-radius:4px;padding:0 5px;margin-left:6px;vertical-align:1px}
.toast{position:fixed;right:24px;bottom:24px;max-width:min(420px,calc(100vw - 48px));
padding:10px 14px;background:var(--raised);border:1px solid var(--line-strong);
border-radius:10px;font-size:13px;color:var(--fg);opacity:0;transform:translateY(4px);
transition:opacity 180ms var(--ease),transform 180ms var(--ease);pointer-events:none;
box-shadow:var(--toast-shadow)}
.toast.show{opacity:1;transform:none}
.toast.bad{border-color:rgba(229,72,77,.45);color:var(--bad)}
@media (max-width:1120px){
.seg6{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media (max-width:980px){
.app{grid-template-columns:1fr;border:0}
.rail{position:static;height:auto;display:grid;grid-template-columns:1fr 1fr;
gap:0 24px;padding:20px 24px;border-right:0;border-bottom:1px solid var(--line)}
.brand{grid-column:1/-1;padding-bottom:16px}
.rail-block{padding:16px 0;border-top:1px solid var(--line)}
.rail-balance{grid-column:1/-1}
.rail-theme{grid-column:1/-1}
.rail-foot{grid-column:1/-1;margin-top:0;padding-top:16px;border-top:1px solid var(--line)}
.main{padding:24px 24px 40px}
.sec{grid-template-columns:1fr;gap:10px;padding:20px 0}
.sec-head p{max-width:none}
.hist-wrap{grid-template-columns:1fr}
.hist-list{max-height:300px;padding-right:4px;border-right:0;
border-bottom:1px solid var(--line);padding-bottom:12px}}
@media (max-width:640px){
.rail{grid-template-columns:1fr}
.brand,.rail-balance,.rail-foot{grid-column:auto}
.two,.three{grid-template-columns:1fr}
.key-row{grid-template-columns:1fr}
.key-row.deleting{grid-template-columns:1fr}
.key-row button{width:100%}
.hist-row{padding:9px 8px}
.hist-enter{height:26px;padding:0 8px}
.hist-msg{grid-template-columns:34px minmax(0,1fr);gap:8px}
.hist-role{font-size:9.5px}
.seg6{grid-template-columns:repeat(2,minmax(0,1fr))}
.main{padding:20px 20px 36px}
.launch{padding:16px}
.launch-row input{flex-basis:100%}
.launch-row .pri{width:100%}
.sec{gap:8px}}
@media (prefers-reduced-motion:reduce){
*{transition-duration:.01ms!important}
button:active{transform:none}}
</style></head><body>
<div class="app">

<aside class="rail">
  <div class="brand">
    <h1>Claude Code 控制台</h1>
    <span class="brand-note">仅监听 127.0.0.1</span>
  </div>

  <div class="rail-block">
    <div class="k">网关</div>
    <div class="status" id="gw">—</div>
    <div class="rail-line mono">127.0.0.1:3425</div>
  </div>

  <div class="rail-block">
    <div class="k">当前模型</div>
    <div class="mono rail-model" id="curmodel">—</div>
  </div>

  <div class="rail-block rail-balance">
    <div class="k-row"><span class="k">余额</span>
      <button class="link" onclick="refresh()">刷新</button></div>
    <div id="quotaLedger" hidden></div>
    <details class="raw" id="quotaRaw">
      <summary>原始输出</summary>
      <pre id="quota">读取中…</pre>
    </details>
  </div>

  <div class="rail-block rail-theme">
    <div class="k">外观</div>
    <div class="seg mt-10" id="themeSeg"></div>
  </div>

  <div class="rail-foot" id="ver"></div>
</aside>

<main class="main">

  <section class="launch">
    <div class="k">启动终端</div>
    <div class="launch-row">
      <input id="ws" value="">
      <button class="pri" onclick="openTerm('plain')">打开终端</button>
    </div>
    <div class="row launch-sub">
      <button onclick="openTerm('resume')">继续上次会话</button>
      <button onclick="gotoHistory()">历史对话</button>
      <button onclick="openTerm('picker')">选模型进</button>
      <button onclick="openFolder()">Finder 打开</button>
    </div>
  </section>

  <section class="sec">
    <div class="sec-head"><h2>网关</h2><p>magpie 代理进程</p></div>
    <div class="sec-body">
      <div class="mono">127.0.0.1:3425</div>
      <div class="row mt-12">
        <button class="pri" onclick="gw('start')">启动</button>
        <button class="danger" onclick="gw('stop')">停止</button>
        <button onclick="gw('restart')">重启</button>
      </div>
    </div>
  </section>

  <section class="sec">
    <div class="sec-head"><h2>模型</h2><p>新会话生效</p></div>
    <div class="sec-body">
      <div>
        <label>切换到</label>
        <select id="model"></select>
      </div>
      <div class="row mt-12">
        <button class="pri" onclick="setModel()">应用</button>
        <button onclick="setModel('default')">恢复默认</button>
      </div>
    </div>
  </section>

  <section class="sec" id="histSec">
    <div class="sec-head"><h2>历史对话</h2><p>本地记录 · 可继续</p></div>
    <div class="sec-body">
      <div class="hist-bar">
        <span class="k" id="histStat">读取中…</span>
        <button onclick="loadSessions()">刷新</button>
      </div>
      <div class="hist-wrap">
        <div class="hist-list" id="histList"><div class="hist-placeholder">读取中…</div></div>
        <div class="hist-detail" id="histView"><div class="hist-placeholder">选择一个会话查看</div></div>
      </div>
    </div>
  </section>

  <section class="sec">
    <div class="sec-head"><h2>思考档位</h2><p>主代理与子代理</p></div>
    <div class="sec-body">
      <div class="two">
        <div>
          <label>主代理</label>
          <div class="seg seg3" id="efforts"></div>
        </div>
        <div>
          <label>子代理</label>
          <div class="seg seg3" id="subefforts"></div>
        </div>
      </div>
      <p class="hint">改动在新会话生效</p>
    </div>
  </section>

  <section class="sec">
    <div class="sec-head"><h2>权限</h2><p>工作区规则与默认模式</p></div>
    <div class="sec-body">
      <div>
        <label>默认模式 defaultMode</label>
        <div class="seg seg6" id="modes"></div>
      </div>
      <div class="row mt-12">
        <button class="pri" onclick="applyPreset()">套用工作区预设</button>
        <button onclick="quickMode('plan')">只读模式</button>
        <button onclick="quickMode('bypassPermissions')">彻底放开</button>
        <button onclick="quickMode('default')">恢复逐项询问</button>
      </div>
      <div class="two mt-16">
        <div>
          <label>allow<span class="lh">每行一条</span></label>
          <textarea class="ta-lg" id="allowT" spellcheck="false"></textarea>
        </div>
        <div>
          <label>deny<span class="lh">优先于 allow</span></label>
          <textarea class="ta-lg" id="denyT" spellcheck="false"></textarea>
        </div>
      </div>
      <div class="two mt-16">
        <div>
          <label>ask<span class="lh">命中先询问</span></label>
          <textarea id="askT" spellcheck="false"></textarea>
        </div>
        <div>
          <label>额外可访问目录</label>
          <textarea id="dirsT" spellcheck="false"></textarea>
        </div>
      </div>
      <div class="row mt-16">
        <button class="pri" onclick="savePerms()">保存权限</button>
        <span class="mono" id="pstat"></span>
      </div>
      <p class="hint">deny > ask > allow；acceptEdits 下改文件不再询问，Bash 按规则走；Edit() 覆盖所有写文件工具</p>
    </div>
  </section>

  <section class="sec">
    <div class="sec-head"><h2>思考开关</h2><p>开关与预算</p></div>
    <div class="sec-body">
      <div class="seg toggles">
        <button id="thBtn" onclick="toggleThink()">—</button>
        <button id="adBtn" onclick="toggleAdaptive()">—</button>
      </div>
      <div class="mt-16">
        <label>思考预算<span class="lh">MAX_THINKING_TOKENS · 留空为模型默认</span></label>
        <div class="row">
          <input id="budget" class="grow" placeholder="32000">
          <button onclick="saveBudget()">保存</button>
        </div>
      </div>
      <p class="hint">关掉思考更省钱更快，复杂任务会明显变弱</p>
    </div>
  </section>

  <section class="sec">
    <div class="sec-head"><h2>提供商与密钥</h2><p>账户 · API key</p></div>
    <div class="sec-body">
      <div id="prov">读取中…</div>
      <div class="key-form">
        <div class="key-head">
          <div class="k">API key</div>
          <div class="seg" id="keyMode"></div>
        </div>
        <div class="key-row">
          <select id="keyTarget" aria-label="API key 目标"></select>
          <input id="keyValue" type="password" placeholder="粘贴 API key"
                 aria-label="API key" autocomplete="new-password" spellcheck="false"
                 onkeydown="if(event.key==='Enter')saveKey()">
          <button class="pri" id="keyBtn" onclick="saveKey()">添加</button>
        </div>
        <p class="hint" id="keyHint">中转站已全部屏蔽。新增调用 magpie provider add，替换调用 provider key；key 只写入 magpie，不回显。自定义供应商请用 magpie provider add。</p>
      </div>
    </div>
  </section>

  <section class="sec">
    <div class="sec-head"><h2>跑一轮</h2><p>claude -p · 无界面</p></div>
    <div class="sec-body">
      <textarea id="prompt" placeholder="列出当前目录的文件并总结"></textarea>
      <div class="row mt-12">
        <button class="pri" onclick="ask()">运行</button>
        <label class="check"><input type="checkbox" id="vbs"> 显示思考细节</label>
        <span class="mono" id="askst"></span>
      </div>
      <pre id="answer" class="answer">—</pre>
    </div>
  </section>

  <section class="sec">
    <div class="sec-head"><h2>环境</h2><p>路径与版本</p></div>
    <div class="sec-body">
      <div class="three">
        <div class="meta"><div class="k">配置目录</div><div class="v" id="cfg"></div></div>
        <div class="meta"><div class="k">settings.json</div><div class="v" id="stp"></div></div>
        <div class="meta"><div class="k">默认工作区</div><div class="v" id="wsdef"></div></div>
      </div>
    </div>
  </section>

</main>
</div>
<div class="toast" id="toast" role="status" aria-live="polite"></div>

<script>
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const noise = l => /^\d{4}\/\d{2}\/\d{2}/.test(l) || /plugin:|NODE_|EXTRA_CA/.test(l);
let S = {};

function paintQuota(text){
  const lines = (text || '').split('\n');
  const rows = [];
  lines.forEach(line => {
    if(noise(line)) return;
    const m = line.match(/^(\S+)\s+balance\s+(\S+)\s+left\b/);
    if(m) rows.push({name: m[1], amount: m[2]});
  });
  const box = $('quotaLedger'), raw = $('quotaRaw');
  box.innerHTML = rows.map(r => {
    const parts = r.amount.match(/^([^\d.,]*)([\d.,]+)$/) || ['', '', r.amount];
    const cur = parts[1] ? `<span class="cur">${esc(parts[1])}</span>` : '';
    return `<div class="ledger-row"><span class="name">${esc(r.name)}</span>` +
           `<span class="amount">${cur}${esc(parts[2])}</span></div>`;
  }).join('');
  box.hidden = !rows.length;
  raw.open = !rows.length;
  $('quota').textContent = lines.filter(l => !noise(l)).join('\n').trim() || '—';
}

function parseProviders(lines){
  return (lines || []).filter(l => !noise(l)).map(line => {
    const parts = line.trim().split(/\s{2,}/);
    if(parts.length < 3) return null;
    const fetched = ((line.match(/fetched\s+([^·]+)/) || [])[1] || '')
      .replace(/←\s*Claude Code.*/, '').trim();
    return {
      name: parts[0], id: parts[1], host: parts[2],
      models: (line.match(/\d+ of \d+ models/) || [''])[0],
      fetched: fetched.trim(),
      inUse: /← Claude Code/.test(line)
    };
  }).filter(Boolean);
}

function paintProviders(lines, err){
  const list = (lines || []).filter(l => !noise(l));
  const rows = parseProviders(lines);
  const box = $('prov');
  if(!rows.length){
    box.innerHTML = `<pre>${esc(list.join('\n') || err || '—')}</pre>`;
    return;
  }
  box.innerHTML = rows.map(r => {
    const tag = r.inUse ? '<span class="tag">当前</span>' : '';
    const meta = [r.models, r.fetched].filter(Boolean).join(' · ');
    return `<div class="prov-row"><div><div class="prov-name">${esc(r.name)}${tag}</div>` +
           `<div class="prov-sub mono">${esc(r.id)} · ${esc(r.host)}</div></div>` +
           `<div class="prov-meta mono">${esc(meta)}</div></div>`;
  }).join('');
}

let keyMode = 'add';
let presetsReady = false;
let PRESETS = [];
let BLOCKED_IDS = new Set();
const KEY_GROUPS = {
  'partners (sponsors)': '合作方', vendors: '厂商', relays: '中转',
  local: '本地', "another computer's magpie": '远程 Magpie'
};
const KEY_LABELS = {add: '新增', replace: '替换已有', delete: '删除'};
const keyActionLabel = () => keyMode === 'add' ? '添加'
  : keyMode === 'replace' ? '替换' : '删除';

function paintKeyForm(){
  if(!$('keyMode')) return;
  paintSeg('keyMode', ['add', 'replace', 'delete'], keyMode,
    v => KEY_LABELS[v] || v, 'setKeyMode');
  const btn = $('keyBtn');
  btn.textContent = keyActionLabel();
  btn.classList.toggle('pri', keyMode !== 'delete');
  btn.classList.toggle('danger', keyMode === 'delete');
  const row = document.querySelector('.key-row');
  if(row) row.classList.toggle('deleting', keyMode === 'delete');
  const hint = $('keyHint');
  if(hint){
    hint.textContent = keyMode === 'delete'
      ? '删除调用 magpie provider rm；会移除该 provider 及其 key，不可撤销。如模型列表未更新，重启网关。中转站已全部屏蔽。'
      : keyMode === 'replace'
      ? '替换调用 provider key；key 只写入 magpie，不回显。中转站已全部屏蔽。'
      : '新增调用 magpie provider add；key 只写入 magpie，不回显。中转站已全部屏蔽，自定义供应商请用 magpie provider add。';
  }
  const sel = $('keyTarget');
  const prev = sel.value;
  let html = '';
  if(keyMode === 'add'){
    if(!presetsReady){
      html = '<option value="">正在读取预设…</option>';
    }else{
      const presets = PRESETS.filter(p => p.needs_key && !p.added && !p.blocked);
      const groups = {};
      presets.forEach(p => {
        const g = p.group || '其他';
        (groups[g] = groups[g] || []).push(p);
      });
      Object.keys(groups).forEach(g => {
        html += `<optgroup label="新增 · ${esc(KEY_GROUPS[g] || g)}">` +
          groups[g].map(p =>
            `<option value="add:${esc(p.id)}">${esc(p.name)} · ${esc(p.id)}</option>`
          ).join('') + '</optgroup>';
      });
      if(!html) html = '<option value="">没有可添加的预设</option>';
    }
  }else{
    if(!Array.isArray(S.providers)){
      html = '<option value="">正在读取 provider…</option>';
    }else{
      const existing = parseProviders(S.providers)
        .filter(p => !BLOCKED_IDS.has(p.id));
      html = existing.map(p =>
        `<option value="replace:${esc(p.id)}">${esc(p.name)} · ${esc(p.id)}</option>`
      ).join('');
      if(!html) html = `<option value="">没有可${keyMode === 'delete' ? '删除' : '替换'}的 provider</option>`;
    }
  }
  if(html && !html.includes('value=""')){
    const ph = keyMode === 'delete' ? '选择要删除的 provider…'
      : keyMode === 'replace' ? '选择要替换的 provider…'
      : '选择要新增的供应商…';
    html = `<option value="">${ph}</option>` + html;
  }
  sel.innerHTML = html;
  if(prev && [...sel.options].some(o => o.value === prev)) sel.value = prev;
  else sel.value = '';
}

function setKeyMode(mode){
  keyMode = mode === 'replace' || mode === 'delete' ? mode : 'add';
  if(keyMode === 'delete') $('keyValue').value = '';
  selectSeg('keyMode', keyMode);
  paintKeyForm();
}

async function loadPresets(){
  try{
    const r = await api('/api/presets');
    PRESETS = r.presets || [];
    BLOCKED_IDS = new Set(r.blocked_ids || []);
  }catch(e){
    PRESETS = [];
    BLOCKED_IDS = new Set();
  }
  presetsReady = true;
  paintKeyForm();
}

async function deleteProvider(){
  const target = $('keyTarget').value;
  if(!target.startsWith('replace:')){
    toast('先选择要删除的 provider', false);
    return;
  }
  const id = target.slice(8);
  const provider = Array.isArray(S.providers)
    ? parseProviders(S.providers).find(p => p.id === id) : null;
  const name = provider ? provider.name : id;
  if(!confirm(`删除 provider「${name}（${id}）」？\n\n会移除它的 key 和模型配置，且不可撤销。` +
              (provider && provider.inUse
                ? '\n\n它当前正被 Claude Code 使用，删除后需要切换模型。' : ''))) return;
  const btn = $('keyBtn');
  btn.disabled = true; btn.textContent = '删除中…';
  try{
    const r = await api('/api/provider_rm', {id});
    toast(r.msg || (r.ok ? '已删除' : '删除失败'), r.ok);
    if(r.ok){ refresh(); loadPresets(); }
  }catch(e){
    toast('请求失败', false);
  }finally{
    btn.disabled = false;
    btn.textContent = keyActionLabel();
  }
}

async function saveKey(){
  if(keyMode === 'delete') return deleteProvider();
  const target = $('keyTarget').value;
  const key = $('keyValue').value.trim();
  if(!target){ toast('先选择目标', false); return; }
  if(!key){ toast('先粘贴 API key', false); $('keyValue').focus(); return; }
  const btn = $('keyBtn');
  btn.disabled = true; btn.textContent = '保存中…';
  try{
    const r = await api('/api/provider_key', {target, key});
    if(r.ok) $('keyValue').value = '';
    toast(r.msg || (r.ok ? '已保存' : '保存失败'), r.ok);
    if(r.ok){
      // 先把刚添加的预设从下拉里移掉，再让后端刷新缓存
      if(target.startsWith('add:')){
        const id = target.slice(4);
        PRESETS.forEach(p => { if(p.id === id) p.added = true; });
        paintKeyForm();
      }
      refresh(); loadPresets();
    }
  }catch(e){
    toast('请求失败', false);
  }finally{
    btn.disabled = false;
    btn.textContent = keyActionLabel();
  }
}

let HIST = [];
let histSel = '';
let histSeq = 0;

function relTime(ts){
  const t = Number(ts) || 0;
  if(!t) return '';
  const s = Math.max(0, Date.now() / 1000 - t);
  if(s < 60) return '刚刚';
  if(s < 3600) return Math.floor(s / 60) + ' 分钟前';
  if(s < 86400) return Math.floor(s / 3600) + ' 小时前';
  if(s < 86400 * 7) return Math.floor(s / 86400) + ' 天前';
  const d = new Date(t * 1000);
  return (d.getMonth() + 1) + '月' + d.getDate() + '日';
}

function gotoHistory(){
  const el = $('histSec');
  if(!el) return;
  const smooth = !matchMedia('(prefers-reduced-motion: reduce)').matches;
  el.scrollIntoView({behavior: smooth ? 'smooth' : 'auto', block: 'start'});
}

function paintHistList(){
  const box = $('histList');
  if(!HIST.length){
    box.innerHTML = '<div class="hist-placeholder">没有历史会话</div>';
    $('histStat').textContent = '0 个会话';
    return;
  }
  $('histStat').textContent = `最近 ${HIST.length} 个会话`;
  box.innerHTML = HIST.map(s => {
    const last = s.last && s.last !== s.title
      ? `<div class="hist-last">${esc(s.last)}</div>` : '';
    const act = s.id === histSel ? ' act' : '';
    return `<div class="hist-row${act}" role="button" tabindex="0"
      title="${esc(s.cwd)}"
      onclick="viewSession('${esc(s.id)}')"
      onkeydown="if(event.key==='Enter')viewSession('${esc(s.id)}')">
      <div class="hist-main">
        <div class="hist-title">${esc(s.title)}</div>${last}
        <div class="hist-meta">${esc(s.project)} · ${relTime(s.mtime)}</div>
      </div>
      <button class="hist-enter"
        onclick="event.stopPropagation();enterSession('${esc(s.id)}')"
        onkeydown="event.stopPropagation()">进入</button>
    </div>`;
  }).join('');
}

async function loadSessions(){
  $('histStat').textContent = '读取中…';
  let failed = false;
  try{
    const r = await api('/api/sessions');
    HIST = r.sessions || [];
  }catch(e){
    HIST = [];
    failed = true;
  }
  if(histSel && !HIST.some(s => s.id === histSel)) histSel = '';
  paintHistList();
  if(failed){
    $('histStat').textContent = '读取失败';
    $('histView').innerHTML = '<div class="hist-placeholder">读取失败</div>';
    return;
  }
  if(HIST.length){
    const id = histSel || HIST[0].id;
    histSel = id;
    paintHistList();
    viewSession(id);
  }else{
    histSel = '';
    $('histView').innerHTML = '<div class="hist-placeholder">没有历史会话</div>';
  }
}

async function viewSession(id){
  histSel = id;
  paintHistList();
  const seq = ++histSeq;
  $('histView').innerHTML = '<div class="hist-placeholder">读取中…</div>';
  try{
    const d = await api('/api/session?id=' + encodeURIComponent(id));
    if(seq !== histSeq) return;
    if(!d.ok){
      $('histView').innerHTML =
        `<div class="hist-placeholder">${esc(d.msg || '读取失败')}</div>`;
      return;
    }
    renderSession(HIST.find(s => s.id === id) || {}, d);
  }catch(e){
    if(seq !== histSeq) return;
    $('histView').innerHTML = '<div class="hist-placeholder">读取失败</div>';
  }
}

function renderSession(s, d){
  const id = d.id || s.id || '';
  const title = s.title || d.title || '（无标题会话）';
  const cwd = s.cwd || d.cwd || '';
  const project = s.project || d.project || '';
  const count = (d.messages || []).length;
  const meta = [project, relTime(s.mtime || d.mtime), `${count} 条消息`]
    .filter(Boolean).join(' · ') + (d.truncated ? ' · 仅显示最近部分' : '');
  const rows = (d.messages || []).map(m => {
    const role = m.role === 'user' ? '你' : 'Claude';
    const cls = m.role === 'user' ? '' : ' assistant';
    const text = m.text ? `<div class="hist-text${cls}">${esc(m.text)}</div>` : '';
    const tools = (m.tools && m.tools.length)
      ? `<div class="hist-tools">工具 · ${esc(m.tools.join(' · '))}</div>` : '';
    return `<div class="hist-msg"><div class="hist-role">${role}</div>` +
           `<div>${text}${tools}</div></div>`;
  }).join('');
  $('histView').innerHTML =
    `<div class="hist-dhead"><div class="hist-dmain">` +
      `<div class="hist-dtitle">${esc(title)}</div>` +
      `<div class="hist-dmeta">${esc(meta)}</div>` +
      (cwd ? `<div class="hist-dcwd">${esc(cwd)}</div>` : '') +
    `</div><button class="pri" onclick="enterSession('${esc(id)}')">进入</button></div>` +
    `<div class="hist-thread" id="histThread">` +
      (rows || '<div class="hist-placeholder">没有可显示的消息</div>') +
    `</div>`;
  const th = $('histThread');
  if(th) th.scrollTop = th.scrollHeight;
}

async function enterSession(id){
  toast('正在打开终端…');
  try{
    const r = await api('/api/terminal', {kind: 'session', session: id});
    toast(r.msg, r.ok);
  }catch(e){
    toast('打开失败', false);
  }
}

function toast(msg, ok = true){
  const t = $('toast'); t.textContent = msg;
  t.classList.toggle('bad', !ok);
  t.classList.add('show'); clearTimeout(t._t);
  t._t = setTimeout(() => t.classList.remove('show'), 3000);
}
async function api(path, body){
  const r = await fetch(path, body ? {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)} : {});
  return r.json();
}
function placeThumb(seg, instant){
  if(!seg) return;
  let t = seg.querySelector('.seg-thumb');
  const isNew = !t;
  if(isNew){
    t = document.createElement('i');
    t.className = 'seg-thumb';
    seg.prepend(t);
  }
  const act = seg.querySelector('button.act');
  if(!act){ t.style.opacity = '0'; return; }
  t.style.opacity = '1';
  const sr = seg.getBoundingClientRect(), br = act.getBoundingClientRect();
  const x = br.left - sr.left - seg.clientLeft;
  const y = br.top - sr.top - seg.clientTop;
  if(isNew || instant) t.style.transition = 'none';
  t.style.width = br.width + 'px';
  t.style.height = br.height + 'px';
  t.style.transform = `translate(${x}px, ${y}px)`;
  if(isNew || instant){
    void t.offsetWidth;
    t.style.transition = '';
  }
}

function paintSeg(id, values, current, label, handler){
  const el = $(id);
  if(!el) return;
  const sig = values.join('|');
  if(el.dataset.sig !== sig){
    el.innerHTML = values.map(v =>
      `<button type="button" data-v="${esc(v)}" onclick="${handler}('${esc(v)}')">${esc(label(v))}</button>`
    ).join('');
    el.dataset.sig = sig;
  }
  el.querySelectorAll('button').forEach(b => {
    b.classList.toggle('act', b.dataset.v === String(current));
  });
  placeThumb(el);
}

function selectSeg(id, value){
  const el = $(id);
  if(!el) return;
  el.querySelectorAll('button').forEach(b => {
    b.classList.toggle('act', b.dataset.v === String(value));
  });
  placeThumb(el);
}

function setTheme(theme){
  theme = theme === 'light' ? 'light' : 'dark';
  document.documentElement.dataset.theme = theme;
  try{ localStorage.setItem('ccpanel-theme', theme); }catch(e){}
  paintSeg('themeSeg', ['dark', 'light'], theme,
    v => v === 'dark' ? '深色' : '浅色', 'setTheme');
}

function paintEfforts(){
  const opts = ['default', ...(S.efforts||[])];
  const label = v => v === 'default' ? '默认' : v;
  paintSeg('efforts', opts, S.effort || 'default', label, 'setEffort');
  paintSeg('subefforts', opts, S.subagent_effort || 'default', label, 'setSubEffort');
}
function paintModels(){
  const cur = S.model || '';
  $('curmodel').textContent = cur || '未设置 · 走官方默认';
  const sel = $('model');
  const base = cur.replace(/\[1m\]$/, '');
  const list = S.models || [];
  sel.innerHTML = list.map(m =>
    `<option value="${m.id}" ${m.id===base?'selected':''}>${m.label} — ${m.id}</option>`).join('')
    || '<option value="">无可用模型</option>';
}
function paintPerms(){
  const p = S.perms || {};
  paintSeg('modes', S.modes||[], p.defaultMode || 'default', v => v, 'quickMode');
  $('allowT').value = (p.allow||[]).join('\n');
  $('denyT').value = (p.deny||[]).join('\n');
  $('askT').value = (p.ask||[]).join('\n');
  $('dirsT').value = (p.additionalDirectories||[]).join('\n');
  $('pstat').textContent = `模式 ${p.defaultMode||'default'} · allow ${(p.allow||[]).length} · deny ${(p.deny||[]).length} · ask ${(p.ask||[]).length}`;
}
function quickMode(m){
  selectSeg('modes', m);
  api('/api/perms', {mode: m}).then(r => { toast(r.msg, r.ok); refresh(); });
}
function applyPreset(){
  if(!confirm('将写入 acceptEdits + 数十条 allow/deny/ask 规则，覆盖当前权限设置。继续？')) return;
  api('/api/perms', {preset:'workspace'}).then(r => { toast(r.msg, r.ok); refresh(); });
}
function savePerms(){
  api('/api/perms', {mode: (S.perms||{}).defaultMode,
                     allow_text: $('allowT').value,
                     deny_text: $('denyT').value,
                     ask_text: $('askT').value,
                     dirs_text: $('dirsT').value})
    .then(r => { toast(r.msg, r.ok); refresh(); });
}
function paintThinking(){
  const t = S.thinking || {};
  const b1 = $('thBtn'), b2 = $('adBtn');
  b1.textContent = t.disabled ? '思考 · 关' : '思考 · 开';
  b1.className = t.disabled ? '' : 'act';
  b2.textContent = t.adaptive_off ? '自适应 · 关' : '自适应 · 开';
  b2.className = t.adaptive_off ? '' : 'act';
  $('budget').value = t.budget || '';
}
function toggleThink(){
  const t = S.thinking || {};
  api('/api/thinking', {disabled: !t.disabled})
    .then(r => { toast(r.msg, r.ok); refresh(); });
}
function toggleAdaptive(){
  const t = S.thinking || {};
  api('/api/thinking', {adaptive_off: !t.adaptive_off})
    .then(r => { toast(r.msg, r.ok); refresh(); });
}
function saveBudget(){
  api('/api/thinking', {budget: $('budget').value})
    .then(r => { toast(r.msg, r.ok); refresh(); });
}
function paint(){
  const gw = $('gw');
  gw.className = 'status' + (S.gateway ? '' : ' off');
  gw.innerHTML = `<span class="lamp${S.gateway ? ' on' : ''}"></span>${S.gateway ? '运行中' : '已停止'}`;
  $('ver').textContent = `claude ${(S.claude_version||'').replace(' (Claude Code)','')} · ${S.magpie_version||''}`;
  paintQuota(S.quota);
  paintProviders(S.providers, S.providers_err);
  paintKeyForm();
  $('cfg').textContent = S.config_dir; $('stp').textContent = S.settings_path;
  $('wsdef').textContent = S.workspace || '—';
  if(!$('ws').value) $('ws').value = S.workspace;
  paintModels(); paintEfforts(); paintThinking(); paintPerms();
}
async function refresh(){ S = await api('/api/state'); paint(); }

function gw(action){
  toast(action === 'stop' ? '正在停止…' : '正在启动…');
  api('/api/gateway', {action}).then(r => { toast(r.msg, r.ok); refresh(); });
}
function setModel(mode){
  if(mode === 'default'){
    api('/api/claude_default').then(r => { toast(r.msg, r.ok); refresh(); }); return;
  }
  const v = $('model').value;
  if(!v){ toast('没有可选模型', false); return; }
  api('/api/model', {model: v}).then(r => { toast(r.msg, r.ok); refresh(); });
}
function setEffort(level){
  selectSeg('efforts', level);
  api('/api/effort', {level}).then(r => { toast(r.msg, r.ok); refresh(); });
}
function setSubEffort(level){
  selectSeg('subefforts', level);
  api('/api/effort', {level, field:'subagent'}).then(r => { toast(r.msg, r.ok); refresh(); });
}
function openTerm(kind){
  api('/api/terminal', {kind, cwd: $('ws').value})
    .then(r => toast(r.msg, r.ok));
}
function openFolder(){
  api('/api/folder', {cwd: $('ws').value}).then(r => toast(r.msg, r.ok));
}
async function ask(){
  const p = $('prompt').value.trim();
  if(!p){ toast('先写点什么', false); return; }
  $('askst').textContent = '运行中…'; $('answer').textContent = '…';
  const r = await api('/api/ask', {prompt: p, cwd: $('ws').value,
                                   verbose: $('vbs').checked});
  $('askst').textContent = r.ok ? '完成' : '失败';
  $('answer').textContent = r.out;
}
setTheme(document.documentElement.dataset.theme || 'dark');
refresh();
loadPresets();
loadSessions();
addEventListener('resize', () => {
  ['efforts', 'subefforts', 'modes', 'themeSeg', 'keyMode'].forEach(id => placeThumb($(id), true));
});
setInterval(() => { if(S.gateway && !document.hidden) api('/api/ping').then(r => {
  if(r.gateway !== S.gateway){ S.gateway = r.gateway; paint(); }
}); }, 5000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def do_GET(self):
        p = urlparse(self.path).path
        if p in ("/", "/index.html"):
            self._send(200, HTML, "text/html; charset=utf-8")
        elif p == "/api/state":
            self._json(state())
        elif p == "/api/presets":
            rows = presets()
            self._json({
                "presets": [x for x in rows if not x.get("blocked")],
                "blocked_ids": [x["id"] for x in rows if x.get("blocked")],
            })
        elif p == "/api/sessions":
            self._json({"sessions": sessions_payload(), "dir": SESSIONS_DIR})
        elif p == "/api/session":
            sid = (parse_qs(urlparse(self.path).query).get("id") or [""])[0]
            self._json(session_detail(sid))
        elif p == "/api/ping":
            self._json({"gateway": gateway_up()})
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):
        p = urlparse(self.path).path
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            body = {}
        try:
            if p == "/api/gateway":
                a = body.get("action")
                if a == "start":
                    ok, msg = start_gateway()
                elif a == "stop":
                    ok, msg = stop_gateway()
                else:
                    stop_gateway()
                    ok, msg = start_gateway()
                return self._json({"ok": ok, "msg": msg})
            if p == "/api/model":
                ok, msg = set_model(body.get("model", ""))
                return self._json({"ok": ok, "msg": msg})
            if p == "/api/claude_default":
                ok, out = run([MAGPIE, "claude", "default"])
                return self._json({"ok": ok, "msg": out or "已恢复默认"})
            if p == "/api/effort":
                ok, msg = set_effort(body.get("level", ""),
                                     body.get("field", "effort"))
                return self._json({"ok": ok, "msg": msg})
            if p == "/api/terminal":
                return self._json(open_terminal(body.get("kind", "plain"),
                                                body.get("cwd", DEFAULT_WS),
                                                body.get("session")))
            if p == "/api/folder":
                cwd = body.get("cwd") or DEFAULT_WS
                if not os.path.isdir(cwd):
                    return self._json({"ok": False, "msg": "目录不存在"})
                subprocess.Popen(["/usr/bin/open", cwd])
                return self._json({"ok": True, "msg": "已在 Finder 打开"})
            if p == "/api/perms":
                if body.get("preset"):
                    ok, msg = apply_preset(body["preset"])
                else:
                    ok, msg = set_perms(
                        body.get("mode"),
                        body.get("allow_text") if "allow_text" in body else body.get("allow"),
                        body.get("deny_text") if "deny_text" in body else body.get("deny"),
                        body.get("ask_text") if "ask_text" in body else body.get("ask"),
                        body.get("dirs_text") if "dirs_text" in body else body.get("dirs"))
                return self._json({"ok": ok, "msg": msg})
            if p == "/api/thinking":
                ok, msg = set_thinking(body.get("disabled"),
                                       body.get("adaptive_off"),
                                       body.get("budget"))
                return self._json({"ok": ok, "msg": msg})
            if p == "/api/provider_key":
                ok, msg = set_provider_key(body.get("target", ""),
                                           body.get("key", ""))
                return self._json({"ok": ok, "msg": msg})
            if p == "/api/provider_rm":
                ok, msg = remove_provider(body.get("id", ""))
                return self._json({"ok": ok, "msg": msg})
            if p == "/api/ask":
                ok, out = run_prompt(body.get("prompt", ""),
                                     body.get("cwd", DEFAULT_WS),
                                     verbose=bool(body.get("verbose")))
                return self._json({"ok": ok, "out": out})
            self._send(404, "not found", "text/plain")
        except Exception as e:  # noqa
            self._json({"ok": False, "msg": repr(e)}, 500)


def osa(script):
    return run(["osascript", "-e", script], timeout=20)


def sh_quote(s):
    return "'" + s.replace("'", "'\\''") + "'"


def open_terminal(kind, cwd, session_id=None):
    if kind == "session":
        path = find_session_file(session_id or "")
        if not path:
            return {"ok": False, "msg": "会话不存在"}
        cwd = session_summary(path, session_id, 0)["cwd"]
        if not cwd or not os.path.isdir(cwd):
            return {"ok": False, "msg": "会话工作目录不存在: " + str(cwd)}
        cmd = "claude --resume " + sh_quote(session_id)
    else:
        if not cwd or not os.path.isdir(cwd):
            return {"ok": False, "msg": "工作目录不存在: " + str(cwd)}
        cmd = "claude"
        if kind == "resume":
            cmd = "claude --continue"
        elif kind == "picker":
            cmd = "claude --model " + sh_quote(models()[0]["id"].replace("[1m]", "")) \
                if models() else "claude"
    inner = "cd {} && export CLAUDE_CONFIG_DIR={} && {}".format(
        sh_quote(cwd), sh_quote(CLAUDE_CONFIG_DIR), cmd)
    script = ('tell application "Terminal"\nactivate\n'
              'do script "{}"\nend tell').format(inner.replace('"', '\\"'))
    code, out = osa(script)
    if code == 0:
        return {"ok": True, "msg": "已打开终端：" + os.path.basename(cwd)}
    return {"ok": False, "msg": out}


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("Claude Code 控制台 → http://127.0.0.1:{}".format(PORT))
    print("配置目录: " + CLAUDE_CONFIG_DIR)
    # 预热预设列表，避免第一次打开页面时等 3 秒
    threading.Thread(target=presets, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")


if __name__ == "__main__":
    main()

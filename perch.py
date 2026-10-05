#!/usr/bin/env python3
"""perch: supervise long-running agents in tmux. Stdlib only, Python 3.11+."""
import argparse, contextlib, fcntl, fnmatch, glob, hashlib, hmac, json, os, re, shutil, subprocess, sys, time, tomllib
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import unquote

HOME = Path(os.environ.get("PERCH_HOME", Path.home() / ".perch"))
STATE, EVENTS, MAILBOX = HOME / "state.json", HOME / "events.log", HOME / "mailbox.jsonl"
CONFIG = Path(os.environ.get("PERCH_CONFIG", "agents.toml"))
PREFIX = os.environ.get("PERCH_PREFIX", "perch_")  # tmux session = PREFIX + agent; set it to adopt sessions that already exist


def tmux(*args):
    return subprocess.run(["tmux", *args], capture_output=True, text=True)


def load_agents():
    return tomllib.loads(CONFIG.read_text())["agents"]


def load_fleet():
    return tomllib.loads(CONFIG.read_text()).get("fleet", {})


def load_state():
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def save_state(s):
    HOME.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")  # atomic: dashboard may read mid-write
    tmp.write_text(json.dumps(s, indent=1))
    tmp.replace(STATE)


def log_event(agent, what):
    HOME.mkdir(parents=True, exist_ok=True)
    if EVENTS.exists() and EVENTS.stat().st_size > 1_000_000:
        EVENTS.replace(EVENTS.with_suffix(".log.1"))  # keep one old file: bounded disk, recent history
    with EVENTS.open("a") as f:
        f.write(f"{time.strftime('%FT%T')} {agent} {what}\n")


def alert(fleet, msg):
    """Run [fleet].notify_cmd with the text in $PERCH_MSG (env, not argv: no shell-quoting of agent output).
    Never raises: a broken notifier must not stop supervision."""
    log_event("fleet", f"alert: {msg}")
    if "notify_cmd" not in fleet:
        return
    try:
        subprocess.run(fleet["notify_cmd"], shell=True, timeout=15, env={**os.environ, "PERCH_MSG": msg})
    except Exception as e:
        log_event("fleet", f"notify failed: {e}")


def session(name):
    return PREFIX + name


def alive(name):
    return tmux("has-session", "-t", session(name)).returncode == 0


def pane(name):
    return tmux("capture-pane", "-p", "-t", session(name)).stdout


def start(name, spec):
    tmux("new-session", "-d", "-s", session(name), "-c", os.path.expanduser(spec.get("cwd", "~")), spec["cmd"])
    log_event(name, "started")


def stop(name):
    tmux("kill-session", "-t", session(name))
    log_event(name, "stopped")


def tail(text, n=10):
    """Markers are matched on the last n lines only: a resolved error stays in scrollback and must not trigger."""
    return "\n".join(text.rstrip().splitlines()[-n:])  # capture-pane pads the pane with blank lines


def is_stuck(prev_hash, text, markers, idle_markers=()):
    """Frozen = screen identical to last tick AND a known stuck marker in its tail, unless an idle marker
    (prompt footer: the turn already ended) is there too. A working agent's screen changes."""
    same = prev_hash == hashlib.sha1(text.encode()).hexdigest()
    t = tail(text)
    return same and any(m in t for m in markers) and not any(m in t for m in idle_markers)


HEALTH_FAILS = 2  # consecutive failures before restart: a fresh start needs a tick or two to come up
LOOP_MAX, LOOP_WINDOW = 3, 600  # restarts allowed per window before we give up on an agent


def healthy(name, spec):
    """health_cmd gets $PERCH_SESSION (tmux session) so it can inspect the agent's process tree."""
    if "health_cmd" not in spec:
        return True
    try:
        return subprocess.run(spec["health_cmd"], shell=True, timeout=10, capture_output=True,
                              cwd=os.path.expanduser(spec.get("cwd", "~")),
                              env={**os.environ, "PERCH_SESSION": session(name)}).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def bump(name, st, now, why, fleet):
    """Record a restart; halt the agent if it is crash-looping."""
    first = st["restarts"] == 0 and why == "not running"  # initial boot of a never-seen agent: not news
    st["restarts"] += 1
    st["hash"], st["health_fails"] = None, 0
    st["recent"] = [t for t in st.get("recent", []) if now - t < LOOP_WINDOW] + [now]
    log_event(name, f"restarted: {why}")
    if len(st["recent"]) >= LOOP_MAX:
        stop(name)
        st["stopped"] = True  # same flag as an operator stop: only `perch start` clears it
        alert(fleet, f"{name}: {LOOP_MAX} restarts in {LOOP_WINDOW // 60} min ({why}), halted. Fix it, then `perch start {name}`")
    elif not first:
        alert(fleet, f"{name} restarted: {why}")


def ring_doorbell(name):
    """Type ONE fixed line into the agent's session. Content never travels this way."""
    cmd = f"Mailbox has new messages. Run: PERCH_HOME={HOME} python3 {Path(__file__).resolve()} inbox {name}"
    tmux("send-keys", "-t", session(name), "-l", cmd)
    tmux("send-keys", "-t", session(name), "Enter")
    log_event(name, "doorbell")


def level(value, warn, crit, high_is_bad=True):
    """0 ok / 1 warn / 2 crit."""
    v = value if high_is_bad else -value
    w, c = (warn, crit) if high_is_bad else (-warn, -crit)
    return 2 if v >= c else 1 if v >= w else 0


def system():
    du = shutil.disk_usage("/")
    mem = next(int(l.split()[1]) // 1024 for l in open("/proc/meminfo") if l.startswith("MemAvailable"))
    return {"disk_pct": round(du.used / du.total * 100), "mem_avail_mb": mem}


def system_extra():
    """Dashboard-only extras; each is best-effort (no thermal zone on a VM = omitted)."""
    out = {"load1": os.getloadavg()[0], "uptime_h": round(float(open("/proc/uptime").read().split()[0]) / 3600)}
    with contextlib.suppress(OSError, ValueError):
        out["temp_c"] = round(int(open("/sys/class/thermal/thermal_zone0/temp").read()) / 1000, 1)
    return out


def check_system(state, fleet):
    """Alert only when a level ESCALATES, so a stuck-high disk pages once, not every tick."""
    sysm, prev = system(), state.setdefault("_system", {})
    lv = {"disk": level(sysm["disk_pct"], fleet.get("disk_warn", 80), fleet.get("disk_crit", 90)),
          "mem": level(sysm["mem_avail_mb"], fleet.get("mem_warn_mb", 1000), fleet.get("mem_crit_mb", 400), False)}
    for k, v in lv.items():
        if v > prev.get(k, 0):
            alert(fleet, f"{k} {'critical' if v == 2 else 'warning'}: {sysm}")
    prev.update(lv)


def check_probe(state, fleet, run=subprocess.run):
    """[fleet].probe_cmd: infra no single agent owns (proxy, DNS, port forward). Alert on down/up transitions only."""
    if "probe_cmd" not in fleet:
        return
    try:
        ok = run(fleet["probe_cmd"], shell=True, timeout=15, capture_output=True).returncode == 0
    except subprocess.TimeoutExpired:
        ok = False
    prev = state.setdefault("_probe", {})
    if not ok and not prev.get("down"):
        alert(fleet, f"probe failed: {fleet['probe_cmd']}")
    elif ok and prev.get("down"):
        alert(fleet, "probe recovered")
    prev["down"] = not ok


def notice(name, st, spec, text, fleet):
    """Per-agent `notice_regex`: alert once per distinct matching line, touch nothing (observe only)."""
    if "notice_regex" not in spec:
        return
    hits = [l.strip() for l in text.splitlines() if re.search(spec["notice_regex"], l, re.I)]
    line = hits[-1] if hits else None
    if line and line != st.get("notice"):
        alert(fleet, f"{name}: {line}")
    st["notice"] = line


def auto_reply(name, spec, text):
    """Answer a known harness dialog (match -> keys). Returns True if a rule fired."""
    for rule in spec.get("auto_replies", []):
        if rule["match"] in text:
            tmux("send-keys", "-t", session(name), *rule["keys"])
            log_event(name, f"auto_reply: {rule['match']!r}")
            return True
    return False


def tick(agents, state, now=None, fleet=None):
    """One supervision pass (run from cron). Returns updated state."""
    now, fleet = now or time.time(), fleet or {}
    for name, spec in agents.items():
        st = state.setdefault(name, {"restarts": 0, "last_seen": None, "hash": None})
        if st.get("stopped"):  # operator stop or crash-loop halt; don't resurrect
            continue
        if not alive(name):
            start(name, spec)
            bump(name, st, now, "not running", fleet)
            continue
        text = pane(name)
        notice(name, st, spec, text, fleet)
        if auto_reply(name, spec, text):
            continue  # a dialog is not a freeze; answer it, judge next tick
        if any(m in tail(text) for m in spec.get("restart_markers", [])):  # prompt nobody can answer: no need to wait a tick
            stop(name); start(name, spec)
            bump(name, st, now, "unanswerable prompt", fleet)
            continue
        if is_stuck(st["hash"], text, spec.get("stuck_markers", []), spec.get("idle_markers", [])):
            stop(name); start(name, spec)
            bump(name, st, now, "stuck", fleet)
            continue
        st["health_fails"] = 0 if healthy(name, spec) else st.get("health_fails", 0) + 1
        if st["health_fails"] >= HEALTH_FAILS:
            stop(name); start(name, spec)
            bump(name, st, now, "health_cmd failed", fleet)
            continue
        new_hash = hashlib.sha1(text.encode()).hexdigest()
        idle = st["hash"] == new_hash  # screen static since last tick: not mid-turn
        st["hash"], st["last_seen"] = new_hash, now
        if spec.get("doorbell") and idle:
            total, unread = mail(name)
            if unread and st.get("rung") != total:  # once per new batch, not every tick
                ring_doorbell(name)
                st["rung"] = total
    check_system(state, fleet)
    check_probe(state, fleet)
    check_containers(state, fleet)
    return state


@contextlib.contextmanager
def locked():
    """Serialise tick / CLI / dashboard: they all read-modify-write state.json."""
    HOME.mkdir(parents=True, exist_ok=True)
    with (HOME / ".lock").open("w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def control(cmd, name, agents):
    """start/stop/restart; `stopped` flag keeps tick from resurrecting an operator stop."""
    with locked():
        state = load_state()
        st = state.setdefault(name, {"restarts": 0, "last_seen": None, "hash": None})
        st["stopped"] = cmd == "stop"
        st["recent"] = []  # operator took over: fresh crash-loop budget
        save_state(state)
        if cmd != "start":
            stop(name)
        if cmd != "stop":
            start(name, agents[name])


def send_chat(name, spec, text):
    """Type one line into an agent's session, from the dashboard. Opt-in per agent (`chat = true`); returns False if refused."""
    text = " ".join(text.split())[:2000]  # one line: a newline would submit twice and split the human's message
    if not spec.get("chat") or not text or not alive(name):
        return False
    tmux("send-keys", "-t", session(name), "-l", "--", text)  # "--": text starting with "-" must not parse as a tmux option
    tmux("send-keys", "-t", session(name), "Enter")
    log_event(name, f"chat: {text[:200]!r}")
    return True


def containers(patterns, run=subprocess.run):
    """Read-only docker status for `[fleet].containers` name globs. Missing docker = empty, never an error."""
    if not patterns:
        return []
    try:
        out = run(["docker", "ps", "-a", "--format", "{{.Names}}\t{{.Status}}\t{{.Image}}\t{{.Ports}}"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    rows = [l.split("\t") for l in out.splitlines() if l.count("\t") == 3]
    return [{"name": n, "status": s, "up": s.startswith("Up"), "image": i, "ports": po}
            for n, s, i, po in sorted(rows) if any(fnmatch.fnmatch(n, g) for g in patterns)]


def check_containers(state, fleet, run=subprocess.run):
    """Alert once when a listed container goes Up -> not Up. Observe only: never restarts (these may be live-trading)."""
    prev = state.setdefault("_containers", {})
    watch = fleet.get("containers_alert", fleet.get("containers"))  # alert on a subset (e.g. live ones; dry-runs are stopped on purpose)
    for c in containers(watch, run):
        if prev.get(c["name"]) and not c["up"]:
            alert(fleet, f"container down: {c['name']} ({c['status']})")
        prev[c["name"]] = c["up"]


def cron_logs(patterns, now=None, limit=200):
    """Age of the newest write to each log matching `[fleet].cron_logs` globs, stalest first: a job that silently stopped shows up on top."""
    now = now or time.time()
    files = {f for g in patterns or [] for f in glob.glob(os.path.expanduser(g))}
    rows = [{"name": "/".join(Path(f).parts[-2:]), "age_s": int(now - os.stat(f).st_mtime)} for f in files if os.path.isfile(f)]
    return sorted(rows, key=lambda r: -r["age_s"])[:limit]


def recent_events(n=100):
    return EVENTS.read_text().splitlines()[-n:] if EVENTS.exists() else []


def container_logs(patterns, name, run=subprocess.run):
    """Last 200 lines of a container's logs; only for names `containers()` would list (never an arbitrary docker target)."""
    if name not in {c["name"] for c in containers(patterns, run)}:
        return None
    try:
        r = run(["docker", "logs", "--tail", "200", "--timestamps", name], capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        return "(docker logs timed out)"
    return r.stdout + r.stderr  # docker writes the container's stderr to ours


def status(agents, state, fleet=None):
    agent_rows = [{"agent": n, "up": alive(n), "restarts": state.get(n, {}).get("restarts", 0),
                   "last_seen": state.get(n, {}).get("last_seen"), "halted": bool(state.get(n, {}).get("stopped")),
                   "chat": bool(agents[n].get("chat"))}
                  for n in agents]
    return {"agents": agent_rows, "system": {**system(), **system_extra()}, "containers": containers((fleet or {}).get("containers")),
            "cron_logs": cron_logs((fleet or {}).get("cron_logs"))}


def send_msg(sender, to, message):
    HOME.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"from": sender, "to": to, "ts": time.strftime("%FT%T"), "message": message})
    with MAILBOX.open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(line + "\n")


def mail(me):
    """(total lines, unread messages for `me` or 'all') without moving the cursor."""
    cursor = HOME / f".cursor-{me}"
    seen = int(cursor.read_text()) if cursor.exists() else 0
    lines = []
    if MAILBOX.exists():
        with MAILBOX.open() as f:
            fcntl.flock(f, fcntl.LOCK_SH)  # don't read a half-written line
            lines = f.read().splitlines()
    msgs = [json.loads(l) for l in lines[seen:]]
    return len(lines), [m for m in msgs if m["to"] in (me, "all")]


def inbox(me):
    """Unread messages; reading advances the per-reader cursor."""
    total, msgs = mail(me)
    (HOME / f".cursor-{me}").write_text(str(total))
    return msgs


PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>agent-perch</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1c2330;--mute:#6b7586;--line:#e3e6eb;--up:#1a9d5c;--down:#d6403a;--halt:#c98a0b;--btn:#eef0f4;--btn-h:#e0e4ea;--term:#0f141b;--term-fg:#c9d4e3}
@media(prefers-color-scheme:dark){:root{--bg:#0e1116;--card:#171c24;--fg:#e6ebf2;--mute:#8693a6;--line:#262d38;--up:#3dcf85;--down:#ff6b64;--halt:#e6b04a;--btn:#222a35;--btn-h:#2d3745}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:980px;margin:0 auto;padding:28px 16px 48px}
header{display:flex;flex-wrap:wrap;align-items:baseline;justify-content:space-between;gap:8px;margin-bottom:20px}
h1{margin:0;font-size:22px;letter-spacing:-.01em}h1 small{color:var(--mute);font-weight:400;font-size:14px;margin-left:8px}
#stamp{color:var(--mute);font-size:13px}
nav{display:flex;gap:6px;margin-bottom:16px}nav button.on{background:var(--fg);color:var(--bg)}
section[data-tab]{display:none}
.sys{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin-bottom:20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.label{color:var(--mute);font-size:12px;text-transform:uppercase;letter-spacing:.06em}
.big{font-size:22px;font-weight:600;margin-top:2px}
.bar{height:6px;border-radius:3px;background:var(--line);margin-top:8px;overflow:hidden}.bar i{display:block;height:100%;background:var(--up);border-radius:3px}
.agents{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:12px}
.agent{display:flex;flex-direction:column;gap:10px}
.top{display:flex;align-items:center;justify-content:space-between;gap:8px}.name{font-weight:600;word-break:break-all}
.pill{font-size:12px;font-weight:600;padding:2px 10px;border-radius:999px;color:#fff;white-space:nowrap}
.UP{background:var(--up)}.DOWN{background:var(--down)}.HALTED{background:var(--halt)}
.meta{color:var(--mute);font-size:13px;display:flex;justify-content:space-between}
.btns{display:flex;flex-wrap:wrap;gap:6px}
button{font:inherit;font-size:13px;color:var(--fg);background:var(--btn);border:0;border-radius:8px;padding:5px 12px;cursor:pointer}button:hover{background:var(--btn-h)}
#logbox{margin-top:20px;display:none}#logbox h2{font-size:14px;margin:0 0 8px;color:var(--mute);font-weight:500;display:flex;justify-content:space-between;align-items:center}
#chat{display:none;gap:8px;margin-top:10px}#chat input{flex:1;font:inherit;font-size:14px;color:var(--fg);background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px 12px}
pre{margin:0;background:var(--term);color:var(--term-fg);border-radius:12px;padding:14px 16px;font:12.5px/1.45 ui-monospace,Menlo,Consolas,monospace;overflow:auto;max-height:55vh}
</style>
<main>
<header><h1>agent-perch<small>supervised agents</small></h1><span id=stamp></span></header>
<nav id=tabs><button data-t=agents>Agents</button><button data-t=containers>Containers</button><button data-t=events>Events</button><button data-t=system>System</button></nav>
<section data-tab=events><pre id=events>-</pre></section>
<section data-tab=system><div class=sys style="margin-bottom:16px"><div class=card><div class=label>Disk used</div><div class=big id=disk>-</div><div class=bar><i id=diskbar></i></div></div>
<div class=card><div class=label>Memory available</div><div class=big id=mem>-</div></div>
<div class=card><div class=label>Load (1 min)</div><div class=big id=loadv>-</div></div>
<div class=card><div class=label>Uptime</div><div class=big id=uptime>-</div></div>
<div class=card><div class=label>CPU temp</div><div class=big id=temp>-</div></div></div>
<div class=label style="margin-bottom:8px" id=cronlabel>Job logs (stalest first)</div><div class=card id=cronlogs style="max-height:50vh;overflow:auto"></div></section>
<section data-tab=agents><div class=card style="margin-bottom:12px"><div class=label>Agents up</div><div class=big id=upcount>-</div></div><section class=agents id=agents></section></section>
<section data-tab=containers><section class=agents id=containers></section><div id=noct class=meta>no containers configured ([fleet] containers)</div></section>
<section id=logbox><h2><span id=logname></span><button id=logclose>close</button></h2><pre id=log></pre>
<form id=chat><input id=msg placeholder="type to this agent (Enter to send)" autocomplete=off><button>send</button></form></section>
</main>
<script>
let token = '';
const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; };
const ago = (ts) => { if (!ts) return 'never'; const s = Math.max(0, Date.now() / 1000 - ts); return s < 90 ? 'just now' : s < 5400 ? Math.round(s / 60) + ' min ago' : s < 129600 ? Math.round(s / 3600) + ' h ago' : Math.round(s / 86400) + ' d ago'; };
// try first without a token (tailnet identity may already authorise us); ask only after a 403
async function call(url, opts = {}) {
  let r = await fetch(url, {...opts, headers: {'X-Token': token}});
  if (r.status == 403) {
    token = prompt('token') || '';
    if (token) r = await fetch(url, {...opts, headers: {'X-Token': token}});
    if (r.status == 403) token = '';
  }
  return r;
}
async function act(cmd, agent) {
  const r = await call(`do/${cmd}/${encodeURIComponent(agent)}`, {method: 'POST'});
  if (r.status == 403) alert('bad token or control disabled');
  load();
}
async function loadEvents() {
  const r = await call('events'); if (r.status == 200) $('events').textContent = await r.text();
}
function tab(t) {
  if (t == 'events') loadEvents();
  document.querySelectorAll('section[data-tab]').forEach((s) => s.style.display = s.dataset.tab == t ? 'block' : 'none');
  document.querySelectorAll('#tabs button').forEach((b) => b.classList.toggle('on', b.dataset.t == t));
  try { localStorage.setItem('tab', t); } catch (e) {}
}
document.querySelectorAll('#tabs button').forEach((b) => b.onclick = () => tab(b.dataset.t));
let tabInit = 'agents'; try { tabInit = localStorage.getItem('tab') || 'agents'; } catch (e) {}
tab(tabInit);
let watching = null, timer = null, chatOn = false, logBase = 'logs';
async function refreshLog() {
  if (!watching) return;
  const pre = $('log'), atEnd = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
  const r = await call(`${logBase}/${encodeURIComponent(watching)}`);
  if (r.status == 403) return $('logclose').click();
  pre.textContent = await r.text();
  if (atEnd) pre.scrollTop = pre.scrollHeight;
}
function logs(agent, chat, base = 'logs') {
  watching = agent; chatOn = chat; logBase = base;
  $('logbox').style.display = 'block'; $('logname').textContent = agent + ' (live)';
  $('chat').style.display = chat ? 'flex' : 'none';
  clearInterval(timer); timer = setInterval(refreshLog, 3000);
  refreshLog().then(() => $('logbox').scrollIntoView({behavior: 'smooth'}));
}
$('logclose').onclick = () => { watching = null; clearInterval(timer); $('logbox').style.display = 'none'; };
$('chat').onsubmit = async (e) => {
  e.preventDefault();
  const text = $('msg').value.trim(); if (!text || !watching) return;
  const r = await call(`send/${encodeURIComponent(watching)}`, {method: 'POST', body: text});
  if (r.status == 403) alert(await r.text()); else { $('msg').value = ''; setTimeout(refreshLog, 800); }
};
async function load() {
  const j = await (await fetch('status.json')).json(), d = j.agents;
  $('disk').textContent = j.system.disk_pct + '%'; $('diskbar').style.width = j.system.disk_pct + '%';
  $('diskbar').style.background = j.system.disk_pct >= 90 ? 'var(--down)' : j.system.disk_pct >= 80 ? 'var(--halt)' : 'var(--up)';
  $('mem').textContent = j.system.mem_avail_mb + ' MB';
  $('upcount').textContent = d.filter((a) => a.up && !a.halted).length + ' / ' + d.length;
  $('stamp').textContent = 'updated ' + new Date().toLocaleTimeString();
  $('cronlabel').style.display = $('cronlogs').style.display = j.cron_logs.length ? 'block' : 'none';
  $('cronlogs').replaceChildren(...j.cron_logs.map((c) => { const row = el('div', 'meta'); row.append(el('span', '', c.name), el('span', '', ago(Date.now() / 1000 - c.age_s))); return row; }));
  $('noct').style.display = j.containers.length ? 'none' : 'block';
  $('loadv').textContent = j.system.load1.toFixed(2); $('uptime').textContent = j.system.uptime_h >= 48 ? Math.round(j.system.uptime_h / 24) + ' d' : j.system.uptime_h + ' h';
  $('temp').textContent = j.system.temp_c === undefined ? 'n/a' : j.system.temp_c + ' °C';
  $('containers').replaceChildren(...j.containers.map((c) => {
    const card = el('div', 'card agent'), top = el('div', 'top');
    top.append(el('span', 'name', c.name), el('span', 'pill ' + (c.up ? 'UP' : 'DOWN'), c.up ? 'UP' : 'DOWN'));
    const meta = el('div', 'meta'); meta.append(el('span', '', c.status), el('span', '', c.image));
    if (c.ports) meta.append(el('span', '', c.ports));
    const btns = el('div', 'btns'), lb = el('button', '', 'logs'); lb.onclick = () => logs(c.name, false, 'clogs'); btns.append(lb);
    card.append(top, meta, btns); return card;
  }));
  $('agents').replaceChildren(...d.map((a) => {
    const st = a.halted ? 'HALTED' : a.up ? 'UP' : 'DOWN', card = el('div', 'card agent'), top = el('div', 'top'), btns = el('div', 'btns');
    top.append(el('span', 'name', a.agent), el('span', 'pill ' + st, st));
    const meta = el('div', 'meta'); meta.append(el('span', '', a.restarts + ' restarts'), el('span', '', 'seen ' + ago(a.last_seen)));
    for (const c of ['start', 'stop', 'restart']) { const b = el('button', '', c); b.onclick = () => act(c, a.agent); btns.append(b); }
    const lb = el('button', '', 'logs'); lb.onclick = () => logs(a.agent, a.chat); btns.append(lb);
    card.append(top, meta, btns); return card;
  }));
}
load(); setInterval(load, 10000);
</script>"""


def authed(headers):
    """Token match, or (when $PERCH_TS_USER is set) the login `tailscale serve` stamps on tailnet requests."""
    token, ts_user = os.environ.get("PERCH_TOKEN", ""), os.environ.get("PERCH_TS_USER", "")
    if ts_user and headers.get("Tailscale-User-Login", "") == ts_user:  # ponytail: header is forgeable by local processes; they could read the token file anyway
        return True
    return bool(token) and hmac.compare_digest(headers.get("X-Token", ""), token)


class Dash(BaseHTTPRequestHandler):
    """GET is read-only (except /logs/<agent>, which needs the token when one is set). POST /do/<start|stop|restart>/<agent> and POST /send/<agent> (body = one line of text,
    agents with `chat = true` only) need X-Token == $PERCH_TOKEN; with the env var unset, control is disabled entirely."""

    def reply(self, code, body, ctype="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):
        if self.path == "/status.json":
            self.reply(200, json.dumps(status(load_agents(), load_state(), load_fleet())), "application/json")
        elif self.path.startswith(("/logs/", "/clogs/", "/events")):
            # a pane can hold anything the agent printed: gated whenever any auth is configured
            if (os.environ.get("PERCH_TOKEN") or os.environ.get("PERCH_TS_USER")) and not authed(self.headers):
                return self.reply(403, "token required")
            if self.path == "/events":
                return self.reply(200, "\n".join(reversed(recent_events())) or "(no events)")
            name = unquote(self.path.split("/", 2)[2])
            if self.path.startswith("/clogs/"):
                text = container_logs(load_fleet().get("containers"), name)
                return self.reply(200, text) if text is not None else self.reply(404, "unknown container")
            self.reply(200, pane(name) if name in load_agents() and alive(name) else "(not running)")
        else:
            self.reply(200, PAGE, "text/html")

    def do_POST(self):
        if not authed(self.headers):
            return self.reply(403, "forbidden")
        parts = self.path.split("/")  # ['', 'do', cmd, agent] or ['', 'send', agent]
        agents = load_agents()
        if len(parts) == 3 and parts[1] == "send" and unquote(parts[2]) in agents:
            body = self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 4096)).decode(errors="replace")
            name = unquote(parts[2])
            return self.reply(200, "ok") if send_chat(name, agents[name], body) else self.reply(403, "chat disabled for this agent")
        if len(parts) != 4 or parts[1] != "do" or parts[2] not in ("start", "stop", "restart") or unquote(parts[3]) not in agents:
            return self.reply(404, "not found")
        control(parts[2], unquote(parts[3]), agents)
        self.reply(200, "ok")

    def log_message(self, *a):
        pass


def init():
    """First-run helper: write a starter config next to where PERCH_CONFIG points, then print the cron line."""
    example = Path(__file__).with_name("agents.example.toml")
    if CONFIG.exists():
        print(f"{CONFIG} already exists, left alone")
    elif example.exists():
        CONFIG.write_text(example.read_text())
        print(f"wrote {CONFIG} (edit it: replace the demo agent with yours)")
    else:
        sys.exit("agents.example.toml not found next to perch.py; copy it from the repo")
    cfg = CONFIG.resolve()
    print(f"\ncron (every 5 min):\n*/5 * * * * PERCH_CONFIG={cfg} {sys.executable} {Path(__file__).resolve()} tick")
    print("or systemd: see docs/perch.service and docs/perch.timer")


def main(argv=None):
    p = argparse.ArgumentParser(prog="perch")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ls")
    sub.add_parser("tick")
    for c in ("start", "stop", "restart", "logs"):
        sub.add_parser(c).add_argument("agent")
    s = sub.add_parser("send"); s.add_argument("sender"); s.add_argument("to"); s.add_argument("message")
    sub.add_parser("inbox").add_argument("me")
    sub.add_parser("init")
    s = sub.add_parser("serve"); s.add_argument("--port", type=int, default=8080)
    a = p.parse_args(argv)

    if a.cmd == "send":
        return send_msg(a.sender, a.to, a.message)
    if a.cmd == "inbox":
        for m in inbox(a.me):
            print(json.dumps(m))
        return
    if a.cmd == "init":
        return init()
    agents = load_agents()
    if a.cmd in ("start", "stop", "restart", "logs") and a.agent not in agents:
        sys.exit(f"unknown agent: {a.agent}")
    if a.cmd == "ls":
        st = status(agents, load_state())
        for r in st["agents"]:
            print(f"{r['agent']:20} {'HALTED' if r['halted'] else 'UP' if r['up'] else 'DOWN':7} restarts={r['restarts']}")
        print(f"system: {st['system']}")
    elif a.cmd == "tick":
        with locked():
            save_state(tick(agents, load_state(), fleet=load_fleet()))
    elif a.cmd in ("start", "stop", "restart"):
        control(a.cmd, a.agent, agents)
    elif a.cmd == "logs":
        print(pane(a.agent))
    elif a.cmd == "serve":
        HTTPServer(("127.0.0.1", a.port), Dash).serve_forever()


if __name__ == "__main__":
    main()

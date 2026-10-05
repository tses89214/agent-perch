#!/usr/bin/env python3
"""perch: supervise long-running agents in tmux. Stdlib only, Python 3.11+."""
import argparse, contextlib, fcntl, hashlib, hmac, json, os, re, shutil, subprocess, sys, time, tomllib
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


def status(agents, state):
    agent_rows = [{"agent": n, "up": alive(n), "restarts": state.get(n, {}).get("restarts", 0),
                   "last_seen": state.get(n, {}).get("last_seen"), "halted": bool(state.get(n, {}).get("stopped"))}
                  for n in agents]
    return {"agents": agent_rows, "system": system()}


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


PAGE = """<!doctype html><meta charset=utf-8><title>agent-perch</title>
<body style="font:14px monospace;margin:2rem"><h3>agent-perch</h3><p id=sys></p><table id=t border=1 cellpadding=4></table><pre id=log></pre>
<script>
let token = '';
async function act(cmd, agent) {
  if (!token) token = prompt('token') || '';
  const r = await fetch(`do/${cmd}/${encodeURIComponent(agent)}`, {method: 'POST', headers: {'X-Token': token}});
  if (r.status == 403) { token = ''; alert('bad token or control disabled'); }
  load();
}
async function logs(agent) {
  log.textContent = await (await fetch(`logs/${encodeURIComponent(agent)}`)).text();
}
async function load() {
  const j = await (await fetch('status.json')).json(), d = j.agents;
  sys.textContent = `disk ${j.system.disk_pct}%  mem available ${j.system.mem_avail_mb} MB`;
  t.replaceChildren();
  const row = (cells) => { const tr = t.insertRow(); cells.forEach(c => tr.insertCell().append(c)); return tr; };
  row(['agent', 'up', 'restarts', 'last seen', 'control']);
  for (const a of d) {
    const btns = document.createElement('span');
    for (const c of ['start', 'stop', 'restart']) {
      const b = document.createElement('button'); b.textContent = c; b.onclick = () => act(c, a.agent); btns.append(b);
    }
    const lb = document.createElement('button'); lb.textContent = 'logs'; lb.onclick = () => logs(a.agent); btns.append(lb);
    row([a.agent, a.halted ? 'HALTED' : a.up ? 'UP' : 'DOWN', String(a.restarts), a.last_seen ? new Date(a.last_seen * 1000).toLocaleString() : '-', btns]);
  }
}
load(); setInterval(load, 10000);
</script>"""


class Dash(BaseHTTPRequestHandler):
    """GET is read-only. POST /do/<start|stop|restart>/<agent> needs X-Token == $PERCH_TOKEN;
    with the env var unset, control is disabled entirely."""

    def reply(self, code, body, ctype="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):
        if self.path == "/status.json":
            self.reply(200, json.dumps(status(load_agents(), load_state())), "application/json")
        elif self.path.startswith("/logs/"):
            name = unquote(self.path[6:])
            self.reply(200, pane(name) if name in load_agents() and alive(name) else "(not running)")
        else:
            self.reply(200, PAGE, "text/html")

    def do_POST(self):
        token = os.environ.get("PERCH_TOKEN", "")
        if not token or not hmac.compare_digest(self.headers.get("X-Token", ""), token):
            return self.reply(403, "forbidden")
        parts = self.path.split("/")  # ['', 'do', cmd, agent]
        agents = load_agents()
        if len(parts) != 4 or parts[1] != "do" or parts[2] not in ("start", "stop", "restart") or unquote(parts[3]) not in agents:
            return self.reply(404, "not found")
        control(parts[2], unquote(parts[3]), agents)
        self.reply(200, "ok")

    def log_message(self, *a):
        pass


def main(argv=None):
    p = argparse.ArgumentParser(prog="perch")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ls")
    sub.add_parser("tick")
    for c in ("start", "stop", "restart", "logs"):
        sub.add_parser(c).add_argument("agent")
    s = sub.add_parser("send"); s.add_argument("sender"); s.add_argument("to"); s.add_argument("message")
    sub.add_parser("inbox").add_argument("me")
    s = sub.add_parser("serve"); s.add_argument("--port", type=int, default=8080)
    a = p.parse_args(argv)

    if a.cmd == "send":
        return send_msg(a.sender, a.to, a.message)
    if a.cmd == "inbox":
        for m in inbox(a.me):
            print(json.dumps(m))
        return
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

#!/usr/bin/env python3
"""agentctl: supervise long-running agents in tmux. Stdlib only, Python 3.11+."""
import argparse, contextlib, fcntl, hashlib, hmac, json, os, subprocess, sys, time, tomllib
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import unquote

HOME = Path(os.environ.get("AGENT_FLEET_HOME", Path.home() / ".agent-fleet"))
STATE, EVENTS, MAILBOX = HOME / "state.json", HOME / "events.log", HOME / "mailbox.jsonl"
CONFIG = Path(os.environ.get("AGENT_FLEET_CONFIG", "agents.toml"))


def tmux(*args):
    return subprocess.run(["tmux", *args], capture_output=True, text=True)


def load_agents():
    return tomllib.loads(CONFIG.read_text())["agents"]


def load_state():
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def save_state(s):
    HOME.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")  # atomic: dashboard may read mid-write
    tmp.write_text(json.dumps(s, indent=1))
    tmp.replace(STATE)


def log_event(agent, what):
    HOME.mkdir(parents=True, exist_ok=True)
    with EVENTS.open("a") as f:
        f.write(f"{time.strftime('%FT%T')} {agent} {what}\n")


def session(name):
    return f"fleet_{name}"


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


def is_stuck(prev_hash, text, markers):
    """Frozen = screen identical to last tick AND a known stuck marker on it.
    Idle prompt never matches a marker; a working agent's screen changes."""
    same = prev_hash == hashlib.sha1(text.encode()).hexdigest()
    return same and any(m in text for m in markers)


HEALTH_FAILS = 2  # consecutive failures before restart: a fresh start needs a tick or two to come up


def healthy(spec):
    if "health_cmd" not in spec:
        return True
    try:
        return subprocess.run(spec["health_cmd"], shell=True, timeout=10, capture_output=True,
                              cwd=os.path.expanduser(spec.get("cwd", "~"))).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def restart(name, spec, st, why):
    stop(name)
    start(name, spec)
    log_event(name, f"restarted: {why}")
    st["restarts"] += 1
    st["hash"], st["health_fails"] = None, 0


def ring_doorbell(name):
    """Type ONE fixed line into the agent's session. Content never travels this way."""
    cmd = f"Mailbox has new messages. Run: AGENT_FLEET_HOME={HOME} python3 {Path(__file__).resolve()} inbox {name}"
    tmux("send-keys", "-t", session(name), "-l", cmd)
    tmux("send-keys", "-t", session(name), "Enter")
    log_event(name, "doorbell")


def tick(agents, state, now=None):
    """One supervision pass (run from cron). Returns updated state."""
    now = now or time.time()
    for name, spec in agents.items():
        st = state.setdefault(name, {"restarts": 0, "last_seen": None, "hash": None})
        if st.get("stopped"):  # operator ran `stop`; don't resurrect
            continue
        if not alive(name):
            start(name, spec)
            st["restarts"] += 1
            st["hash"] = None
            continue
        text = pane(name)
        if is_stuck(st["hash"], text, spec.get("stuck_markers", [])):
            restart(name, spec, st, "stuck")
            continue
        st["health_fails"] = 0 if healthy(spec) else st.get("health_fails", 0) + 1
        if st["health_fails"] >= HEALTH_FAILS:
            restart(name, spec, st, "health_cmd failed")
            continue
        new_hash = hashlib.sha1(text.encode()).hexdigest()
        idle = st["hash"] == new_hash  # screen static since last tick: not mid-turn
        st["hash"], st["last_seen"] = new_hash, now
        if spec.get("doorbell") and idle:
            total, unread = mail(name)
            if unread and st.get("rung") != total:  # once per new batch, not every tick
                ring_doorbell(name)
                st["rung"] = total
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
        state.setdefault(name, {"restarts": 0, "last_seen": None, "hash": None})["stopped"] = cmd == "stop"
        save_state(state)
        if cmd != "start":
            stop(name)
        if cmd != "stop":
            start(name, agents[name])


def status(agents, state):
    return [{"agent": n, "up": alive(n), "restarts": state.get(n, {}).get("restarts", 0),
             "last_seen": state.get(n, {}).get("last_seen")} for n in agents]


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


PAGE = """<!doctype html><meta charset=utf-8><title>agent-fleet</title>
<body style="font:14px monospace;margin:2rem"><h3>agent-fleet</h3><table id=t border=1 cellpadding=4></table><pre id=log></pre>
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
  const d = await (await fetch('status.json')).json();
  t.replaceChildren();
  const row = (cells) => { const tr = t.insertRow(); cells.forEach(c => tr.insertCell().append(c)); return tr; };
  row(['agent', 'up', 'restarts', 'last seen', 'control']);
  for (const a of d) {
    const btns = document.createElement('span');
    for (const c of ['start', 'stop', 'restart']) {
      const b = document.createElement('button'); b.textContent = c; b.onclick = () => act(c, a.agent); btns.append(b);
    }
    const lb = document.createElement('button'); lb.textContent = 'logs'; lb.onclick = () => logs(a.agent); btns.append(lb);
    row([a.agent, a.up ? 'UP' : 'DOWN', String(a.restarts), a.last_seen ? new Date(a.last_seen * 1000).toLocaleString() : '-', btns]);
  }
}
load(); setInterval(load, 10000);
</script>"""


class Dash(BaseHTTPRequestHandler):
    """GET is read-only. POST /do/<start|stop|restart>/<agent> needs X-Token == $AGENT_FLEET_TOKEN;
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
        token = os.environ.get("AGENT_FLEET_TOKEN", "")
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
    p = argparse.ArgumentParser(prog="agentctl")
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
        for r in status(agents, load_state()):
            print(f"{r['agent']:20} {'UP' if r['up'] else 'DOWN':5} restarts={r['restarts']}")
    elif a.cmd == "tick":
        with locked():
            save_state(tick(agents, load_state()))
    elif a.cmd in ("start", "stop", "restart"):
        control(a.cmd, a.agent, agents)
    elif a.cmd == "logs":
        print(pane(a.agent))
    elif a.cmd == "serve":
        HTTPServer(("127.0.0.1", a.port), Dash).serve_forever()


if __name__ == "__main__":
    main()

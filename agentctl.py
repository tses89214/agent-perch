#!/usr/bin/env python3
"""agentctl: supervise long-running agents in tmux. Stdlib only, Python 3.11+."""
import argparse, fcntl, hashlib, json, os, subprocess, sys, time, tomllib
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

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
    STATE.write_text(json.dumps(s, indent=1))


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


def tick(agents, state, now=None):
    """One supervision pass (run from cron). Returns updated state."""
    now = now or time.time()
    for name, spec in agents.items():
        st = state.setdefault(name, {"restarts": 0, "last_seen": None, "hash": None})
        if not alive(name):
            start(name, spec)
            st["restarts"] += 1
            st["hash"] = None
            continue
        text = pane(name)
        if is_stuck(st["hash"], text, spec.get("stuck_markers", [])):
            stop(name)
            start(name, spec)
            log_event(name, "restarted: stuck")
            st["restarts"] += 1
            st["hash"] = None
            continue
        st["hash"] = hashlib.sha1(text.encode()).hexdigest()
        st["last_seen"] = now
    return state


def status(agents, state):
    return [{"agent": n, "up": alive(n), "restarts": state.get(n, {}).get("restarts", 0),
             "last_seen": state.get(n, {}).get("last_seen")} for n in agents]


def send_msg(sender, to, message):
    HOME.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"from": sender, "to": to, "ts": time.strftime("%FT%T"), "message": message})
    with MAILBOX.open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(line + "\n")


def inbox(me):
    """Unread messages for `me` (or 'all'); per-reader cursor file."""
    cursor = HOME / f".cursor-{me}"
    seen = int(cursor.read_text()) if cursor.exists() else 0
    lines = MAILBOX.read_text().splitlines() if MAILBOX.exists() else []
    cursor.write_text(str(len(lines)))
    msgs = [json.loads(l) for l in lines[seen:]]
    return [m for m in msgs if m["to"] in (me, "all")]


PAGE = """<!doctype html><meta charset=utf-8><title>agent-fleet</title>
<body style="font:14px monospace;margin:2rem"><h3>agent-fleet</h3><table id=t border=1 cellpadding=4></table>
<script>fetch('status.json').then(r=>r.json()).then(d=>t.innerHTML='<tr><th>agent<th>up<th>restarts<th>last seen</tr>'+
d.map(a=>`<tr><td>${a.agent}<td>${a.up?'UP':'DOWN'}<td>${a.restarts}<td>${a.last_seen?new Date(a.last_seen*1000).toLocaleString():'-'}</tr>`).join(''))</script>"""


class Dash(BaseHTTPRequestHandler):  # read-only: no POST handler, nothing mutates
    def do_GET(self):
        if self.path == "/status.json":
            body, ctype = json.dumps(status(load_agents(), load_state())).encode(), "application/json"
        else:
            body, ctype = PAGE.encode(), "text/html"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body)

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
        save_state(tick(agents, load_state()))
    elif a.cmd == "start":
        start(a.agent, agents[a.agent])
    elif a.cmd == "stop":
        stop(a.agent)
    elif a.cmd == "restart":
        stop(a.agent); start(a.agent, agents[a.agent])
    elif a.cmd == "logs":
        print(pane(a.agent))
    elif a.cmd == "serve":
        HTTPServer(("127.0.0.1", a.port), Dash).serve_forever()


if __name__ == "__main__":
    main()

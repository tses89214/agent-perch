"""Run: python3 test_perch.py. Needs tmux. Uses a temp HOME so nothing real is touched."""
import hashlib, os, tempfile, time

os.environ["PERCH_HOME"] = tempfile.mkdtemp()
import perch as a

h = lambda t: hashlib.sha1(t.encode()).hexdigest()

# stuck needs BOTH unchanged screen and marker: idle prompt must never be killed
assert a.is_stuck(h("Press Enter"), "Press Enter", ["Press Enter"])
assert not a.is_stuck(h("old"), "Press Enter", ["Press Enter"])      # screen changed = working
assert not a.is_stuck(h("$ "), "$ ", ["Press Enter"])                # idle, no marker

# idle marker suppresses a stuck marker (turn already ended); old scrollback beyond the tail is ignored
assert not a.is_stuck(h("err\nbypass on"), "err\nbypass on", ["err"], ["bypass on"])
old = "err\n" + "\n".join("x" * 1 for _ in range(12))
assert not a.is_stuck(h(old), old, ["err"])

# send/inbox: addressed + broadcast delivered once, others' mail skipped
a.send_msg("x", "bob", "hi"); a.send_msg("x", "all", "yo"); a.send_msg("x", "carol", "no")
assert [m["message"] for m in a.inbox("bob")] == ["hi", "yo"]
assert a.inbox("bob") == []

# real tmux: tick starts a dead agent, then leaves a healthy one alone
agents = {"t1": {"cmd": "sleep 60", "stuck_markers": ["zzz"]}}
try:
    st = a.tick(agents, {})
    time.sleep(0.3)
    assert a.alive("t1") and st["t1"]["restarts"] == 1
    st = a.tick(agents, st)
    assert st["t1"]["restarts"] == 1
    # operator stop must stick across ticks
    a.stop("t1"); st["t1"]["stopped"] = True
    st = a.tick(agents, st)
    assert not a.alive("t1")
finally:
    a.stop("t1")

# health_cmd: needs HEALTH_FAILS consecutive failures (one blip must not restart), passing resets
agents = {"h1": {"cmd": "sleep 60", "health_cmd": "false"}}
try:
    st = a.tick(agents, {}); time.sleep(0.3)          # start
    st = a.tick(agents, st); assert st["h1"]["restarts"] == 1 and st["h1"]["health_fails"] == 1
    st = a.tick(agents, st); assert st["h1"]["restarts"] == 2     # 2nd consecutive fail
    agents["h1"]["health_cmd"] = "true"; st["h1"]["health_fails"] = 1
    st = a.tick(agents, st); assert st["h1"]["health_fails"] == 0 and st["h1"]["restarts"] == 2
finally:
    a.stop("h1")

# doorbell: rings only when idle AND unread; once per batch; reading silences it
agents = {"d1": {"cmd": "cat", "doorbell": True}}      # `cat` echoes what it is typed
a.send_msg("x", "d1", "hello")
try:
    st = a.tick(agents, {}); time.sleep(0.3)
    st = a.tick(agents, st)                              # first screen sample, not yet idle
    assert "rung" not in st["d1"] and "Mailbox" not in a.pane("d1")
    st = a.tick(agents, st); time.sleep(0.3)             # idle + unread -> ring
    assert "Mailbox has new messages" in a.pane("d1"), a.pane("d1")
    n = a.pane("d1").count("Mailbox has new messages")
    st = a.tick(agents, st); st = a.tick(agents, st); time.sleep(0.3)
    assert a.pane("d1").count("Mailbox has new messages") == n   # no re-ring
finally:
    a.stop("d1")

# crash loop: LOOP_MAX restarts in the window halts the agent and alerts; later ticks leave it alone.
# The initial boot of a never-seen agent does not alert.
out = a.HOME / "alerts.txt"
fleet = {"notify_cmd": f'echo "$PERCH_MSG" >> {out}'}
agents = {"c1": {"cmd": "true"}}                   # exits immediately
st = {}
for _ in range(3):
    st = a.tick(agents, st, fleet=fleet); time.sleep(0.3)
assert st["c1"]["stopped"] and st["c1"]["restarts"] == 3
alerts = out.read_text().splitlines()
assert len([l for l in alerts if "restarted" in l]) == 1 and any("halted" in l for l in alerts), alerts
st = a.tick(agents, st, fleet=fleet); assert st["c1"]["restarts"] == 3     # halted: not resurrected

# resource levels, and alert only on escalation (not every tick)
assert (a.level(85, 80, 90), a.level(95, 80, 90), a.level(70, 80, 90)) == (1, 2, 0)
assert (a.level(300, 1000, 400, False), a.level(2000, 1000, 400, False)) == (2, 0)
out.unlink()
sysst = {}
hot = {**fleet, "disk_warn": 0, "disk_crit": 0, "mem_warn_mb": 10**9, "mem_crit_mb": 10**9}
a.check_system(sysst, hot); a.check_system(sysst, hot)
assert len(out.read_text().splitlines()) == 2      # disk + mem once each, second pass silent

# a broken notifier must not break supervision
a.alert({"notify_cmd": "exit 1"}, "x")

# events.log rotates instead of growing forever
a.EVENTS.write_text("x" * 1_000_001)
a.log_event("t", "after"); assert a.EVENTS.with_suffix(".log.1").exists() and a.EVENTS.stat().st_size < 100

# auto_reply: a known dialog gets its keys; the agent proceeds
agents = {"r1": {"cmd": "sh -c 'echo Resume-now?; read x; echo got-$x; sleep 60'",
                 "auto_replies": [{"match": "Resume-now?", "keys": ["y", "Enter"]}]}}
try:
    st = a.tick(agents, {}); time.sleep(0.5)
    st = a.tick(agents, st); time.sleep(0.5)
    assert "got-y" in a.pane("r1"), a.pane("r1")
finally:
    a.stop("r1")

# restart_markers: restart at once (no unchanged-screen wait); notice_regex: alert once per distinct line, no action
out.unlink()
agents = {"m1": {"cmd": "sh -c 'echo Enter to select; echo you hit the limit; sleep 60'",
                 "restart_markers": ["Enter to select"], "notice_regex": "hit.*limit"}}
try:
    st = a.tick(agents, {}, fleet=fleet); time.sleep(0.4)
    st = a.tick(agents, st, fleet=fleet)               # menu visible -> restarted on first sighting
    assert st["m1"]["restarts"] == 2, st
    alerts = out.read_text().splitlines()
    assert sum("hit the limit" in l for l in alerts) == 1 and any("unanswerable" in l for l in alerts), alerts
    time.sleep(0.4); st = a.tick(agents, st, fleet=fleet)
    assert sum("hit the limit" in l for l in out.read_text().splitlines()) == 1
finally:
    a.stop("m1")

# probe: alert on transitions only; health_cmd sees $PERCH_SESSION
out.unlink()
pst = {}
for cmd in ("false", "false", "true", "true"):
    a.check_probe(pst, {**fleet, "probe_cmd": cmd})
assert [l.split()[0] for l in out.read_text().splitlines()] == ["probe", "probe"], out.read_text()
assert a.healthy("x", {"health_cmd": f'test "$PERCH_SESSION" = {a.session("x")}'})

# dashboard page: every element id the script touches exists, and the script parses (a typo here = blank dashboard)
import re, shutil
ids = set(re.findall(r"\$\('(\w+)'\)", a.PAGE))
assert ids and all(f"id={i}" in a.PAGE for i in ids), ids - set(re.findall(r"id=(\w+)", a.PAGE))
js = re.search(r"<script>(.*)</script>", a.PAGE, re.S).group(1)
if shutil.which("node"):
    (a.HOME / "page.js").write_text(js)
    assert os.system(f"node --check {a.HOME / 'page.js'}") == 0

# chat: refused unless the agent opted in; one line only; logged; over HTTP it also needs the token
import threading, urllib.request, urllib.error
agents = {"c1": {"cmd": "cat", "chat": True}, "c2": {"cmd": "cat"}}
a.CONFIG = a.HOME / "chat.toml"
a.CONFIG.write_text('[agents.c1]\ncmd="cat"\nchat=true\n[agents.c2]\ncmd="cat"\n')
os.environ["PERCH_TOKEN"] = "tok"
srv = a.HTTPServer(("127.0.0.1", 0), a.Dash); threading.Thread(target=srv.serve_forever, daemon=True).start()
def post(path, body, token="tok"):
    req = urllib.request.Request(f"http://127.0.0.1:{srv.server_port}{path}", body.encode(), {"X-Token": token}, method="POST")
    try:
        return urllib.request.urlopen(req).status
    except urllib.error.HTTPError as e:
        return e.code
try:
    st = a.tick(agents, {}); time.sleep(0.3)
    assert not a.send_chat("c2", agents["c2"], "nope") and "nope" not in a.pane("c2")      # not opted in
    assert post("/send/c1", "hi\nthere", token="wrong") == 403                             # no token, no typing
    assert post("/send/c2", "x") == 403 and post("/send/c1", "hello  from\nweb") == 200
    time.sleep(0.4)
    assert a.send_chat("c1", agents["c1"], "-n --help") ; time.sleep(0.4)      # leading dash is text, not a tmux flag
    assert "-n --help" in a.pane("c1")
    assert "hello from web" in a.pane("c1") and "chat: 'hello from web'" in a.EVENTS.read_text()
    assert [r["chat"] for r in a.status(agents, st)["agents"]] == [True, False]
finally:
    srv.shutdown(); a.stop("c1"); a.stop("c2"); del os.environ["PERCH_TOKEN"]

# containers: only requested globs, up/down from the Status text, no docker = empty list not a crash
fake = lambda *a, **k: type("R", (), {"stdout": "app-live\tUp 2 days\timg:1\t127.0.0.1:80->80/tcp\napp-old\tExited (130) 5 days ago\timg:1\t\nother\tUp 1 hour\timg:2\t\n", "stderr": "err-line\n"})()
assert [(c["name"], c["up"]) for c in a.containers(["app-*"], fake)] == [("app-live", True), ("app-old", False)]
assert a.containers(["app-live"], fake)[0]["image"] == "img:1" and a.containers(["app-live"], fake)[0]["ports"]
assert "err-line" in a.container_logs(["app-*"], "app-live", fake)
assert a.container_logs(["app-*"], "other", fake) is None        # outside the configured globs: never passed to docker
assert a.container_logs(["app-*"], "--all", fake) is None         # nor an option-looking name
def slow(*x, **k):
    if x[0][1] == "logs": raise a.subprocess.TimeoutExpired("docker", 10)
    return fake()
assert "timed out" in a.container_logs(["app-*"], "app-live", slow)   # hung docker must not 500 the dashboard
assert a.containers([], fake) == [] and a.containers(["x"], lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError())) == []

# logs need the token whenever one is set (panes can hold anything an agent printed)
os.environ["PERCH_TOKEN"] = "tok"
srv = a.HTTPServer(("127.0.0.1", 0), a.Dash); threading.Thread(target=srv.serve_forever, daemon=True).start()
def get(path, token=None):
    req = urllib.request.Request(f"http://127.0.0.1:{srv.server_port}{path}", headers={"X-Token": token} if token else {})
    try:
        return urllib.request.urlopen(req).status
    except urllib.error.HTTPError as e:
        return e.code
try:
    assert get("/logs/c1") == 403 and get("/logs/c1", "wrong") == 403 and get("/logs/c1", "tok") == 200
    assert get("/status.json") == 200                       # status stays readable without a token
    del os.environ["PERCH_TOKEN"]
    assert get("/logs/c1") == 200                            # no token configured = local-only default stays open
finally:
    srv.shutdown()

# tailnet identity: right login passes with no token, wrong/absent login does not, token still works
os.environ["PERCH_TOKEN"] = "tok"; os.environ["PERCH_TS_USER"] = "me@x"
assert a.authed({"Tailscale-User-Login": "me@x"}) and a.authed({"X-Token": "tok"})
assert not a.authed({"Tailscale-User-Login": "you@x"}) and not a.authed({})
del os.environ["PERCH_TOKEN"]
assert a.authed({"Tailscale-User-Login": "me@x"}) and not a.authed({"X-Token": "tok"})
del os.environ["PERCH_TS_USER"]
assert not a.authed({"Tailscale-User-Login": "me@x"})   # feature is off unless configured

# container down alert: fires once on Up->Down transition, not for a container first seen down, never restarts anything
calls = []
a.alert = lambda fleet, msg: calls.append(msg)
rows = {"v": "svc\tUp 1 hour\timg\t\nold\tExited (1) 2 days ago\timg\t\n"}
fake = lambda *x, **k: type("R", (), {"stdout": rows["v"], "stderr": ""})()
st, fl = {}, {"containers": ["*"]}
a.check_containers(st, fl, fake); assert calls == []             # first sight: baseline only, "old" is not news
rows["v"] = "svc\tExited (137) 1 minute ago\timg\t\nold\tExited (1) 2 days ago\timg\t\n"
a.check_containers(st, fl, fake); a.check_containers(st, fl, fake)
assert len(calls) == 1 and "svc" in calls[0]                      # once, then silent while it stays down
rows["v"] = "svc\tUp 5 seconds\timg\t\nold\tExited (1) 2 days ago\timg\t\n"
a.check_containers(st, fl, fake); rows["v"] = rows["v"].replace("Up 5 seconds", "Exited (1) now")
a.check_containers(st, fl, fake); assert len(calls) == 2           # recovers then dies again = new alert

calls.clear(); st2 = {}
a.check_containers(st2, {"containers": ["*"], "containers_alert": ["nomatch*"]}, fake); rows["v"] = "svc\tExited (1) now\timg\t\n"
a.check_containers(st2, {"containers": ["*"], "containers_alert": ["nomatch*"]}, fake); assert calls == []   # outside the alert subset: shown, never paged

# cron log ages: stalest first, missing globs are fine
d = a.HOME / "cl"; d.mkdir(exist_ok=True)
for n, age in (("fresh.log", 10), ("stale.log", 5000)):
    (d / n).write_text("x"); os.utime(d / n, (time.time() - age,) * 2)
rows_ = a.cron_logs([str(d / "*.log"), "/nonexistent/*.log"])
assert [r["name"] for r in rows_] == ["cl/stale.log", "cl/fresh.log"] and rows_[0]["age_s"] >= 5000
assert a.cron_logs(None) == []

# events: newest-last in the file, endpoint reverses; init refuses to clobber an existing config
a.log_event("x", "hello"); assert a.recent_events(1)[0].endswith("x hello")
a.CONFIG = a.HOME / "init.toml"; a.CONFIG.write_text("keep"); a.init(); assert a.CONFIG.read_text() == "keep"

# lock: second holder is excluded while the first is inside
import fcntl
with a.locked():
    with (a.HOME / ".lock").open("w") as f2:
        try:
            fcntl.flock(f2, fcntl.LOCK_EX | fcntl.LOCK_NB); raise SystemExit("lock not exclusive")
        except BlockingIOError:
            pass
print("ok")

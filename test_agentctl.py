"""Run: python3 test_agentctl.py. Needs tmux. Uses a temp HOME so nothing real is touched."""
import hashlib, os, tempfile, time

os.environ["AGENT_FLEET_HOME"] = tempfile.mkdtemp()
import agentctl as a

h = lambda t: hashlib.sha1(t.encode()).hexdigest()

# stuck needs BOTH unchanged screen and marker: idle prompt must never be killed
assert a.is_stuck(h("Press Enter"), "Press Enter", ["Press Enter"])
assert not a.is_stuck(h("old"), "Press Enter", ["Press Enter"])      # screen changed = working
assert not a.is_stuck(h("$ "), "$ ", ["Press Enter"])                # idle, no marker

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
fleet = {"notify_cmd": f'echo "$FLEET_MSG" >> {out}'}
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

# lock: second holder is excluded while the first is inside
import fcntl
with a.locked():
    with (a.HOME / ".lock").open("w") as f2:
        try:
            fcntl.flock(f2, fcntl.LOCK_EX | fcntl.LOCK_NB); raise SystemExit("lock not exclusive")
        except BlockingIOError:
            pass
print("ok")

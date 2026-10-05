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
finally:
    a.stop("t1")
print("ok")

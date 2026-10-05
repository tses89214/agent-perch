# Case study: running a fleet of always-on agents on a Raspberry Pi

`agent-fleet` is the generic tool. This is the environment it came from, and the parts that stayed environment-specific on purpose.

## The setup

About ten long-running Claude Code agents, each in its own tmux session, each reachable from a messaging app (Telegram) through its own webhook. One Raspberry Pi, one Unix user, a home-network router in front.

## Ingress: messages must reach the right agent

- Each agent runs a small webhook server on a distinct localhost port. nginx routes by URL path prefix to the right port, so one TLS hostname and one forwarded router port serve every agent.
- A request is only believed if it carries the secret token the messaging platform echoes back when the webhook was registered, and only a single allow-listed user id may talk to an agent. Anything else is dropped.
- Two fixed harness commands (`/clear`, `/compact`) are typed into the agent's own pane when the allow-listed user sends them. Fixed text, authenticated sender, no model-visible content. Anything that carries real content between agents goes through the file mailbox instead (see the README for why).

## Failure modes found in production, and what each led to

| Symptom | Cause | Fix |
|---|---|---|
| Agent silently stops answering | tmux session died | liveness check + restart (now `tick`) |
| Session alive, never answers, screen shows an interactive menu | frozen mid-turn on a keypress nobody can send | unchanged-screen AND known-marker rule |
| Session alive, screen normal, no messages arrive | the agent's channel subprocess died and the harness never respawned it (known upstream issue) | process-tree check (now `health_cmd`) |
| Same dialog after every restart | a harness confirmation prompt, not a model state | answer it with its own default key (now `auto_replies`) |
| Messages stop arriving, every local process healthy | outside path broke: nginx redirect, router port forward, dynamic-DNS record | external check that alerts through an independent bot, because restarting an agent cannot fix it. Environment-specific, so not in the tool |
| Unexplained reboot | power or thermal | boot-time alert that includes the Pi's under-voltage and throttling flags, since the system journal was volatile-only at the time and the cause often could not be reconstructed afterwards |

## What was deliberately not generalised

- External-path check, boot diagnostics and full-system backup: they depend on this network and this hardware.
- Quota-exhaustion detection: still observe-only. The exact banner text had to be captured from a real occurrence before any automatic switching could be designed, so the detector logs and alerts and touches nothing.
- A routing proxy for failing over between model providers was built, tested, and removed: the free tiers it was meant to use did not work in practice, and a second account's quota was not independent of the first.

## Lessons

1. Detect with two weak signals together (screen unchanged and a marker), not one strong-looking one.
2. Alert on transitions, not states, or the alerts get muted.
3. If a fix cannot work (restarting an agent for a broken router), alert a human through a different path instead of looping.
4. Keep inter-agent content out of other agents' terminals.

# agent-fleet

Keep a fleet of long-running agent processes (e.g. Claude Code bots) alive on one machine. One file, stdlib only, Python 3.11+.

![architecture](docs/architecture.svg)

## Why

Running several always-on agents on a Raspberry Pi, three things kept failing: a session dies silently, a session freezes on a keypress prompt nobody can answer, and agents need to talk to each other. This is the extracted, generic version of the watchdog that handles those.

## Use

```
cp agents.example.toml agents.toml     # declare agents
python3 agentctl.py tick               # run from cron every minute
python3 agentctl.py ls | start | stop | restart | logs <agent>
python3 agentctl.py send <from> <to|all> "msg"; python3 agentctl.py inbox <me>
AGENT_FLEET_TOKEN=secret python3 agentctl.py serve --port 8080  # dashboard; without the token it is read-only
python3 test_agentctl.py               # needs tmux
```

## Design decisions

- **Frozen = unchanged screen AND a known marker.** Either signal alone misfires: an idle agent at a prompt has an unchanged screen; a working agent shows a marker-like word mid-output. Both together means a keypress prompt nobody will answer. An idle agent is only at risk if its unchanged screen also contains a marker, so pick markers specific to the stuck prompt. Cost: markers are per-agent config you must know up front.
- **tmux as the process boundary.** Sessions survive supervisor crashes, and you can attach to see exactly what an agent sees. Cost: liveness is "session exists", not "work is progressing".
- **Mailbox is a file, not keystroke injection.** Typing into another agent's terminal corrupts whatever it is mid-way through. A jsonl append (flock) with per-reader cursors is boring and inspectable. Cost: agents must poll; no push.
- **Health = optional `health_cmd`, restart after 2 consecutive failures.** Session-exists and screen markers cannot see a hung agent whose screen looks normal (the real fleet needed a check that its MCP child process was alive). The agent's owner knows what "healthy" means, so it is a shell command, not a built-in guess. Two failures, not one, so a slow start or a blip does not cause a restart loop; cost is up to ~2 ticks of detection delay.
- **Doorbell is opt-in per agent and carries no content.** A file mailbox is useless if nothing makes an LLM agent read it. Typing a message into its terminal would work but is a prompt-injection channel and corrupts a turn in progress. So the supervisor types one fixed line ("run `agentctl inbox <name>`") only when the agent declared `doorbell = true`, the screen has been static for a tick (not mid-turn), no stuck marker is showing, and there is an unread message; it rings once per new batch. Content still travels through the file. Ceiling: a static screen at an unmarked menu would still receive the line.
- **One flock serialises tick, CLI and dashboard.** All three read-modify-write `state.json`. Without it, a `stop` landing mid-tick is overwritten by the tick's stale copy and the agent is resurrected. A lock file beats a daemon owning state because the supervisor stays a cron one-shot.
- **Alerts via a user-supplied `notify_cmd`, text in `$FLEET_MSG`.** Without alerts a restart is invisible: the Pi fleet's value was being told. The tool should not own a Telegram/Slack/email client, so it runs your command. The message goes through an environment variable, not argv, so agent-controlled text is never re-parsed by the shell. A failing notifier is logged and swallowed: alerting must not take supervision down with it. Cost: no delivery guarantee or retry.
- **Crash-loop halt: 3 restarts in 10 minutes stops the agent and alerts once.** An agent that dies on boot would otherwise be restarted every tick forever, burning CPU and spamming. The halt reuses the operator-`stop` flag, so there is one "do not touch" mechanism; only `agentctl start|restart` clears it and resets the budget. The very first boot of a never-seen agent counts toward the window but does not alert (a reboot should not page once per agent). Cost: a flapping-but-recoverable agent needs a human.
- **Resource alerts fire on escalation only (ok -> warn -> crit).** A disk stuck at 85% should page once, not every minute. Thresholds are configurable; defaults are a Pi's (80/90% disk, 1000/400 MB available). Reads `/proc/meminfo` and `shutil`, no dependencies. Cost: it watches the machine, not each agent, so no per-agent CPU/memory limits (see Limits).
- **`auto_replies`: known dialog text -> keys, instead of a restart.** Some prompts are harness-level and a restart just shows the same prompt again (Claude Code's resume-from-summary dialog). Matching text from your own config and sending only keys from that config keeps this mechanical: no model-visible content is injected. It runs before stuck detection so a dialog is answered, not killed. Cost: you must know the dialog's text.
- **`events.log` rotates at 1 MB, keeping one old file.** Bounded disk on an SD card; recent history is enough for a restart post-mortem.
- **Stateless supervisor.** `tick` is a one-shot cron job reading `state.json`; no daemon to supervise the supervisor.
- **Dashboard control is off unless `AGENT_FLEET_TOKEN` is set, and binds localhost.** Start/stop/restart/logs buttons; POSTs need the token (constant-time compare), unknown agents 404. Plain HTTP, so use a TLS reverse proxy for remote access; token is a shared secret, not per-user auth.

## Limits (deliberate)

Single machine, single tenant. No per-agent CPU/memory limits (cgroups/ulimit would be the next step). No auth, no secrets management, no isolation between agents (they share a Unix user). Hosting agents for other people would need all three; that is roadmap, not built.

## Origin

Generalised from a live Raspberry Pi fleet watchdog. The Pi-specific parts (webhook ingress, nginx routing, backups, reboot diagnostics) are written up in [docs/case-study.md](docs/case-study.md) instead of built into the tool.

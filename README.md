# agent-fleet

Supervisor for long-running agent processes (e.g. Claude Code bots) in tmux. Stdlib only, Python 3.11+, one file.

- `agents.toml` declares agents (command, cwd, stuck-screen markers)
- `agentctl tick` (cron, every minute): start dead agents; restart frozen ones (screen unchanged since last tick AND a stuck marker visible, so idle or busy agents are never killed)
- `agentctl send/inbox`: file-based mailbox between agents (jsonl + flock), replaces injecting keys into another agent's terminal
- `agentctl serve`: read-only dashboard (`/`, `/status.json`), binds 127.0.0.1
- `agentctl ls|start|stop|restart|logs <agent>`

```
cp agents.example.toml agents.toml
python3 agentctl.py tick && python3 agentctl.py ls
python3 test_agentctl.py
```

Extracted from a real Raspberry Pi fleet watchdog. Single-tenant by design; multi-tenant hosting (isolation, secrets) is roadmap, not built.

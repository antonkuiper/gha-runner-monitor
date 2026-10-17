# gha-runner-monitor

A single-file, dependency-free live dashboard for a machine that runs
**self-hosted GitHub Actions runners**. It fuses two views into one browser page:

- **System & GPU load** — per-core CPU, memory, network, disk I/O, load average,
  and (if present) NVIDIA GPU utilization / VRAM / temperature. The btop/nvtop part.
- **Runner activity** — a tile per runner slot (idle / **busy** / offline), the jobs
  running *right now* with live elapsed time, today's job count with pass/fail, the
  build-minutes run on your own hardware, and a jobs-per-hour sparkline.

It is one Python file using only the standard library — no `pip install`, no build
step, no database. The web page (HTML/CSS/JS) is inlined in the same file.

## Requirements

- **Python 3.8+** (standard library only)
- **Linux** — system metrics are read from `/proc` (`/proc/stat`, `/proc/meminfo`,
  `/proc/net/dev`, `/proc/diskstats`, `/proc/loadavg`)
- *Optional:* `nvidia-smi` on `PATH` — the GPU panel appears only when it's available
- *Optional:* the [`gh` CLI](https://cli.github.com/), authenticated — needed only for
  the runner/job panels

## Quick start

```bash
# pure system + GPU monitor (no GitHub needed)
python3 dashboard.py

# light up the runner panels
ORG=my-org REPOS="my-org/repo-a,my-org/repo-b" python3 dashboard.py
```

Then open **http://localhost:8787**. `Ctrl-C` to stop.

Without `ORG`/`REPOS` it runs as a plain system/GPU monitor. Set them to pull runner
and job data via `gh` (so you must be signed in with `gh auth login`, with a token that
can read the org's runners and the repos' Actions runs).

## Configuration

All configuration is via environment variables:

| Variable | Default | Meaning |
|----------|---------|---------|
| `PORT` | `8787` | Port to serve on |
| `HOST` | `127.0.0.1` | Bind address (keep it local unless you know what you're doing) |
| `ORG` | *(unset)* | GitHub org whose self-hosted runners to show |
| `REPOS` | *(unset)* | Comma-separated `owner/repo` list to count jobs for |
| `RUNNERS_TTL` | `15` | Seconds between runner-status polls |
| `RUNS_TTL` | `45` | Seconds between workflow-run polls |

### Why two refresh cadences

System and GPU stats are read locally and cheaply, so the page refreshes them every
1.5 s. GitHub data goes through the `gh` CLI, so it's **cached** and refreshed at most
every `RUNNERS_TTL` / `RUNS_TTL` seconds. This keeps the dashboard lively without
burning through your GitHub API rate limit, no matter how many browser tabs watch it.

## Run it as a service (systemd user unit)

```ini
# ~/.config/systemd/user/gha-runner-monitor.service
[Unit]
Description=GitHub Actions runner monitor
After=network-online.target

[Service]
ExecStart=/usr/bin/python3 /path/to/gha-runner-monitor/dashboard.py
Restart=on-failure
RestartSec=3
Environment=PORT=8787
Environment=ORG=my-org
Environment=REPOS=my-org/repo-a,my-org/repo-b

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now gha-runner-monitor
# to survive reboots without logging in:  sudo loginctl enable-linger "$USER"
```

A ready-to-edit copy lives at [`contrib/gha-runner-monitor.service`](contrib/gha-runner-monitor.service).

## Security notes

- Binds to `127.0.0.1` by default — it is not exposed to your network.
- Contains **no credentials**. GitHub access is delegated entirely to your local `gh`
  CLI session.
- Runs read-only: it only *reads* metrics and the GitHub API; it never mutates anything.

## License

[MIT](LICENSE)

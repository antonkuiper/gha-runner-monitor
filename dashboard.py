#!/usr/bin/env python3
"""Home-runner productivity dashboard.

A single-file, stdlib-only local web GUI that fuses live system/GPU load
(btop/nvtop style) with GitHub self-hosted runner job activity.

Data sources, all without sudo:
  - CPU / memory / network : /proc/stat, /proc/meminfo, /proc/net/dev
  - GPU                    : nvidia-smi (if present)
  - Runner slots & jobs    : gh api (cached, slow cadence to spare the budget)

Usage:
  python3 dashboard.py                       # system + GPU monitor on :8787
  ORG=my-org REPOS="my-org/repo-a,my-org/repo-b" python3 dashboard.py

Without ORG/REPOS it runs as a pure system/GPU monitor; set them to light up
the runner-slot and job panels (requires the `gh` CLI, authenticated).
Open http://localhost:8787 in a browser. Ctrl-C to stop.
"""
import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---- config -----------------------------------------------------------------
PORT = int(os.environ.get("PORT", "8787"))
HOST = os.environ.get("HOST", "127.0.0.1")
ORG = os.environ.get("ORG", "").strip()
REPOS = [r.strip() for r in os.environ.get("REPOS", "").split(",") if r.strip()]
RUNNERS_TTL = float(os.environ.get("RUNNERS_TTL", "15"))   # seconds between runner polls
RUNS_TTL = float(os.environ.get("RUNS_TTL", "45"))         # seconds between run polls
# minutes a job would cost on GitHub-hosted runners is just its wall-clock here.

# ---- shared state -----------------------------------------------------------
_lock = threading.Lock()
_prev_cpu = {}          # cpu name -> (total, idle)
_prev_net = None        # (ts, rx_bytes, tx_bytes)
_prev_disk = None       # (ts, read_sectors, write_sectors)
_gh_cache = {"runners": None, "runners_ts": 0.0, "runs": None, "runs_ts": 0.0}


# ---- helpers ----------------------------------------------------------------
def _run(cmd, timeout=15):
    """Run a command, return stdout text or None on any failure."""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if out.returncode != 0:
            return None
        return out.stdout
    except Exception:
        return None


def read_cpu():
    """Per-core and overall CPU busy %, computed as delta since last call."""
    try:
        with open("/proc/stat") as fh:
            lines = fh.readlines()
    except Exception:
        return {"overall": 0.0, "cores": []}
    overall = 0.0
    cores = []
    with _lock:
        for ln in lines:
            if not ln.startswith("cpu"):
                continue
            parts = ln.split()
            name = parts[0]
            vals = [int(x) for x in parts[1:]]
            if len(vals) < 5:
                continue
            idle = vals[3] + vals[4]  # idle + iowait
            total = sum(vals)
            prev = _prev_cpu.get(name)
            _prev_cpu[name] = (total, idle)
            busy = 0.0
            if prev:
                dt = total - prev[0]
                di = idle - prev[1]
                if dt > 0:
                    busy = max(0.0, min(100.0, 100.0 * (dt - di) / dt))
            if name == "cpu":
                overall = busy
            else:
                cores.append(round(busy, 1))
    return {"overall": round(overall, 1), "cores": cores}


def read_mem():
    info = {}
    try:
        with open("/proc/meminfo") as fh:
            for ln in fh:
                k, _, v = ln.partition(":")
                info[k] = int(v.strip().split()[0])  # kB
    except Exception:
        return {"used": 0, "total": 0, "pct": 0.0}
    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable", info.get("MemFree", 0))
    used = max(0, total - avail)
    pct = round(100.0 * used / total, 1) if total else 0.0
    return {"used_mb": round(used / 1024), "total_mb": round(total / 1024), "pct": pct}


def read_net():
    """Aggregate rx/tx byte rate across physical interfaces."""
    global _prev_net
    rx = tx = 0
    try:
        with open("/proc/net/dev") as fh:
            for ln in fh.readlines()[2:]:
                iface, _, rest = ln.partition(":")
                iface = iface.strip()
                if iface in ("lo",) or iface.startswith(("docker", "veth", "br-")):
                    continue
                f = rest.split()
                if len(f) >= 9:
                    rx += int(f[0])
                    tx += int(f[8])
    except Exception:
        return {"rx_bps": 0, "tx_bps": 0}
    now = time.time()
    with _lock:
        prev = _prev_net
        _prev_net = (now, rx, tx)
    if not prev:
        return {"rx_bps": 0, "tx_bps": 0}
    dt = now - prev[0]
    if dt <= 0:
        return {"rx_bps": 0, "tx_bps": 0}
    return {"rx_bps": max(0, int((rx - prev[1]) / dt)),
            "tx_bps": max(0, int((tx - prev[2]) / dt))}


def read_disk():
    """Aggregate read/write byte rate across physical disks (sectors * 512)."""
    global _prev_disk
    rd = wr = 0
    try:
        with open("/proc/diskstats") as fh:
            for ln in fh:
                p = ln.split()
                if len(p) < 10:
                    continue
                if not re.match(r"^(sd[a-z]+|nvme\d+n\d+|vd[a-z]+|mmcblk\d+)$", p[2]):
                    continue
                rd += int(p[5])
                wr += int(p[9])
    except Exception:
        return {"read_bps": 0, "write_bps": 0}
    now = time.time()
    with _lock:
        prev = _prev_disk
        _prev_disk = (now, rd, wr)
    if not prev:
        return {"read_bps": 0, "write_bps": 0}
    dt = now - prev[0]
    if dt <= 0:
        return {"read_bps": 0, "write_bps": 0}
    return {"read_bps": max(0, int((rd - prev[1]) * 512 / dt)),
            "write_bps": max(0, int((wr - prev[2]) * 512 / dt))}


def read_load():
    cores = os.cpu_count() or 1
    try:
        with open("/proc/loadavg") as fh:
            a = fh.read().split()
        return {"l1": float(a[0]), "l5": float(a[1]), "l15": float(a[2]), "cores": cores}
    except Exception:
        return {"l1": 0.0, "l5": 0.0, "l15": 0.0, "cores": cores}


def read_gpu():
    out = _run(["nvidia-smi",
                "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                "--format=csv,noheader,nounits"], timeout=5)
    if not out:
        return None
    gpus = []
    for ln in out.strip().splitlines():
        f = [x.strip() for x in ln.split(",")]
        if len(f) < 5:
            continue
        try:
            gpus.append({"name": f[0], "util": float(f[1]),
                         "mem_used": float(f[2]), "mem_total": float(f[3]),
                         "temp": float(f[4])})
        except ValueError:
            continue
    return gpus or None


def gh_json(path):
    out = _run(["gh", "api", path], timeout=20)
    if not out:
        return None
    try:
        return json.loads(out)
    except Exception:
        return None


def get_runners():
    if not ORG:
        return {"online": 0, "busy": 0, "list": []}
    now = time.time()
    with _lock:
        if _gh_cache["runners"] and now - _gh_cache["runners_ts"] < RUNNERS_TTL:
            return _gh_cache["runners"]
    data = gh_json(f"orgs/{ORG}/actions/runners?per_page=100")
    result = {"online": 0, "busy": 0, "list": []}
    if data and isinstance(data.get("runners"), list):
        for r in data["runners"]:
            online = r.get("status") == "online"
            busy = bool(r.get("busy"))
            result["list"].append({"name": r.get("name", "?"),
                                    "status": r.get("status", "?"), "busy": busy})
            if online:
                result["online"] += 1
            if busy:
                result["busy"] += 1
        result["list"].sort(key=lambda x: x["name"])
    with _lock:
        _gh_cache["runners"] = result
        _gh_cache["runners_ts"] = now
    return result


def _parse_iso(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def get_runs():
    now = time.time()
    with _lock:
        if _gh_cache["runs"] and now - _gh_cache["runs_ts"] < RUNS_TTL:
            return _gh_cache["runs"]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    in_progress, per_hour = [], [0] * 24
    count = success = failure = 0
    minutes = 0.0
    for repo in REPOS:
        path = f"repos/{repo}/actions/runs?per_page=100&created=%3E%3D{today}"
        data = gh_json(path)
        if not data or not isinstance(data.get("workflow_runs"), list):
            continue
        for run in data["workflow_runs"]:
            status = run.get("status")
            started = _parse_iso(run.get("run_started_at") or run.get("created_at") or "")
            if status in ("in_progress", "queued"):
                now_dt = datetime.now(timezone.utc)
                short = repo.split("/")[-1]
                run_id = run.get("id")
                # Expand into jobs so we can show WHERE each one runs: a run can
                # be "in progress" while its remaining jobs sit on GitHub-hosted
                # runners, which never touch your self-hosted slots.
                jobs_data = gh_json(
                    f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=50") if run_id else None
                active = [j for j in (jobs_data or {}).get("jobs", [])
                          if j.get("status") in ("in_progress", "queued")]
                if active:
                    for j in active:
                        st = _parse_iso(j.get("started_at") or "")
                        el = int((now_dt - st).total_seconds()) if st else 0
                        labels = j.get("labels", []) or []
                        home = ("self-hosted" in labels) or (j.get("runner_group_name") == "home")
                        in_progress.append({"name": run.get("name", "?"), "job": j.get("name", ""),
                                            "repo": short, "status": j.get("status", status),
                                            "elapsed": el, "branch": run.get("head_branch", ""),
                                            "home": home,
                                            "runner": j.get("runner_name") or ("hosted" if not home else "")})
                else:
                    elapsed = int((now_dt - started).total_seconds()) if started else 0
                    in_progress.append({"name": run.get("name", "?"), "job": "",
                                        "repo": short, "status": status, "elapsed": elapsed,
                                        "branch": run.get("head_branch", ""),
                                        "home": None, "runner": ""})
            created = _parse_iso(run.get("created_at") or "")
            if created:
                per_hour[created.hour] += 1
            if status == "completed":
                count += 1
                concl = run.get("conclusion")
                if concl == "success":
                    success += 1
                elif concl in ("failure", "timed_out", "cancelled"):
                    failure += 1
                updated = _parse_iso(run.get("updated_at") or "")
                if started and updated and updated > started:
                    minutes += (updated - started).total_seconds() / 60.0
    in_progress.sort(key=lambda x: -x["elapsed"])
    result = {"in_progress": in_progress, "per_hour": per_hour,
              "today": {"count": count, "success": success, "failure": failure,
                        "minutes": round(minutes, 1)}}
    with _lock:
        _gh_cache["runs"] = result
        _gh_cache["runs_ts"] = now
    return result


def collect():
    return {"ts": time.time(), "cpu": read_cpu(), "mem": read_mem(),
            "net": read_net(), "disk": read_disk(), "load": read_load(),
            "gpu": read_gpu(),
            "runners": get_runners(), "runs": get_runs(),
            "org": ORG, "repos": REPOS}


# ---- web --------------------------------------------------------------------
PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Home Runner</title>
<style>
  :root{
    --bg:#0d1117; --panel:#161b22; --line:#21262d; --fg:#e6edf3; --dim:#8b949e;
    --accent:#2f81f7; --ok:#3fb950; --busy:#d29922; --bad:#f85149; --gpu:#a371f7;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
    font:14px/1.4 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;padding:16px}
  h1{font-size:16px;margin:0 0 2px;letter-spacing:.5px}
  .sub{color:var(--dim);font-size:12px;margin-bottom:14px}
  .grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(280px,1fr))}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px}
  .panel h2{font-size:12px;text-transform:uppercase;letter-spacing:1px;
    color:var(--dim);margin:0 0 10px;font-weight:600}
  .row{display:flex;justify-content:space-between;align-items:center;margin:6px 0}
  .big{font-size:30px;font-weight:700;line-height:1}
  .bar{height:8px;border-radius:5px;background:var(--line);overflow:hidden;margin-top:4px}
  .bar>i{display:block;height:100%;border-radius:5px;transition:width .4s ease}
  .cores{display:grid;grid-template-columns:repeat(auto-fill,minmax(26px,1fr));gap:4px}
  .core{height:34px;background:var(--line);border-radius:3px;position:relative;overflow:hidden}
  .core>i{position:absolute;bottom:0;left:0;right:0;transition:height .4s ease}
  .slots{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:8px}
  .slot{border:1px solid var(--line);border-radius:7px;padding:8px}
  .slot .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
  .idle .dot{background:var(--dim)} .onl .dot{background:var(--ok)} .bz .dot{background:var(--busy);box-shadow:0 0 8px var(--busy)}
  .slot .nm{font-size:11px;color:var(--dim);word-break:break-all;margin-top:4px}
  .job{border-left:3px solid var(--busy);padding:4px 0 4px 8px;margin:6px 0}
  .job .t{font-size:11px;color:var(--dim)}
  .stat{display:flex;gap:16px;flex-wrap:wrap}
  .stat .k{color:var(--dim);font-size:11px;text-transform:uppercase}
  .stat .v{font-size:22px;font-weight:700}
  .v.ok{color:var(--ok)} .v.bad{color:var(--bad)}
  canvas{width:100%;height:46px;display:block}
  .pulse{animation:p 1.4s infinite} @keyframes p{50%{opacity:.4}}
  .off{opacity:.5} .foot{color:var(--dim);font-size:11px;margin-top:12px}
  @media (prefers-color-scheme:light){:root:not([data-theme]){
    --bg:#f6f8fa;--panel:#fff;--line:#d0d7de;--fg:#1f2328;--dim:#656d76}}
</style></head>
<body>
  <h1>⚙ HOME RUNNER <span id="pool" class="sub"></span></h1>
  <div class="sub" id="head">connecting…</div>
  <div class="grid">
    <div class="panel"><h2>Runner slots <span id="busycount"></span></h2>
      <div class="slots" id="slots"></div>
      <div class="foot" id="runnerfoot"></div></div>

    <div class="panel"><h2>Today</h2>
      <div class="stat">
        <div><div class="k">jobs</div><div class="v" id="jobs">–</div></div>
        <div><div class="k">ok</div><div class="v ok" id="ok">–</div></div>
        <div><div class="k">failed</div><div class="v bad" id="fail">–</div></div>
        <div><div class="k">min saved</div><div class="v" id="min">–</div></div>
      </div>
      <h2 style="margin-top:14px">Jobs / hour (UTC)</h2>
      <canvas id="spark" width="600" height="46"></canvas></div>

    <div class="panel"><h2>Running now</h2>
      <div id="running"><div class="sub">idle — nothing building</div></div>
      <div class="foot"><span style="color:var(--ok)">●</span> home runner &nbsp; ☁ GitHub-hosted (doesn't use your slots)</div></div>

    <div class="panel"><h2>CPU</h2>
      <div class="row"><span>overall</span><span class="big" id="cpupct">–</span></div>
      <div class="cores" id="cores"></div></div>

    <div class="panel"><h2>Memory &amp; Network</h2>
      <div class="row"><span id="memtxt">memory</span><span id="mempct">–</span></div>
      <div class="bar"><i id="membar" style="width:0;background:var(--accent)"></i></div>
      <div class="row" style="margin-top:12px"><span>net ↓</span><span id="rx">–</span></div>
      <div class="row"><span>net ↑</span><span id="tx">–</span></div></div>

    <div class="panel"><h2>Load &amp; Disk I/O</h2>
      <div class="row"><span id="loadtxt">load avg (1·5·15)</span><span id="load">–</span></div>
      <div class="bar"><i id="loadbar" style="width:0;background:var(--accent)"></i></div>
      <div class="row" style="margin-top:12px"><span>disk read</span><span id="dr">–</span></div>
      <div class="bar"><i id="drbar" style="width:0;background:var(--ok)"></i></div>
      <div class="row" style="margin-top:8px"><span>disk write</span><span id="dw">–</span></div>
      <div class="bar"><i id="dwbar" style="width:0;background:var(--busy)"></i></div></div>

    <div class="panel" id="gpupanel"><h2>GPU</h2><div id="gpus"></div></div>
  </div>
  <div class="foot" id="foot"></div>

<script>
const $=id=>document.getElementById(id);
function col(p){return p<50?'var(--ok)':p<85?'var(--busy)':'var(--bad)';}
function human(b){b*=1;const u=['B','K','M','G'];let i=0;while(b>=1024&&i<3){b/=1024;i++;}return b.toFixed(b<10&&i?1:0)+u[i]+'/s';}
function dur(s){s|=0;const m=(s/60)|0;return m?m+'m'+String(s%60).padStart(2,'0')+'s':s+'s';}

function drawSpark(data){
  const c=$('spark'),x=c.getContext('2d'),W=c.width,H=c.height;
  x.clearRect(0,0,W,H);const n=data.length,max=Math.max(1,...data),bw=W/n;
  for(let i=0;i<n;i++){const h=data[i]/max*(H-4);
    x.fillStyle=data[i]?'var(--accent)':'#30363d';
    x.fillStyle=data[i]?getComputedStyle(document.body).getPropertyValue('--accent'):'#30363d';
    x.fillRect(i*bw+1,H-h,bw-2,h||1);}
}

async function tick(){
  let d;try{d=await (await fetch('/api/stats')).json();}catch(e){$('head').textContent='server unreachable';return;}
  $('pool').textContent=d.org?('· '+d.org+' · '+d.repos.join(', ')):'· system monitor';
  const rn=d.runners;
  const t0=new Date(d.ts*1000).toLocaleTimeString();
  $('head').textContent=d.org?(rn.busy+' of '+rn.online+' runners busy · '+t0)
    :('system & GPU monitor · '+t0);
  $('busycount').textContent='('+rn.busy+'/'+rn.online+')';
  // slots
  $('slots').innerHTML=rn.list.map(s=>{
    const cls=s.busy?'bz':(s.status==='online'?'onl':'idle');
    const lbl=s.busy?'BUSY':(s.status==='online'?'idle':s.status);
    return `<div class="slot ${cls}"><span class="dot"></span><b>${lbl}</b><div class="nm">${s.name}</div></div>`;}).join('')
    || '<div class="sub">'+(d.org?'no runners registered':'set ORG + REPOS env to track runners')+'</div>';
  $('runnerfoot').textContent=(d.org&&!rn.online)?'(cannot reach gh api — check `gh auth status`)':'';
  // today
  const t=d.runs.today;
  $('jobs').textContent=t.count;$('ok').textContent=t.success;$('fail').textContent=t.failure;
  $('min').textContent=t.minutes;
  drawSpark(d.runs.per_hour);
  // running
  const ip=d.runs.in_progress;
  $('running').innerHTML=ip.length?ip.map(j=>{
    const loc = j.home===true
      ? `<span style="color:var(--ok)">● ${j.runner||'home'}</span>`
      : j.home===false
        ? `<span style="color:var(--dim)">☁ ${j.runner||'hosted'}</span>` : '';
    const jn = j.job ? (' · '+j.job) : '';
    const bc = j.home===true ? 'var(--ok)' : j.home===false ? 'var(--dim)' : 'var(--busy)';
    return `<div class="job pulse" style="border-left-color:${bc}">
       <b>${j.repo}</b> · ${j.name}${jn} <span class="t">${j.branch}</span>
       <div class="t">${j.status} · ${dur(j.elapsed)}${loc?(' · '+loc):''}</div></div>`;}).join('')
    :'<div class="sub">idle — nothing building</div>';
  // cpu
  $('cpupct').textContent=d.cpu.overall.toFixed(0)+'%';
  $('cpupct').style.color=col(d.cpu.overall);
  $('cores').innerHTML=d.cpu.cores.map(p=>
    `<div class="core" title="${p}%"><i style="height:${p}%;background:${col(p)}"></i></div>`).join('');
  // mem
  const m=d.mem;$('memtxt').textContent=`memory ${(m.used_mb/1024).toFixed(1)}/${(m.total_mb/1024).toFixed(1)} GB`;
  $('mempct').textContent=m.pct+'%';$('membar').style.width=m.pct+'%';$('membar').style.background=col(m.pct);
  $('rx').textContent=human(d.net.rx_bps);$('tx').textContent=human(d.net.tx_bps);
  // load & disk
  const L=d.load;
  $('load').textContent=L.l1.toFixed(2)+' · '+L.l5.toFixed(2)+' · '+L.l15.toFixed(2);
  const lp=Math.min(100,100*L.l1/L.cores);
  $('loadbar').style.width=lp+'%';$('loadbar').style.background=col(lp);
  // disk bars scale to a rolling 200 MB/s ceiling so movement is visible
  const cap=200*1048576;
  $('dr').textContent=human(d.disk.read_bps);$('dw').textContent=human(d.disk.write_bps);
  $('drbar').style.width=Math.min(100,100*d.disk.read_bps/cap)+'%';
  $('dwbar').style.width=Math.min(100,100*d.disk.write_bps/cap)+'%';
  // gpu
  if(d.gpu){$('gpupanel').style.display='';
    $('gpus').innerHTML=d.gpu.map(g=>{const mp=g.mem_total?100*g.mem_used/g.mem_total:0;return`
      <div class="row"><span>${g.name}</span><span>${g.temp}°C</span></div>
      <div class="row"><span>util</span><span>${g.util}%</span></div>
      <div class="bar"><i style="width:${g.util}%;background:var(--gpu)"></i></div>
      <div class="row" style="margin-top:8px"><span>vram ${(g.mem_used/1024).toFixed(1)}/${(g.mem_total/1024).toFixed(1)} GB</span><span>${mp.toFixed(0)}%</span></div>
      <div class="bar"><i style="width:${mp}%;background:var(--gpu)"></i></div>`;}).join('');
  }else{$('gpupanel').style.display='none';}
  $('foot').textContent='refresh 1.5s · gh cached · :'+location.port;
}
tick();setInterval(tick,1500);
</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # quiet

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/stats"):
            try:
                body = json.dumps(collect()).encode()
            except Exception as e:
                body = json.dumps({"error": str(e)}).encode()
            self._send(200, body, "application/json")
        elif self.path in ("/", "/index.html"):
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain")


def main():
    # prime the delta-based readers so the first paint has data
    read_cpu()
    read_net()
    read_disk()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Home-runner dashboard: http://{HOST}:{PORT}  (org={ORG} repos={','.join(REPOS)})")
    print("Ctrl-C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()

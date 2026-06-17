#!/usr/bin/env python3
"""
Infinite Subagent — Fleet MCP Server.

Deploy this single file on every machine you want your local Claude Code
(or any MCP client) to command as a subagent. At startup it auto-detects
which AI tools (``claude`` / ``codex``) are installed and exposes them —
plus a set of core system tools — over MCP via stdio.

It is designed to sit behind an SSH-tunneled MCP client (see the README),
so no port has to be opened: your local client runs
``ssh <host> python3 -u server.py`` and speaks JSON-RPC over the SSH stdio
channel.

Only one thing is host-specific: the *optional* credentials file
(``fleet.env``) used for headless Claude Code auth. Its location is
resolved via the ``FLEET_ENV_FILE`` env var, then a short list of default
paths — see ``_find_fleet_env()``.

Requirements: Python 3.10+ and ``pip install mcp``.
"""

import json
import os
import sys
import subprocess
import shutil
import platform
import time
import shlex
from pathlib import Path
from typing import Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
import mcp.types as types

# ── Logging (stderr only — stdout is the MCP transport) ──────────────
HOSTNAME = platform.node()
LOG_FILE = f"/tmp/infinite-subagent-{HOSTNAME}.log"
sys.stderr = open(LOG_FILE, "a", buffering=1)
HOME = os.path.expanduser("~")


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


# ── Capability detection (runs once at startup) ─────────────────────
CLAUDE = shutil.which("claude") is not None
CODEX = shutil.which("codex") is not None
DOCKER = shutil.which("docker") is not None

log(f"STARTUP: host={HOSTNAME} claude={CLAUDE} codex={CODEX} docker={DOCKER}")


# ══════════════════════════════════════════════════════════════════════
# Tool implementations
# ══════════════════════════════════════════════════════════════════════

def _run(cmd: str, timeout: int = 30) -> dict:
    """Execute a shell command, return {stdout, stderr, exit_code}."""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           timeout=timeout, executable="/bin/bash")
        return {"stdout": r.stdout[-50000:], "stderr": r.stderr[-10000:],
                "exit_code": r.returncode}
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": f"Timeout after {timeout}s", "exit_code": -1}
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "exit_code": -1}


def tool_system_info() -> dict:
    mem = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if "MemTotal" in line:
                    mem["total_kb"] = int(line.split()[1])
                elif "MemAvailable" in line:
                    mem["available_kb"] = int(line.split()[1])
    except Exception:
        mem = {}

    disk = {}
    try:
        st = os.statvfs("/")
        disk["total_gb"] = round(st.f_frsize * st.f_blocks / (1024**3), 1)
        disk["free_gb"] = round(st.f_frsize * st.f_bavail / (1024**3), 1)
        disk["used_pct"] = round((1 - st.f_bavail / st.f_blocks) * 100, 1)
    except Exception:
        disk = {}

    uptime = ""
    try:
        with open("/proc/uptime") as f:
            secs = float(f.read().split()[0])
            d, r = divmod(int(secs), 86400)
            h, r = divmod(r, 3600)
            m, _ = divmod(r, 60)
            uptime = f"{d}d {h}h {m}m"
    except Exception:
        uptime = "unknown"

    os_name = f"{platform.system()} {platform.release()}"
    try:
        for fp in ["/etc/os-release", "/usr/lib/os-release"]:
            if os.path.exists(fp):
                with open(fp) as f:
                    for line in f:
                        if line.startswith("PRETTY_NAME="):
                            os_name = line.split("=", 1)[1].strip().strip('"')
    except Exception:
        pass

    return {
        "hostname": HOSTNAME,
        "arch": platform.machine(),
        "os": os_name,
        "cpu_count": os.cpu_count() or 0,
        "memory": mem,
        "disk_root": disk,
        "uptime": uptime,
        "claude_available": CLAUDE,
        "codex_available": CODEX,
        "docker_available": DOCKER,
    }


def tool_run_command(command: str, timeout: int = 30) -> dict:
    log(f"EXEC: {command[:120]}")
    return _run(command, timeout)


def tool_list_processes(filter_name: str = "") -> dict:
    cmd = "ps aux --sort=-%mem | head -50"
    if filter_name:
        cmd = f"ps aux | grep -i '{filter_name}' | grep -v grep"
    r = _run(cmd, 10)
    procs = []
    for line in r["stdout"].strip().split("\n"):
        parts = line.split()
        if len(parts) >= 11:
            try:
                procs.append({"user": parts[0], "pid": int(parts[1]),
                              "cpu": float(parts[2]), "mem": float(parts[3]),
                              "command": " ".join(parts[10:])})
            except ValueError:
                pass
    return {"processes": procs[:50], "count": len(procs)}


def tool_read_file(path: str, lines: int = 50, offset: int = 0) -> dict:
    try:
        p = Path(path).resolve()
        if p.stat().st_size > 10 * 1024 * 1024:
            return {"error": f"File too large ({p.stat().st_size} bytes)"}
        with open(p) as f:
            all_lines = f.readlines()
        return {"path": str(p), "total_lines": len(all_lines),
                "content": "".join(all_lines[offset:offset + lines])}
    except FileNotFoundError:
        return {"error": f"Not found: {path}"}
    except PermissionError:
        return {"error": f"Permission denied: {path}"}
    except Exception as e:
        return {"error": str(e)}


def tool_write_file(path: str, content: str) -> dict:
    safe = ["/tmp/", "/var/tmp/", "/home/", "/root/",
            "/etc/nginx/", "/etc/systemd/", "/usr/local/", "/opt/"]
    p = Path(path).resolve()
    if not any(str(p).startswith(pr) for pr in safe):
        return {"error": f"Path not in allowed dirs: {p}"}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return {"path": str(p), "bytes_written": len(content), "success": True}
    except Exception as e:
        return {"error": str(e)}


def tool_check_service(name: str) -> dict:
    active = _run(f"systemctl is-active {name}", 10)["stdout"].strip()
    enabled = _run(f"systemctl is-enabled {name}", 10)["stdout"].strip()
    return {"service": name, "active": active, "enabled": enabled}


def tool_restart_service(name: str) -> dict:
    r = _run(f"sudo systemctl restart {name}", 30)
    return {"service": name, "success": r["exit_code"] == 0,
            "stdout": r["stdout"], "stderr": r["stderr"]}


def tool_docker_status() -> dict:
    if not DOCKER:
        return {"error": "Docker not installed"}
    r = _run("docker ps -a --format '{{json .}}'", 10)
    containers = []
    for line in r["stdout"].strip().split("\n"):
        if line:
            try:
                containers.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return {"containers": containers, "count": len(containers)}


# ── Headless AI-tool auth ────────────────────────────────────────────

def _load_env_file(path: str) -> dict:
    """Load KEY=VALUE pairs from an env file, ignoring comments and blanks."""
    env = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    except Exception:
        pass
    return env


def _find_fleet_env() -> Optional[str]:
    """Resolve the optional credentials file for headless Claude Code auth.

    Search order:
      1. ``$FLEET_ENV_FILE`` (explicit override)
      2. ``~/.claude/fleet.env``
      3. ``/etc/fleet/fleet.env``
      4. ``./fleet.env`` (next to this script)

    Returns the first path that exists, or ``None``. When ``None``, the
    Claude Code tool falls back to whatever auth the host already has.
    """
    candidates = []
    if os.environ.get("FLEET_ENV_FILE"):
        candidates.append(os.environ["FLEET_ENV_FILE"])
    candidates += [
        os.path.join(HOME, ".claude", "fleet.env"),
        "/etc/fleet/fleet.env",
        os.path.join(os.getcwd(), "fleet.env"),
    ]
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    return None


def tool_claude_analyze(prompt: str, workdir: Optional[str] = None) -> dict:
    """Run Claude Code headlessly on this host and return its output."""
    if not CLAUDE:
        return {"error": "Claude Code not on this machine"}
    workdir = workdir or HOME
    try:
        log(f"CLAUDE: {prompt[:100]}")
        env = os.environ.copy()
        env_file = _find_fleet_env()
        if env_file:
            env.update(_load_env_file(env_file))
            log(f"loaded creds from {env_file}")
        # stdin=DEVNULL avoids the "no stdin data received" warning when
        # spawned from a non-interactive SSH session.
        r = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "text"],
            capture_output=True, text=True, timeout=300, cwd=workdir, env=env,
            stdin=subprocess.DEVNULL)
        return {"host": HOSTNAME, "exit_code": r.returncode,
                "output": r.stdout[-100000:], "stderr": r.stderr[-5000:]}
    except subprocess.TimeoutExpired:
        return {"error": "Claude Code timed out (300s)"}
    except Exception as e:
        return {"error": str(e)}


def tool_codex_execute(task: str, workdir: Optional[str] = None) -> dict:
    """Run a Codex task headlessly on this host and return its output.

    Uses ``codex exec`` (non-interactive) with two flags required for an
    unattended fleet node: ``--skip-git-repo-check`` and
    ``--dangerously-bypass-approvals-and-sandbox`` (the fleet node is
    trusted and needs full access, mirroring the Claude Code tool).
    """
    if not CODEX:
        return {"error": "Codex not on this machine"}
    workdir = workdir or HOME
    try:
        log(f"CODEX: {task[:100]}")
        r = subprocess.run(
            ["codex", "exec", "--skip-git-repo-check",
             "--dangerously-bypass-approvals-and-sandbox", task],
            capture_output=True, text=True, timeout=300, cwd=workdir,
            stdin=subprocess.DEVNULL)
        return {"host": HOSTNAME, "exit_code": r.returncode,
                "output": r.stdout[-100000:], "stderr": r.stderr[-5000:]}
    except subprocess.TimeoutExpired:
        return {"error": "Codex timed out (300s)"}
    except Exception as e:
        return {"error": str(e)}


# ══════════════════════════════════════════════════════════════════════
# Async task model — fire-and-forget background agent jobs.
# Solves two pain points of the synchronous claude_analyze/codex_execute:
#   1) the caller is no longer blocked for up to 300s (task_start returns a
#      task_id at once, so the host's MCP client is free to do other work);
#   2) results land on disk under TASK_DIR, so a dropped SSH/MCP connection
#      can still recover them later via task_result/task_list — long jobs
#      survive reconnects instead of being lost mid-run.
# ══════════════════════════════════════════════════════════════════════
TASK_DIR = "/tmp/infinite-subagent-tasks"


def _agent_cmd(agent, prompt):
    """Return (base_command_string, extra_env_dict) for the given agent."""
    agent = (agent or "").lower()
    if agent == "claude":
        env = {}
        env_file = _find_fleet_env()
        if env_file:
            env.update(_load_env_file(env_file))
        return f"claude -p {shlex.quote(prompt)} --output-format text", env
    if agent == "codex":
        return (f"codex exec --skip-git-repo-check "
                f"--dangerously-bypass-approvals-and-sandbox {shlex.quote(prompt)}"), {}
    raise ValueError(f"Unknown agent: {agent!r} (expected 'claude' or 'codex')")


def _task_read_state(task_id):
    """Read full state for a task from its on-disk files (meta + exit_code + output)."""
    task_dir = Path(TASK_DIR) / task_id
    meta_path = task_dir / "meta.json"
    if not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text())
    except Exception:
        meta = {}
    pid = meta.get("pid")
    exit_code = None
    state = "unknown"
    exit_path = task_dir / "exit_code"
    out_path = task_dir / "out.log"
    if exit_path.exists():
        # exit_code file is the only trustworthy "done" signal (PID reuse
        # makes kill -0 unreliable).
        state = "done"
        try:
            exit_code = int(exit_path.read_text().strip())
            if exit_code != 0:
                state = "error"
        except Exception:
            exit_code = None
    elif pid:
        # process provably gone but no exit_code file -> killed/crashed
        if "yes" in _run(f"kill -0 {int(pid)} 2>/dev/null && echo yes", 5)["stdout"]:
            state = "running"
        else:
            state = "gone"
    output_tail = ""
    if out_path.exists():
        try:
            output_tail = out_path.read_text()[-4000:]
        except Exception:
            pass
    start_ts = meta.get("start_ts")
    elapsed = round(time.time() - start_ts, 1) if start_ts else None
    return {
        "task_id": task_id, "agent": meta.get("agent"),
        "prompt_head": meta.get("prompt_head"), "workdir": meta.get("workdir"),
        "start_ts": start_ts, "pid": pid, "state": state,
        "exit_code": exit_code, "elapsed": elapsed, "output_tail": output_tail,
    }


def tool_task_start(agent, prompt, workdir=None, timeout=1800):
    """Start a long agent task in the background; return immediately with task_id."""
    agent_lc = (agent or "").lower()
    if agent_lc == "claude" and not CLAUDE:
        return {"error": "Claude Code not on this machine"}
    if agent_lc == "codex" and not CODEX:
        return {"error": "Codex not on this machine"}
    try:
        base, extra_env = _agent_cmd(agent_lc, prompt)
    except ValueError as e:
        return {"error": str(e)}

    workdir = workdir or HOME

    task_id = f"{int(time.time())}-{os.urandom(3).hex()}"
    task_dir = Path(TASK_DIR) / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    out_path = task_dir / "out.log"
    exit_path = task_dir / "exit_code"
    meta_path = task_dir / "meta.json"

    # wrapper: cd, run agent, then record exit code to a file (the only
    # reliable completion signal — PID reuse makes kill -0 untrustworthy).
    wrapper = (f"cd {shlex.quote(workdir)} && {base} "
               f"> {shlex.quote(str(out_path))} 2>&1 </dev/null; "
               f"echo $? > {shlex.quote(str(exit_path))}")

    # Persist extra env into a 0600 file and source it at launch, so secrets
    # never appear in `ps` argv.
    env_path = task_dir / "env"
    env_path.write_text("\n".join(f"{k}={shlex.quote(v)}" for k, v in extra_env.items()) + "\n")
    env_path.chmod(0o600)

    # setsid makes the wrapper the leader of a new process group (PGID ==
    # its PID), so the watchdog can kill the whole group; </dev/null + 2>&1
    # detach stdio so _run returns immediately.
    daemon = (f"set -a; . {shlex.quote(str(env_path))}; set +a; "
              f"setsid bash -c {shlex.quote(wrapper)} </dev/null >/dev/null 2>&1 & echo $!")
    r = _run(daemon, 10)
    pid = None
    try:
        last = r["stdout"].strip().splitlines()[-1]
        pid = int(last) if last else None
    except (ValueError, IndexError):
        pid = None

    meta_path.write_text(json.dumps({
        "agent": agent_lc, "prompt_head": prompt[:200], "prompt_len": len(prompt),
        "workdir": workdir, "start_ts": int(time.time()), "pid": pid, "timeout": timeout,
    }, ensure_ascii=False))

    if pid and timeout and timeout > 0:
        _run(f'nohup bash -c "sleep {int(timeout)}; kill -- -{pid} 2>/dev/null" '
             f'</dev/null >/dev/null 2>&1 &', 5)

    log(f"TASK_START: {task_id} agent={agent_lc} pid={pid}")
    return {"task_id": task_id, "pid": pid, "status": "running",
            "agent": agent_lc, "workdir": workdir}


def tool_task_status(task_id):
    s = _task_read_state(task_id)
    if not s:
        return {"error": f"Task not found: {task_id}"}
    return {"task_id": s["task_id"], "state": s["state"], "exit_code": s["exit_code"],
            "elapsed": s["elapsed"], "pid": s["pid"], "output_tail": s["output_tail"][-2000:]}


def tool_task_result(task_id, max_bytes=100000):
    s = _task_read_state(task_id)
    if not s:
        return {"error": f"Task not found: {task_id}"}
    out_path = Path(TASK_DIR) / task_id / "out.log"
    output = ""
    if out_path.exists():
        try:
            raw = out_path.read_text()
            output = raw[-max_bytes:] if len(raw) > max_bytes else raw
        except Exception:
            output = ""
    return {"task_id": s["task_id"], "state": s["state"],
            "exit_code": s["exit_code"], "output": output}


def tool_task_list():
    base = Path(TASK_DIR)
    tasks = []
    if base.exists():
        for d in sorted(base.iterdir(), reverse=True):
            if not d.is_dir():
                continue
            s = _task_read_state(d.name)
            if s:
                tasks.append({
                    "task_id": s["task_id"], "agent": s["agent"], "state": s["state"],
                    "start_ts": s["start_ts"], "prompt_head": s["prompt_head"],
                    "elapsed": s["elapsed"],
                })
    return {"count": len(tasks), "tasks": tasks}


# ══════════════════════════════════════════════════════════════════════
# MCP Server
# ══════════════════════════════════════════════════════════════════════

server = Server(f"subagent-{HOSTNAME}")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    tools = [
        types.Tool(
            name="system_info",
            description=f"Get system info for {HOSTNAME}: CPU, memory, disk, OS, uptime, available AI tools",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="run_command",
            description=f"Execute a shell command on {HOSTNAME}",
            inputSchema={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute"},
                    "timeout": {"type": "integer", "description": "Timeout seconds (default 30)", "default": 30},
                },
                "required": ["command"],
            },
        ),
        types.Tool(
            name="list_processes",
            description=f"List running processes on {HOSTNAME}",
            inputSchema={
                "type": "object",
                "properties": {
                    "filter_name": {"type": "string", "description": "Optional: filter by process name"},
                },
                "required": [],
            },
        ),
        types.Tool(
            name="read_file",
            description=f"Read a file from {HOSTNAME} (max 10MB)",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute file path"},
                    "lines": {"type": "integer", "description": "Lines to read (default 50)", "default": 50},
                    "offset": {"type": "integer", "description": "Start line offset (default 0)", "default": 0},
                },
                "required": ["path"],
            },
        ),
        types.Tool(
            name="write_file",
            description=f"Write content to a file on {HOSTNAME} (safe paths only)",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute file path"},
                    "content": {"type": "string", "description": "Content to write"},
                },
                "required": ["path", "content"],
            },
        ),
        types.Tool(
            name="check_service",
            description=f"Check systemd service status on {HOSTNAME}",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Service name (e.g., nginx, ssh)"},
                },
                "required": ["name"],
            },
        ),
        types.Tool(
            name="restart_service",
            description=f"Restart a systemd service on {HOSTNAME}",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Service name to restart"},
                },
                "required": ["name"],
            },
        ),
        types.Tool(
            name="docker_status",
            description=f"Get Docker container statuses on {HOSTNAME}" if DOCKER else f"Check Docker (not installed on {HOSTNAME})",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="task_start",
            description=f"Start a long agent (claude/codex) task on {HOSTNAME} in the BACKGROUND; returns immediately with a task_id instead of blocking the caller for up to 300s. Output is captured to disk so it survives SSH/MCP disconnects. Poll with task_status, fetch with task_result. Use this (not claude_analyze/codex_execute) for any job that may run long.",
            inputSchema={
                "type": "object",
                "properties": {
                    "agent": {"type": "string", "enum": ["claude", "codex"], "description": "Which agent to run"},
                    "prompt": {"type": "string", "description": "Prompt/task to execute"},
                    "workdir": {"type": "string", "description": "Working directory (default: home dir)"},
                    "timeout": {"type": "integer", "description": "Hard timeout in seconds (default 1800); a watchdog kills the task's process group after this", "default": 1800},
                },
                "required": ["agent", "prompt"],
            },
        ),
        types.Tool(
            name="task_status",
            description=f"Poll the status of a background task on {HOSTNAME}. Returns state (running/done/error/gone), exit_code, elapsed seconds, and an output tail. Fast — safe to call in a loop.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Task ID returned by task_start"},
                },
                "required": ["task_id"],
            },
        ),
        types.Tool(
            name="task_result",
            description=f"Fetch the full output of a background task on {HOSTNAME} (from any session — survives SSH/MCP reconnect). Use after task_status reports done, or to read partial output mid-run.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Task ID returned by task_start"},
                    "max_bytes": {"type": "integer", "description": "Max bytes of output to return (default 100000)", "default": 100000},
                },
                "required": ["task_id"],
            },
        ),
        types.Tool(
            name="task_list",
            description=f"List all background tasks on {HOSTNAME}, including ones started in previous MCP sessions (for resume-after-reconnect).",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
    ]

    if CLAUDE:
        tools.append(types.Tool(
            name="claude_analyze",
            description=f"Run Claude Code analysis on {HOSTNAME}. CC has full filesystem access on this machine.",
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Prompt to send to Claude Code"},
                    "workdir": {"type": "string", "description": "Working directory (default: home dir)"},
                },
                "required": ["prompt"],
            },
        ))

    if CODEX:
        tools.append(types.Tool(
            name="codex_execute",
            description=f"Run a Codex task on {HOSTNAME}. Codex has full filesystem access on this machine.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "Task for Codex to execute"},
                    "workdir": {"type": "string", "description": "Working directory (default: home dir)"},
                },
                "required": ["task"],
            },
        ))

    return tools


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    handlers = {
        "system_info":     lambda: tool_system_info(),
        "run_command":     lambda: tool_run_command(arguments.get("command", ""), arguments.get("timeout", 30)),
        "list_processes":  lambda: tool_list_processes(arguments.get("filter_name", "")),
        "read_file":       lambda: tool_read_file(arguments.get("path", ""), arguments.get("lines", 50), arguments.get("offset", 0)),
        "write_file":      lambda: tool_write_file(arguments.get("path", ""), arguments.get("content", "")),
        "check_service":   lambda: tool_check_service(arguments.get("name", "")),
        "restart_service": lambda: tool_restart_service(arguments.get("name", "")),
        "docker_status":   lambda: tool_docker_status(),
        # workdir omitted on purpose -> None -> defaults to home dir
        "claude_analyze":  lambda: tool_claude_analyze(arguments.get("prompt", ""), arguments.get("workdir")),
        "codex_execute":   lambda: tool_codex_execute(arguments.get("task", ""), arguments.get("workdir")),
        "task_start":      lambda: tool_task_start(arguments.get("agent", ""), arguments.get("prompt", ""), arguments.get("workdir"), arguments.get("timeout", 1800)),
        "task_status":     lambda: tool_task_status(arguments.get("task_id", "")),
        "task_result":     lambda: tool_task_result(arguments.get("task_id", ""), arguments.get("max_bytes", 100000)),
        "task_list":       lambda: tool_task_list(),
    }

    handler = handlers.get(name)
    if not handler:
        return [types.TextContent(type="text", text=f"Unknown tool: {name}")]

    try:
        result = handler()
        text = json.dumps(result, indent=2, ensure_ascii=False) if isinstance(result, dict) else str(result)
    except Exception as e:
        log(f"TOOL ERROR {name}: {e}")
        text = json.dumps({"error": str(e)})

    return [types.TextContent(type="text", text=text)]


# ══════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════

async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    log(f"Starting infinite-subagent server on {HOSTNAME}")
    asyncio.run(main())

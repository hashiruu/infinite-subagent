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

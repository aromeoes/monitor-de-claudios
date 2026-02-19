#!/usr/bin/env python3
"""Ghostty Process Monitor — Sidebar TUI for monitoring Claude Code instances."""

import subprocess
import os
import re
import time
from datetime import datetime

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text
from rich.columns import Columns


def run_cmd(cmd: str) -> str:
    try:
        return subprocess.check_output(cmd, shell=True, stderr=subprocess.DEVNULL, text=True)
    except subprocess.CalledProcessError:
        return ""


def parse_etime(etime: str) -> int:
    """Parse ps etime string (dd-HH:MM:SS or HH:MM:SS or MM:SS) to total seconds."""
    etime = etime.strip()
    days = 0
    if "-" in etime:
        d, etime = etime.split("-", 1)
        days = int(d)
    parts = etime.split(":")
    parts = [int(p) for p in parts]
    if len(parts) == 3:
        h, m, s = parts
    elif len(parts) == 2:
        h, m, s = 0, parts[0], parts[1]
    else:
        h, m, s = 0, 0, parts[0]
    return days * 86400 + h * 3600 + m * 60 + s


def fmt_duration(secs: int) -> str:
    """Format seconds into human-readable compact duration."""
    if secs < 60:
        return f"{secs}s"
    elif secs < 3600:
        return f"{secs // 60}m"
    elif secs < 86400:
        h = secs // 3600
        m = (secs % 3600) // 60
        return f"{h}h {m}m" if m else f"{h}h"
    else:
        d = secs // 86400
        h = (secs % 86400) // 3600
        return f"{d}d {h}h" if h else f"{d}d"


def get_process_table():
    """Get full process table as list of dicts."""
    raw = run_cmd("ps -eo pid,ppid,etime,command")
    procs = []
    for line in raw.strip().splitlines()[1:]:
        parts = line.split()
        if len(parts) < 4:
            continue
        pid, ppid, etime = parts[0], parts[1], parts[2]
        cmd = " ".join(parts[3:])
        try:
            procs.append({
                "pid": int(pid),
                "ppid": int(ppid),
                "etime": etime,
                "cmd": cmd,
            })
        except ValueError:
            continue
    return procs


def find_ghostty_pid(procs):
    """Find the main Ghostty process."""
    for p in procs:
        if "Ghostty.app" in p["cmd"] and p["ppid"] == 1:
            return p["pid"]
    return None


def find_children(procs, ppid):
    """Find direct children of a PID."""
    return [p for p in procs if p["ppid"] == ppid]


def find_all_descendants(procs, pid):
    """Find all descendant processes (full subtree via BFS)."""
    descendants = []
    queue = [pid]
    while queue:
        parent = queue.pop(0)
        for p in procs:
            if p["ppid"] == parent:
                descendants.append(p)
                queue.append(p["pid"])
    return descendants


def get_cwds(pids):
    """Batch-fetch working directories via lsof."""
    if not pids:
        return {}
    pid_str = ",".join(str(p) for p in pids)
    raw = run_cmd(f"lsof -a -d cwd -p {pid_str}")
    cwds = {}
    for line in raw.strip().splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 9:
            try:
                pid = int(parts[1])
                path = parts[-1]
                cwds[pid] = path
            except (ValueError, IndexError):
                continue
    return cwds


def _is_mcp_process(cmd):
    """Check if a command is an MCP server or similar always-on process."""
    cmd_lower = cmd.lower()
    return "mcp" in cmd_lower or "@playwright" in cmd or "@supabase" in cmd


def _extract_shell_cmd(cmd):
    """Extract the user-facing command from a shell-snapshot command string."""
    match = re.search(r"eval '(.+?)'", cmd) or re.search(r'eval "(.+?)"', cmd)
    if match:
        result = match.group(1)
        if len(result) > 30:
            result = result[:27] + "..."
        return result
    return None


def detect_claude_status(procs, claude_proc):
    """Detect what Claude is doing based on its child processes.

    Uses caffeinate as the primary signal for whether Claude is actively working.
    Shell-snapshot processes represent Bash tool invocations.

    Returns (status, active_cmd, bg_procs) where:
    - status: "active" | "thinking" | "waiting"
    - active_cmd: command string for the active tool (if active)
    - bg_procs: list of background process description strings
    """
    direct_children = find_children(procs, claude_proc["pid"])

    has_caffeinate = False
    shell_snapshots = []
    other_children = []

    for child in direct_children:
        cmd = child["cmd"]
        if _is_mcp_process(cmd):
            continue
        if "caffeinate" in cmd.lower():
            has_caffeinate = True
            continue
        if "/bin/zsh -c" in cmd and "shell-snapshot" in cmd:
            shell_snapshots.append(child)
            continue
        other_children.append(child)

    def snapshot_label(proc):
        return _extract_shell_cmd(proc["cmd"]) or "..."

    def proc_label(proc):
        parts = proc["cmd"].split()
        return os.path.basename(parts[0]) if parts else proc["cmd"]

    # Collect background labels from non-snapshot, non-MCP children
    other_bg = [proc_label(c) for c in other_children]

    if shell_snapshots and has_caffeinate:
        # Caffeinate running = Claude is actively working.
        # The most recently started shell-snapshot is the foreground tool.
        # Older ones are background servers from earlier tool calls.
        shell_snapshots.sort(key=lambda s: parse_etime(s["etime"]))
        newest = shell_snapshots[0]  # smallest etime = most recent
        bg_snapshots = shell_snapshots[1:]

        active_cmd = f"$ {snapshot_label(newest)}"
        bg_procs = [snapshot_label(s) for s in bg_snapshots] + other_bg
        return "active", active_cmd, bg_procs

    if shell_snapshots and not has_caffeinate:
        # No caffeinate = Claude is at the prompt.
        # All shell-snapshots are leftover background servers.
        bg_procs = [snapshot_label(s) for s in shell_snapshots] + other_bg
        return "waiting", None, bg_procs

    if has_caffeinate:
        # Caffeinate but no shell-snapshots = Claude is thinking
        # (API call in progress, or using internal tools like Read/Edit/Grep)
        return "thinking", None, other_bg

    # Nothing running = waiting for user input
    return "waiting", None, other_bg


def build_tab_card(tab, width):
    """Build a Rich Panel for a single tab."""
    name = tab["name"]
    uptime = fmt_duration(tab["uptime_secs"])
    status = tab["status"]
    bash_cmd = tab.get("bash_cmd")
    claude_uptime = tab.get("claude_uptime")
    has_claude = tab.get("has_claude", False)

    lines = []

    # Uptime line
    lines.append(Text.assemble(("  ", ""), (uptime, "dim")))

    # Status line
    if status == "active":
        lines.append(Text.assemble(("  ", ""), ("ACTIVE", "bold green")))
    elif status == "thinking":
        lines.append(Text.assemble(("  ", ""), ("THINKING", "bold yellow")))
    elif status == "waiting":
        lines.append(Text.assemble(("  ", ""), ("WAITING", "bold blue")))
    else:
        lines.append(Text.assemble(("  ", ""), ("idle shell", "dim")))

    # Current command
    if bash_cmd:
        cmd_text = bash_cmd if len(bash_cmd) <= width - 4 else bash_cmd[: width - 7] + "..."
        lines.append(Text(f"  {cmd_text}", style="cyan"))

    # Background processes
    bg_procs = tab.get("bg_procs", [])
    if bg_procs:
        bg_text = ", ".join(bg_procs)
        max_len = width - 8  # "  bg: " prefix + padding
        if len(bg_text) > max_len:
            bg_text = bg_text[: max_len - 3] + "..."
        lines.append(Text(f"  bg: {bg_text}", style="dim cyan"))

    # Claude uptime
    if has_claude and claude_uptime:
        lines.append(Text.assemble(("  claude: ", "dim"), (claude_uptime, "magenta")))

    body = Text("\n").join(lines)

    # Title color
    if status == "active":
        border_style = "green"
    elif status == "thinking":
        border_style = "yellow"
    elif status == "waiting":
        border_style = "blue"
    else:
        border_style = "dim"

    return Panel(
        body,
        title=f" {name} ",
        title_align="left",
        border_style=border_style,
        width=width,
        padding=(0, 1),
    )


def gather_tabs():
    """Main data gathering: discover Ghostty tabs and their state."""
    procs = get_process_table()
    ghostty_pid = find_ghostty_pid(procs)
    if ghostty_pid is None:
        return []

    # Find login shells spawned by Ghostty
    logins = find_children(procs, ghostty_pid)
    logins = [p for p in logins if "login" in p["cmd"]]

    tabs = []
    shell_pids = []

    for login in logins:
        # Find the zsh shell under each login
        shells = find_children(procs, login["pid"])
        shells = [s for s in shells if s["cmd"].startswith("-") or "zsh" in s["cmd"] or "bash" in s["cmd"]]
        if not shells:
            continue

        shell = shells[0]
        shell_pids.append(shell["pid"])

        tab = {
            "login_pid": login["pid"],
            "shell_pid": shell["pid"],
            "uptime_secs": parse_etime(login["etime"]),
        }

        # Check for claude process
        shell_children = find_children(procs, shell["pid"])
        claude_procs = [c for c in shell_children if re.search(r'\bclaude\b', c["cmd"]) and "Claude.app" not in c["cmd"]]

        if claude_procs:
            claude = claude_procs[0]
            tab["has_claude"] = True
            tab["claude_uptime"] = fmt_duration(parse_etime(claude["etime"]))
            tab["_claude_proc"] = claude  # temp ref for status detection
        else:
            tab["has_claude"] = False
            tab["status"] = "idle"

        tabs.append(tab)

    # Determine status for each tab with a claude session
    for tab in tabs:
        if "_claude_proc" in tab:
            claude = tab.pop("_claude_proc")
            status, bash_cmd, bg_procs = detect_claude_status(procs, claude)
            tab["status"] = status
            tab["bash_cmd"] = bash_cmd
            tab["bg_procs"] = bg_procs

    # Batch-fetch cwds
    cwds = get_cwds(shell_pids)
    for tab in tabs:
        cwd = cwds.get(tab["shell_pid"], "")
        tab["cwd"] = cwd
        # Use last path component as name
        tab["name"] = os.path.basename(cwd) if cwd else f"pid:{tab['shell_pid']}"

    # Sort: active first, then thinking, then idle; within each group by uptime desc
    order = {"active": 0, "thinking": 1, "waiting": 2, "idle": 3}
    tabs.sort(key=lambda t: (order.get(t["status"], 9), -t["uptime_secs"]))

    return tabs


def render():
    """Render the full dashboard."""
    tabs = gather_tabs()
    width = 40

    parts = []
    for tab in tabs:
        parts.append(build_tab_card(tab, width))

    claude_count = sum(1 for t in tabs if t.get("has_claude"))
    footer = Text(
        f" {len(tabs)} tabs | {claude_count} claude | 2s",
        style="dim",
        justify="center",
    )
    parts.append(Text(""))
    parts.append(footer)

    group = Console().group if hasattr(Console, "group") else None
    from rich.console import Group
    return Group(*parts)


def main():
    console = Console()
    try:
        with Live(render(), console=console, refresh_per_second=0.5, screen=False) as live:
            while True:
                time.sleep(2)
                live.update(render())
    except KeyboardInterrupt:
        console.print("\n[dim]gmon stopped[/dim]")


if __name__ == "__main__":
    main()

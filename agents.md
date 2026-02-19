# Ghostty Monitor — Agent Reference

## Overview

A single-file Python TUI dashboard (`ghostty_monitor.py`) that monitors all open Ghostty terminal tabs and displays the real-time status of Claude Code sessions running inside them. Uses the `rich` library for rendering and polls the process tree every 2 seconds via `ps` and `lsof`.

## Architecture

```
main() → Live loop (2s)
  → render()
    → gather_tabs()
      → get_process_table()     # ps -eo pid,ppid,etime,command
      → find_ghostty_pid()      # locate Ghostty.app (ppid=1)
      → find_children()         # login → shell → claude hierarchy
      → detect_claude_status()  # classify based on caffeinate + shell-snapshot
      → get_cwds()              # lsof -a -d cwd -p <pids>
    → build_tab_card()          # Rich Panel per tab
    → footer                    # "N tabs | M claude | 2s"
```

## Process Tree

Each Ghostty tab creates this hierarchy:

```
Ghostty.app (ppid=1)
  └── login
        └── -zsh (login shell)
              └── claude (Claude Code CLI — Node.js process)
                    ├── caffeinate -i              (keep-alive, present while working)
                    ├── /bin/zsh -c ... shell-snapshot ... eval '<cmd>'   (Bash tool)
                    │     └── <actual command>
                    ├── npx @anthropic/mcp-...     (MCP servers, always-on)
                    └── node server.js             (background server, leftover)
```

## Status Detection

Status is determined by examining direct children of the `claude` process. Two key signals:

- **`caffeinate`**: Claude Code spawns `caffeinate -i` while actively working (API calls, tool execution) and kills it when returning to the prompt. This is the primary signal for "busy vs idle."
- **`shell-snapshot`**: The `/bin/zsh -c ... shell-snapshot ... eval '<cmd>'` pattern is how Claude's Bash tool executes commands. Its presence means a Bash tool is currently running.

### Decision Matrix

| caffeinate | shell-snapshot | Status       | Meaning                                         |
|------------|----------------|--------------|-------------------------------------------------|
| yes        | yes            | **ACTIVE**   | Running a Bash tool (command shown)             |
| no         | yes            | **WAITING**  | At prompt; shell-snapshots are background servers |
| yes        | no             | **THINKING** | API call or internal tools (Read/Edit/Grep)     |
| no         | no             | **WAITING**  | At prompt, nothing running                      |

### The Four Statuses

| Status         | Color  | Border | Needs user input? |
|----------------|--------|--------|--------------------|
| **ACTIVE**     | green  | green  | No — running a bash command |
| **THINKING**   | yellow | yellow | No — API call or internal tool |
| **WAITING**    | blue   | blue   | Yes — at prompt |
| **idle shell** | dim    | dim    | N/A — no Claude session in tab |

### Filtered (Ignored) Processes

These child processes are always-on and do not affect status:

- **MCP servers**: any command containing `mcp`, `@playwright`, or `@supabase`
- **`caffeinate`**: used as a signal only, not counted as tool activity

### Background Process Detection

When Claude starts a long-running server (e.g., `npm run dev` via `run_in_background`), the shell-snapshot wrapper stays alive as a child of `claude` even after Claude returns to the prompt.

Detection logic:
- If `caffeinate` is absent, all shell-snapshots are classified as **background** (Claude is done working)
- If `caffeinate` is present and multiple shell-snapshots exist, the **newest** one (smallest `etime`) is the active foreground tool; older ones are background
- Background processes are displayed as `bg: npm run dev` on the card without affecting the status

### What "idle shell" Means

A Ghostty tab has an active shell process (zsh/bash) but no `claude` process running in it. The user is at a regular terminal prompt. Closing a Ghostty tab kills that tab's shell — "idle shell" does not mean something is lingering after closing.

## Display

Each tab renders as a Rich Panel:

```
┌─ my-project ──────────────────┐
│  5m                           │   ← tab uptime (from login process)
│  ACTIVE                       │   ← status
│  $ npm test                   │   ← active command (cyan)
│  bg: npm run dev              │   ← background processes (dim cyan)
│  claude: 23m                  │   ← claude session uptime (magenta)
└───────────────────────────────┘
```

Tabs are sorted: active first, then thinking, then waiting, then idle. Within each group, longest-running tabs appear first.

## Key Functions

| Function | Purpose |
|---|---|
| `get_process_table()` | Parses `ps -eo pid,ppid,etime,command` into list of dicts |
| `find_ghostty_pid()` | Finds Ghostty.app with ppid=1 |
| `find_children()` | Direct children of a PID |
| `find_all_descendants()` | Full subtree via BFS |
| `get_cwds()` | Batch-fetches working directories via `lsof` |
| `_is_mcp_process()` | Checks if command is an MCP server |
| `_extract_shell_cmd()` | Extracts command from `eval '...'` in shell-snapshot |
| `detect_claude_status()` | Core detection — returns `(status, active_cmd, bg_procs)` |
| `build_tab_card()` | Builds a Rich Panel for one tab |
| `gather_tabs()` | Discovers all tabs and their state |
| `render()` | Assembles full dashboard |

## Design Decisions

- **Stateless polling**: Every 2-second cycle does a fresh process-tree walk. No state is persisted between cycles. Simple and avoids stale data.
- **Caffeinate over TCP**: Previously used `lsof` TCP connection checks to distinguish thinking from waiting. This was unreliable due to HTTP/2 persistent connections causing flickering. Caffeinate is a binary, stable signal.
- **Direct children only**: Status detection looks at direct children of the `claude` process (not the full subtree) for classification. Shell-snapshot and caffeinate are always direct children.
- **Tab naming**: Uses the basename of the shell's current working directory (via `lsof`). Falls back to `pid:<N>` if unavailable.

## Dependencies

- Python 3
- `rich` library (console, live, panel, text, columns)
- macOS system tools: `ps`, `lsof`
- Ghostty terminal emulator

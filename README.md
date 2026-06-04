# MCP Server for WinDbg Crash Analysis

A Model Context Protocol server that bridges AI models with WinDbg for crash dump analysis and remote debugging.

> **Fork notice**: This repository is a fork of [svnscha/mcp-windbg](https://github.com/svnscha/mcp-windbg), maintained at **[hardeyhuang/mcp-windbg](https://github.com/hardeyhuang/mcp-windbg)** with additional fixes and features (e.g. idle session auto-cleanup via `--idle-timeout`). The PyPI package `mcp-windbg` tracks the upstream project, **not this fork** — to get the changes here you must install directly from this Git repo (see [Installation](#installation) below).

<!-- mcp-name: io.github.svnscha/mcp-windbg -->

## Overview

This MCP server integrates with [CDB](https://learn.microsoft.com/en-us/windows-hardware/drivers/debugger/opening-a-crash-dump-file-using-cdb) to enable AI models to analyze Windows crash dumps and connect to remote debugging sessions using WinDbg/CDB.

## What is this?

An AI-powered tool that bridges LLMs with WinDbg for crash dump analysis and live debugging. Execute debugger commands through natural language queries like *"Show me the call stack and explain this access violation"*.

## What This is Not

Not a magical auto-fix solution. It's a Python wrapper around CDB that leverages LLM knowledge to assist with debugging.

## Usage Modes

- **Crash Dump Analysis**: Examine Windows crash dumps
- **Live Debugging**: Connect to remote debugging targets
- **Directory Analysis**: Process multiple dumps for patterns

## Quick Start

### Prerequisites
- Windows with [Debugging Tools for Windows](https://developer.microsoft.com/en-us/windows/downloads/windows-sdk/) or [WinDbg from Microsoft Store](https://apps.microsoft.com/detail/9pgjgd53tn86).
- Python 3.10 or higher
- Any MCP-compatible client (GitHub Copilot, Claude Desktop, Cline, Cursor, Windsurf etc.)
- Configure MCP server in your chosen client

> [!TIP]
> In enterprise environments, MCP server usage might be restricted by organizational policies. Check with your IT team about AI tool usage and ensure you have the necessary permissions before proceeding.

### Installation

> **Note**: This is a **fork** of [svnscha/mcp-windbg](https://github.com/svnscha/mcp-windbg) maintained at [hardeyhuang/mcp-windbg](https://github.com/hardeyhuang/mcp-windbg). It is **not** published to PyPI under this fork's changes, so `pip install mcp-windbg` will give you the upstream package, **without** the fixes/features added here (e.g. `--idle-timeout`, etc). Install directly from this Git repository instead.

**Option A — Install from this fork's Git repo (recommended):**
```bash
# Latest main branch
pip install git+https://github.com/hardeyhuang/mcp-windbg.git

# Or pin to a specific tag / commit
pip install git+https://github.com/hardeyhuang/mcp-windbg.git@main
```

**Option B — Install from a local clone (for development):**
```bash
git clone https://github.com/hardeyhuang/mcp-windbg.git
cd mcp-windbg

# pip (editable install)
pip install -e .

# or with uv (recommended, matches CI)
uv sync
```

**Option C — Run without installing, using `uv` / `pipx` directly from Git:**
```bash
# uv
uvx --from git+https://github.com/hardeyhuang/mcp-windbg.git mcp-windbg --help

# pipx
pipx run --spec git+https://github.com/hardeyhuang/mcp-windbg.git mcp-windbg --help
```

After installation the `mcp-windbg` console script and the `python -m mcp_windbg` module entry point both become available.

## Transport Options

The MCP server supports multiple transport protocols:

| Transport | Description | Use Case |
|-----------|-------------|----------|
| `stdio` (default) | Standard input/output | Local MCP clients like VS Code, Claude Desktop |
| `streamable-http` | Streamable HTTP | Modern HTTP clients with bidirectional streaming |

### Starting with Different Transports

**Standard I/O (default):**
```bash
mcp-windbg
# or explicitly
mcp-windbg --transport stdio
```

**Streamable HTTP:**
```bash
mcp-windbg --transport streamable-http --host 127.0.0.1 --port 8000
```
Endpoint: `http://127.0.0.1:8000/mcp`

### Command Line Options

All flags are optional; the defaults shown below are the same ones the binary will pick if the flag is omitted. Copy these into the `args` array of any IDE config (next section) and tweak as needed.

| Flag | Default | Description |
|------|---------|-------------|
| `--transport {stdio,streamable-http}` | `stdio` | Transport protocol. `stdio` is what every desktop MCP client uses; `streamable-http` is for running the server as a separate HTTP service. |
| `--host HOST` | `127.0.0.1` | HTTP bind host (only used with `streamable-http`). |
| `--port PORT` | `8000` | HTTP bind port (only used with `streamable-http`). |
| `--cdb-path PATH` | auto-detect | Custom path to `cdb.exe`. Auto-detected from Windows Kits / Debugging Tools / Microsoft Store WinDbg. Override if you have a portable copy. |
| `--symbols-path PATH` | from `_NT_SYMBOL_PATH` env | Custom symbols path. Equivalent to setting `_NT_SYMBOL_PATH`. Example: `SRV*C:\Symbols*https://msdl.microsoft.com/download/symbols`. |
| `--timeout SECONDS` | `30` | Per-command timeout. Increase for very slow `!analyze -v` on large dumps. |
| `--init-timeout SECONDS` | `max(60, timeout*4)` | Timeout for the *first* CDB prompt (cold-start dump load + initial symbol download). Bump on slow networks. |
| `--idle-timeout SECONDS` | `1200` (20 min) | Auto-close idle CDB sessions after N seconds of inactivity to release memory. The next command transparently respawns CDB. Set `0` to disable. |
| `--verbose` | off | Enable verbose output (also bumps log level to `DEBUG`). |
| `--log-level LEVEL` | `INFO` (or `DEBUG` if `--verbose`) | One of `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. |
| `--log-file PATH` | none | Also write logs to this file. Logs always also go to stderr (stdout is reserved for the MCP JSON-RPC stream). |

> **Important**: under the default `stdio` transport, the server uses **stdout** for JSON-RPC. Never add `print(...)` or `--log-file -` style configs that would write to stdout — use `--log-file` or stderr instead.


## IDE / Client Configuration

All examples below use the `stdio` transport (recommended for desktop IDEs) and pre-fill **every supported parameter with its default value** so you can comment out / delete what you don't need. Replace the symbol cache path (`C:\Symbols`) with whatever you actually use.

### 1. Visual Studio Code (GitHub Copilot / Agent Mode)

Press `F1` → **MCP: Open User Configuration**, then paste:

```json
{
    "servers": {
        "mcp_windbg": {
            "type": "stdio",
            "command": "python",
            "args": [
                "-m", "mcp_windbg",
                "--transport", "stdio",
                "--timeout", "30",
                "--init-timeout", "120",
                "--idle-timeout", "1200"
            ],
            "env": {
                "_NT_SYMBOL_PATH": "SRV*C:\\Symbols*https://msdl.microsoft.com/download/symbols"
            }
        }
    }
}
```

To bind to a custom CDB / symbols path, add:
```json
"args": [
    "-m", "mcp_windbg",
    "--cdb-path", "C:\\Program Files (x86)\\Windows Kits\\10\\Debuggers\\x64\\cdb.exe",
    "--symbols-path", "SRV*C:\\Symbols*https://msdl.microsoft.com/download/symbols",
    "--timeout", "30",
    "--init-timeout", "120",
    "--idle-timeout", "1200",
    "--log-file", "C:\\Logs\\mcp-windbg.log"
]
```

### 2. Claude Desktop

Edit `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
    "mcpServers": {
        "mcp_windbg": {
            "command": "python",
            "args": [
                "-m", "mcp_windbg",
                "--transport", "stdio",
                "--timeout", "30",
                "--init-timeout", "120",
                "--idle-timeout", "1200"
            ],
            "env": {
                "_NT_SYMBOL_PATH": "SRV*C:\\Symbols*https://msdl.microsoft.com/download/symbols"
            }
        }
    }
}
```

Then fully quit and restart Claude Desktop (the tray icon, not just the window).

### 3. Cursor

Edit `%USERPROFILE%\.cursor\mcp.json` (global) or `<project>\.cursor\mcp.json` (per-project):

```json
{
    "mcpServers": {
        "mcp_windbg": {
            "command": "python",
            "args": [
                "-m", "mcp_windbg",
                "--transport", "stdio",
                "--timeout", "30",
                "--init-timeout", "120",
                "--idle-timeout", "1200"
            ],
            "env": {
                "_NT_SYMBOL_PATH": "SRV*C:\\Symbols*https://msdl.microsoft.com/download/symbols"
            }
        }
    }
}
```

### 4. Windsurf

Edit `%USERPROFILE%\.codeium\windsurf\mcp_config.json`:

```json
{
    "mcpServers": {
        "mcp_windbg": {
            "command": "python",
            "args": [
                "-m", "mcp_windbg",
                "--transport", "stdio",
                "--timeout", "30",
                "--init-timeout", "120",
                "--idle-timeout", "1200"
            ],
            "env": {
                "_NT_SYMBOL_PATH": "SRV*C:\\Symbols*https://msdl.microsoft.com/download/symbols"
            }
        }
    }
}
```

### 5. Cline (VS Code extension)

Open the Cline panel → `MCP Servers` → `Configure MCP Servers`, then:

```json
{
    "mcpServers": {
        "mcp_windbg": {
            "command": "python",
            "args": [
                "-m", "mcp_windbg",
                "--transport", "stdio",
                "--timeout", "30",
                "--init-timeout", "120",
                "--idle-timeout", "1200"
            ],
            "env": {
                "_NT_SYMBOL_PATH": "SRV*C:\\Symbols*https://msdl.microsoft.com/download/symbols"
            },
            "disabled": false,
            "autoApprove": []
        }
    }
}
```

### 6. CodeBuddy / Continue / other generic MCP clients

Most clients accept the same shape. Use whatever JSON file the client documents and reuse the `command` + `args` + `env` block above.

```json
{
    "mcpServers": {
        "mcp_windbg": {
            "command": "python",
            "args": [
                "-m", "mcp_windbg",
                "--transport", "stdio",
                "--timeout", "30",
                "--init-timeout", "120",
                "--idle-timeout", "1200",
                "--verbose"
            ],
            "env": {
                "_NT_SYMBOL_PATH": "SRV*C:\\Symbols*https://msdl.microsoft.com/download/symbols"
            }
        }
    }
}
```

> **Tip**: If `python` is not on `PATH` (or you installed into a virtualenv / `uv` env), replace `"command": "python"` with the absolute interpreter path, e.g. `"C:\\Python312\\python.exe"` or `"D:\\repos\\mcp-windbg\\.venv\\Scripts\\python.exe"`.
>
> To skip a system-wide install entirely, you can have the IDE launch the server straight from this fork's Git repo via `uvx`:
> ```json
> "command": "uvx",
> "args": [
>     "--from", "git+https://github.com/hardeyhuang/mcp-windbg.git",
>     "mcp-windbg",
>     "--transport", "stdio",
>     "--timeout", "30",
>     "--init-timeout", "120",
>     "--idle-timeout", "1200"
> ]
> ```

### HTTP Transport (shared / remote server)

For scenarios where you want the MCP server to run as a long-lived service (remote access, shared symbol cache, easier debugging), use `streamable-http`:

**1. Start the server manually:**
```bash
python -m mcp_windbg ^
    --transport streamable-http ^
    --host 127.0.0.1 ^
    --port 8000 ^
    --timeout 30 ^
    --init-timeout 120 ^
    --idle-timeout 1200
```

**2. Configure your IDE to connect over HTTP** (VS Code shown — most clients have an equivalent):
```json
{
    "servers": {
        "mcp_windbg_http": {
            "type": "http",
            "url": "http://127.0.0.1:8000/mcp"
        }
    }
}
```

> **Workspace-specific and alternative configuration**: See [Installation documentation](https://github.com/svnscha/mcp-windbg/wiki/Installation) for details on workspace-only setup and additional clients.

Once configured, restart your MCP client and start debugging:

```
Analyze the crash dump at C:\dumps\app.dmp
```

## MCP Compatibility

This server implements the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/), making it compatible with any MCP-enabled client:

The beauty of MCP is that you write the server once, and it works everywhere. Choose your favorite AI assistant!

### Tools

| Tool | Purpose | Use Case |
|------|---------|----------|
| [`list_windbg_dumps`](https://github.com/svnscha/mcp-windbg/wiki/Tools#list_windbg_dumps) | List crash dump files | Discovery and batch analysis |
| [`open_windbg_dump`](https://github.com/svnscha/mcp-windbg/wiki/Tools#open_windbg_dump) | Analyze crash dumps | Initial crash dump analysis |
| [`close_windbg_dump`](https://github.com/svnscha/mcp-windbg/wiki/Tools#close_windbg_dump) | Cleanup dump sessions | Resource management |
| [`open_windbg_remote`](https://github.com/svnscha/mcp-windbg/wiki/Tools#open_windbg_remote) | Connect to remote debugging | Live debugging sessions |
| [`close_windbg_remote`](https://github.com/svnscha/mcp-windbg/wiki/Tools#close_windbg_remote) | Cleanup remote sessions | Resource management |
| [`run_windbg_cmd`](https://github.com/svnscha/mcp-windbg/wiki/Tools#run_windbg_cmd) | Execute WinDbg commands | Custom analysis and investigation |
| [`send_ctrl_break`](https://github.com/svnscha/mcp-windbg/wiki/Tools#send_ctrl_break) | Break into a running target | Interrupt execution during live debugging |

## Documentation

**[Documentation](https://github.com/svnscha/mcp-windbg/wiki)**

| Topic | Description |
|-------|-------------|
| **[Getting Started](https://github.com/svnscha/mcp-windbg/wiki/Getting-Started)** | Quick setup and first steps |
| **[Installation](https://github.com/svnscha/mcp-windbg/wiki/Installation)** | Detailed installation for pip, MCP registry, and from source |
| **[Usage](https://github.com/svnscha/mcp-windbg/wiki/Usage)** | MCP client integration, command-line usage, and workflows |
| **[Tools Reference](https://github.com/svnscha/mcp-windbg/wiki/Tools)** | Complete API reference and examples |
| **[Troubleshooting](https://github.com/svnscha/mcp-windbg/wiki/Troubleshooting)** | Common issues and solutions |

## Examples

### Crash Dump Analysis

> Analyze this heap address with !heap -p -a 0xABCD1234 and check for buffer overflow"

> Execute !peb and tell me if there are any environment variables that might affect this crash"

> Run .ecxr followed by k and explain the exception's root cause"

### Remote Debugging

> "Connect to tcp:Port=5005,Server=192.168.0.100 and show me the current thread state"

> "Send CTRL+BREAK to the live session, then dump all thread stacks with ~*k"

> "Check for timing issues in the thread pool with !runaway and !threads"

> "Show me all threads with ~*k and identify which one is causing the hang"

## Blog

Read about the development journey: [The Future of Crash Analysis: AI Meets WinDbg](https://svnscha.de/posts/ai-meets-windbg/)

### Links

- [Reddit: I taught Copilot to analyze Windows Crash Dumps](https://www.reddit.com/r/programming/comments/1kes3wq/i_taught_copilot_to_analyze_windows_crash_dumps/)
- [Hackernews: AI Meets WinDbg](https://news.ycombinator.com/item?id=43892096)

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=svnscha/mcp-windbg&type=Date)](https://www.star-history.com/#svnscha/mcp-windbg&Date)

## License

MIT

from .server import serve, serve_http

def _configure_logging(level: str, log_file: str | None, verbose: bool) -> None:
    """Configure root logging for the MCP server.

    NOTE: We deliberately log to stderr (and optionally to a file) instead of
    stdout, because the default `stdio` MCP transport uses stdout for the
    JSON-RPC protocol; writing arbitrary text to stdout would corrupt frames.
    """
    import logging
    import sys

    # If verbose flag is set but no explicit level was provided, bump to DEBUG.
    effective_level = level.upper() if level else ("DEBUG" if verbose else "INFO")
    numeric_level = getattr(logging, effective_level, logging.INFO)

    handlers: list[logging.Handler] = []

    stderr_handler = logging.StreamHandler(stream=sys.stderr)
    stderr_handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s (%(threadName)s): %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    handlers.append(stderr_handler)

    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s [%(levelname)s] %(name)s (%(threadName)s): %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
        )
        handlers.append(file_handler)

    # Force=True replaces any default handlers that might have been installed
    # (for example by uvicorn) so our format/level always wins.
    logging.basicConfig(level=numeric_level, handlers=handlers, force=True)

    # Make sure our own loggers honor the chosen level.
    logging.getLogger("mcp_windbg").setLevel(numeric_level)


def main():
    """MCP WinDbg Server - Windows crash dump analysis functionality for MCP"""
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(
        description="Give a model the ability to analyze Windows crash dumps with WinDbg/CDB"
    )
    parser.add_argument("--cdb-path", type=str, help="Custom path to cdb.exe")
    parser.add_argument("--symbols-path", type=str, help="Custom symbols path")
    parser.add_argument("--timeout", type=int, default=30, help="Command timeout in seconds")
    parser.add_argument(
        "--init-timeout",
        type=int,
        default=None,
        help="Timeout in seconds for the initial CDB prompt (loading the dump + first symbol fetch). "
             "Defaults to max(60, timeout*4) which is usually enough for cold starts.",
    )
    parser.add_argument(
        "--idle-timeout",
        type=int,
        default=20 * 60,
        help="Auto-close an idle CDB session after this many seconds with no commands "
             "(default: 1200 = 20 minutes). The next command for the same dump/remote "
             "target will transparently spawn a fresh CDB process. Set to 0 to disable.",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable verbose output")
    parser.add_argument(
        "--log-level",
        type=str,
        default="",
        choices=["", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging level (defaults to DEBUG when --verbose, otherwise INFO)",
    )
    parser.add_argument(
        "--log-file",
        type=str,
        default=None,
        help="Optional path to a log file. Logs always also go to stderr.",
    )

    # Transport options
    parser.add_argument(
        "--transport",
        type=str,
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="Transport protocol to use (default: stdio)"
    )
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host to bind HTTP server to (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind HTTP server to (default: 8000)")

    args = parser.parse_args()

    _configure_logging(level=args.log_level, log_file=args.log_file, verbose=args.verbose)

    if args.transport == "stdio":
        asyncio.run(serve(
            cdb_path=args.cdb_path,
            symbols_path=args.symbols_path,
            timeout=args.timeout,
            verbose=args.verbose,
            init_timeout=args.init_timeout,
            idle_timeout=args.idle_timeout,
        ))
    else:
        asyncio.run(serve_http(
            host=args.host,
            port=args.port,
            cdb_path=args.cdb_path,
            symbols_path=args.symbols_path,
            timeout=args.timeout,
            verbose=args.verbose,
            init_timeout=args.init_timeout,
            idle_timeout=args.idle_timeout,
        ))


if __name__ == "__main__":
    main()

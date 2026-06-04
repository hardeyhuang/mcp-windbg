import subprocess
import threading
import re
import os
import platform
import signal
import time
import logging
from typing import List, Optional

logger = logging.getLogger(__name__)

# Regular expression to detect CDB prompts
PROMPT_REGEX = re.compile(r"^\d+:\d+>\s*$")

# Command marker to reliably detect command completion
COMMAND_MARKER = ".echo COMMAND_COMPLETED_MARKER"
COMMAND_MARKER_PATTERN = re.compile(r"COMMAND_COMPLETED_MARKER")

# Per-command timeout heuristics. The first match wins.
# We deliberately bias these high because the most common cause of "the
# session is stuck" is symbol downloads on the first heavy command.
# Each entry: (regex, timeout_seconds, human_label)
_LONG_COMMAND_PATTERNS = [
    # Very expensive: full analysis, force reload, lazy module enumeration
    (re.compile(r"^\s*!analyze\b", re.IGNORECASE), 600, "!analyze (heavy + symbol download)"),
    (re.compile(r"^\s*\.reload\b", re.IGNORECASE), 600, ".reload (symbol download)"),
    (re.compile(r"^\s*!sym\s+noisy\b", re.IGNORECASE), 600, "!sym noisy"),
    (re.compile(r"^\s*\.symfix\b", re.IGNORECASE), 300, ".symfix"),
    # Module / thread enumeration: usually fast but can be slow with many modules
    (re.compile(r"^\s*lm\b", re.IGNORECASE), 180, "lm (modules)"),
    (re.compile(r"^\s*~\*?\s*kb?\b"), 180, "~* k (all stacks)"),
    (re.compile(r"^\s*kb?\b"), 120, "k / kb stack"),
    (re.compile(r"^\s*!peb\b", re.IGNORECASE), 180, "!peb"),
]


def _classify_command_timeout(command: str, default_timeout: int) -> int:
    """Pick a sensible timeout for a CDB command.

    Rules:
      * If the caller-provided default_timeout is already greater than the
        heuristic value, keep the larger one (caller knows best).
      * Otherwise use the heuristic value for known long-running commands.
      * For everything else, fall back to default_timeout.
    """
    cmd = command.strip()
    for pattern, suggested, label in _LONG_COMMAND_PATTERNS:
        if pattern.search(cmd):
            chosen = max(default_timeout, suggested)
            if chosen != default_timeout:
                logger.debug(
                    "Auto-bumping timeout for %s: %ss -> %ss (matched %s)",
                    cmd, default_timeout, chosen, label,
                )
            return chosen
    return default_timeout

# Default paths where cdb.exe might be located
DEFAULT_CDB_PATHS = [
    # Traditional Windows SDK locations
    r"C:\Program Files (x86)\Windows Kits\10\Debuggers\x64\cdb.exe",
    r"C:\Program Files (x86)\Windows Kits\10\Debuggers\x86\cdb.exe",
    r"C:\Program Files\Debugging Tools for Windows (x64)\cdb.exe",
    r"C:\Program Files\Debugging Tools for Windows (x86)\cdb.exe",

    # Microsoft Store WinDbg Preview locations (architecture-specific)
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WindowsApps\cdbX64.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WindowsApps\cdbX86.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WindowsApps\cdbARM64.exe")
]

class CDBError(Exception):
    """Custom exception for CDB-related errors"""
    pass

class CDBSession:
    def __init__(
        self,
        dump_path: Optional[str] = None,
        remote_connection: Optional[str] = None,
        cdb_path: Optional[str] = None,
        symbols_path: Optional[str] = None,
        initial_commands: Optional[List[str]] = None,
        timeout: int = 10,
        verbose: bool = False,
        additional_args: Optional[List[str]] = None,
        init_timeout: Optional[int] = None,
    ):
        """
        Initialize a new CDB debugging session.

        Args:
            dump_path: Path to the crash dump file (mutually exclusive with remote_connection)
            remote_connection: Remote debugging connection string (e.g., "tcp:Port=5005,Server=192.168.0.100")
            cdb_path: Custom path to cdb.exe. If None, will try to find it automatically
            symbols_path: Custom symbols path. If None, uses default Windows symbols
            initial_commands: List of commands to run when CDB starts
            timeout: Timeout in seconds for waiting for CDB responses
            verbose: Whether to print additional debug information
            additional_args: Additional arguments to pass to cdb.exe

        Raises:
            CDBError: If cdb.exe cannot be found or started
            FileNotFoundError: If the dump file cannot be found
            ValueError: If invalid parameters are provided
        """
        # Validate that exactly one of dump_path or remote_connection is provided
        if not dump_path and not remote_connection:
            raise ValueError("Either dump_path or remote_connection must be provided")
        if dump_path and remote_connection:
            raise ValueError("dump_path and remote_connection are mutually exclusive")

        if dump_path and not os.path.isfile(dump_path):
            raise FileNotFoundError(f"Dump file not found: {dump_path}")

        self.dump_path = dump_path
        self.remote_connection = remote_connection
        self.timeout = timeout
        # Initial-prompt timeout for CDB cold start.
        # - Dump mode: loading a big dump and the very first symbol resolution
        #   can easily take minutes, so we default to max(60, 4 * timeout).
        # - Remote mode: CDB's own DebugConnect retry already bounds the wait
        #   to ~5 min on an unreachable target. Letting init_timeout exceed
        #   that just makes the MCP client hang longer for no benefit, so we
        #   cap the default at 90s. Callers can pass init_timeout explicitly
        #   when they need a longer window.
        if init_timeout is not None:
            self.init_timeout = init_timeout
        elif remote_connection:
            self.init_timeout = min(90, max(60, timeout * 2))
        else:
            self.init_timeout = max(60, timeout * 4)
        self.verbose = verbose

        # Find cdb executable
        self.cdb_path = self._find_cdb_executable(cdb_path)
        if not self.cdb_path:
            raise CDBError("Could not find cdb.exe. Please provide a valid path.")

        # Prepare command args
        cmd_args = [self.cdb_path]

        # Add connection type specific arguments
        if self.dump_path:
            cmd_args.extend(["-z", self.dump_path])
        elif self.remote_connection:
            cmd_args.extend(["-remote", self.remote_connection])

        # Add symbols path if provided
        if symbols_path:
            cmd_args.extend(["-y", symbols_path])

        # Add any additional arguments
        if additional_args:
            cmd_args.extend(additional_args)

        try:
            # Only create a new process group for remote sessions where CTRL+BREAK is needed
            creationflags = 0
            if os.name == 'nt' and self.remote_connection:
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

            logger.info(
                "Launching CDB: exe=%s args=%s dump=%s remote=%s timeout=%s",
                self.cdb_path, cmd_args[1:], self.dump_path, self.remote_connection, self.timeout,
            )
            self._launch_started_at = time.monotonic()
            self.process = subprocess.Popen(
                cmd_args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                # CDB output frequently contains non-UTF-8 bytes when commands
                # like `du` / `db` dump raw memory that happens to include
                # binary data, or when the system locale is GBK / cp936.
                # Using `errors="replace"` prevents the reader thread from
                # dying with a UnicodeDecodeError, which previously left the
                # session "alive but deaf" and caused every subsequent command
                # to hit a 60s timeout.
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
            )
            logger.info("CDB process started: pid=%s", self.process.pid)
        except Exception as e:
            logger.exception("Failed to start CDB process")
            raise CDBError(f"Failed to start CDB process: {str(e)}")

        self.output_lines = []
        self.lock = threading.Lock()
        # Tracks the last time the session received any caller-driven activity
        # (command or ctrl+break). Used by the idle reaper in server.py to
        # auto-shutdown sessions that have been silent for too long, so cdb.exe
        # does not occupy memory indefinitely. Initialized to "now" so a brand
        # new session is not immediately reaped before its first command.
        self.last_activity_at = time.monotonic()
        # Serializes the entire "write command -> wait for marker" transaction.
        # Without this, two concurrent send_command() calls would race: each
        # writes its own marker, and one thread can consume the OTHER thread's
        # marker, returning empty/wrong output and leaving the other thread to
        # time out for the full cmd_timeout. Must be a separate lock from
        # self.lock (which is held briefly by the reader thread on every line).
        self._command_lock = threading.Lock()
        self.ready_event = threading.Event()
        # Diagnostics: keep track of last activity from CDB stdout to help debug stalls
        self._last_output_at = time.monotonic()
        self._total_lines_read = 0
        self._recent_lines: List[str] = []  # ring-buffer of the most recent lines
        self._recent_lines_max = 20
        self._current_command: Optional[str] = None
        self._command_started_at: Optional[float] = None
        # Becomes False once the reader thread has exited (EOF or fatal error).
        # Once dead, the session can no longer observe completion markers and
        # should not silently let callers wait the full timeout for nothing.
        self._reader_alive = True
        self._reader_error: Optional[str] = None
        # Becomes True if the session enters an unrecoverable state (e.g.
        # resync after a timeout failed to re-establish marker alignment).
        # Subsequent send_command calls fail fast so the caller can re-create
        # the session instead of getting silently corrupted output.
        self._broken = False
        self._broken_reason: Optional[str] = None
        self.reader_thread = threading.Thread(target=self._read_output, name="cdb-reader")
        self.reader_thread.daemon = True
        self.reader_thread.start()

        # Wait for CDB to initialize by sending an echo marker
        try:
            logger.info("Waiting for initial CDB prompt (init_timeout=%ss)", self.init_timeout)
            self._wait_for_prompt(timeout=self.init_timeout)
            logger.info(
                "CDB became ready after %.2fs (lines so far=%d)",
                time.monotonic() - self._launch_started_at, self._total_lines_read,
            )
        except CDBError:
            logger.error(
                "CDB initialization timed out after %.2fs. Lines read=%d. Last lines:\n%s",
                time.monotonic() - self._launch_started_at,
                self._total_lines_read,
                "\n".join(self._recent_lines),
            )
            self.shutdown()
            raise CDBError("CDB initialization timed out")

        # Run initial commands if provided
        if initial_commands:
            for cmd in initial_commands:
                self.send_command(cmd)

    def _find_cdb_executable(self, custom_path: Optional[str] = None) -> Optional[str]:
        """Find the cdb.exe executable"""
        if custom_path and os.path.isfile(custom_path):
            return custom_path

        for path in DEFAULT_CDB_PATHS:
            if os.path.isfile(path):
                return path

        return None

    def _read_output(self):
        """Thread function to continuously read CDB output"""
        if not self.process or not self.process.stdout:
            self._reader_alive = False
            self.ready_event.set()
            return

        buffer = []
        exit_reason = "EOF"
        try:
            for line in self.process.stdout:
                line = line.rstrip()
                now = time.monotonic()
                if self.verbose:
                    # IMPORTANT: do NOT print to stdout under stdio MCP transport,
                    # otherwise it would corrupt the JSON-RPC frames. Use logger.
                    logger.debug("CDB > %s", line)

                with self.lock:
                    buffer.append(line)
                    self._last_output_at = now
                    self._total_lines_read += 1
                    self._recent_lines.append(line)
                    if len(self._recent_lines) > self._recent_lines_max:
                        self._recent_lines = self._recent_lines[-self._recent_lines_max:]
                    # Check if the marker is in this line
                    if COMMAND_MARKER_PATTERN.search(line):
                        # Remove the marker line itself
                        if buffer and COMMAND_MARKER_PATTERN.search(buffer[-1]):
                            buffer.pop()
                        elapsed = (
                            now - self._command_started_at
                            if self._command_started_at is not None else 0.0
                        )
                        logger.info(
                            "CDB command finished: cmd=%r lines=%d elapsed=%.2fs",
                            self._current_command, len(buffer), elapsed,
                        )
                        self.output_lines = buffer
                        buffer = []
                        self.ready_event.set()
        except Exception as e:
            # Catch *everything* (incl. UnicodeDecodeError, even though we now
            # use errors="replace"; better safe than sorry) so the reader
            # thread cannot silently die and leave callers waiting on
            # ready_event for the full command timeout.
            exit_reason = f"error: {e!r}"
            self._reader_error = repr(e)
            logger.warning("CDB output reader stopped: %s", e)
            if self.verbose:
                logger.debug("CDB output reader error: %s", e, exc_info=True)
        finally:
            self._reader_alive = False
            # Wake up anything that is currently waiting for a marker;
            # send_command / _wait_for_prompt will then notice the reader
            # is gone and surface a clear error instead of timing out.
            self.ready_event.set()
            logger.info(
                "CDB stdout reader exited (%s). Total lines=%d, last activity %.2fs ago",
                exit_reason,
                self._total_lines_read,
                time.monotonic() - self._last_output_at,
            )

    def _wait_for_prompt(self, timeout=None):
        """Wait for CDB to be ready for commands by sending a marker"""
        try:
            self.ready_event.clear()
            logger.debug("Sending initial prompt marker to CDB")
            self.process.stdin.write(f"{COMMAND_MARKER}\n")
            self.process.stdin.flush()

            if not self.ready_event.wait(timeout=timeout or self.timeout):
                idle = time.monotonic() - self._last_output_at
                logger.error(
                    "Timed out waiting for CDB initial prompt. idle=%.2fs lines=%d. Last lines:\n%s",
                    idle, self._total_lines_read, "\n".join(self._recent_lines),
                )
                raise CDBError(f"Timed out waiting for CDB prompt")

            # ready_event may have been set by the reader thread shutting
            # down (EOF / decode error / process died) rather than by an
            # actual marker. Detect that case and surface a clear error.
            if not self._reader_alive:
                proc_rc = self.process.poll() if self.process else None
                raise CDBError(
                    "CDB reader thread is not running "
                    f"(reader_error={self._reader_error}, process_returncode={proc_rc})"
                )
        except IOError as e:
            logger.exception("Failed to communicate with CDB during prompt wait")
            raise CDBError(f"Failed to communicate with CDB: {str(e)}")

    def send_command(self, command: str, timeout: Optional[int] = None) -> List[str]:
        """
        Send a command to CDB and return the output

        Args:
            command: The command to send
            timeout: Custom timeout for this command (overrides instance timeout)

        Returns:
            List of output lines from CDB

        Raises:
            CDBError: If the command times out or CDB is not responsive
        """
        if not self.process:
            raise CDBError("CDB process is not running")

        # Reject multi-line commands: our completion-marker protocol relies on
        # writing "<cmd>\n.echo MARKER\n", and embedded newlines would let CDB
        # interpret part of the command body as the marker setup, desyncing
        # the stream and causing apparent hangs.
        if "\n" in command or "\r" in command:
            raise CDBError(
                "Multi-line commands are not supported (embedded \\n / \\r). "
                "Split into separate send_command calls."
            )

        # Serialize concurrent send_command calls on the same session. CDB has
        # a single stdin/stdout, so two callers writing their own markers in
        # parallel would deterministically corrupt each other's output.
        with self._command_lock:
            # Refresh idle timer both before and after the command:
            #   - "before" prevents the reaper from killing the session in the
            #     middle of a long-running command (e.g. !analyze).
            #   - "after" captures the actual end-of-activity timestamp.
            self.last_activity_at = time.monotonic()
            try:
                return self._send_command_locked(command, timeout)
            finally:
                self.last_activity_at = time.monotonic()

    def _send_command_locked(self, command: str, timeout: Optional[int]) -> List[str]:
        # Fail fast if the reader thread has died or the underlying CDB
        # process has exited. Without these checks, send_command would write
        # the marker, never see it come back, and stall the caller for the
        # full cmd_timeout (typically 60s) on every subsequent command --
        # exactly the cascading-timeout pattern we want to avoid.
        if self.process.poll() is not None:
            raise CDBError(
                f"CDB process has exited (returncode={self.process.returncode}); "
                "session is dead and must be re-created."
            )
        if not self._reader_alive:
            raise CDBError(
                "CDB output reader is no longer running "
                f"(reader_error={self._reader_error}); session is dead and must be re-created."
            )
        if self._broken:
            raise CDBError(
                f"CDB session is in a broken state ({self._broken_reason}); "
                "it must be re-created before further commands can be issued."
            )

        # Pick a sensible timeout. If caller provided one, respect it; else use
        # heuristics for known-long commands like !analyze / .reload / lm.
        if timeout is not None:
            cmd_timeout = timeout
        else:
            cmd_timeout = _classify_command_timeout(command, self.timeout)

        self.ready_event.clear()
        with self.lock:
            self.output_lines = []
            self._current_command = command
            self._command_started_at = time.monotonic()

        logger.info("CDB command begin: cmd=%r timeout=%ss", command, cmd_timeout)

        try:
            # Send the command followed by our marker to detect completion
            self.process.stdin.write(f"{command}\n{COMMAND_MARKER}\n")
            self.process.stdin.flush()
        except IOError as e:
            logger.exception("Failed to write command to CDB stdin")
            raise CDBError(f"Failed to send command: {str(e)}")

        if not self.ready_event.wait(timeout=cmd_timeout):
            with self.lock:
                idle = time.monotonic() - self._last_output_at
                tail = "\n".join(self._recent_lines)
                lines_so_far = self._total_lines_read
            logger.error(
                "CDB command TIMEOUT: cmd=%r timeout=%ss idle_for=%.2fs total_lines=%d. "
                "Last %d lines from CDB:\n%s",
                command, cmd_timeout, idle, lines_so_far, len(self._recent_lines), tail,
            )
            # Try to resync the stream so that the next command does not get
            # poisoned by leftover output from this timed-out command. Without
            # this, a single timeout tends to cascade into "every subsequent
            # command also times out".
            self._resync_after_timeout()
            raise CDBError(f"Command timed out after {cmd_timeout} seconds: {command}")

        # ready_event may have fired because the reader thread exited (EOF
        # or fatal decode error) rather than because we actually saw a
        # completion marker. Detect that and fail loudly with the cause.
        if not self._reader_alive:
            raise CDBError(
                "CDB output reader stopped while waiting for command "
                f"{command!r} to complete (reader_error={self._reader_error}, "
                f"process_returncode={self.process.poll()})."
            )

        with self.lock:
            result = self.output_lines.copy()
            self.output_lines = []
            self._current_command = None
            self._command_started_at = None
        return result

    def _resync_after_timeout(self) -> None:
        """Best-effort recovery after a command timed out.

        Strategy: drop any partial buffer the reader has, then send a fresh
        marker and wait briefly. If CDB is still alive but was simply slow,
        the marker will eventually come through; future commands will then be
        properly aligned. If CDB has died, this becomes a no-op and the next
        send_command will surface that.
        """
        if not self.process or self.process.poll() is not None:
            logger.warning("Resync skipped: CDB process is no longer running")
            return
        if not self._reader_alive:
            logger.warning(
                "Resync skipped: CDB output reader is dead (%s); "
                "the session must be re-created to recover.",
                self._reader_error,
            )
            return
        try:
            with self.lock:
                self.output_lines = []
                self._current_command = None
                self._command_started_at = None
            self.ready_event.clear()
            try:
                self.process.stdin.write(f"{COMMAND_MARKER}\n")
                self.process.stdin.flush()
            except IOError as e:
                logger.warning("Resync write failed: %s", e)
                return
            # Drain quickly; whatever marker we see will cause ready_event to fire.
            drain_seconds = max(5, self.timeout // 2)
            if self.ready_event.wait(timeout=drain_seconds):
                logger.info("Resync OK after timeout (drained within %ss)", drain_seconds)
            else:
                # If we cannot reacquire stream alignment, the session is no
                # longer trustworthy: a stale marker is still in flight in the
                # pipe, and the next send_command would consume IT instead of
                # its own marker, returning empty/wrong output. Mark broken so
                # the caller is forced to rebuild the session.
                logger.warning(
                    "Resync did not see marker within %ss; marking session as broken.",
                    drain_seconds,
                )
                self._broken = True
                self._broken_reason = f"resync_timeout_after_{drain_seconds}s"
            # Either way, throw away whatever buffer was assembled during resync.
            with self.lock:
                self.output_lines = []
        except Exception as e:
            logger.exception("Resync after timeout failed: %s", e)

    def shutdown(self):
        """Clean up and terminate the CDB process.

        Three-stage shutdown to avoid leaving zombie cdb.exe instances:
          1. Graceful: send 'q' (or CTRL+B for remote) and wait up to 5s.
          2. Terminate: SIGTERM / TerminateProcess and wait up to 5s.
          3. Kill: hard kill and wait up to 3s; log if even that fails.
        """
        try:
            if self.process and self.process.poll() is None:
                logger.info("Shutting down CDB session (pid=%s)", self.process.pid)
                # Stage 1: graceful quit. Loaded symbol caches can make CDB
                # take several seconds to exit cleanly, so don't be stingy.
                try:
                    if self.remote_connection:
                        # For remote connections, send CTRL+B to detach
                        self.process.stdin.write("\x02")  # CTRL+B
                        self.process.stdin.flush()
                    else:
                        # For dump files, send 'q' to quit
                        self.process.stdin.write("q\n")
                        self.process.stdin.flush()
                except Exception as e:
                    logger.debug("Graceful shutdown attempt failed: %s", e)

                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass

                # Stage 2: terminate.
                if self.process.poll() is None:
                    logger.warning("CDB did not exit gracefully, terminating (pid=%s)", self.process.pid)
                    try:
                        self.process.terminate()
                    except Exception as e:
                        logger.debug("terminate() raised: %s", e)
                    try:
                        self.process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        # Stage 3: hard kill.
                        logger.error(
                            "CDB did not exit after terminate, killing (pid=%s)", self.process.pid,
                        )
                        try:
                            self.process.kill()
                        except Exception as e:
                            logger.debug("kill() raised: %s", e)
                        try:
                            self.process.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            logger.error(
                                "CDB still alive after kill -- leaking pid=%s",
                                self.process.pid,
                            )

                logger.info("CDB process exited with code=%s", self.process.returncode)
        except Exception as e:
            logger.exception("Error during CDB shutdown")
            if self.verbose:
                logger.debug("Error during shutdown: %s", e)
        finally:
            # Best-effort: let the reader thread finish so it releases its
            # reference to stdout. Bounded so atexit cannot stall forever.
            try:
                if self.reader_thread and self.reader_thread.is_alive():
                    self.reader_thread.join(timeout=2)
            except Exception:
                pass
            self.process = None

    def send_ctrl_break(self) -> None:
        """Send a CTRL+BREAK event to the CDB process to break in.

        Only valid for remote-debugging sessions, because only those are
        launched with CREATE_NEW_PROCESS_GROUP. In dump-file mode, sending
        CTRL+BREAK_EVENT to a process that shares the parent's console group
        would propagate to the MCP server itself and could kill it.

        Raises:
            CDBError: If the signal cannot be delivered, the process is not
                running, or the session is not a remote session.
        """
        if not self.process or self.process.poll() is not None:
            raise CDBError("CDB process is not running")

        if not self.remote_connection:
            raise CDBError(
                "send_ctrl_break is only supported for remote debugging sessions; "
                "this session is attached to a dump file and was not launched "
                "with a dedicated process group, so CTRL+BREAK could affect the "
                "MCP server itself."
            )

        try:
            # On Windows, deliver CTRL+BREAK to the new process group we created
            logger.info("Sending CTRL+BREAK to CDB (pid=%s)", self.process.pid)
            self.process.send_signal(signal.CTRL_BREAK_EVENT)
            # Treat ctrl+break as user-driven activity: don't let the idle
            # reaper kill the session right after the user just interrupted it.
            self.last_activity_at = time.monotonic()
        except Exception as e:
            logger.exception("Failed to send CTRL+BREAK")
            raise CDBError(f"Failed to send CTRL+BREAK: {str(e)}")

    def get_session_id(self) -> str:
        """Get a unique identifier for this CDB session."""
        if self.dump_path:
            return os.path.abspath(self.dump_path)
        elif self.remote_connection:
            return f"remote:{self.remote_connection}"
        else:
            raise CDBError("Session has no valid identifier")

    def __enter__(self):
        """Support for context manager protocol"""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Clean up when exiting context manager"""
        self.shutdown()

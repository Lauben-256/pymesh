"""
PyMesh Chat — Entry Point (Phase 5)
Defaults to the full split-pane TUI.
Use --simple for the plain terminal fallback.
"""

import argparse
import asyncio
import getpass
import logging
import sys
import os
import platform


def _configure_windows() -> None:
    """
    Apply all Windows-specific fixes before anything else runs.
    Must be called at the very top of main before any asyncio or curses use.
    """
    if platform.system() != "Windows":
        return

    # Fix 1: Force UTF-8 output so box-drawing chars and emoji don't crash
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    # Fix 2: Windows Python 3.8+ defaults to ProactorEventLoop which
    # doesn't support some operations we use. Force SelectorEventLoop.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # type: ignore[attr-defined]


# Apply Windows fixes immediately at import time
_configure_windows()


from pymesh.crypto.identity import get_or_create_identity, identity_exists, IdentityError
from pymesh.utils.constants import (
    APP_NAME, APP_VERSION,
    DEFAULT_PORT, DEFAULT_INACTIVITY_TIMEOUT,
    LOG_FORMAT, LOG_DATE_FORMAT,
)


def setup_logging(verbose: bool) -> None:
    level   = logging.DEBUG if verbose else logging.WARNING
    # In TUI mode log to a file so we don't corrupt curses display
    handler = (logging.FileHandler(os.path.join(os.path.expanduser("~"), ".pymesh", "pymesh.log"))
               if not verbose else logging.StreamHandler(sys.stderr))
    logging.basicConfig(level=level, format=LOG_FORMAT,
                        datefmt=LOG_DATE_FORMAT, handlers=[handler])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pymesh",
        description=f"{APP_NAME} v{APP_VERSION} — Secure P2P Terminal Chat",
    )
    parser.add_argument("--alias",    "-a", required=True)
    parser.add_argument("--session",  "-s", default="default")
    parser.add_argument("--port",     "-p", type=int, default=DEFAULT_PORT)
    parser.add_argument("--connect",  "-c", metavar="HOST[:PORT]")
    parser.add_argument("--identity", "-i", metavar="FILE")
    parser.add_argument("--timeout",  "-t", type=int, default=DEFAULT_INACTIVITY_TIMEOUT)
    parser.add_argument("--downloads","-d", metavar="DIR")
    parser.add_argument("--simple",   action="store_true",
                        help="Use plain terminal UI instead of full TUI")
    parser.add_argument("--verbose",  "-v", action="store_true")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    setup_logging(args.verbose)

    # ── Identity ──────────────────────────────────────────────────────────────
    print()
    if args.identity:
        import pymesh.utils.constants as C
        C.IDENTITY_FILE = args.identity

    if identity_exists():
        print("  [*]  Identity found. Enter your passphrase to unlock it.")
    else:
        print("  [*]  No identity found. A new keypair will be created.")
        print("       Choose a strong passphrase to protect your private key.")
        print()

    try:
        passphrase = getpass.getpass("  Passphrase: ")
        if not passphrase:
            print("  [ERROR] Passphrase cannot be empty.")
            return
        if not identity_exists():
            confirm = getpass.getpass("  Confirm passphrase: ")
            if passphrase != confirm:
                print("  [ERROR] Passphrases do not match.")
                return
        identity = get_or_create_identity(passphrase)
    except IdentityError as exc:
        print(f"\n  [ERROR] {exc}")
        return
    except KeyboardInterrupt:
        print("\n  Cancelled.")
        return

    print(f"\n  [OK]  Identity loaded  --  fingerprint: {identity.fingerprint[:32]}...")
    print()

    # ── Node ──────────────────────────────────────────────────────────────────
    from pymesh.core.node import Node
    node = Node(
        alias=args.alias,
        session_name=args.session,
        identity=identity,
        port=args.port,
        inactivity_timeout=args.timeout,
        **({"download_dir": args.downloads} if args.downloads else {}),
    )
    await node.start()

    # Parse --connect
    connect_target = None
    if args.connect:
        host = args.connect
        port = DEFAULT_PORT
        if ":" in host:
            host, port_str = host.rsplit(":", 1)
            try:
                port = int(port_str)
            except ValueError:
                print(f"  [ERROR] Invalid port in --connect: {port_str}")
                await node.stop()
                return
        connect_target = (host, port)

    # ── Launch UI ─────────────────────────────────────────────────────────────
    # macOS/Linux: use curses TUI unless --simple is passed
    # Windows:     always use AnsiUI (ANSI color via SetConsoleMode)
    #              curses cannot initialize on Windows even with windows-curses
    use_tui = _tui_available() and not args.simple

    try:
        if use_tui:
            from pymesh.ui.tui import TUI
            ui = TUI(node, connect_on_start=connect_target)
            await ui.run()
        else:
            # AnsiTUI: split-pane ANSI UI for Windows and --simple on macOS/Linux
            from pymesh.ui.terminal import AnsiTUI
            ui = AnsiTUI(node, connect_on_start=connect_target)
            await ui.run()
    except KeyboardInterrupt:
        pass
    finally:
        await node.stop()


def _tui_available() -> bool:
    """
    Return True only when the curses TUI can actually initialize a screen.

    curses requires a real Unix PTY to call initscr(). On Windows this is
    never available — even with windows-curses installed, curses.wrapper()
    crashes with '_curses.error: setupterm: could not find terminal'.

    Windows always gets the AnsiUI path, which uses ANSI escape codes
    enabled via SetConsoleMode (works in cmd.exe, PowerShell, Windows Terminal).
    """
    if platform.system() == "Windows":
        return False          # curses cannot init on Windows — use AnsiUI
    try:
        import curses
        import _curses  # noqa: F401  — confirms the C extension is present
        return hasattr(curses, "wrapper")
    except ImportError:
        return False


async def _delayed_connect(node, host: str, port: int) -> None:
    await asyncio.sleep(0.5)
    ok = await node.connect_to(host, port)
    if not ok:
        print(f"\n  [WARN] Could not connect to {host}:{port}")


def run() -> None:
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()

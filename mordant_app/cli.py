"""
Mordant: media viewer for images and videos which can save media to multiple
selected directories at once

Usage:
    mordant [options] [--status-messages | --no-status-messages] [--] [<path>...]
    mordant (-h | --help)
    mordant --version

Options:
    -h --help                       Show this help text.
    --version                       Show the version.
    --config-directory=<directory>  Use the specified configuration directory.
    --max-recents=<count>           Maximum recent-directory buttons [default: unlimited].
    --status-messages               Show transfer status messages (the default).
    --no-status-messages            Hide transfer status messages.
    --gapplication-service          Run as a D-Bus application service.

Files and directories are accepted. A single file also opens its neighbouring
media. With no paths, the current directory is opened. Use -- before paths
whose names start with a hyphen.

Configuration defaults to $XDG_CONFIG_HOME/mordant or ~/.config/mordant.
Help and version output do not require a graphical display.
"""

from pathlib import Path
import sys

from docopt import DocoptExit, docopt

from . import __VERSION__


def parse_max_recents(value: str) -> int | None:
    """Convert the docopt value into a positive limit or no limit."""

    if value.casefold() == "unlimited":
        return None
    try:
        limit = int(value)
    except ValueError as error:
        raise ValueError(
            "--max-recents must be a positive integer or 'unlimited'"
        ) from error
    if limit < 1:
        raise ValueError("--max-recents must be a positive integer or 'unlimited'")
    return limit


def main(argv=None):
    try:
        options = docopt(__doc__, argv=argv, version=f"mordant {__VERSION__}")
    except DocoptExit as error:
        print(str(error), file=sys.stderr)
        return 2
    try:
        max_recents = parse_max_recents(options["--max-recents"])
    except ValueError as error:
        print(f"mordant: {error}", file=sys.stderr)
        return 2
    try:
        from gi.repository import Gio
        from .app import Gdk, MordantApplication
    except (ImportError, ValueError) as error:
        print(f"mordant: missing dependency: {error}.", file=sys.stderr)
        return 1
    if Gdk.Display.get_default() is None:
        print("mordant: no graphical display is available. A desktop session is required.", file=sys.stderr)
        return 1
    directory = options["--config-directory"]
    service_mode = options["--gapplication-service"]
    try:
        app = MordantApplication(
            Path(directory) if directory is not None else None,
            status_messages=not options["--no-status-messages"],
            single_instance=service_mode,
            max_recents=max_recents,
        )
    except (OSError, TypeError, ValueError) as error:
        print(f"mordant: could not use configuration directory: {error}", file=sys.stderr)
        return 1
    if service_mode:
        return app.run(["mordant", "--gapplication-service"])
    try:
        app.register(None)
        files = [Gio.File.new_for_path(str(Path(path).expanduser().resolve()))
                 for path in (options["<path>"] or [Path.cwd()])]
        # NON_UNIQUE makes this a local open signal for this Mordant process.
        app.open(files, "")
        return app.run(["mordant"])
    except Exception as error:
        print(f"mordant: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

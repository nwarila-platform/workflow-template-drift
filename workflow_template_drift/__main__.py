"""The command line: read the arguments, run the check, print the report.

Exit status: 0 the repository passes, 1 it has drifted, 2 the check could not be carried out.
"""

import argparse
import os
import re
import sys
from pathlib import Path

from .checker import DriftError, check_repository

LABEL = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


def _template(value: str) -> tuple[str, Path]:
    """Split one ``--template`` value, ``OWNER/REPO=DIRECTORY``, into its label and directory."""
    label, _, directory = value.partition("=")
    if not (LABEL.fullmatch(label) and directory):
        raise argparse.ArgumentTypeError(f"expected OWNER/REPO=DIRECTORY, got {value!r}")
    return label, Path(directory)


def _write(report: str) -> None:
    """Write the report to standard output directly, without Python's output buffer.

    A buffered write that fails is only discovered while the interpreter exits, after the exit
    status has been chosen. Written directly, a report that cannot be delivered is an error here.
    """
    data = report.encode()
    while data:
        written = os.write(sys.stdout.fileno(), data)
        if written == 0:
            raise OSError("could not write the report")
        data = data[written:]


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="template-drift",
        description="Report where a repository has drifted from the templates it follows.",
        allow_abbrev=False,  # every option must be written in full
    )
    parser.add_argument(
        "--workspace",
        required=True,
        type=Path,
        metavar="DIRECTORY",
        help="the repository to check",
    )
    parser.add_argument(
        "--template",
        required=True,
        action="append",
        type=_template,
        metavar="OWNER/REPO=DIRECTORY",
        help="a template's checkout and the name to report it under; repeat for each template",
    )
    parser.add_argument(
        "--fail-on",
        choices=("error", "warning"),
        default="error",
        help="the lowest severity that fails the check (default: error)",
    )
    parser.add_argument(
        "--format",
        choices=("text",),
        default="text",
        help="the report format; text is the only one, and the organization's runner passes it",
    )
    arguments = parser.parse_args()

    # Status 1 means "the repository has drifted", and Python itself exits with 1 on an uncaught
    # exception. Every failure, including a failure to write the report, is therefore caught here
    # and leaves with status 2 instead.
    try:
        report, status = check_repository(
            arguments.workspace, arguments.template, arguments.fail_on
        )
        _write(report)
    except Exception as error:
        detail = str(error) if isinstance(error, DriftError) else f"{type(error).__name__}: {error}"
        print(f"template-drift: error: {detail}", file=sys.stderr)
        return 2
    return status


if __name__ == "__main__":
    sys.exit(main())

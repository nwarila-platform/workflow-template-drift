"""Command-line entry point with a JSON-only completion contract."""

from __future__ import annotations

import argparse
import sys

from .engine import EvaluationError, _error_result, canonical_bytes, evaluate


class JsonParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise EvaluationError("invalid_usage", "invalid command-line arguments")


def _parser() -> JsonParser:
    parser = JsonParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--fail-on", required=True, choices=("error", "warning"))
    parser.add_argument("--format", required=True, choices=("json",))
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        result, exit_code = evaluate(args.workspace, args.source, args.config, args.fail_on)
    except EvaluationError as error:
        result = _error_result(error)
        exit_code = 2
    except Exception:
        result = _error_result(EvaluationError("internal_error", "unexpected internal evaluator error"))
        exit_code = 2
    sys.stdout.buffer.write(canonical_bytes(result))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

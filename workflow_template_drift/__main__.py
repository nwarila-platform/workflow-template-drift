"""Command-line entry point for deterministic text and patch output."""
from __future__ import annotations
import argparse
import re
import sys
from .engine import EvaluationError, _error_result, error_bytes, evaluate, patch_bytes, text_bytes
TEMPLATE_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z", re.ASCII)
class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise EvaluationError("invalid_usage", "invalid command-line arguments")
def _parser() -> Parser:
    parser = Parser(add_help=False, allow_abbrev=False)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--template", required=True, action="append")
    parser.add_argument("--config", default="template-drift.json")
    parser.add_argument("--fail-on", default="error", choices=("error", "warning"))
    parser.add_argument("--format", choices=("text", "patch"), default="text")
    return parser
def _templates(values: list[str]) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for value in values:
        label, separator, root = value.partition("=")
        key = label.lower()
        if not separator or not root or TEMPLATE_RE.fullmatch(label) is None or key in seen:
            raise EvaluationError("invalid_usage", "invalid command-line arguments")
        seen.add(key)
        result.append((label, root))
    return result
def main(argv: list[str] | None = None) -> int:
    output_format = "text"
    try:
        args = _parser().parse_args(argv)
        output_format = args.format
        result, exit_code = evaluate(args.workspace, _templates(args.template), args.config, args.fail_on)
    except EvaluationError as error:
        result = _error_result(error)
        exit_code = 2
    except Exception:
        result = _error_result(EvaluationError("internal_error", "unexpected internal evaluator error"))
        exit_code = 2
    if exit_code == 2:
        sys.stderr.buffer.write(error_bytes(result))
    else:
        sys.stdout.buffer.write(patch_bytes(result) if output_format == "patch" else text_bytes(result))
    return exit_code
if __name__ == "__main__":
    raise SystemExit(main())

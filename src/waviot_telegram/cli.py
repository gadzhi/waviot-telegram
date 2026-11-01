"""CLI for preparing and collecting a read-only audit."""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .audit import collect_audit, write_audit
from .config import load_settings, node_id
from .waviot_client import WaviotClient


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Waviot audit. No Telegram messages or meter changes.")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="Validate local configuration without network calls.")
    for name in ("discover", "sample"):
        command = commands.add_parser(name, help="Collect tree and optional device observations.")
        command.add_argument("--element-id", action="append", type=node_id, default=[])
        command.add_argument("--expected-scope", type=Path)
        command.add_argument("--output-dir", type=Path)
        if name == "sample":
            command.add_argument("--modem-id", required=True)
            command.add_argument("--channel", required=True)
            command.add_argument("--days", type=int, default=7)
            command.add_argument("--include-balance", action="store_true")
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.env_file)
        if args.command == "check":
            print("API key: configured" if settings.api_key else "API key: missing (set WAVIOT_API_KEY in local .env)")
            print("House scope: configured" if settings.house_id else "House scope: not selected; discovery cannot prove full-house coverage")
            print("Network calls: none. Live alerts: disabled.")
            return 0 if settings.api_key else 2
        if not settings.api_key:
            print("WAVIOT_API_KEY is missing. Put it in local .env, not in command arguments or chat.", file=sys.stderr)
            return 2
        expected = json.loads(args.expected_scope.read_text()) if args.expected_scope else None
        output = args.output_dir or Path("artifacts") / datetime.now(timezone.utc).strftime("audit-%Y%m%dT%H%M%S%fZ")
        if output.exists():
            raise ValueError("Output directory already exists; choose a new directory.")
        with WaviotClient(settings.api_key, timeout=settings.timeout) as client:
            report = collect_audit(
                settings, client, element_ids=args.element_id, expected_scope=expected,
                modem_id=getattr(args, "modem_id", None), channel=getattr(args, "channel", None),
                days=getattr(args, "days", 7), include_balance=getattr(args, "include_balance", False),
            )
        write_audit(report, output, secrets=(settings.api_key,))
        print(f"Audit observations saved: {output.resolve()}")
        print("Live alerts: disabled. Review report.md and evidence.json before drawing conclusions.")
        return 2 if report["errors"] or report["coverage"]["status"] == "missing" else 0
    except (ValueError, OSError):
        print("Audit configuration/output error. Check .env, scope JSON, IDs, history window and output directory; no credentials are displayed.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

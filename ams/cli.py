"""`ams` command line."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ams.llm import get_llm, load_dotenv
from ams.mapping import build_plan, choose_pair
from ams.parsers import parse_service
from ams.pipeline import run
from ams.report import rerender
from ams.security import scan


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ams", description="Automated Microservice Synthesis")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("parse", help="Phase 1 only: print a service's normalized schema")
    p.add_argument("service", type=Path)

    m = sub.add_parser("map", help="Phases 1-2: print the field mapping between two services")
    m.add_argument("--provider", type=Path, required=True)
    m.add_argument("--consumer", type=Path, required=True)
    m.add_argument("--no-llm", action="store_true")

    r = sub.add_parser("run", help="full pipeline: parse, map, generate, validate, scan")
    r.add_argument("--provider", type=Path, required=True, help="service that exposes the API")
    r.add_argument("--consumer", type=Path, required=True, help="service that needs to call it")
    r.add_argument("--out", type=Path, default=Path("ams-out"))
    r.add_argument("--pair", help="force the entity pair, e.g. Order:OrderRecord")
    r.add_argument("--no-llm", action="store_true", help="deterministic mapping and repair only")
    r.add_argument("--no-scan", action="store_true", help="skip the security scanners")
    r.add_argument("--apply", action="store_true", help="copy the verified layer into the consumer")

    s = sub.add_parser("scan", help="run the five security scanners on a directory")
    s.add_argument("target", type=Path)

    rp = sub.add_parser("report", help="rebuild the Markdown + HTML reports from a run's ams-report.json")
    rp.add_argument("out", type=Path, nargs="?", default=Path("ams-out"))
    rp.add_argument("--open", action="store_true", help="open the HTML report in a browser")

    sub.add_parser("llm", help="show which LLM provider is configured and check that the key works")

    args = ap.parse_args(argv)
    load_dotenv()
    if args.cmd == "llm":
        return _check_llm()
    if args.cmd == "parse":
        print(json.dumps(parse_service(args.service).to_dict(), indent=2))
        return 0
    if args.cmd == "map":
        prov, cons = parse_service(args.provider), parse_service(args.consumer)
        plan = build_plan(prov, cons, *choose_pair(prov, cons), get_llm(disabled=args.no_llm))
        print(json.dumps(plan.to_dict(), indent=2))
        return 0
    if args.cmd == "scan":
        results = scan(args.target)
        print(json.dumps([x.to_dict() for x in results], indent=2))
        return 1 if any(x.status in ("findings", "error") for x in results) else 0
    if args.cmd == "report":
        md_path, html_path = rerender(args.out)
        print(f"wrote {md_path.as_posix()} and {html_path.as_posix()}")
        if args.open:
            import webbrowser

            webbrowser.open(html_path.resolve().as_uri())
        return 0

    pair = tuple(args.pair.split(":", 1)) if args.pair else None
    report = run(args.provider, args.consumer, args.out, pair, use_llm=not args.no_llm,
                 scanners=not args.no_scan, apply=args.apply)
    print(f"\n{'PASS' if report.passed else 'FAIL'}: report at {(args.out / 'ams-report.html').as_posix()}")
    return 0 if report.passed else 1


def _check_llm() -> int:
    llm = get_llm()
    if llm is None:
        print("no LLM configured: set GEMINI_API_KEY, GROQ_API_KEY or ANTHROPIC_API_KEY (environment or .env)")
        return 1
    answer = llm.complete_json("You check that an API key works.", 'Return {"ok": true}.')
    if answer and answer.get("ok") is True:
        print(f"{llm.name}: ok")
        return 0
    print(f"{llm.name}: FAILED ({llm.last_error or answer})")
    return 1


if __name__ == "__main__":
    sys.exit(main())

"""Orchestrator: runs the four phases plus the security gate and writes the report."""

from __future__ import annotations

import json
import shutil
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ams.generate import generate
from ams.llm import get_llm
from ams.mapping import MappingPlan, build_plan, choose_pair
from ams.parsers import parse_service
from ams.security import ScanResult, scan
from ams.validate import E2EResult, HealResult, end_to_end, heal


@dataclass
class Report:
    provider: str
    consumer: str
    plan: MappingPlan
    generated: list[str]
    heal: HealResult
    e2e: E2EResult
    security: list[ScanResult]
    timings: dict[str, float] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        # a scanner that errored has not shown the code is clean, so it fails the gate too
        scans_ok = not any(s.status in ("findings", "error") for s in self.security)
        return self.heal.passed and self.e2e.passed and scans_ok

    def to_dict(self) -> dict:
        return {
            "passed": self.passed, "provider": self.provider, "consumer": self.consumer,
            "mapping": self.plan.to_dict(), "generated": self.generated, "heal": asdict(self.heal),
            "e2e": self.e2e.to_dict(), "security": [s.to_dict() for s in self.security], "timings_s": self.timings,
        }


def run(provider_root: Path, consumer_root: Path, out_dir: Path, pair: tuple[str, str] | None = None,
        use_llm: bool = True, scanners: bool = True, apply: bool = False,
        log: Callable[[str], None] = print) -> Report:
    out_dir.mkdir(parents=True, exist_ok=True)
    llm = get_llm(disabled=not use_llm)
    timings: dict[str, float] = {}

    def phase(name: str):
        start = time.perf_counter()
        return lambda: timings.__setitem__(name, round(time.perf_counter() - start, 3))

    done = phase("1_parse")
    provider = parse_service(provider_root)
    consumer = parse_service(consumer_root)
    (out_dir / "schemas").mkdir(exist_ok=True)
    for svc in (provider, consumer):
        (out_dir / "schemas" / f"{svc.name}.json").write_text(json.dumps(svc.to_dict(), indent=2), encoding="utf8")
    done()
    log(f"[1/4] parsed {provider.name} ({provider.language}/{provider.framework}): "
        f"{len(provider.entities)} entities, {len(provider.endpoints)} endpoints; "
        f"{consumer.name} ({consumer.language}/{consumer.framework}): {len(consumer.entities)} entities")

    done = phase("2_map")
    if pair:
        p_ent, c_ent = provider.entity(pair[0]), consumer.entity(pair[1])
        if p_ent is None or c_ent is None:
            raise ValueError(f"unknown entity pair {pair}")
    else:
        p_ent, c_ent = choose_pair(provider, consumer)
    plan = build_plan(provider, consumer, p_ent, c_ent, llm)
    (out_dir / "mapping.json").write_text(json.dumps(plan.to_dict(), indent=2), encoding="utf8")
    done()
    log(f"[2/4] mapped {p_ent.name} -> {c_ent.name} with {plan.engine}: "
        f"{len(plan.pairs)} fields, coverage {plan.coverage:.0%}"
        + (f", unmapped: {', '.join(plan.unmapped_consumer)}" if plan.unmapped_consumer else ""))

    done = phase("3_generate")
    gen = generate(provider, consumer, plan, out_dir)
    done()
    log(f"[3/4] generated {gen.client_file.relative_to(out_dir).as_posix()}, "
        f"{gen.compose_file.name}, {gen.requirements_file.name}")

    done = phase("4_validate")
    healed = heal([gen.client_file, gen.package_dir / "__init__.py"], llm)
    if healed.passed:
        e2e = end_to_end(provider, consumer, plan, gen, out_dir)
    else:
        e2e = E2EResult(False, [], [], "compile failed")
    done()
    ok_checks = sum(c.ok for c in e2e.checks)
    log(f"[4/4] compile {'ok' if healed.passed else 'FAILED'} after {len(healed.rounds)} heal round(s); "
        f"end-to-end {'ok' if e2e.passed else 'FAILED'} ({ok_checks}/{len(e2e.checks)} field checks)"
        + (f": {e2e.error}" if e2e.error else ""))

    results: list[ScanResult] = []
    if scanners:
        done = phase("security")
        results = scan(out_dir, gen.files)
        done()
        log("[sec] " + ", ".join(f"{r.tool}: {r.status}" + (f" ({r.findings})" if r.findings else "") for r in results))
        for r in results:
            for item in r.items[:20]:
                log(f"      {r.tool}: {item}")

    if apply and healed.passed and e2e.passed:
        dest = Path(consumer.root) / "ams_generated"
        shutil.copytree(gen.package_dir, dest, dirs_exist_ok=True)
        log(f"[apply] copied integration layer into {dest}")

    report = Report(provider.name, consumer.name, plan, [p.relative_to(out_dir).as_posix() for p in gen.files],
                    healed, e2e, results, timings)
    (out_dir / "ams-report.json").write_text(json.dumps(report.to_dict(), indent=2, default=str), encoding="utf8")
    (out_dir / "ams-report.md").write_text(markdown(report), encoding="utf8")
    return report


def markdown(r: Report) -> str:
    lines = [f"# AMS report: {r.consumer} -> {r.provider}", "",
             f"**Result:** {'PASS' if r.passed else 'FAIL'}  ", f"**Mapping engine:** {r.plan.engine}", "",
             f"## Field mapping ({r.plan.provider_entity} -> {r.plan.consumer_entity})", "",
             "| Consumer field | Provider field (wire) | Confidence | Source |", "|---|---|---|---|"]
    lines += [f"| `{p.consumer_field}` | `{p.provider_json}` | {p.confidence:.2f} | {p.source} |" for p in r.plan.pairs]
    lines += [f"| `{f}` | _unmapped_ | | |" for f in r.plan.unmapped_consumer]
    lines += ["", "## Validation", "",
              f"- Compile: {'pass' if r.heal.passed else 'fail'} ({len(r.heal.rounds)} heal round(s))"]
    lines += [f"  - round {h.round}: {h.action} ({len(h.diagnostics)} diagnostic(s))" for h in r.heal.rounds]
    checks = f"{sum(c.ok for c in r.e2e.checks)}/{len(r.e2e.checks)} field checks"
    lines += [f"- End-to-end: {'pass' if r.e2e.passed else 'fail'}, {checks}, requests: {', '.join(r.e2e.requests)}"]
    if r.security:
        lines += ["", "## Security (delta-scoped, parallel)", "", "| Scanner | Status | Findings |", "|---|---|---|"]
        lines += [f"| {s.tool} | {s.status} | {s.findings if s.status != 'skipped' else '-'} |" for s in r.security]
        details = [f"- **{s.tool}** ({s.status}): {s.detail}" for s in r.security if s.detail]
        details += [f"- `{s.tool}`: {item}" for s in r.security for item in s.items]
        if details:
            lines += ["", *details]
    return "\n".join(lines) + "\n"

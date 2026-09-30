"""Phase 4: self-healing compile loop + end-to-end payload verification.

1. Compile: byte-compile every generated module and lint it (Ruff: syntax errors + pyflakes).
2. Heal: apply Ruff's safe autofixes; anything left goes to the LLM with the exact
   diagnostics as context. Repeat until clean or the round budget runs out.
3. End-to-end: serve a mock of the provider built from its *parsed* schema, run the
   generated client against it in a separate process, and check every mapped field
   arrives in the consumer model with the provider's value.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ams.generate import Generated
from ams.llm import LLM
from ams.mapping import MappingPlan
from ams.schema import Entity, ServiceSchema


@dataclass
class Diagnostic:
    file: str
    line: int
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.file}:{self.line}: {self.code} {self.message}"


@dataclass
class HealRound:
    round: int
    diagnostics: list[str]
    action: str


@dataclass
class HealResult:
    passed: bool
    rounds: list[HealRound] = field(default_factory=list)


def compile_check(files: list[Path]) -> list[Diagnostic]:
    diags = []
    for f in files:
        try:
            compile(f.read_text(encoding="utf8"), str(f), "exec")
        except SyntaxError as e:
            diags.append(Diagnostic(f.name, e.lineno or 0, "SyntaxError", e.msg))
    if diags:
        return diags
    proc = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--select", "E9,F", "--output-format", "json", *map(str, files)],
        capture_output=True, text=True, check=False,
    )
    for item in json.loads(proc.stdout or "[]"):
        name, row = Path(item["filename"]).name, item["location"]["row"]
        diags.append(Diagnostic(name, row, item["code"] or "E", item["message"]))
    return diags


def _ruff_fix(files: list[Path]) -> None:
    subprocess.run([sys.executable, "-m", "ruff", "check", "--select", "E9,F", "--fix", "--quiet", *map(str, files)],
                   capture_output=True, text=True, check=False)


HEAL_SYSTEM = (
    "You repair generated Python integration code. You get one file and the linter/compiler "
    "diagnostics for it. Return the complete corrected file only, no commentary. Change as little "
    "as possible and never change public names, field aliases or the mapping."
)


def heal(files: list[Path], llm: LLM | None, max_rounds: int = 3) -> HealResult:
    result = HealResult(passed=False)
    for n in range(1, max_rounds + 1):
        diags = compile_check(files)
        if not diags:
            result.passed = True
            return result
        _ruff_fix(files)
        remaining = compile_check(files)
        if not remaining:
            result.rounds.append(HealRound(n, [str(d) for d in diags], "ruff --fix resolved all diagnostics"))
            continue
        if llm is None:
            result.rounds.append(HealRound(n, [str(d) for d in remaining], "no LLM configured; cannot repair further"))
            return result
        for f in files:
            file_diags = [str(d) for d in remaining if d.file == f.name]
            if not file_diags:
                continue
            fixed = llm.complete_text(HEAL_SYSTEM, f"File {f.name}:\n```python\n{f.read_text(encoding='utf8')}\n```\n"
                                                   f"Diagnostics:\n" + "\n".join(file_diags))
            if fixed:
                code = re.sub(r"^```(?:python)?\s*|\s*```\s*$", "", fixed.strip())
                f.write_text(code + "\n", encoding="utf8")
        result.rounds.append(HealRound(n, [str(d) for d in remaining], f"LLM repair ({llm.name})"))
    result.passed = not compile_check(files)
    return result


# ---------------------------------------------------------------- end-to-end


def sample_value(ctype: str, provider: ServiceSchema, name: str, depth: int = 0):
    seed = sum(ord(c) for c in name)
    if ctype.startswith("list<"):
        return [sample_value(ctype[5:-1], provider, name, depth + 1)]
    if ctype.startswith("ref:"):
        ent = provider.entity(ctype[4:])
        if ent is None:
            return None
        if ent.kind == "enum":
            return ent.values[seed % len(ent.values)] if ent.values else None
        return sample_entity(ent, provider, depth + 1) if depth < 3 else None
    return {
        "string": f"{name}-{seed % 97}",
        "integer": 1000 + seed % 9000,
        "decimal": round((seed % 10000) / 100 + 0.49, 2),
        "boolean": seed % 2 == 0,
        "datetime": f"2026-09-{1 + seed % 28:02d}T{seed % 24:02d}:15:30",
        "date": f"2026-09-{1 + seed % 28:02d}",
        "uuid": "3f2c9a1e-7b4d-4e8a-9c21-5d6f0a1b2c3d",
    }.get(ctype, {})


def sample_entity(ent: Entity, provider: ServiceSchema, depth: int = 0) -> dict:
    return {f.json_name: sample_value(f.type, provider, f.name, depth) for f in ent.fields}


class MockProvider:
    """HTTP mock of the provider, answering each parsed endpoint with a schema-shaped payload."""

    def __init__(self, provider: ServiceSchema) -> None:
        self.hits: list[str] = []
        routes = []
        for ep in provider.endpoints:
            pattern = "^" + re.sub(r"\{\w+\}", r"[^/]+", ep.path) + "/?$"
            ret = ep.returns or ""
            is_list = ret.startswith("list<")
            ent = provider.entity(ret[5:-1] if is_list else ret)
            body = sample_entity(ent, provider) if ent is not None and ent.kind != "enum" else {}
            routes.append((ep.method, re.compile(pattern), [body] if is_list else body))
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self) -> None:
                path = self.path.split("?")[0]
                for method, rx, payload in routes:
                    if method == self.command and rx.match(path):
                        mock.hits.append(f"{self.command} {self.path}")
                        raw = json.dumps(payload).encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(raw)))
                        self.end_headers()
                        self.wfile.write(raw)
                        return
                self.send_response(404)
                self.end_headers()

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _serve

            def log_message(self, *args: object) -> None:  # keep test output quiet
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self) -> MockProvider:
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()


CHILD = """
import inspect, json, sys
from {module} import {client}
out = {{}}
with {client}(base_url=sys.argv[1], retries=0) as client:
    for name in {methods!r}:
        method = getattr(client, name)
        kwargs = {{p.name: 1 for p in inspect.signature(method).parameters.values() if p.default is p.empty}}
        result = method(**kwargs)
        record = result[0] if isinstance(result, list) else result
        out[name] = record.model_dump(mode="json")
print(json.dumps(out))
"""


def _same(expected, actual) -> bool:
    if expected is None or actual is None:
        return expected == actual
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        try:
            return Decimal(str(expected)) == Decimal(str(actual))
        except ArithmeticError:
            return False
    if isinstance(expected, str) and re.match(r"\d{4}-\d{2}-\d{2}T", expected):
        try:
            return datetime.fromisoformat(expected) == datetime.fromisoformat(str(actual).replace("Z", "+00:00"))
        except ValueError:
            return False
    return expected == actual


@dataclass
class Check:
    method: str
    consumer_field: str
    provider_json: str
    expected: object
    actual: object
    ok: bool


@dataclass
class E2EResult:
    passed: bool
    checks: list[Check]
    requests: list[str]
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def end_to_end(provider: ServiceSchema, consumer: ServiceSchema, plan: MappingPlan, gen: Generated,
               out_dir: Path) -> E2EResult:
    p_ent = provider.entity(plan.provider_entity)
    assert p_ent is not None
    expected = sample_entity(p_ent, provider)
    script = CHILD.format(module=gen.module, client=gen.client_class, methods=gen.mapped_methods)
    env_path = [str(Path(consumer.root).resolve()), str(out_dir.resolve())]
    with MockProvider(provider) as mock:
        proc = subprocess.run([sys.executable, "-c", script, mock.url], capture_output=True, text=True,
                              env={**os.environ, "PYTHONPATH": os.pathsep.join(env_path)}, timeout=60, check=False)
    if proc.returncode != 0:
        return E2EResult(False, [], mock.hits, proc.stderr.strip().splitlines()[-1] if proc.stderr else "failed")
    results = json.loads(proc.stdout.strip().splitlines()[-1])
    checks = []
    for method, record in results.items():
        for pair in plan.pairs:
            want, got = expected.get(pair.provider_json), record.get(pair.consumer_field)
            checks.append(Check(method, pair.consumer_field, pair.provider_json, want, got, _same(want, got)))
    return E2EResult(bool(checks) and all(c.ok for c in checks), checks, mock.hits)

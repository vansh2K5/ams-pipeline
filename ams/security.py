"""Security gate: five scanners run in parallel over only what AMS generated (delta scope).

Semgrep (code patterns), Gitleaks (secrets), OSV-Scanner (known-vulnerable dependencies),
Trivy (vulns + secrets + misconfig) and Checkov (IaC / container config). A scanner is only
reported clean when it actually examined something: not installed, or nothing in scope,
is "skipped" with the reason; empty or unreadable output is an "error". Findings are kept
as labels (rule or advisory id + location) so the report says what was found, not just how many.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

Parser = Callable[[str], list[str]]


@dataclass
class ScanResult:
    tool: str
    status: str  # clean | findings | skipped | error
    findings: int = 0
    detail: str = ""
    items: list[str] = field(default_factory=list)  # e.g. "GHSA-xxxx h11@0.14.0 (transitive)"

    def to_dict(self) -> dict:
        return asdict(self)


class NothingScanned(Exception):
    """The scanner ran but found nothing in scope to examine."""


def _load(text: str):
    """Parse a scanner's JSON output. Empty output is an error, never an implicit "clean"."""
    text = text.strip()
    if not text:
        raise ValueError("scanner produced no JSON output")
    return json.loads(text)


def _semgrep(target: Path, files: list[Path]) -> tuple[list[str], Parser]:
    rules = [arg for pack in ("p/python", "p/security-audit", "p/secrets") for arg in ("--config", pack)]
    cmd = ["semgrep", "scan", *rules, "--json", "--quiet", "--metrics", "off", *map(str, files)]

    def parse(out: str) -> list[str]:
        data = _load(out)
        if not data.get("paths", {}).get("scanned"):
            raise NothingScanned("no files scanned")
        return [f"{r['check_id']} {Path(r['path']).name}:{r['start']['line']}" for r in data.get("results", [])]

    return cmd, parse


def _gitleaks(target: Path, files: list[Path]) -> tuple[list[str], Parser]:
    report = Path(tempfile.mkdtemp()) / "gitleaks.json"
    cmd = ["gitleaks", "dir", str(target), "--no-banner", "--exit-code", "0",
           "--report-format", "json", "--report-path", str(report)]

    def parse(_: str) -> list[str]:
        if not report.exists():
            raise ValueError("gitleaks wrote no report")
        leaks = json.loads(report.read_text() or "[]")
        return [f"{f['RuleID']} {Path(f['File']).name}:{f['StartLine']}" for f in leaks]

    return cmd, parse


def _osv(target: Path, files: list[Path]) -> tuple[list[str], Parser]:
    # --no-ignore: the output dir is usually gitignored, which OSV-Scanner honours by default
    cmd = ["osv-scanner", "scan", "source", "--no-ignore", "--format", "json", "-r", str(target)]

    def parse(out: str) -> list[str]:
        data = _load(out)
        items = []
        for result in data.get("results", []):
            for pkg in result.get("packages", []):
                info = pkg.get("package", {})
                for vuln in pkg.get("vulnerabilities", []):
                    items.append(f"{vuln['id']} {info.get('name')}@{info.get('version')}")
        return items

    return cmd, parse


def _trivy(target: Path, files: list[Path]) -> tuple[list[str], Parser]:
    cmd = ["trivy", "fs", "--quiet", "--format", "json", "--scanners", "vuln,secret,misconfig", str(target)]

    def parse(out: str) -> list[str]:
        results = _load(out).get("Results") or []
        if not results:
            raise NothingScanned("no scannable targets")
        items = []
        for r in results:
            vulns = r.get("Vulnerabilities") or []
            items += [f"{v['VulnerabilityID']} {v['PkgName']}@{v['InstalledVersion']}" for v in vulns]
            items += [f"{s['RuleID']} {r['Target']}" for s in r.get("Secrets") or []]
            items += [f"{m['ID']} {r['Target']}" for m in r.get("Misconfigurations") or [] if m.get("Status") == "FAIL"]
        return items

    return cmd, parse


def _checkov(target: Path, files: list[Path]) -> tuple[list[str], Parser]:
    cmd = ["checkov", "-d", str(target), "-o", "json", "--quiet", "--compact"]

    def parse(out: str) -> list[str]:
        data = _load(out)
        reports = [r for r in (data if isinstance(data, list) else [data]) if isinstance(r, dict) and "results" in r]
        if not reports:
            raise NothingScanned("no IaC resources in scope")
        failed = [c for r in reports for c in r["results"].get("failed_checks", [])]
        return [f"{c['check_id']} {c.get('file_path', '')}" for c in failed]

    return cmd, parse


SCANNERS = {"semgrep": _semgrep, "gitleaks": _gitleaks, "osv-scanner": _osv, "trivy": _trivy, "checkov": _checkov}


def _run(tool: str, target: Path, files: list[Path], timeout: int) -> ScanResult:
    if shutil.which(tool) is None:
        return ScanResult(tool, "skipped", detail="not installed")
    cmd, parse = SCANNERS[tool](target, files)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=target, check=False)
    except subprocess.TimeoutExpired:
        return ScanResult(tool, "error", detail=f"timed out after {timeout}s")
    except OSError as e:
        return ScanResult(tool, "error", detail=str(e))
    stderr = (proc.stderr.strip().splitlines() or ["no output"])[-1][:200]
    # osv-scanner exits 1 when it finds vulns, checkov 1 on failed checks; both still print JSON
    if proc.returncode not in (0, 1):
        return ScanResult(tool, "error", detail=f"exit {proc.returncode}: {stderr}")
    try:
        items = parse(proc.stdout)
    except NothingScanned as e:
        return ScanResult(tool, "skipped", detail=str(e))
    except (ValueError, KeyError) as e:  # ValueError includes JSONDecodeError
        return ScanResult(tool, "error", detail=f"unreadable output ({e}): {stderr}")
    return ScanResult(tool, "findings" if items else "clean", len(items), items=items)


def scan(target: Path, files: list[Path] | None = None, tools: list[str] | None = None,
         timeout: int = 600) -> list[ScanResult]:
    """Scan `target` (the generated delta). All scanners run concurrently."""
    target = target.resolve()  # scanners run with cwd=target, so relative paths would resolve twice
    files = [f.resolve() for f in files] if files else [p for p in target.rglob("*") if p.is_file()]
    tools = tools or list(SCANNERS)
    with ThreadPoolExecutor(max_workers=len(tools)) as pool:
        return list(pool.map(lambda t: _run(t, target, files, timeout), tools))

"""Security gate: five scanners run in parallel over only what AMS generated (delta scope).

Semgrep (code patterns), Gitleaks (secrets), OSV-Scanner (known-vulnerable dependencies),
Trivy (vulns + secrets + misconfig) and Checkov (IaC / container config). A scanner that
is not installed is reported as skipped, never silently treated as clean.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class ScanResult:
    tool: str
    status: str  # clean | findings | skipped | error
    findings: int = 0
    detail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _load(text: str):
    text = text.strip()
    return json.loads(text) if text else {}


def _semgrep(target: Path, files: list[Path]) -> tuple[list[str], Callable[[str], int]]:
    cmd = ["semgrep", "scan", "--config", "auto", "--json", "--quiet", "--metrics", "off", *map(str, files)]
    return cmd, lambda out: len(_load(out).get("results", []))


def _gitleaks(target: Path, files: list[Path]) -> tuple[list[str], Callable[[str], int]]:
    report = Path(tempfile.mkdtemp()) / "gitleaks.json"
    cmd = ["gitleaks", "dir", str(target), "--no-banner", "--exit-code", "0",
           "--report-format", "json", "--report-path", str(report)]

    def count(_: str) -> int:
        return len(json.loads(report.read_text() or "[]")) if report.exists() else 0

    return cmd, count


def _osv(target: Path, files: list[Path]) -> tuple[list[str], Callable[[str], int]]:
    cmd = ["osv-scanner", "scan", "source", "--format", "json", "-r", str(target)]

    def count(out: str) -> int:
        data = _load(out)
        return sum(len(p.get("vulnerabilities", [])) for r in data.get("results", []) for p in r.get("packages", []))

    return cmd, count


def _trivy(target: Path, files: list[Path]) -> tuple[list[str], Callable[[str], int]]:
    cmd = ["trivy", "fs", "--quiet", "--format", "json", "--scanners", "vuln,secret,misconfig", str(target)]

    def count(out: str) -> int:
        results = _load(out).get("Results") or []
        return sum(len(r.get("Vulnerabilities") or []) + len(r.get("Secrets") or [])
                   + len([m for m in (r.get("Misconfigurations") or []) if m.get("Status") == "FAIL"])
                   for r in results)

    return cmd, count


def _checkov(target: Path, files: list[Path]) -> tuple[list[str], Callable[[str], int]]:
    cmd = ["checkov", "-d", str(target), "-o", "json", "--quiet", "--compact"]

    def count(out: str) -> int:
        data = _load(out)
        reports = data if isinstance(data, list) else [data]
        return sum(len(r.get("results", {}).get("failed_checks", [])) for r in reports if isinstance(r, dict))

    return cmd, count


SCANNERS = {"semgrep": _semgrep, "gitleaks": _gitleaks, "osv-scanner": _osv, "trivy": _trivy, "checkov": _checkov}


def _run(tool: str, target: Path, files: list[Path], timeout: int) -> ScanResult:
    if shutil.which(tool) is None:
        return ScanResult(tool, "skipped", detail="not installed")
    cmd, count = SCANNERS[tool](target, files)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=target, check=False)
        # osv-scanner exits 1 when it finds vulns, checkov 1 on failed checks; both still print JSON
        n = count(proc.stdout)
    except subprocess.TimeoutExpired:
        return ScanResult(tool, "error", detail=f"timed out after {timeout}s")
    except (json.JSONDecodeError, OSError) as e:
        return ScanResult(tool, "error", detail=f"{type(e).__name__}: {e}")
    if proc.returncode not in (0, 1) and n == 0:
        return ScanResult(tool, "error", detail=(proc.stderr.strip().splitlines() or ["failed"])[-1][:200])
    return ScanResult(tool, "findings" if n else "clean", n)


def scan(target: Path, files: list[Path] | None = None, tools: list[str] | None = None,
         timeout: int = 600) -> list[ScanResult]:
    """Scan `target` (the generated delta). All scanners run concurrently."""
    files = files or [p for p in target.rglob("*") if p.is_file()]
    tools = tools or list(SCANNERS)
    with ThreadPoolExecutor(max_workers=len(tools)) as pool:
        return list(pool.map(lambda t: _run(t, target, files, timeout), tools))

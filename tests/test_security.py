import subprocess
from pathlib import Path

from ams import security


def fake_tools(monkeypatch, stdout: str, returncode: int = 0):
    monkeypatch.setattr(security.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs["cwd"]))
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="boom")

    monkeypatch.setattr(security.subprocess, "run", run)
    return calls


def test_missing_scanner_is_skipped_not_clean(monkeypatch, tmp_path):
    monkeypatch.setattr(security.shutil, "which", lambda tool: None)
    (result,) = security.scan(tmp_path, tools=["trivy"])
    assert result.status == "skipped"


def test_empty_output_is_an_error_not_clean(monkeypatch, tmp_path):
    fake_tools(monkeypatch, stdout="")
    results = security.scan(tmp_path, tools=["semgrep", "trivy", "checkov", "osv-scanner"])
    assert {r.status for r in results} == {"error"}


def test_gitleaks_without_report_is_an_error(monkeypatch, tmp_path):
    fake_tools(monkeypatch, stdout="")
    (result,) = security.scan(tmp_path, tools=["gitleaks"])
    assert result.status == "error"


def test_findings_are_counted(monkeypatch, tmp_path):
    out = '{"paths": {"scanned": ["x.py"]}, "results": [{"check_id": "a"}, {"check_id": "b"}]}'
    fake_tools(monkeypatch, stdout=out, returncode=1)
    (result,) = security.scan(tmp_path, tools=["semgrep"])
    assert (result.status, result.findings) == ("findings", 2)


def test_scanning_nothing_is_skipped_not_clean(monkeypatch, tmp_path):
    fake_tools(monkeypatch, stdout='{"paths": {"scanned": []}, "results": []}')
    (result,) = security.scan(tmp_path, tools=["semgrep"])
    assert (result.status, result.detail) == ("skipped", "no files scanned")
    fake_tools(monkeypatch, stdout='{"passed": 0, "failed": 0, "resource_count": 0}')
    (result,) = security.scan(tmp_path, tools=["checkov"])
    assert result.status == "skipped"


def test_crash_exit_code_is_an_error(monkeypatch, tmp_path):
    fake_tools(monkeypatch, stdout='{"results": []}', returncode=2)
    (result,) = security.scan(tmp_path, tools=["semgrep"])
    assert result.status == "error" and "exit 2" in result.detail


def test_paths_are_absolute_so_cwd_cannot_double_them(monkeypatch, tmp_path):
    calls = fake_tools(monkeypatch, stdout='{"Results": []}')
    (tmp_path / "out").mkdir()
    monkeypatch.chdir(tmp_path)
    security.scan(Path("out"), tools=["trivy"])
    cmd, cwd = calls[0]
    assert Path(cmd[-1]).is_absolute() and Path(cwd).is_absolute()

import json
from pathlib import Path

from ams.cli import main
from ams.pipeline import run
from ams.report import failures, html, markdown

EXAMPLES = Path(__file__).parent.parent / "examples"


def run_examples(out: Path) -> dict:
    run(EXAMPLES / "order-service", EXAMPLES / "billing-service", out, use_llm=False, scanners=False,
        log=lambda _: None)
    return json.loads((out / "ams-report.json").read_text())


def test_run_writes_all_three_reports(tmp_path):
    data = run_examples(tmp_path)
    assert data["generated_at"].endswith("+00:00")
    assert set(data["schemas"]) == {"order-service", "billing-service"}
    page = (tmp_path / "ams-report.html").read_text()
    assert "PASS" in page and "14/14" in page
    for field in ("order_ref", "client_id", "amount_due", "placed_at"):
        assert field in page
    assert "BigDecimal" in page  # provider source types come from the parsed schema
    assert "Why it failed" not in page
    assert "## Field mapping (Order -> OrderRecord)" in (tmp_path / "ams-report.md").read_text()


def test_failures_explain_what_went_wrong(tmp_path):
    data = run_examples(tmp_path)
    data["passed"] = False
    data["e2e"]["passed"] = False
    data["e2e"]["checks"][1].update(ok=False, actual=1713)
    data["security"] = [{"tool": "osv-scanner", "status": "findings", "findings": 1, "detail": "",
                         "items": ["GHSA-xxxx anyio@4.9.0"]},
                        {"tool": "semgrep", "status": "error", "findings": 0, "detail": "exit 2", "items": []}]
    why = failures(data)
    assert any("client_id" in w and "customerId" in w for w in why)
    assert any("GHSA-xxxx anyio@4.9.0" in w for w in why)
    assert any("semgrep errored" in w for w in why)
    page = html(data)
    assert "Why it failed" in page and "FAIL" in page
    assert "Why it failed" in markdown(data)


def test_html_escapes_untrusted_names(tmp_path):
    data = run_examples(tmp_path)
    data["mapping"]["pairs"][0]["reason"] = "<script>alert(1)</script>"
    page = html(data)
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_report_command_rerenders(tmp_path, capsys):
    run_examples(tmp_path)
    (tmp_path / "ams-report.html").unlink()
    assert main(["report", str(tmp_path)]) == 0
    assert (tmp_path / "ams-report.html").exists()
    assert "ams-report.html" in capsys.readouterr().out

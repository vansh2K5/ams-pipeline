import json
import shutil
from pathlib import Path

from ams.cli import main
from ams.generate import Generated
from ams.parsers import parse_service
from ams.pipeline import run
from ams.validate import compile_check, end_to_end, heal

EXAMPLES = Path(__file__).parent.parent / "examples"


def test_full_pipeline_on_examples(tmp_path):
    report = run(EXAMPLES / "order-service", EXAMPLES / "billing-service", tmp_path,
                 use_llm=False, scanners=False, log=lambda _: None)
    assert report.heal.passed
    assert report.e2e.passed, report.e2e.error
    assert len(report.e2e.checks) == 14 and all(c.ok for c in report.e2e.checks)
    assert report.e2e.requests == ["GET /api/orders/1", "GET /api/orders"]
    saved = json.loads((tmp_path / "ams-report.json").read_text())
    assert saved["passed"] is True
    assert (tmp_path / "docker-compose.ams.yml").read_text().count("ORDER_SERVICE_URL") == 1
    assert "order_ref=src.order_id" in (tmp_path / "ams_generated" / "order_service_client.py").read_text()


def test_heal_fixes_lint_errors_without_llm(tmp_path):
    f = tmp_path / "mod.py"
    f.write_text("import os\nimport sys\n\nprint(sys.argv)\n")
    assert [d.code for d in compile_check([f])] == ["F401"]
    result = heal([f], llm=None)
    assert result.passed and "import os" not in f.read_text()


def test_heal_reports_what_it_cannot_fix(tmp_path):
    f = tmp_path / "mod.py"
    f.write_text("print(undefined_name)\n")
    result = heal([f], llm=None)
    assert not result.passed
    assert "F821" in result.rounds[-1].diagnostics[0]


def test_heal_uses_llm_repair(tmp_path):
    class Fixer:
        name = "fake"

        def complete_text(self, system, user):
            assert "F821" in user
            return "```python\nundefined_name = 1\nprint(undefined_name)\n```"

    f = tmp_path / "mod.py"
    f.write_text("print(undefined_name)\n")
    assert heal([f], llm=Fixer()).passed


def test_e2e_catches_a_wrong_mapping(tmp_path):
    report = run(EXAMPLES / "order-service", EXAMPLES / "billing-service", tmp_path,
                 use_llm=False, scanners=False, log=lambda _: None)
    assert report.e2e.passed
    client = tmp_path / "ams_generated" / "order_service_client.py"
    client.write_text(client.read_text().replace("client_id=src.customer_id", "client_id=src.order_id"))
    gen = Generated(client.parent, client, tmp_path / "docker-compose.ams.yml", tmp_path / "requirements.txt",
                    "ams_generated.order_service_client", "OrderServiceClient", "to_order_record",
                    ["get_order_as_order_record"])
    result = end_to_end(parse_service(EXAMPLES / "order-service"), parse_service(EXAMPLES / "billing-service"),
                        report.plan, gen, tmp_path)
    assert not result.passed
    assert [c.consumer_field for c in result.checks if not c.ok] == ["client_id"]


def test_cli_run(tmp_path, capsys):
    out = tmp_path / "out"
    code = main(["run", "--provider", str(EXAMPLES / "order-service"), "--consumer",
                 str(EXAMPLES / "billing-service"), "--out", str(out), "--no-llm", "--no-scan"])
    assert code == 0
    assert "PASS" in capsys.readouterr().out
    shutil.rmtree(out)

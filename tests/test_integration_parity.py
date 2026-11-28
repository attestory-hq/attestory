import hashlib
import io
import json
import subprocess
import sys
from pathlib import Path

import openpyxl
from fastapi.testclient import TestClient

from attestory.server import create_app

COWORK_AUDIT_SCRIPT = (
    Path(__file__).parents[1]
    / "plugins"
    / "claude-cowork"
    / "skills"
    / "attestory-cowork"
    / "scripts"
    / "workbook_audit.py"
)


def _workbook_bytes(*, formula: bool = True, revenue: int = 250) -> bytes:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Revenue"
    sheet["B2"] = revenue
    sheet["B3"] = 162
    sheet["B4"] = "=SUM(B2:B3)" if formula else revenue + 162
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _run_cowork(*arguments: str) -> dict:
    result = subprocess.run(
        [sys.executable, str(COWORK_AUDIT_SCRIPT), *arguments],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode in {0, 1}, result.stderr
    payload = json.loads(result.stdout)
    if result.returncode == 1:
        assert payload["status"] in {"FAIL", "INCOMPLETE"}
    return payload


def _chatgpt_audit(
    client: TestClient,
    candidate_bytes: bytes,
    original_bytes: bytes | None,
) -> dict:
    files = {"candidate_file": ("candidate.xlsx", candidate_bytes)}
    if original_bytes is not None:
        files["original_file"] = ("original.xlsx", original_bytes)
    response = client.post(
        "/v1/excel/edit-verify",
        files=files,
        data={"confirm_backend_processing": "true"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _cowork_stage(workbook_path: Path, workbook_bytes: bytes) -> dict:
    workbook_path.write_bytes(workbook_bytes)
    return _run_cowork("stage", str(workbook_path))


def test_cowork_and_chatgpt_fail_on_same_formula_override(tmp_path: Path):
    original_bytes = _workbook_bytes()
    source = tmp_path / "Forecast.xlsx"
    staged = _cowork_stage(source, original_bytes)
    candidate_path = Path(staged["candidate"])
    candidate_workbook = openpyxl.load_workbook(candidate_path)
    candidate_workbook["Revenue"]["B4"] = 412
    candidate_workbook.save(candidate_path)
    candidate_bytes = candidate_path.read_bytes()

    cowork = _run_cowork("audit", staged["baseline"], str(candidate_path))
    client = TestClient(create_app(tmp_path / "chatgpt"))
    chatgpt = _chatgpt_audit(client, candidate_bytes, original_bytes)

    assert cowork["status"] == chatgpt["status"] == "FAIL"
    cowork_finding = next(
        finding
        for finding in cowork["findings"]
        if finding.get("kind") == "hardcode_override"
    )
    chatgpt_finding = next(
        finding
        for finding in chatgpt["cell_findings"]
        if finding.get("kind") == "hardcode_override"
    )
    assert (cowork_finding["kind"], cowork_finding["location"]) == (
        chatgpt_finding["kind"],
        chatgpt_finding["location"],
    ) == ("hardcode_override", "Revenue!B4")
    candidate_sha256 = hashlib.sha256(candidate_bytes).hexdigest()
    assert cowork["candidate_sha256"] == chatgpt["candidate_sha256"] == candidate_sha256

    candidate_download = client.get(chatgpt["candidate_download_url"])
    assert candidate_download.content == candidate_bytes
    assert candidate_download.headers["x-attestory-sha256"] == hashlib.sha256(
        candidate_bytes
    ).hexdigest()
    assert Path(cowork["candidate"]).read_bytes() == candidate_bytes
    cowork_receipt = Path(cowork["receipt"]).read_text(encoding="utf-8")
    assert chatgpt["candidate_sha256"] in cowork_receipt
    chatgpt_receipt = client.get(chatgpt["receipt_download_url"])
    assert chatgpt_receipt.status_code == 200
    assert chatgpt["candidate_sha256"] in chatgpt_receipt.text


def test_cowork_and_chatgpt_pass_same_clean_candidate(tmp_path: Path):
    original_bytes = _workbook_bytes(formula=False)
    candidate_bytes = _workbook_bytes(formula=False, revenue=260)
    source = tmp_path / "Forecast.xlsx"
    staged = _cowork_stage(source, original_bytes)
    candidate_path = Path(staged["candidate"])
    candidate_path.write_bytes(candidate_bytes)

    cowork = _run_cowork("audit", staged["baseline"], str(candidate_path))
    client = TestClient(create_app(tmp_path / "chatgpt"))
    chatgpt = _chatgpt_audit(client, candidate_bytes, original_bytes)

    assert cowork["status"] == chatgpt["status"] == "PASS"
    assert cowork["verified"] is chatgpt["verified"] is True
    candidate_sha256 = hashlib.sha256(candidate_bytes).hexdigest()
    assert cowork["candidate_sha256"] == chatgpt["candidate_sha256"] == candidate_sha256
    assert Path(cowork["candidate"]).read_bytes() == candidate_bytes
    assert client.get(chatgpt["candidate_download_url"]).content == candidate_bytes
    assert Path(cowork["receipt"]).is_file()
    receipt = client.get(chatgpt["receipt_download_url"])
    assert receipt.status_code == 200
    assert chatgpt["candidate_sha256"] in receipt.text


def test_cowork_and_chatgpt_report_missing_baseline_as_incomplete(
    tmp_path: Path,
):
    candidate_bytes = _workbook_bytes()
    source = tmp_path / "Forecast.xlsx"
    staged = _cowork_stage(source, candidate_bytes)
    Path(staged["baseline"]).unlink()

    cowork = _run_cowork("audit", staged["baseline"], staged["candidate"])
    client = TestClient(create_app(tmp_path / "chatgpt"))
    chatgpt = _chatgpt_audit(client, candidate_bytes, original_bytes=None)

    assert cowork["status"] == chatgpt["status"] == "INCOMPLETE"
    assert cowork["verified"] is chatgpt["verified"] is False
    assert cowork["incomplete_findings"]
    assert chatgpt["incomplete_findings"]
    assert Path(cowork["receipt"]).is_file()
    receipt = client.get(chatgpt["receipt_download_url"])
    assert receipt.status_code == 200
    assert "INCOMPLETE" in receipt.text

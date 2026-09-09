from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_generated_contract_is_current_and_complete():
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "generate_contract.py"), "--check"],
        check=True,
    )
    document = yaml.safe_load((ROOT / "openapi" / "hermes-mobile-v1.yaml").read_text())
    assert document["openapi"] == "3.1.0"
    operations = {
        operation["operationId"]
        for path in document["paths"].values()
        for operation in path.values()
    }
    assert len(operations) == 35
    assert {
        "pair",
        "createRun",
        "streamRunEvents",
        "answerApproval",
        "uploadAttachment",
        "sync",
    } <= operations


def test_every_error_response_uses_common_envelope():
    document = yaml.safe_load((ROOT / "openapi" / "hermes-mobile-v1.yaml").read_text())
    for path in document["paths"].values():
        for operation in path.values():
            for status, response in operation["responses"].items():
                if int(status) >= 400:
                    assert response == {"$ref": "#/components/responses/MobileError"}

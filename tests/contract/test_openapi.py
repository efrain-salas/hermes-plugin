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
    assert len(operations) == 45
    assert {
        "pair",
        "createRun",
        "streamRunEvents",
        "answerApproval",
        "uploadAttachment",
        "sync",
        "listScheduledTasks",
        "listScheduledTaskRuns",
        "getScheduledRun",
    } <= operations
    schemas = document["components"]["schemas"]
    assert schemas["ReasoningEffort"]["enum"] == [
        "none",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
        "ultra",
    ]
    assert (
        document["paths"]["/p/{profile}/v1/mobile/models"]["get"]["responses"]["200"][
            "content"
        ]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/ModelsResponse"
    )


def test_every_error_response_uses_common_envelope():
    document = yaml.safe_load((ROOT / "openapi" / "hermes-mobile-v1.yaml").read_text())
    for path in document["paths"].values():
        for operation in path.values():
            for status, response in operation["responses"].items():
                if int(status) >= 400:
                    assert response == {"$ref": "#/components/responses/MobileError"}

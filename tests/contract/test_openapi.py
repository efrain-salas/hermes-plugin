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
    assert len(operations) == 51
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
        "listInbox",
        "replyToInboxItem",
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
    assert (
        document["paths"]["/p/{profile}/v1/mobile/sync"]["get"]["responses"]["200"]
        ["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/SyncResponse"
    )
    assert schemas["SyncChange"]["required"] == ["type", "entity", "id"]
    inbox_list = document["paths"]["/p/{profile}/v1/mobile/inbox"]["get"]
    assert {parameter["name"] for parameter in inbox_list["parameters"]} >= {
        "unread",
        "kind",
        "limit",
        "cursor",
    }
    inbox_reply = document["paths"][
        "/p/{profile}/v1/mobile/inbox/{inbox_item_id}/reply"
    ]["post"]
    assert "202" in inbox_reply["responses"] and "200" not in inbox_reply["responses"]
    assert any(
        parameter["name"] == "Idempotency-Key" and parameter["required"]
        for parameter in inbox_reply["parameters"]
    )
    create_run = document["paths"][
        "/p/{profile}/v1/mobile/conversations/{conversation_id}/runs"
    ]["post"]
    assert "202" in create_run["responses"] and "200" not in create_run["responses"]
    assert create_run["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/RunCreateInput"
    }
    assert schemas["RunCreateInput"]["properties"]["mode"]["enum"] == ["full", "quick"]
    assert schemas["RunAccepted"]["properties"]["mode"]["enum"] == ["full", "quick"]
    assert "mode" in schemas["RunAccepted"]["required"]
    assert any(
        parameter["name"] == "Idempotency-Key" and parameter["required"]
        for parameter in create_run["parameters"]
    )
    generated_client = (ROOT / "generated" / "hermes-mobile-client.ts").read_text()
    assert "sync(options: RequestOptions = {}): Promise<SyncResponse>" in generated_client
    assert "replyToInboxItem(" in generated_client
    assert "): Promise<RunAccepted>" in generated_client
    assert 'export type RunMode = "full" | "quick";' in generated_client
    assert "mode: RunMode;" in generated_client


def test_every_error_response_uses_common_envelope():
    document = yaml.safe_load((ROOT / "openapi" / "hermes-mobile-v1.yaml").read_text())
    for path in document["paths"].values():
        for operation in path.values():
            for status, response in operation["responses"].items():
                if int(status) >= 400:
                    assert response == {"$ref": "#/components/responses/MobileError"}

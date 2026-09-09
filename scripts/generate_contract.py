#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]

ENDPOINTS = [
    ("get", "/p/{profile}/v1/mobile/health", "health"),
    ("get", "/p/{profile}/v1/mobile/capabilities", "getCapabilities"),
    ("get", "/p/{profile}/v1/mobile/bootstrap", "bootstrap"),
    ("post", "/p/{profile}/v1/mobile/auth/pair", "pair"),
    ("post", "/p/{profile}/v1/mobile/auth/refresh", "refresh"),
    ("post", "/p/{profile}/v1/mobile/auth/logout", "logout"),
    ("get", "/p/{profile}/v1/mobile/me", "me"),
    ("get", "/p/{profile}/v1/mobile/devices", "listDevices"),
    ("post", "/p/{profile}/v1/mobile/devices", "upsertDevice"),
    ("patch", "/p/{profile}/v1/mobile/devices/{device_id}", "patchDevice"),
    ("delete", "/p/{profile}/v1/mobile/devices/{device_id}", "deleteDevice"),
    ("get", "/p/{profile}/v1/mobile/conversations", "listConversations"),
    ("post", "/p/{profile}/v1/mobile/conversations", "createConversation"),
    (
        "get",
        "/p/{profile}/v1/mobile/conversations/{conversation_id}",
        "getConversation",
    ),
    (
        "patch",
        "/p/{profile}/v1/mobile/conversations/{conversation_id}",
        "patchConversation",
    ),
    (
        "delete",
        "/p/{profile}/v1/mobile/conversations/{conversation_id}",
        "deleteConversation",
    ),
    (
        "post",
        "/p/{profile}/v1/mobile/conversations/{conversation_id}/fork",
        "forkConversation",
    ),
    (
        "post",
        "/p/{profile}/v1/mobile/conversations/{conversation_id}/read",
        "readConversation",
    ),
    (
        "get",
        "/p/{profile}/v1/mobile/conversations/{conversation_id}/messages",
        "listMessages",
    ),
    (
        "post",
        "/p/{profile}/v1/mobile/conversations/{conversation_id}/runs",
        "createRun",
    ),
    ("get", "/p/{profile}/v1/mobile/runs/{run_id}", "getRun"),
    ("get", "/p/{profile}/v1/mobile/runs/{run_id}/events", "streamRunEvents"),
    ("post", "/p/{profile}/v1/mobile/runs/{run_id}/cancel", "cancelRun"),
    ("post", "/p/{profile}/v1/mobile/runs/{run_id}/steer", "steerRun"),
    (
        "post",
        "/p/{profile}/v1/mobile/runs/{run_id}/approvals/{approval_id}",
        "answerApproval",
    ),
    ("post", "/p/{profile}/v1/mobile/runs/{run_id}/retry", "retryRun"),
    ("get", "/p/{profile}/v1/mobile/attachments", "listAttachments"),
    ("post", "/p/{profile}/v1/mobile/attachments", "uploadAttachment"),
    ("get", "/p/{profile}/v1/mobile/attachments/{attachment_id}", "getAttachment"),
    (
        "get",
        "/p/{profile}/v1/mobile/attachments/{attachment_id}/content",
        "getAttachmentContent",
    ),
    (
        "delete",
        "/p/{profile}/v1/mobile/attachments/{attachment_id}",
        "deleteAttachment",
    ),
    (
        "post",
        "/p/{profile}/v1/mobile/attachments/{attachment_id}/retry",
        "retryAttachment",
    ),
    ("get", "/p/{profile}/v1/mobile/models", "listModels"),
    ("get", "/p/{profile}/v1/mobile/toolsets", "listToolsets"),
    ("get", "/p/{profile}/v1/mobile/sync", "sync"),
]


def openapi() -> dict:
    paths: dict = {}
    public = {"health", "pair", "refresh"}
    for method, path, operation in ENDPOINTS:
        parameters = [
            {"name": name, "in": "path", "required": True, "schema": {"type": "string"}}
            for name in re.findall(r"{([^}]+)}", path)
        ]
        item = {
            "operationId": operation,
            "parameters": parameters,
            "responses": {
                "200": {"description": "Success"},
                "400": {"$ref": "#/components/responses/MobileError"},
                "401": {"$ref": "#/components/responses/MobileError"},
                "404": {"$ref": "#/components/responses/MobileError"},
                "500": {"$ref": "#/components/responses/MobileError"},
            },
        }
        if operation not in public:
            item["security"] = [{"mobileBearer": []}]
        if method in {"post", "patch"} and operation not in {
            "logout",
            "cancelRun",
            "retryRun",
            "retryAttachment",
        }:
            content_type = (
                "multipart/form-data"
                if operation == "uploadAttachment"
                else "application/json"
            )
            item["requestBody"] = {
                "required": True,
                "content": {
                    content_type: {
                        "schema": {"type": "object", "additionalProperties": True}
                    }
                },
            }
        paths.setdefault(path, {})[method] = item
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Hermes Mobile API",
            "version": "1.0.0",
            "description": "Stable profile-scoped mobile contract.",
        },
        "servers": [{"url": "https://hermes.example.com"}],
        "paths": paths,
        "components": {
            "securitySchemes": {
                "mobileBearer": {
                    "type": "http",
                    "scheme": "bearer",
                    "bearerFormat": "JWT",
                }
            },
            "responses": {
                "MobileError": {
                    "description": "Typed mobile error",
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/ErrorEnvelope"}
                        }
                    },
                }
            },
            "schemas": {
                "ErrorEnvelope": {
                    "type": "object",
                    "required": ["error"],
                    "properties": {
                        "error": {
                            "type": "object",
                            "required": [
                                "code",
                                "message",
                                "request_id",
                                "retryable",
                                "details",
                            ],
                            "properties": {
                                "code": {"type": "string"},
                                "message": {"type": "string"},
                                "request_id": {"type": "string", "pattern": "^req_"},
                                "retryable": {"type": "boolean"},
                                "details": {"type": "object"},
                            },
                        }
                    },
                },
                "RunEvent": {
                    "type": "object",
                    "required": [
                        "event_id",
                        "sequence",
                        "type",
                        "run_id",
                        "conversation_id",
                        "created_at",
                        "data",
                    ],
                    "properties": {
                        "event_id": {"type": "string"},
                        "sequence": {"type": "integer"},
                        "type": {"type": "string"},
                        "run_id": {"type": "string"},
                        "conversation_id": {"type": "string"},
                        "created_at": {"type": "string", "format": "date-time"},
                        "data": {"type": "object"},
                    },
                },
            },
        },
    }


TS_HEADER = """// Generated by scripts/generate_contract.py; do not edit.

export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
export interface MobileError { code: string; message: string; request_id: string; retryable: boolean; details: Record<string, Json>; }
export interface ErrorEnvelope { error: MobileError; }
export interface RunEvent { event_id: string; sequence: number; type: string; run_id: string; conversation_id: string; created_at: string; data: Record<string, Json>; }
export interface RequestOptions { body?: unknown; query?: Record<string, string | number | boolean | undefined>; idempotencyKey?: string; signal?: AbortSignal; }
export interface StreamOptions extends RequestOptions { lastEventId?: string; }

export class HermesMobileClient {
  constructor(public baseUrl: string, public profile: string, private accessToken?: string) {}
  setAccessToken(token?: string): void { this.accessToken = token; }
  private path(template: string, params: Record<string, string>): string { return template.replace(/\\{([^}]+)\\}/g, (_, key: string) => encodeURIComponent(params[key] ?? "")); }
  private async request(method: string, path: string, options: RequestOptions = {}): Promise<unknown> {
    const url = new URL(path, this.baseUrl);
    for (const [key, value] of Object.entries(options.query ?? {})) if (value !== undefined) url.searchParams.set(key, String(value));
    const headers: Record<string, string> = { Accept: "application/json" };
    if (this.accessToken) headers.Authorization = `Bearer ${this.accessToken}`;
    if (options.idempotencyKey) headers["Idempotency-Key"] = options.idempotencyKey;
    let body: BodyInit | undefined;
    if (options.body instanceof FormData) body = options.body; else if (options.body !== undefined) { headers["Content-Type"] = "application/json"; body = JSON.stringify(options.body); }
    const response = await fetch(url, { method, headers, body, signal: options.signal });
    if (response.status === 204) return undefined;
    const payload = await response.json();
    if (!response.ok) throw (payload as ErrorEnvelope).error;
    return payload;
  }
  private async *events(path: string, options: StreamOptions = {}): AsyncGenerator<RunEvent> {
    const url = new URL(path, this.baseUrl);
    for (const [key, value] of Object.entries(options.query ?? {})) if (value !== undefined) url.searchParams.set(key, String(value));
    const headers: Record<string, string> = { Accept: "text/event-stream" };
    if (this.accessToken) headers.Authorization = `Bearer ${this.accessToken}`;
    if (options.lastEventId) headers["Last-Event-ID"] = options.lastEventId;
    const response = await fetch(url, { headers, signal: options.signal });
    if (!response.ok) throw ((await response.json()) as ErrorEnvelope).error;
    if (!response.body) return;
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done }).replace(/\\r\\n/g, "\\n");
      let boundary: number;
      while ((boundary = buffer.indexOf("\\n\\n")) >= 0) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const data = frame.split("\\n").filter(line => line.startsWith("data:"))
          .map(line => line.slice(5).trimStart()).join("\\n");
        if (data) yield JSON.parse(data) as RunEvent;
      }
      if (done) break;
    }
  }
"""


def typescript() -> str:
    lines = [TS_HEADER]
    for method, path, operation in ENDPOINTS:
        params = re.findall(r"{([^}]+)}", path)
        args = ["options: RequestOptions = {}"]
        param_map = ["profile: this.profile"]
        for param in params:
            if param == "profile":
                continue
            args.insert(-1, f"{param}: string")
            param_map.append(f"{param}: {param}")
        if operation == "streamRunEvents":
            args[-1] = "options: StreamOptions = {}"
            lines.append(
                f'  {operation}({", ".join(args)}): AsyncGenerator<RunEvent> {{ return this.events(this.path("{path}", {{ {", ".join(param_map)} }}), options); }}'
            )
        else:
            lines.append(
                f'  {operation}({", ".join(args)}): Promise<unknown> {{ return this.request("{method.upper()}", this.path("{path}", {{ {", ".join(param_map)} }}), options); }}'
            )
    lines.append("}\n")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    outputs = {
        ROOT / "openapi" / "hermes-mobile-v1.yaml": yaml.safe_dump(
            openapi(), sort_keys=False, allow_unicode=True
        ),
        ROOT / "generated" / "hermes-mobile-client.ts": typescript(),
    }
    dirty = []
    for path, content in outputs.items():
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                dirty.append(str(path.relative_to(ROOT)))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    if dirty:
        print("Generated files are stale: " + ", ".join(dirty))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

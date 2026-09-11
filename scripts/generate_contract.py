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
    ("get", "/p/{profile}/v1/mobile/inbox", "listInbox"),
    ("post", "/p/{profile}/v1/mobile/inbox/read-all", "readAllInbox"),
    ("get", "/p/{profile}/v1/mobile/inbox/{inbox_item_id}", "getInboxItem"),
    (
        "post",
        "/p/{profile}/v1/mobile/inbox/{inbox_item_id}/read",
        "readInboxItem",
    ),
    (
        "post",
        "/p/{profile}/v1/mobile/inbox/{inbox_item_id}/conversation",
        "createInboxConversation",
    ),
    (
        "post",
        "/p/{profile}/v1/mobile/inbox/{inbox_item_id}/reply",
        "replyToInboxItem",
    ),
    ("get", "/p/{profile}/v1/mobile/scheduled-tasks", "listScheduledTasks"),
    (
        "get",
        "/p/{profile}/v1/mobile/scheduled-tasks/{scheduled_task_id}",
        "getScheduledTask",
    ),
    (
        "patch",
        "/p/{profile}/v1/mobile/scheduled-tasks/{scheduled_task_id}",
        "patchScheduledTask",
    ),
    (
        "delete",
        "/p/{profile}/v1/mobile/scheduled-tasks/{scheduled_task_id}",
        "deleteScheduledTask",
    ),
    (
        "post",
        "/p/{profile}/v1/mobile/scheduled-tasks/{scheduled_task_id}/pause",
        "pauseScheduledTask",
    ),
    (
        "post",
        "/p/{profile}/v1/mobile/scheduled-tasks/{scheduled_task_id}/resume",
        "resumeScheduledTask",
    ),
    (
        "post",
        "/p/{profile}/v1/mobile/scheduled-tasks/{scheduled_task_id}/run",
        "runScheduledTask",
    ),
    (
        "get",
        "/p/{profile}/v1/mobile/scheduled-tasks/{scheduled_task_id}/runs",
        "listScheduledTaskRuns",
    ),
    (
        "get",
        "/p/{profile}/v1/mobile/scheduled-runs/{scheduled_run_id}",
        "getScheduledRun",
    ),
    (
        "post",
        "/p/{profile}/v1/mobile/scheduled-runs/{scheduled_run_id}/read",
        "readScheduledRun",
    ),
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
            "pauseScheduledTask",
            "resumeScheduledTask",
            "runScheduledTask",
            "readScheduledRun",
            "readInboxItem",
            "readAllInbox",
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
        if operation in {"createConversation", "patchConversation"}:
            schema_name = (
                "ConversationCreateInput"
                if operation == "createConversation"
                else "ConversationPatchInput"
            )
            item["requestBody"]["content"]["application/json"]["schema"] = {
                "$ref": f"#/components/schemas/{schema_name}"
            }
        if operation == "createInboxConversation":
            item["requestBody"]["content"]["application/json"]["schema"] = {
                "$ref": "#/components/schemas/InboxConversationInput"
            }
        if operation == "replyToInboxItem":
            item["requestBody"]["content"]["application/json"]["schema"] = {
                "$ref": "#/components/schemas/InboxReplyInput"
            }
            item["parameters"].append(
                {
                    "name": "Idempotency-Key",
                    "in": "header",
                    "required": True,
                    "schema": {"type": "string", "minLength": 1},
                }
            )
            item["responses"].pop("200")
            item["responses"]["202"] = {
                "description": "Reply accepted as an asynchronous run",
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/RunAccepted"}
                    }
                },
            }
            item["responses"]["409"] = {
                "$ref": "#/components/responses/MobileError"
            }
        if operation == "createRun":
            item["requestBody"]["content"]["application/json"]["schema"] = {
                "$ref": "#/components/schemas/RunCreateInput"
            }
            item["parameters"].append(
                {
                    "name": "Idempotency-Key",
                    "in": "header",
                    "required": True,
                    "schema": {"type": "string", "minLength": 1},
                }
            )
            item["responses"].pop("200")
            item["responses"]["202"] = {
                "description": "Run accepted as an asynchronous turn",
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/RunAccepted"}
                    }
                },
            }
            item["responses"]["409"] = {
                "$ref": "#/components/responses/MobileError"
            }
        if operation == "createInboxConversation":
            item["responses"]["200"]["description"] = "Existing linked conversation"
            item["responses"]["201"] = {"description": "Conversation created and linked"}
        if operation == "readAllInbox":
            item["responses"]["200"] = {
                "description": "Unread items marked as read",
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/InboxReadAllResult"}
                    }
                },
            }
        if operation in {"listInbox", "getInboxItem", "readInboxItem"}:
            item["responses"]["200"] = {
                "description": "Unified durable activity",
                "content": {
                    "application/json": {
                        "schema": {
                            "$ref": (
                                "#/components/schemas/InboxPage"
                                if operation == "listInbox"
                                else "#/components/schemas/InboxItem"
                            )
                        }
                    }
                },
            }
        if operation == "listInbox":
            item["parameters"].extend(
                [
                    {
                        "name": "unread",
                        "in": "query",
                        "required": False,
                        "description": "true returns unread items; false returns read items",
                        "schema": {"type": "boolean"},
                    },
                    {
                        "name": "kind",
                        "in": "query",
                        "required": False,
                        "description": "Exact activity kind or dotted-prefix family",
                        "schema": {
                            "type": "string",
                            "maxLength": 100,
                            "pattern": "^[a-z0-9_.-]+$",
                        },
                    },
                    {
                        "name": "limit",
                        "in": "query",
                        "required": False,
                        "schema": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 100,
                            "default": 30,
                        },
                    },
                    {
                        "name": "cursor",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "string"},
                    },
                ]
            )
        if operation == "listModels":
            item["responses"]["200"] = {
                "description": "Provider model catalog and profile defaults",
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/ModelsResponse"}
                    }
                },
            }
        if operation == "sync":
            item["responses"]["200"] = {
                "description": "Incremental changes after the supplied cursor",
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/SyncResponse"}
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
                "ReasoningEffort": {
                    "type": "string",
                    "enum": [
                        "none",
                        "minimal",
                        "low",
                        "medium",
                        "high",
                        "xhigh",
                        "max",
                        "ultra",
                    ],
                },
                "ModelReasoning": {
                    "type": "object",
                    "required": ["supported", "can_disable", "efforts"],
                    "properties": {
                        "supported": {"type": "boolean"},
                        "can_disable": {"type": ["boolean", "null"]},
                        "efforts": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/ReasoningEffort"},
                        },
                    },
                },
                "ModelInfo": {
                    "type": "object",
                    "required": ["id", "name", "reasoning"],
                    "properties": {
                        "id": {"type": "string"},
                        "name": {"type": "string"},
                        "reasoning": {"$ref": "#/components/schemas/ModelReasoning"},
                    },
                },
                "ModelsResponse": {
                    "type": "object",
                    "required": [
                        "items",
                        "default",
                        "default_reasoning_effort",
                    ],
                    "properties": {
                        "items": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/ModelInfo"},
                        },
                        "default": {"type": ["string", "null"]},
                        "default_reasoning_effort": {
                            "anyOf": [
                                {"$ref": "#/components/schemas/ReasoningEffort"},
                                {"type": "null"},
                            ]
                        },
                    },
                },
                "SyncChange": {
                    "type": "object",
                    "required": ["type", "entity", "id"],
                    "properties": {
                        "type": {"type": "string"},
                        "entity": {"type": "object", "additionalProperties": True},
                        "id": {"type": "string"},
                    },
                },
                "SyncResponse": {
                    "type": "object",
                    "required": [
                        "changes",
                        "next_cursor",
                        "has_more",
                        "server_time",
                    ],
                    "properties": {
                        "changes": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/SyncChange"},
                        },
                        "next_cursor": {"type": "string"},
                        "has_more": {"type": "boolean"},
                        "server_time": {"type": "string", "format": "date-time"},
                    },
                },
                "ConversationCreateInput": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "title": {"type": ["string", "null"], "maxLength": 200},
                        "model": {"type": ["string", "null"], "maxLength": 200},
                        "reasoning_effort": {
                            "anyOf": [
                                {"$ref": "#/components/schemas/ReasoningEffort"},
                                {"type": "null"},
                            ]
                        },
                    },
                },
                "ConversationPatchInput": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "title": {"type": ["string", "null"], "maxLength": 200},
                        "archived": {"type": ["boolean", "null"]},
                        "pinned": {"type": ["boolean", "null"]},
                        "model": {"type": ["string", "null"], "maxLength": 200},
                        "reasoning_effort": {
                            "anyOf": [
                                {"$ref": "#/components/schemas/ReasoningEffort"},
                                {"type": "null"},
                            ]
                        },
                    },
                },
                "InboxAction": {
                    "type": "object",
                    "required": ["type"],
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": [
                                "answer_approval",
                                "open_conversation",
                                "create_conversation",
                                "reply",
                            ],
                        },
                        "conversation_id": {"type": "string"},
                        "run_id": {"type": "string"},
                        "approval_id": {"type": "string"},
                        "decisions": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "enum": [
                                    "allow_once",
                                    "allow_session",
                                    "always_allow",
                                    "deny",
                                ],
                            },
                        },
                    },
                },
                "InboxItem": {
                    "type": "object",
                    "required": [
                        "id",
                        "kind",
                        "severity",
                        "title",
                        "body",
                        "source",
                        "conversation_id",
                        "context",
                        "actions",
                        "unread",
                        "resolved",
                        "occurred_at",
                        "read_at",
                        "resolved_at",
                        "updated_at",
                    ],
                    "properties": {
                        "id": {"type": "string"},
                        "kind": {"type": "string"},
                        "severity": {
                            "type": "string",
                            "enum": ["info", "warning", "error", "action_required"],
                        },
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                        "source": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["type", "id"],
                            "properties": {
                                "type": {"type": "string"},
                                "id": {"type": ["string", "null"]},
                            },
                        },
                        "conversation_id": {"type": ["string", "null"]},
                        "context": {"type": "object"},
                        "actions": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/InboxAction"},
                        },
                        "unread": {"type": "boolean"},
                        "resolved": {"type": "boolean"},
                        "occurred_at": {"type": "string", "format": "date-time"},
                        "read_at": {"type": ["string", "null"], "format": "date-time"},
                        "resolved_at": {"type": ["string", "null"], "format": "date-time"},
                        "updated_at": {"type": "string", "format": "date-time"},
                    },
                },
                "InboxPage": {
                    "type": "object",
                    "required": ["items", "has_more", "unread_count"],
                    "properties": {
                        "items": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/InboxItem"},
                        },
                        "next_cursor": {"type": ["string", "null"]},
                        "has_more": {"type": "boolean"},
                        "unread_count": {"type": "integer"},
                    },
                },
                "InboxConversationInput": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "title": {"type": ["string", "null"], "maxLength": 200}
                    },
                },
                "RunInputBlock": {
                    "oneOf": [
                        {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["type", "text"],
                            "properties": {
                                "type": {"const": "text"},
                                "text": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 100000,
                                },
                            },
                        },
                        {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["type", "attachment_id"],
                            "properties": {
                                "type": {"const": "attachment"},
                                "attachment_id": {"type": "string"},
                            },
                        },
                    ]
                },
                "RunCreateInput": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["client_message_id", "input"],
                    "properties": {
                        "client_message_id": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 128,
                        },
                        "mode": {
                            "type": "string",
                            "enum": ["full", "quick"],
                            "default": "full",
                            "description": (
                                "quick runs a lightweight in-process agent: no memory, "
                                "context files, MCP, end-of-turn review or reasoning, "
                                "only the configured web-search toolsets. The turn still "
                                "persists to the conversation history and streams the "
                                "same events."
                            ),
                        },
                        "input": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 20,
                            "items": {"$ref": "#/components/schemas/RunInputBlock"},
                        },
                    },
                },
                "InboxReplyInput": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["client_message_id", "input"],
                    "properties": {
                        "client_message_id": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 128,
                        },
                        "conversation_title": {
                            "type": ["string", "null"],
                            "maxLength": 200,
                        },
                        "input": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 20,
                            "items": {"$ref": "#/components/schemas/RunInputBlock"},
                        },
                    },
                },
                "RunAccepted": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "run_id",
                        "conversation_id",
                        "user_message_id",
                        "status",
                        "mode",
                        "events_url",
                    ],
                    "properties": {
                        "run_id": {"type": "string"},
                        "conversation_id": {"type": "string"},
                        "user_message_id": {"type": ["string", "null"]},
                        "status": {"type": "string"},
                        "mode": {
                            "type": "string",
                            "enum": ["full", "quick"],
                            "default": "full",
                            "description": (
                                "Echoes the mode the run was served with so clients "
                                "can distinguish quick turns and detect transparent "
                                "fallback to the full flow."
                            ),
                        },
                        "events_url": {"type": "string"},
                    },
                },
                "InboxReadAllResult": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["updated"],
                    "properties": {"updated": {"type": "integer", "minimum": 0}},
                },
            },
        },
    }


TS_HEADER = """// Generated by scripts/generate_contract.py; do not edit.

export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
export interface MobileError { code: string; message: string; request_id: string; retryable: boolean; details: Record<string, Json>; }
export interface ErrorEnvelope { error: MobileError; }
export interface RunEvent { event_id: string; sequence: number; type: string; run_id: string; conversation_id: string; created_at: string; data: Record<string, Json>; }
export type ReasoningEffort = "none" | "minimal" | "low" | "medium" | "high" | "xhigh" | "max" | "ultra";
export interface ModelReasoning { supported: boolean; can_disable: boolean | null; efforts: ReasoningEffort[]; }
export interface ModelInfo { id: string; name: string; reasoning: ModelReasoning; }
export interface ModelsResponse { items: ModelInfo[]; default: string | null; default_reasoning_effort: ReasoningEffort | null; }
export interface SyncChange { type: string; entity: Record<string, Json>; id: string; }
export interface SyncResponse { changes: SyncChange[]; next_cursor: string; has_more: boolean; server_time: string; }
export interface ConversationCreateInput { title?: string | null; model?: string | null; reasoning_effort?: ReasoningEffort | null; }
export interface ConversationPatchInput extends ConversationCreateInput { archived?: boolean | null; pinned?: boolean | null; }
export type InboxSeverity = "info" | "warning" | "error" | "action_required";
export type InboxActionType = "answer_approval" | "open_conversation" | "create_conversation" | "reply";
export interface InboxAction { type: InboxActionType; conversation_id?: string; run_id?: string; approval_id?: string; decisions?: string[]; }
export interface InboxItem { id: string; kind: string; severity: InboxSeverity; title: string; body: string; source: { type: string; id: string | null }; conversation_id: string | null; context: Record<string, Json>; actions: InboxAction[]; unread: boolean; resolved: boolean; occurred_at: string; read_at: string | null; resolved_at: string | null; updated_at: string; }
export interface InboxPage { items: InboxItem[]; next_cursor: string | null; has_more: boolean; unread_count: number; }
export interface InboxConversationInput { title?: string | null; }
export type RunMode = "full" | "quick";
export interface RunCreateInput { client_message_id: string; input: Array<{ type: "text"; text: string } | { type: "attachment"; attachment_id: string }>; mode?: RunMode; }
export interface InboxReplyInput { client_message_id: string; input: Array<{ type: "text"; text: string } | { type: "attachment"; attachment_id: string }>; conversation_title?: string | null; }
export interface RunAccepted { run_id: string; conversation_id: string; user_message_id: string | null; status: string; mode: RunMode; events_url: string; }
export interface InboxReadAllResult { updated: number; }
export interface RequestOptions { body?: unknown; query?: Record<string, string | number | boolean | undefined>; idempotencyKey?: string; signal?: AbortSignal; }
export interface StreamOptions extends RequestOptions { lastEventId?: string; }

export class HermesMobileClient {
  constructor(public baseUrl: string, public profile: string, private accessToken?: string) {}
  setAccessToken(token?: string): void { this.accessToken = token; }
  private path(template: string, params: Record<string, string>): string { return template.replace(/\\{([^}]+)\\}/g, (_, key: string) => encodeURIComponent(params[key] ?? "")); }
  private async request<T = unknown>(method: string, path: string, options: RequestOptions = {}): Promise<T> {
    const url = new URL(path, this.baseUrl);
    for (const [key, value] of Object.entries(options.query ?? {})) if (value !== undefined) url.searchParams.set(key, String(value));
    const headers: Record<string, string> = { Accept: "application/json" };
    if (this.accessToken) headers.Authorization = `Bearer ${this.accessToken}`;
    if (options.idempotencyKey) headers["Idempotency-Key"] = options.idempotencyKey;
    let body: BodyInit | undefined;
    if (options.body instanceof FormData) body = options.body; else if (options.body !== undefined) { headers["Content-Type"] = "application/json"; body = JSON.stringify(options.body); }
    const response = await fetch(url, { method, headers, body, signal: options.signal });
    if (response.status === 204) return undefined as T;
    const payload = await response.json();
    if (!response.ok) throw (payload as ErrorEnvelope).error;
    return payload as T;
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
            if operation == "listModels":
                lines.append(
                    f'  {operation}({", ".join(args)}): Promise<ModelsResponse> {{ return this.request<ModelsResponse>("{method.upper()}", this.path("{path}", {{ {", ".join(param_map)} }}), options); }}'
                )
            elif operation == "sync":
                lines.append(
                    f'  {operation}({", ".join(args)}): Promise<SyncResponse> {{ return this.request<SyncResponse>("{method.upper()}", this.path("{path}", {{ {", ".join(param_map)} }}), options); }}'
                )
            elif operation == "listInbox":
                lines.append(
                    f'  {operation}({", ".join(args)}): Promise<InboxPage> {{ return this.request<InboxPage>("{method.upper()}", this.path("{path}", {{ {", ".join(param_map)} }}), options); }}'
                )
            elif operation in {"getInboxItem", "readInboxItem"}:
                lines.append(
                    f'  {operation}({", ".join(args)}): Promise<InboxItem> {{ return this.request<InboxItem>("{method.upper()}", this.path("{path}", {{ {", ".join(param_map)} }}), options); }}'
                )
            elif operation == "replyToInboxItem":
                lines.append(
                    f'  {operation}({", ".join(args)}): Promise<RunAccepted> {{ return this.request<RunAccepted>("{method.upper()}", this.path("{path}", {{ {", ".join(param_map)} }}), options); }}'
                )
            elif operation == "createRun":
                lines.append(
                    f'  {operation}({", ".join(args)}): Promise<RunAccepted> {{ return this.request<RunAccepted>("{method.upper()}", this.path("{path}", {{ {", ".join(param_map)} }}), options); }}'
                )
            elif operation == "readAllInbox":
                lines.append(
                    f'  {operation}({", ".join(args)}): Promise<InboxReadAllResult> {{ return this.request<InboxReadAllResult>("{method.upper()}", this.path("{path}", {{ {", ".join(param_map)} }}), options); }}'
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

from __future__ import annotations

import re
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


ReasoningEffort: TypeAlias = Literal[
    "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"
]

PushEnvironment: TypeAlias = Literal["sandbox", "production"]

_APNS_TOKEN = re.compile(r"^[0-9a-f]+$")


class DevicePair(StrictModel):
    installation_id: str = Field(min_length=8, max_length=128)
    name: str = Field(min_length=1, max_length=100)
    platform: Literal["ios", "android"]
    app_version: str | None = Field(default=None, max_length=50)
    locale: str | None = Field(default=None, max_length=35)
    timezone: str | None = Field(default=None, max_length=100)


class PairRequest(StrictModel):
    pairing_token: str = Field(min_length=32, max_length=256)
    device: DevicePair


class RefreshRequest(StrictModel):
    refresh_token: str = Field(min_length=32, max_length=256)


class Notifications(StrictModel):
    turn_completed: bool = True
    turn_failed: bool = True
    approval_required: bool = True
    scheduled_task_completed: bool = True
    scheduled_task_failed: bool = True
    system_lifecycle: bool = True
    system_critical: bool = True


class DeviceUpdate(StrictModel):
    installation_id: str | None = Field(default=None, min_length=8, max_length=128)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    platform: Literal["ios", "android"] | None = None
    push_provider: Literal["apns"] | None = None
    push_token: str | None = Field(default=None, max_length=512)
    push_environment: PushEnvironment | None = None
    app_version: str | None = Field(default=None, max_length=50)
    locale: str | None = Field(default=None, max_length=35)
    timezone: str | None = Field(default=None, max_length=100)
    notifications: Notifications | None = None

    @field_validator("push_token")
    @classmethod
    def validate_push_token(cls, value: str | None) -> str | None:
        """Normalize an opaque APNs token without assuming a fixed length."""
        if value is None:
            return None
        normalized = value.strip().lower().replace(" ", "").strip("<>")
        if (
            not normalized
            or len(normalized) % 2
            or not _APNS_TOKEN.fullmatch(normalized)
        ):
            raise ValueError(
                "push_token must be an even-length hexadecimal APNs token"
            )
        return normalized

    @model_validator(mode="after")
    def validate_push_registration(self) -> DeviceUpdate:
        fields = self.model_fields_set
        provider_set = "push_provider" in fields
        token_set = "push_token" in fields
        environment_set = "push_environment" in fields
        if provider_set and self.push_provider is None:
            # Explicit null body for the removal contract.
            if self.push_token or self.push_environment:
                raise ValueError(
                    "push_token and push_environment must be omitted to clear push"
                )
            return self
        if self.push_provider == "apns":
            if not self.push_token or not self.push_environment:
                raise ValueError(
                    "push_provider=apns requires push_token and push_environment"
                )
            if self.platform is not None and self.platform != "ios":
                raise ValueError("push_provider=apns requires platform=ios")
            return self
        if token_set or environment_set:
            raise ValueError(
                "push_provider=apns is required with push_token/push_environment"
            )
        return self


class ConversationCreate(StrictModel):
    title: str | None = Field(default=None, max_length=200)
    model: str | None = Field(default=None, max_length=200)
    reasoning_effort: ReasoningEffort | None = None


class ConversationPatch(StrictModel):
    title: str | None = Field(default=None, max_length=200)
    archived: bool | None = None
    pinned: bool | None = None
    model: str | None = Field(default=None, max_length=200)
    reasoning_effort: ReasoningEffort | None = None


class ProfilePreferencesPatch(StrictModel):
    quick_model: str | None = Field(default=None, max_length=200)


class ForkRequest(StrictModel):
    message_id: str
    title: str | None = Field(default=None, max_length=200)


class ReadRequest(StrictModel):
    message_id: str


class TextInput(StrictModel):
    type: Literal["text"]
    text: str = Field(min_length=1, max_length=100_000)


class AttachmentInput(StrictModel):
    type: Literal["attachment"]
    attachment_id: str


class RunCreate(StrictModel):
    client_message_id: str = Field(min_length=1, max_length=128)
    input: list[TextInput | AttachmentInput] = Field(min_length=1, max_length=20)
    mode: Literal["full", "quick"] = "full"


class InboxConversationCreate(StrictModel):
    title: str | None = Field(default=None, max_length=200)


class InboxReply(RunCreate):
    conversation_title: str | None = Field(default=None, max_length=200)


class SteerRequest(StrictModel):
    instruction: str = Field(min_length=1, max_length=20_000)


class ApprovalRequest(StrictModel):
    decision: Literal["allow_once", "allow_session", "always_allow", "deny"]


class ScheduledTaskPatch(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    schedule: str | None = Field(default=None, min_length=1, max_length=500)
    prompt: str | None = Field(default=None, max_length=100_000)
    skills: list[str] | None = Field(default=None, max_length=50)
    repeat: int | None = Field(default=None, ge=1)
    conversation_delivery: Literal["agent", "hub_only", "origin"] | None = None

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


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


class DeviceUpdate(StrictModel):
    installation_id: str | None = Field(default=None, min_length=8, max_length=128)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    platform: Literal["ios", "android"] | None = None
    push_provider: Literal["expo"] | None = None
    push_token: str | None = Field(default=None, max_length=512)
    app_version: str | None = Field(default=None, max_length=50)
    locale: str | None = Field(default=None, max_length=35)
    timezone: str | None = Field(default=None, max_length=100)
    notifications: Notifications | None = None

    @field_validator("push_token")
    @classmethod
    def validate_push(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith(
            ("ExponentPushToken[", "ExpoPushToken[")
        ):
            raise ValueError("unsupported Expo push token")
        return value


class ConversationCreate(StrictModel):
    title: str | None = Field(default=None, max_length=200)
    model: str | None = Field(default=None, max_length=200)


class ConversationPatch(StrictModel):
    title: str | None = Field(default=None, max_length=200)
    archived: bool | None = None
    pinned: bool | None = None
    model: str | None = Field(default=None, max_length=200)


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

from __future__ import annotations

import re

_FENCED_CODE = re.compile(r"```[^\n]*\n?(.*?)```", re.DOTALL)
_FENCED_TILDE = re.compile(r"~~~[^\n]*\n?(.*?)~~~", re.DOTALL)
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_AUTOLINK = re.compile(r"<(https?://[^>\s]+)>")
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+", re.MULTILINE)
_BLOCKQUOTE = re.compile(r"^[ \t]{0,3}>[ \t]?", re.MULTILINE)
_HORIZONTAL_RULE = re.compile(r"^[ \t]{0,3}(?:[-*_][ \t]*){3,}$", re.MULTILINE)
_LIST_MARKER = re.compile(r"^[ \t]*(?:[-*+]|\d{1,3}[.)])[ \t]+", re.MULTILINE)
_TABLE_SEPARATOR = re.compile(
    r"^[ \t]*\|?[ \t]*:?-{2,}:?[ \t]*(?:\|[ \t]*:?-{2,}:?[ \t]*)*\|?[ \t]*$",
    re.MULTILINE,
)
_BOLD_ITALIC = re.compile(r"\*\*\*(?=\S)(.+?)(?<=\S)\*\*\*", re.DOTALL)
_BOLD_ITALIC_UNDERSCORE = re.compile(
    r"(?<!\w)___(?=\S)(.+?)(?<=\S)___(?!\w)", re.DOTALL
)
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", re.DOTALL)
_ITALIC_ASTERISK = re.compile(r"(?<!\w)\*(?=\S)(.+?)(?<=\S)\*(?!\w)", re.DOTALL)
_BOLD_UNDERSCORE = re.compile(r"(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)", re.DOTALL)
_ITALIC_UNDERSCORE = re.compile(r"(?<!\w)_(?=\S)(.+?)(?<=\S)_(?!\w)", re.DOTALL)
_STRIKETHROUGH = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~", re.DOTALL)
_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!>~|])")


def markdown_to_text(content: object) -> str:
    """Render Markdown to the plain text an APNs alert can display safely."""
    text = content if isinstance(content, str) else str(content or "")
    if not text:
        return ""

    text = _FENCED_CODE.sub(r"\1", text)
    text = _FENCED_TILDE.sub(r"\1", text)
    text = _IMAGE.sub(r"\1", text)
    text = _LINK.sub(r"\1", text)
    text = _AUTOLINK.sub(r"\1", text)
    text = _INLINE_CODE.sub(r"\1", text)
    text = _HORIZONTAL_RULE.sub("", text)
    text = _TABLE_SEPARATOR.sub("", text)
    text = _HEADING.sub("", text)
    text = _BLOCKQUOTE.sub("", text)
    text = _LIST_MARKER.sub("", text)

    text = _BOLD_ITALIC.sub(r"\1", text)
    text = _BOLD_ITALIC_UNDERSCORE.sub(r"\1", text)
    text = _BOLD.sub(r"\1", text)
    text = _BOLD_UNDERSCORE.sub(r"\1", text)
    text = _ITALIC_ASTERISK.sub(r"\1", text)
    text = _ITALIC_UNDERSCORE.sub(r"\1", text)
    text = _STRIKETHROUGH.sub(r"\1", text)
    text = _ESCAPE.sub(r"\1", text)

    return text


def plain_notification_text(content: object) -> str:
    """Markdown-free, single-line text suitable for a notification alert."""
    return " ".join(markdown_to_text(content).split())

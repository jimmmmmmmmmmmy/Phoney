"""Google Gemini dialogue for the owner's telephone delegate (text only).

Protocol: https://ai.google.dev/api/generate-content
``streamGenerateContent`` with ``alt=sse`` returns JSON events; the trusted
instruction travels in ``systemInstruction`` while dialogue travels in
``contents``, so remote speech is never treated as an instruction.

The adapter exposes no provider-native tools. The telephone runtime separately
recognizes one explicit end-call marker after a successful reply and playback.
Thought parts are never handed to speech or the command parser.

Nothing in ``app.py`` imports this module: Build 3 capture and transcription
keep working whether or not a Gemini credential exists.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from copy import deepcopy
from dataclasses import dataclass, field

import httpx

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
DEFAULT_MODEL = "gemini-3.5-flash-lite"
METHODS = frozenset({"streamGenerateContent", "generateContent"})
# One SSE event; a longer one is treated as a failed generation.
MAX_EVENT_BYTES = 262_144
MAX_OUTPUT_TOKENS = 8192
# Bounds one whole generation. The per-read timeout only covers a stalled body.
GENERATION_SECONDS = 20.0
READ_SECONDS = 10.0
CONNECT_SECONDS = 5.0
MODEL_ID = re.compile(r"[a-z0-9.-]{1,80}\Z")
SPEAKERS = ("owner", "remote", "agent")
END_CALL = "[/END CALL]"

# The fixed half of the instruction. A selected mode appends its own goal and
# boundaries after this text rather than replacing the delegate rules.
from .prompts import SHARED_PHONE_INSTRUCTION, reply_progress_instruction

DELEGATE_INSTRUCTION = SHARED_PHONE_INSTRUCTION


class GeminiError(RuntimeError):
    """A generation failed: blocked, truncated, empty, malformed, or over limit."""


class _EmptyGeminiResponse(GeminiError):
    """No spoken output and no explicit blocked/truncated finish reason."""


class _FirstTextTimeout(GeminiError):
    """The phone's first-spoken-text budget elapsed before any output."""


def _attempt_trace(stage, attempt, model, started, **fields):
    # Deliberately exclude request/response bodies, headers, URLs, and keys.
    return {"kind": "trace", "stage": stage, "attempt": attempt, "model": model,
            "attempt_ms": int((time.monotonic() - started) * 1000), **fields}


def gemini_url(model: str, method: str = "streamGenerateContent") -> str:
    """Build one GenerateContent endpoint from a bare model ID.

    The ``models/`` prefix is rejected instead of stripped: a caller that
    passes it has a different identifier in mind than the one configured.
    """
    if not isinstance(model, str) or not MODEL_ID.fullmatch(model):
        raise ValueError("Use a model ID, without the models/ prefix")
    if method not in METHODS:
        raise ValueError("Unsupported GenerateContent method")
    return f"{GEMINI_BASE}/{model}:{method}"


def gemini_body(system: str, contents: list[dict], *,
                max_output_tokens: int = 2048, model: str = DEFAULT_MODEL) -> dict:
    """Assemble one request body. History is copied, never aliased."""
    if not isinstance(system, str) or not system.strip():
        raise ValueError("A system instruction is required.")
    if not contents:
        raise ValueError("At least one content entry is required.")
    if (type(max_output_tokens) is not int or isinstance(max_output_tokens, bool)
            or not 1 <= max_output_tokens <= MAX_OUTPUT_TOKENS):
        raise ValueError(f"max_output_tokens must be between 1 and {MAX_OUTPUT_TOKENS}.")
    if not isinstance(model, str) or not MODEL_ID.fullmatch(model):
        raise ValueError("Use a model ID, without the models/ prefix")
    # These explicit model IDs support the lowest-latency setting below.
    # Keep LOW for other models: in particular, 3.8 Flash rejects MINIMAL.
    if model in {"gemini-3.5-flash-lite", "gemini-3.1-flash-lite"}:
        thinking = {"thinkingLevel": "MINIMAL", "includeThoughts": False}
    elif model in {"gemini-2.5-flash", "gemini-2.5-flash-lite"}:
        thinking = {"thinkingBudget": 0, "includeThoughts": False}
    else:
        thinking = {"thinkingLevel": "LOW", "includeThoughts": False}
    return {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": deepcopy(contents),
        "generationConfig": {
            "candidateCount": 1,
            "maxOutputTokens": max_output_tokens,
            "thinkingConfig": thinking,
        },
    }


async def sse_objects(response):
    """Yield each ``data:`` payload of an SSE body as parsed JSON.

    A blank line terminates an event, and a multi-line payload is joined with
    newlines. An event that ends exactly at EOF is still delivered, because a
    final event without a trailing blank line is otherwise silently lost.
    """
    lines: list[str] = []
    size = 0
    async for line in response.aiter_lines():
        if line.startswith("data:"):
            item = line[5:].lstrip()
            size += len(item)
            if size > MAX_EVENT_BYTES:
                raise GeminiError("Gemini SSE event exceeds the adapter limit")
            lines.append(item)
        elif not line and lines:
            yield _parsed_event(lines)
            lines, size = [], 0
    if lines:
        yield _parsed_event(lines)


def _parsed_event(lines: list[str]) -> dict:
    try:
        return json.loads("\n".join(lines))
    except ValueError as exc:
        raise GeminiError("Gemini returned a malformed SSE event") from exc


async def reply_events(http: httpx.AsyncClient, api_key: str, system: str,
                       contents: list[dict], *, model: str = DEFAULT_MODEL,
                       max_output_tokens: int = 2048,
                       timeout: float = GENERATION_SECONDS,
                       first_text_timeout: float | None = None,
                       trace_attempt: int | None = None):
    """Stream one reply as ``{"kind": "text"}`` deltas then one ``complete``.

    Only non-thought text is emitted for speech. The complete event carries
    every returned part, preserved verbatim so opaque ``thoughtSignature``
    fields survive into the next request. A generation that never reaches
    ``STOP`` with spoken text raises instead of yielding ``complete``, so a
    caller can never record a partial reply as provider history.
    """
    if not api_key:
        raise ValueError("GEMINI_API_KEY is required before requesting a reply.")
    saved_parts: list[dict] = []
    finish_reason = None
    spoke = False
    started = time.monotonic()
    async with asyncio.timeout(timeout):
        try:
            async with asyncio.timeout(first_text_timeout) as first_text_guard:
                async with http.stream(
                    "POST",
                    gemini_url(model, "streamGenerateContent"),
                    params={"alt": "sse"},
                    headers={"x-goog-api-key": api_key},
                    json=gemini_body(system, contents, max_output_tokens=max_output_tokens, model=model),
                    timeout=httpx.Timeout(READ_SECONDS, connect=CONNECT_SECONDS),
                ) as response:
                    if trace_attempt is not None:
                        yield _attempt_trace("response-headers", trace_attempt, model, started,
                                             http_status=response.status_code)
                    response.raise_for_status()
                    async for event in sse_objects(response):
                        if "error" in event or event.get("promptFeedback", {}).get("blockReason"):
                            raise GeminiError("Gemini request failed or was blocked")
                        for candidate in event.get("candidates", []):
                            if candidate.get("index", 0) != 0:
                                continue
                            if candidate.get("finishReason"):
                                finish_reason = candidate["finishReason"]
                            for part in candidate.get("content", {}).get("parts", []):
                                if "functionCall" in part:
                                    raise GeminiError("Unexpected tool in the text-only adapter")
                                saved_parts.append(deepcopy(part))
                                if part.get("text") and not part.get("thought", False):
                                    if not spoke:
                                        # Only the first spoken token has a short phone deadline.
                                        # Normal transport/generation bounds cover the rest.
                                        first_text_guard.reschedule(None)
                                        if trace_attempt is not None:
                                            yield _attempt_trace("first-text", trace_attempt, model, started)
                                    spoke = True
                                    yield {"kind": "text", "text": part["text"]}
        except TimeoutError:
            if first_text_guard.expired():
                raise _FirstTextTimeout("Gemini did not produce spoken text in time") from None
            raise
    if not spoke and finish_reason in {None, "STOP"}:
        raise _EmptyGeminiResponse("Gemini response incomplete, blocked, or empty")
    if finish_reason != "STOP":
        raise GeminiError("Gemini response incomplete, blocked, or empty")
    yield {"kind": "complete", "content": {"role": "model", "parts": saved_parts}}


async def reply_events_with_retry(http, api_key, system, contents, *,
                                  first_text_timeout=None, trace=False, **kwargs):
    """Retry one empty or transiently failed reply before any text was emitted.

    An observed provider response can be HTTP 200 + STOP with no content. Both
    attempts share one deadline. A partially spoken response is never replayed.
    Explicit blocked/truncated output, malformed data, and permanent HTTP
    failures do not retry. Caller cancellation always propagates immediately.
    """
    async with asyncio.timeout(kwargs.get("timeout", GENERATION_SECONDS)):
        for attempt in range(2):
            emitted = False
            started = time.monotonic()
            model = kwargs.get("model", DEFAULT_MODEL)
            if trace:
                yield _attempt_trace("request-started", attempt + 1, model, started)
            try:
                async for event in reply_events(http, api_key, system, contents,
                        first_text_timeout=first_text_timeout,
                        trace_attempt=attempt + 1 if trace else None, **kwargs):
                    emitted = emitted or event.get("kind") == "text"
                    yield event
                return
            except (GeminiError, httpx.TimeoutException, httpx.NetworkError,
                    httpx.HTTPStatusError) as exc:
                reason = _reply_retry_reason(exc)
                if trace:
                    yield _attempt_trace("request-failed", attempt + 1, model, started,
                                         reason=reason or "invalid-response", emitted_text=emitted)
                if attempt or emitted or reason is None:
                    raise
                yield {"kind": "retry", "reason": reason, "attempt": 2}


def _reply_retry_reason(exc: Exception) -> str | None:
    """Return a bounded, non-sensitive trace reason for a retryable failure."""
    if isinstance(exc, _FirstTextTimeout):
        return "first-text-timeout"
    if isinstance(exc, _EmptyGeminiResponse):
        return "empty-response"
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return f"http-{status}" if status in {408, 429, 500, 502, 503, 504} else None
    for error_type, reason in (
        (httpx.ReadTimeout, "read-timeout"),
        (httpx.ConnectTimeout, "connect-timeout"),
        (httpx.WriteTimeout, "write-timeout"),
        (httpx.PoolTimeout, "pool-timeout"),
        (httpx.TimeoutException, "transport-timeout"),
        (httpx.ConnectError, "connect-error"),
        (httpx.ReadError, "read-error"),
        (httpx.WriteError, "write-error"),
        (httpx.CloseError, "close-error"),
        (httpx.NetworkError, "network-error"),
    ):
        if isinstance(exc, error_type):
            return reason
    return None


def attributed(speaker: str, text: str) -> dict:
    """One utterance as a ``user`` turn, with the speaker label inside the text.

    Roles stay ``user``/``model`` because that is the provider's vocabulary;
    who actually spoke is application data and belongs in the text.
    """
    if speaker not in SPEAKERS:
        raise ValueError("Speakers must be owner or remote.")
    text = str(text).strip()
    if not text:
        raise ValueError("A conversation turn needs text.")
    return {"role": "user", "parts": [{"text": f"{speaker}: {text}"}]}


@dataclass
class Conversation:
    """Delegate dialogue: one trusted instruction plus attributed history.

    ``contents`` is the provider's own history and is only ever appended to.
    Selecting a different prompt profile replaces the instruction and keeps
    the history, so facts gathered under ``#1`` survive a switch to ``#3``.
    """

    goal: str = ""
    boundaries: str = ""
    contents: list[dict] = field(default_factory=list)
    agent_reply_number: int | None = None
    announcement_provided: bool = True
    runtime_instruction: str = ""

    def __post_init__(self):
        if (self.agent_reply_number is not None
                and (type(self.agent_reply_number) is not int or self.agent_reply_number < 1)):
            raise ValueError("The next agent reply number must be a positive integer.")

    @property
    def system(self) -> str:
        """The instruction sent with the next request: rules, then the mode."""
        parts = [DELEGATE_INSTRUCTION]
        if self.goal.strip():
            parts.append(f"Current goal: {self.goal.strip()}")
        if self.boundaries.strip():
            parts.append(f"Selected agent personality and task: {self.boundaries.strip()}")
        if self.agent_reply_number is not None:
            parts.append(
                f"Runtime call progress: Your next reply is agent reply {self.agent_reply_number} "
                f"since this activation. You have completed {self.agent_reply_number - 1} "
                "fully played agent replies since this activation. This trusted count "
                "excludes earlier human conversation, caller replies, individual sentences, "
                "interrupted replies, and the joining announcement. Use this count when "
                "following the selected task's turn instructions."
            )
            if self.announcement_provided:
                parts.append("The caller hears a separate AI joining announcement before your first reply; "
                             "do not repeat that introduction or announce that you are the owner's AI assistant again. "
                             "Continue the existing conversation naturally.")
            parts.append(reply_progress_instruction(self.agent_reply_number))
        if self.runtime_instruction:
            parts.append(self.runtime_instruction)
        return "\n".join(parts)

    @classmethod
    def handoff(cls, transcript=(), goal: str = "", boundaries: str = "", *,
                agent_reply_number: int | None = None, announcement_provided: bool = True,
                runtime_instruction: str = ""):
        """Start a delegated conversation from the pre-handoff transcript."""
        conversation = cls(goal=goal, boundaries=boundaries,
                           agent_reply_number=agent_reply_number, announcement_provided=announcement_provided,
                           runtime_instruction=runtime_instruction)
        if transcript:
            conversation.add_context(transcript)
        return conversation

    def add_context(self, turns) -> "Conversation":
        """Attach prior dialogue as one labelled packet, not as loose turns."""
        lines = []
        for speaker, text in turns:
            if speaker not in SPEAKERS:
                raise ValueError("Speakers must be owner or remote.")
            text = str(text).strip()
            if text:
                lines.append(f"{speaker}: {text}")
        if not lines:
            raise ValueError("A handoff context needs at least one owner or remote turn.")
        packet = "Conversation so far:\n" + "\n".join(lines)
        self.contents.append({"role": "user", "parts": [{"text": packet}]})
        return self

    def set_mode(self, goal: str = "", boundaries: str = "") -> "Conversation":
        """Select a prompt profile, keeping both history and the voice."""
        self.goal, self.boundaries = goal, boundaries
        return self

    def add_owner(self, text: str) -> "Conversation":
        self.contents.append(attributed("owner", text))
        return self

    def add_remote(self, text: str) -> "Conversation":
        self.contents.append(attributed("remote", text))
        return self

    def add_correction(self, text: str) -> "Conversation":
        """Record what was actually played, so a cleared reply is not implied heard."""
        text = str(text).strip()
        if not text:
            raise ValueError("A delivery correction needs text.")
        self.contents.append({"role": "user", "parts": [{"text": f"delivery: {text}"}]})
        return self

    def record(self, content: dict) -> "Conversation":
        """Keep complete provider parts, later request bodies copy them again."""
        self.contents.append(deepcopy(content))
        return self

    async def reply(self, http: httpx.AsyncClient, api_key: str, *,
                    model: str = DEFAULT_MODEL, max_output_tokens: int = 2048,
                    timeout: float = GENERATION_SECONDS):
        """Yield spoken text deltas, recording the provider content on completion.

        Abandoning this generator mid-reply records nothing, which is what a
        barge-in or mode change needs: the next request must not contain a
        partial reply as though the model had finished it.
        """
        commands = ReplyCommandBuffer()
        async for event in reply_events(http, api_key, self.system, self.contents,
                                        model=model, max_output_tokens=max_output_tokens,
                                        timeout=timeout):
            if event["kind"] == "text":
                spoken = commands.feed(event["text"])
                if spoken:
                    yield spoken
            else:
                self.record(event["content"])
        # Offline text/audio consumers have no phone controller. They omit the
        # control syntax too; only DialogueRun is allowed to act on a command.
        commands.finish()


class ReplyCommandBuffer:
    """Separate a final standalone end-call command from streamed model speech.

    Only ``finish`` authorizes a command, after the provider completed normally.
    Exact marker lines are suppressed even if later text invalidates them.
    Inline/quoted text, unknown markup, and fenced examples remain ordinary
    speech. A possible marker prefix is held across chunks and dropped at EOF,
    so incomplete control syntax never reaches synthesis.
    """

    def __init__(self):
        self.end_call = False
        self._pending = ""
        self._ordinary = False
        self._line = ""
        self._fence = None
        self._commands = 0
        self._trailing_text = False

    def feed(self, text: str) -> str:
        if not isinstance(text, str):
            raise ValueError("Streamed text must be a string.")
        output = []
        for char in text:
            if self._commands and not char.isspace():
                self._trailing_text = True
            self._line += char
            candidate = self._pending + char
            if self._ordinary or self._fence:
                output.append(char)
            elif candidate in (END_CALL + "\n", END_CALL + "\r\n"):
                self._commands += 1
                self._pending = ""
            elif END_CALL.startswith(candidate) or candidate == END_CALL + "\r":
                self._pending = candidate
            else:
                output.append(candidate)
                self._pending = ""
                self._ordinary = True
            if char == "\n":
                line = self._line.strip()
                if self._fence:
                    if line.startswith(self._fence):
                        self._fence = None
                elif line.startswith(("```", "~~~")):
                    self._fence = line[:3]
                self._line = ""
                self._ordinary = False
        return "".join(output)

    def finish(self) -> None:
        """Commit only one complete command occupying the final nonempty line."""
        if self._pending in (END_CALL, END_CALL + "\r"):
            self._commands += 1
        self._pending = ""
        self.end_call = self._commands == 1 and not self._trailing_text


class SentenceBuffer:
    """Turn streamed text into speakable phrases for one bounded TTS queue.

    Phrases end at sentence punctuation, or at a word boundary once the text
    reaches ``limit`` characters so an unpunctuated reply cannot become one
    long utterance. The remainder is only released by ``flush`` after the
    provider signalled completion.
    """

    PUNCTUATION = ".!?;:"

    def __init__(self, limit: int = 120):
        if type(limit) is not int or isinstance(limit, bool) or limit < 8:
            raise ValueError("Sentence limit must be an integer of at least 8.")
        self.limit = limit
        self._buffer = ""

    def feed(self, text: str) -> list[str]:
        """Add a delta and return every phrase it completed."""
        if not isinstance(text, str):
            raise ValueError("Streamed text must be a string.")
        self._buffer += text
        phrases = []
        while (end := self._boundary()) is not None:
            phrase = self._buffer[:end].strip()
            self._buffer = self._buffer[end:].lstrip()
            if phrase:
                phrases.append(phrase)
        return phrases

    def flush(self) -> str | None:
        """Release the trailing phrase once the reply is complete."""
        phrase = self._buffer.strip()
        self._buffer = ""
        return phrase or None

    def _boundary(self) -> int | None:
        """Exclusive end index of the next phrase, or None to keep buffering."""
        window = self._buffer[:self.limit]
        mark = max((window.rfind(mark) for mark in self.PUNCTUATION), default=-1)
        if mark >= 0:
            return mark + 1
        if len(self._buffer) >= self.limit:
            space = window.rfind(" ")
            # One unbroken token still has to be released, or nothing speaks.
            return space if space > 0 else self.limit
        return None

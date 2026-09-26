# Build the voice pipeline

**Partner reference only:** current implementation work is limited to Twilio routing and passive capture. AI detection and voice-agent code remain partner-owned; use [PARTNER_HANDOFF.md](PARTNER_HANDOFF.md) for the implemented integration seam.

Implement **Deepgram Nova-3 → Claude Haiku 4.5 → ElevenLabs Flash v2.5** inside the Python relay in [IMPLEMENTATION.md](IMPLEMENTATION.md). These are implementation instructions and adapter examples; the running Build 1 does not yet contain this pipeline. Provider contracts were checked against official documentation on September 26, 2026. Real API and phone tests remain part of implementation.

## Set up configuration

1. Add `httpx` and `websockets` as direct runtime dependencies and regenerate `requirements-lock.txt` after testing. The examples use `websockets.asyncio.client.connect` with `additional_headers`, the current asyncio API. HTTPX supports asynchronous streamed responses. [websockets client](https://websockets.readthedocs.io/en/stable/reference/asyncio/client.html), [HTTPX streaming](https://www.python-httpx.org/async/).
2. Add the variables below to `.env.example` without values for secrets. Extend the app settings loader to read them. Use these defaults; do not spend the first build evaluating vendors.
3. Put actual settings in the local development `.env`, or in `~/Library/Application Support/NewCollegeOperator/.env` for the installed server. Restart that service after configuration changes, following [SERVER.md](SERVER.md). A GitHub push changes code; it does not populate private credentials.

```dotenv
DEEPGRAM_API_KEY=
DEEPGRAM_MODEL=nova-3
ANTHROPIC_API_KEY=
ANTHROPIC_MODEL=claude-haiku-4-5-20251001
ELEVENLABS_API_KEY=
ELEVENLABS_VOICE_ID=
ELEVENLABS_MODEL=eleven_flash_v2_5
ELEVENLABS_OUTPUT_FORMAT=ulaw_8000
```

The Claude default is the exact API model ID listed in Anthropic's current model table. Keep it configurable for account availability or retirement. [Claude models](https://platform.claude.com/docs/en/models/overview).

## Create the owner's voice once

1. Record about 1–2 minutes of the owner's clean, natural speech. Keep the samples outside Git. Use an Instant Voice Clone for the first build; ElevenLabs documents it as suitable for Flash and low latency. [Instant Voice Cloning](https://elevenlabs.io/docs/eleven-creative/voices/voice-cloning/instant-voice-cloning), [latency guidance](https://elevenlabs.io/docs/eleven-api/guides/how-to/best-practices/latency-optimization).
2. Implement `scripts/clone_voice.py` from the request below. Accept an explicit `--env-file`, a voice name, and sample paths. Load that environment with `python-dotenv`; never print API keys or full request headers.
3. Save the returned ID as `ELEVENLABS_VOICE_ID`. If `requires_verification` is true, complete the provider's verification before using that voice. Reuse the ID on every call; never create a clone inside the media handler.

`POST https://api.elevenlabs.io/v1/voices/add` takes multipart `name` and repeated `files` fields; authenticate with `xi-api-key`. Let HTTPX create the multipart boundary. The API response contains `voice_id` and `requires_verification`. The `files` field spelling below matches the official Python client's request construction. [Create IVC voice](https://elevenlabs.io/docs/api-reference/voices/ivc/create), [official client](https://github.com/elevenlabs/elevenlabs-python/blob/main/src/elevenlabs/voices/ivc/raw_client.py).

```python
from contextlib import ExitStack
from pathlib import Path
import httpx


def create_clone(api_key: str, name: str, sample_paths: list[str]) -> dict:
    if not sample_paths:
        raise ValueError("Supply at least one voice sample")
    with ExitStack() as stack:
        files = [
            ("files", (Path(path).name, stack.enter_context(open(path, "rb"))))
            for path in sample_paths
        ]
        response = httpx.post(
            "https://api.elevenlabs.io/v1/voices/add",
            headers={"xi-api-key": api_key},
            data={"name": name},
            files=files,
            timeout=120,
        )
        response.raise_for_status()
        return response.json()
```

Before call integration, use this voice to generate one sentence and listen locally. That isolates voice/account issues from Twilio routing.

## Stream the other person's speech to Deepgram

Open one persistent STT WebSocket for the remote party. Forward decoded Twilio `media.payload` bytes as binary WebSocket messages, preserving order. Twilio's raw mono μ-law at 8 kHz can go directly to Deepgram with no conversion. For separate owner and remote transcripts, use separate STT connections; do not interleave their mono frames. [Deepgram's Twilio integration](https://developers.deepgram.com/docs/twilio-and-deepgram-stt).

```python
from contextlib import asynccontextmanager
from urllib.parse import urlencode
from websockets.asyncio.client import connect


@asynccontextmanager
async def open_stt(api_key: str, model: str = "nova-3"):
    query = urlencode({
        "model": model,
        "language": "en-US",
        "encoding": "mulaw",
        "sample_rate": 8000,
        "channels": 1,
        "interim_results": "true",
        "punctuate": "true",
        "endpointing": 300,
        "utterance_end_ms": 1000,
        "vad_events": "true",
    })
    async with connect(
        "wss://api.deepgram.com/v1/listen?" + query,
        additional_headers={"Authorization": "Token " + api_key},
        open_timeout=10,
    ) as socket:
        yield socket
```

The bridge's sender calls `await socket.send(base64.b64decode(payload))`. A separate receiver parses the returned JSON; it must keep reading while model and TTS tasks run. The endpoint, header, and query fields are specified in [Deepgram Live Audio](https://developers.deepgram.com/reference/speech-to-text/listen-streaming).

1. On `Results` with `is_final=true`, append `channel.alternatives[0].transcript` to a per-turn buffer. On `speech_final=true`, submit the accumulated text once, then empty the buffer. Check `speech_final` even when that result has an empty transcript. [Final transcript handling](https://developers.deepgram.com/docs/twilio-and-deepgram-stt).
2. On `UtteranceEnd`, flush only an unsubmitted buffer. Ignore `last_word_end=-1` and deduplicate already submitted word ranges. This event depends on `interim_results=true`; the 1000 ms setting is a fallback, not another trigger for the same response. [Utterance End](https://developers.deepgram.com/docs/utterance-end).
3. On the remote party's `SpeechStarted`, cancel the current reply and clear Twilio playback through the relay. `vad_events=true` enables that event. Keep owner control speech separate from remote barge-in. [Speech Started](https://developers.deepgram.com/docs/speech-started).
4. Continue sending incoming audio while AI speech plays. If the audio source pauses entirely, send the text message `{"type":"KeepAlive"}` every three seconds; close the provider socket when the call ends. Do not send that JSON as binary audio. [Deepgram KeepAlive](https://developers.deepgram.com/docs/audio-keep-alive).

## Generate short replies with Claude

Use `POST https://api.anthropic.com/v1/messages`, `x-api-key`, `anthropic-version: 2023-06-01`, and JSON. Send the active mode's instructions in the top-level `system` field and dialogue history in `messages`. Request `stream=true`, cap replies at 200 tokens, and instruct the model to answer in one or two short spoken sentences. [Messages API](https://platform.claude.com/docs/en/api/messages/create).

Map application speakers to valid API roles: include the pre-handoff owner/remote transcript in an initial `user` context packet with explicit speaker labels. Subsequent remote turns use `user`; delivered agent speech uses `assistant`. `owner` and `remote` are not Claude API role values. Track partial or cleared agent speech separately so history does not claim the other party heard it in full. Merge adjacent same-role entries when assembling requests.

```python
import json
import httpx


async def reply_text(
    http: httpx.AsyncClient,
    api_key: str,
    system: str,
    messages: list[dict],
    model: str = "claude-haiku-4-5-20251001",
):
    async with http.stream(
        "POST",
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": 200,
            "system": system,
            "messages": messages,
            "stream": True,
        },
        timeout=30,
    ) as response:
        response.raise_for_status()
        data_lines = []
        async for line in response.aiter_lines():
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
            elif not line and data_lines:
                event = json.loads("\n".join(data_lines))
                data_lines.clear()
                if event.get("type") == "error":
                    raise RuntimeError("Claude stream failed")
                delta = event.get("delta", {})
                if delta.get("type") == "text_delta":
                    yield delta["text"]
```

Claude emits SSE `content_block_delta` events containing `text_delta`; do not speak tool JSON or other event types. [Streaming events](https://platform.claude.com/docs/en/build-with-claude/streaming).

1. Implement a sentence buffer between `reply_text` and TTS. Flush at sentence punctuation, or at a word boundary after roughly 120 characters. Flush remaining text at stream completion. This is our initial latency/quality policy, not a provider requirement.
2. Send buffered pieces to one ordered TTS worker per call. Limit its queue and cancel it when the relay's generation number changes; discard output from older generations.
3. Preserve conversation history across `#1`–`#4`. Replace the active mode instructions for each model request. A prompt change cancels the old generation before launching a reply under the new mode.
4. For owner notifications and IVR navigation, extend the text adapter using [the tool loop below](#add-the-notification-and-ivr-tool-loop). Handle tool blocks separately from spoken text, execute the registered actions, and append their results before requesting the next response.

## Speak through the cloned voice

For the first implementation, send each complete buffered phrase to ElevenLabs' HTTP streaming endpoint. It accepts JSON `text` and `model_id`, the voice ID in the path, and `output_format` in the query. The response body is audio bytes, not SSE or a JSON audio field. [Stream speech](https://elevenlabs.io/docs/api-reference/text-to-speech/stream).

```python
import httpx


async def speech_bytes(
    http: httpx.AsyncClient,
    api_key: str,
    voice_id: str,
    text: str,
    model: str = "eleven_flash_v2_5",
    output_format: str = "ulaw_8000",
):
    async with http.stream(
        "POST",
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream",
        params={"output_format": output_format},
        headers={"xi-api-key": api_key, "content-type": "application/json"},
        json={"text": text, "model_id": model},
        timeout=30,
    ) as response:
        response.raise_for_status()
        async for chunk in response.aiter_bytes():
            if chunk:
                yield chunk
```

`ulaw_8000` is the direct Twilio path: base64-encode those raw μ-law bytes for Twilio's `media.payload`; do not prepend a WAV header. ElevenLabs' official Twilio example uses this format and `eleven_flash_v2_5` directly. Feed chunks into the relay's bounded playback writer rather than sending an entire response at once. [ElevenLabs → Twilio](https://elevenlabs.io/docs/eleven-api/guides/how-to/text-to-speech/twilio).

If testing exposes an unavailable μ-law format for the selected account/model, request `pcm_16000` and pipe the raw signed 16-bit little-endian mono stream through one FFmpeg process per utterance:

```sh
ffmpeg -hide_banner -loglevel error \
  -f s16le -ar 16000 -ac 1 -i pipe:0 \
  -ar 8000 -ac 1 -c:a pcm_mulaw -f mulaw pipe:1
```

Read FFmpeg stdout concurrently with writing stdin; use those bytes for Twilio. Close stdin after the final PCM chunk, drain stdout, and reap the process. On cancellation, terminate and reap it. Do not label PCM or MP3 as μ-law, and do not strip a presumed 44-byte header from arbitrary files. PCM formats are documented by [ElevenLabs](https://elevenlabs.io/blog/text-to-speech-api-integration); FFmpeg's `mulaw` muxer outputs raw audio without container metadata. [FFmpeg raw formats](https://ffmpeg.org/ffmpeg-formats.html#Raw-PCM-muxers).

The later optimization is ElevenLabs' `wss://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream-input`, with `model_id=eleven_flash_v2_5` and `output_format=ulaw_8000`. It accepts incremental text and returns base64 audio in JSON messages. Build the HTTP path first; move to WebSockets only after measuring phrase-boundary delay. [TTS WebSocket contract](https://elevenlabs.io/docs/api-reference/text-to-speech/v-1-text-to-speech-voice-id-stream-input).

## Acceptance checks before enabling takeover

1. A recorded 8 kHz μ-law sample produces one correct finalized Deepgram turn; duplicate end events never cause two replies.
2. A known prompt streams short text from the configured Claude model. Cancel it mid-sentence and verify no later output is played.
3. A cloned greeting plays correctly through a test Twilio call: recognizable voice, correct speed, no static. Test the PCM conversion path separately if it is enabled.
4. The remote speaker can interrupt the clone; `#0` returns control without old speech resuming. Switching modes preserves prior facts and cancels the previous response.
5. Log turn-end, first model text, first TTS bytes, first audio sent to Twilio, and phrase-completion mark times. Marks report completion or clearing, not the instant speech first becomes audible; exclude cleared marks from delivered-speech history. Measure audible delay with a phone test separately. A provider failure invokes the relay's owner-return behavior.

## Add the notification and IVR tool loop

Implement this extension before enabling `#2` notifications or autonomous IVR navigation. These are application-defined client tools: Claude requests them, and the relay executes them. Include `tools=TOOLS` and `tool_choice={"type": "auto", "disable_parallel_tool_use": True}` in every request in the loop. [Client tool lifecycle](https://platform.claude.com/docs/claude/docs/tool-use).

```python
TOOLS = [
    {
        "name": "notify_owner",
        "description": "Privately alert the connected owner when attention is needed.",
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string", "minLength": 1, "maxLength": 240}},
            "required": ["reason"], "additionalProperties": False,
        },
    },
    {
        "name": "send_dtmf",
        "description": "Send keypad digits to the remote automated phone menu.",
        "input_schema": {
            "type": "object",
            "properties": {"digits": {"type": "string", "pattern": "^[0-9A-D*#wW]{1,32}$"}},
            "required": ["digits"], "additionalProperties": False,
        },
    },
]
```

1. Extend the SSE receiver to retain every `content_block_start` by its `index`. For a `tool_use` block, retain `id` and `name` and initialize an empty argument string. Append each `input_json_delta.partial_json` to that index's string; parse it only on `content_block_stop`. Accumulate text blocks too. Never execute partial JSON. [Tool input streaming](https://platform.claude.com/docs/en/agents-and-tools/tool-use/fine-grained-tool-streaming).
2. Read `message_delta.delta.stop_reason`, then wait for `message_stop`. Execute tools only for a complete response with `stop_reason="tool_use"`. If interrupted, malformed, or truncated with `max_tokens`, execute nothing. Use a 512-token cap for requests with tools; abort to owner control if the bounded loop cannot finish.
3. Revalidate arguments in Python, allow only the two registered names, and check session mode/epoch before dispatch. `notify_owner` queues the private earcon/cue described in [IMPLEMENTATION.md](IMPLEMENTATION.md) and returns within 3 seconds. `send_dtmf` calls that document's serialized remote-leg `<Play digits>` adapter, waits for its replacement stream, and has a 45-second deadline. Give uncertain side effects an error result; never automatically resend digits. Deduplicate by `(session_id, tool_use.id)`.
4. Append the reconstructed assistant content and matching user results as below; call Messages again with the same tools and current mode instructions. Stop on `end_turn`. Limit a remote turn to three tool rounds; notify the owner on exhaustion. Send only spoken text through TTS. [Handling tool calls and errors](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls).

```python
def append_tool_results(messages, assistant_blocks, completed):
    """completed maps every tool-use ID to (result_text, failed)."""
    results = []
    for block in assistant_blocks:
        if block["type"] == "tool_use":
            result_text, failed = completed[block["id"]]
            results.append({
                "type": "tool_result",
                "tool_use_id": block["id"],
                "content": result_text,
                "is_error": failed,
            })
    messages.extend([
        {"role": "assistant", "content": assistant_blocks},
        {"role": "user", "content": results},
    ])
```

Return one result per requested tool, including rejected or failed actions with `is_error=true`. Append these results immediately after the assistant tool request before adding new remote speech. If a mode change cancels dispatch, supply an error result for unexecuted actions when preserving that history. Test notifications stay private, digits reach only the remote leg, and duplicate tool events never repeat an action.

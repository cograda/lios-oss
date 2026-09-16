"""One call signature across Anthropic, OpenAI and Google.

    from coglib.llm import call

    r = call("claude-opus-5", "What's in this photo?", image="dog.jpg")
    print(r.text, r.cost, r.secs)

Why this exists rather than three SDKs: comar alone builds four independent
LLM clients (`vision/client.py`, `transcription/gemini.py`,
`transcription/client.py`, `plugin/embedding_provider.py`), three of them the
same two lines of `genai.Client(...)` against separately-configured copies of
the same key. Every one of them then has to rediscover the same three
provider quirks below.

**Token accounting is the reason to centralise, not the calling.** Making a
POST is easy; billing it correctly is not, because the three providers report
reasoning tokens three incompatible ways:

  Google      `thoughtsTokenCount`, a *separate* counter alongside output
  OpenAI      `reasoning_tokens`, nested *inside* `output_tokens`
  Anthropic   not reported at all — thinking is simply inside `output_tokens`

So Google's must be added, OpenAI's must be subtracted to avoid double
counting, and Anthropic's cannot be separated (reported as 0 here: unavailable,
not absent — Opus 5 and Sonnet 5 both think by default). Getting this wrong
made a Gemini cost estimate 4x too low. `Response.cost` is correct on all
three; `Response.reasoning_tokens` is honest about what each provider will
tell you.

**Image support is not uniform either.** Only Google accepts HEIC/HEIF, so a
straight-from-phone iPhone photo is a hard skip on two of three providers.
That surfaces as an explicit `UnsupportedInput` naming the provider and mime
rather than a 400 from somewhere deep in a stack trace.

Deliberately httpx-only: no provider SDKs. coglib is vendored into deployed
container images, so a dependency here is a dependency everywhere.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "MODELS",
    "LLMError",
    "MissingKey",
    "ModelSpec",
    "Response",
    "UnsupportedInput",
    "call",
    "resolve",
]

# Where keys live, in preference order: an explicit file per provider, then the
# conventional env var. Files win because env vars are invisible to a GUI-
# launched process (the same reason comar's token lives in ~/.zshenv) and get
# lost between shells.
KEY_DIR = Path(os.environ.get("COGLIB_KEY_DIR", Path.home() / ".config" / "comar"))

_KEY_FILES = {
    "anthropic": ("anthropic_api_key", "ANTHROPIC_API_KEY"),
    "openai": ("openai_api_key", "OPENAI_API_KEY"),
    "google": ("gemini_api_key", "GEMINI_API_KEY"),
}


class LLMError(RuntimeError):
    """Base for every failure this module raises deliberately."""


class MissingKey(LLMError):
    """No API key for the provider this model belongs to."""


class UnsupportedInput(LLMError):
    """The provider cannot accept this input (e.g. HEIC to Anthropic)."""


@dataclass(frozen=True)
class ModelSpec:
    """A model and what it costs.

    Rates are $/1M tokens. They are recorded here rather than looked up because
    no provider exposes pricing over its API, and a cost you cannot compute is
    a cost you will not check.
    """

    provider: str
    input_rate: float
    output_rate: float
    notes: str = ""


# Rates current as of 2026-09-02 (embedding row); chat rows 2026-08-07. A stale rate silently produces a wrong
# `cost`, so treat this table as something to check when a bill surprises you.
MODELS: dict[str, ModelSpec] = {
    # Anthropic
    "claude-opus-5": ModelSpec("anthropic", 5.00, 25.00),
    "claude-opus-4-8": ModelSpec("anthropic", 5.00, 25.00),
    "claude-sonnet-5": ModelSpec("anthropic", 3.00, 15.00),
    "claude-sonnet-4-6": ModelSpec("anthropic", 3.00, 15.00),
    "claude-fable-5": ModelSpec("anthropic", 10.00, 50.00),
    # The bare `claude-haiku-4-5` alias is not in every account's /v1/models
    # list — only the dated snapshot is. Use the snapshot rather than assuming
    # the alias resolves; both are registered here so either works.
    "claude-haiku-4-5": ModelSpec("anthropic", 1.00, 5.00),
    "claude-haiku-4-5-20251001": ModelSpec("anthropic", 1.00, 5.00),
    # OpenAI
    "gpt-5.6-terra": ModelSpec("openai", 2.00, 12.00),
    "gpt-5.6-luna": ModelSpec("openai", 0.20, 1.20),
    "gpt-4o-mini": ModelSpec("openai", 0.15, 0.60),
    # Google
    "gemini-3.1-pro-preview": ModelSpec("google", 4.00, 18.00),
    # ⏰ INTRODUCTORY RATE — HALVES BACK ON 2027-01-01.
    # Released 2026-08-13 at $0.75/$3.75 "for the rest of 2026"; from
    # 2027-01-01 it becomes $1.50/$7.50, i.e. identical to 3.6-flash below.
    # Until then 3.7 is *cheaper than the model it replaces*, which will not
    # stay true — anything costed against these numbers doubles overnight in
    # January, silently, because nothing here is date-aware.
    # ⏰ INTRODUCTORY RATE — DOUBLES TO 1.50 / 7.50 ON 2027-01-01, same shape
    # as 3.7 below. Released 2026-09-02 at the same intro numbers as 3.7;
    # verified against Google's posted rate 2026-09-09 (not a copy-paste).
    "gemini-3.8-flash": ModelSpec("google", 0.75, 3.75),
    "gemini-3.7-flash": ModelSpec("google", 0.75, 3.75),
    "gemini-3.6-flash": ModelSpec("google", 1.50, 7.50),
    "gemini-3.5-flash-lite": ModelSpec("google", 0.30, 2.50),
    "gemini-3.1-flash-lite": ModelSpec("google", 0.25, 1.50),
    # Embedding: text input only is priced; there are no output tokens. Added
    # 2026-09-02 after 1,350 embedding calls sat in comar's ai_usage ledger as
    # "unknown cost" because this row did not exist. Audio/image/video input
    # to this model is priced differently ($6.50/M audio) and is not what we
    # send — if that ever changes, this rate is wrong for those calls.
    "gemini-embedding-2": ModelSpec("google", 0.20, 0.00, notes="text embedding; $/1M input tokens, no output"),
}

# Short names for the ones typed most often at a terminal.
ALIASES = {
    "opus": "claude-opus-5",
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5-20251001",
    "flash": "gemini-3.7-flash",
    "flash-lite": "gemini-3.5-flash-lite",
    "gpt": "gpt-5.6-terra",
    "gpt-mini": "gpt-5.6-luna",
}

# png/jpeg/webp are the common denominator. Google additionally takes HEIC —
# the difference is operational, not trivia, for anything fed by iPhone photos.
_COMMON_IMAGE_MIME = {"image/png", "image/jpeg", "image/webp", "image/gif"}
_IMAGE_MIME = {
    "anthropic": _COMMON_IMAGE_MIME,
    "openai": _COMMON_IMAGE_MIME,
    "google": _COMMON_IMAGE_MIME | {"image/heic", "image/heif"},
}


@dataclass
class Response:
    """One model's answer, with everything needed to compare it to another's."""

    text: str
    model: str
    provider: str
    prompt_tokens: int = 0
    reasoning_tokens: int = 0
    output_tokens: int = 0
    secs: float = 0.0
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def cost(self) -> float:
        """USD for this call. Reasoning bills at the output rate everywhere."""
        spec = MODELS[self.model]
        billed_out = self.output_tokens + self.reasoning_tokens
        return (self.prompt_tokens * spec.input_rate + billed_out * spec.output_rate) / 1e6

    def __str__(self) -> str:
        return self.text


def resolve(model: str) -> tuple[str, ModelSpec]:
    """Expand an alias and look up the spec. Raises on an unknown model."""
    name = ALIASES.get(model, model)
    spec = MODELS.get(name)
    if spec is None:
        raise LLMError(
            f"unknown model {model!r} — known: {', '.join(sorted(MODELS))} "
            f"(aliases: {', '.join(sorted(ALIASES))})"
        )
    return name, spec


def _key_for(provider: str) -> str:
    """The key for a provider, or "" if none is configured."""
    fname, env = _KEY_FILES[provider]
    path = KEY_DIR / fname
    if path.exists():
        text = path.read_text().strip()
        if text:
            return text
    return os.environ.get(env, "").strip()


#: Back-compat alias — `api_key()` was the public name before `call(api_key=...)`
#: existed. Kept because cogkit and the CLI below both call it.
api_key = _key_for


def available() -> list[str]:
    """Providers that currently have a key. Useful for skipping in a bench."""
    return sorted(p for p in _KEY_FILES if api_key(p))


def mime_for(path: Path) -> str | None:
    guessed, _ = mimetypes.guess_type(str(path))
    if guessed:
        return guessed
    # mimetypes misses HEIC on some systems, and HEIC is exactly the format
    # whose support differs between providers — a None here would read as
    # "unsupported everywhere" and hide Google's ability to take it.
    return {".heic": "image/heic", ".heif": "image/heif"}.get(path.suffix.lower())


def _encode_images(provider: str, images: list[Path]) -> list[tuple[str, str]]:
    out = []
    for img in images:
        mime = mime_for(img)
        if mime is None:
            raise UnsupportedInput(f"cannot determine mime type of {img.name}")
        if mime not in _IMAGE_MIME[provider]:
            raise UnsupportedInput(
                f"{provider} does not accept {mime} ({img.name}) — "
                f"accepts {', '.join(sorted(_IMAGE_MIME[provider]))}"
            )
        out.append((mime, base64.standard_b64encode(img.read_bytes()).decode()))
    return out


def call(
    model: str,
    prompt: str,
    *,
    image: str | Path | None = None,
    images: list[str | Path] | None = None,
    system: str | None = None,
    max_tokens: int = 8192,
    timeout: float = 300.0,
    api_key: str | None = None,
) -> Response:
    """Send one prompt (optionally with images) to one model.

    `max_tokens` defaults high because on models that think by default it caps
    thinking *plus* text — a tight ceiling silently truncates the answer rather
    than erroring.

    `api_key` overrides the file/env lookup for callers that hold the key
    themselves. Files and env vars are the right default for a hand-run tool,
    but a deployed service keeps its secrets somewhere else — comar keeps them
    encrypted in Postgres — and the alternative to this parameter is writing
    the key into the process environment before every call, which races
    between threads and leaks it into anything that dumps `os.environ`.
    """
    import httpx

    name, spec = resolve(model)
    key = (api_key or "").strip() or _key_for(spec.provider)
    if not key:
        fname, env = _KEY_FILES[spec.provider]
        raise MissingKey(
            f"no {spec.provider} key for {name} — put it in {KEY_DIR / fname} "
            f"or set {env}"
        )

    paths = [Path(p) for p in (images or [])]
    if image:
        paths.insert(0, Path(image))
    encoded = _encode_images(spec.provider, paths)

    fn = {"anthropic": _anthropic, "openai": _openai, "google": _google}[spec.provider]
    started = time.time()
    text, p_tok, r_tok, o_tok, raw = fn(httpx, name, prompt, encoded, system, max_tokens, timeout, key)
    return Response(
        text=text, model=name, provider=spec.provider,
        prompt_tokens=p_tok, reasoning_tokens=r_tok, output_tokens=o_tok,
        secs=round(time.time() - started, 2), raw=raw,
    )


def _check(resp) -> dict:
    if resp.status_code >= 400:
        raise LLMError(f"{resp.status_code}: {resp.text[:400]}")
    return resp.json()


def _anthropic(httpx, model, prompt, images, system, max_tokens, timeout, key):
    content: list[dict] = [
        {"type": "image", "source": {"type": "base64", "media_type": m, "data": d}}
        for m, d in images
    ]
    content.append({"type": "text", "text": prompt})
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": content}],
    }
    if system:
        body["system"] = system

    data = _check(httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json=body, timeout=timeout,
    ))
    if data.get("stop_reason") == "refusal":
        raise LLMError("refusal: the safety classifier declined this input")

    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    u = data.get("usage", {})
    # Thinking is inside output_tokens and not separable — 0 here means
    # "not reported", and the cost stays right because output already includes it.
    return text.strip(), u.get("input_tokens", 0), 0, u.get("output_tokens", 0), data


def _openai(httpx, model, prompt, images, system, max_tokens, timeout, key):
    content: list[dict] = [
        {"type": "input_image", "image_url": f"data:{m};base64,{d}"} for m, d in images
    ]
    content.append({"type": "input_text", "text": prompt})
    msgs = [{"role": "user", "content": content}]
    if system:
        msgs.insert(0, {"role": "system", "content": [{"type": "input_text", "text": system}]})

    data = _check(httpx.post(
        "https://api.openai.com/v1/responses",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"},
        json={"model": model, "input": msgs, "max_output_tokens": max_tokens},
        timeout=timeout,
    ))
    # There is no `output_text` convenience field on the raw JSON — only in the
    # SDK wrapper — so walk `output` for message parts.
    text = "".join(
        part.get("text", "")
        for item in data.get("output", [])
        if item.get("type") == "message"
        for part in item.get("content", [])
        if part.get("type") == "output_text"
    )
    u = data.get("usage", {})
    reasoning = (u.get("output_tokens_details") or {}).get("reasoning_tokens", 0) or 0
    # Subtract: reasoning is reported INSIDE output_tokens here, unlike Google.
    output = max(u.get("output_tokens", 0) - reasoning, 0)
    return text.strip(), u.get("input_tokens", 0), reasoning, output, data


def _google(httpx, model, prompt, images, system, max_tokens, timeout, key):
    parts: list[dict] = [
        {"inline_data": {"mime_type": m, "data": d}} for m, d in images
    ]
    parts.append({"text": prompt})
    body: dict = {
        "contents": [{"parts": parts}],
        "generationConfig": {"maxOutputTokens": max_tokens},
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}

    data = _check(httpx.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
        json=body, timeout=timeout,
    ))
    candidates = data.get("candidates") or []
    text = "".join(
        p.get("text", "")
        for c in candidates
        for p in (c.get("content", {}).get("parts") or [])
    )
    u = data.get("usageMetadata", {})
    # Add: thoughts are a SEPARATE counter here, not inside candidatesTokenCount.
    return (
        text.strip(),
        u.get("promptTokenCount", 0),
        u.get("thoughtsTokenCount", 0) or 0,
        u.get("candidatesTokenCount", 0) or 0,
        data,
    )


def _main(argv: list[str] | None = None) -> int:
    """`python -m coglib.llm <model> <prompt> [--image X] [--json]`"""
    import argparse

    ap = argparse.ArgumentParser(prog="coglib.llm", description=__doc__.split("\n")[0])
    ap.add_argument("model", nargs="?", help="model id or alias")
    ap.add_argument("prompt", nargs="?", help="the prompt; '-' reads stdin")
    ap.add_argument("--image", action="append", default=[], help="repeatable")
    ap.add_argument("--system")
    ap.add_argument("--max-tokens", type=int, default=8192)
    ap.add_argument("--json", action="store_true", help="emit the full record")
    ap.add_argument("--list", action="store_true", help="list models and keys")
    args = ap.parse_args(argv)

    if args.list or not args.model:
        have = available()
        print(f"keys found in {KEY_DIR}: {', '.join(have) or 'none'}\n")
        print(f"{'model':30}{'provider':11}{'$/M in':>9}{'$/M out':>9}   key?")
        for name, spec in sorted(MODELS.items(), key=lambda kv: (kv[1].provider, kv[0])):
            mark = "yes" if spec.provider in have else "NO"
            print(f"{name:30}{spec.provider:11}{spec.input_rate:>9.2f}"
                  f"{spec.output_rate:>9.2f}   {mark}")
        print("\naliases: " + ", ".join(f"{k}={v}" for k, v in sorted(ALIASES.items())))
        return 0

    import sys

    prompt = sys.stdin.read() if args.prompt == "-" else (args.prompt or "")
    r = call(args.model, prompt, images=args.image, system=args.system,
             max_tokens=args.max_tokens)

    if args.json:
        print(json.dumps({
            "model": r.model, "provider": r.provider, "text": r.text,
            "prompt_tokens": r.prompt_tokens, "reasoning_tokens": r.reasoning_tokens,
            "output_tokens": r.output_tokens, "cost": r.cost, "secs": r.secs,
        }, indent=2))
    else:
        print(r.text)
        print(f"\n— {r.model} · {r.secs}s · in={r.prompt_tokens} "
              f"reason={r.reasoning_tokens} out={r.output_tokens} · ${r.cost:.5f}",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

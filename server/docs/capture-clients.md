# Capture clients — the Shortcuts that feed the inbox

Two iOS/macOS Shortcuts put things into comar's inbox from a phone:

| Shortcut | Does |
|---|---|
| **Dictator** | records a voice note, posts the audio |
| **Send-to-Comar** | posts any file from the share sheet |

Both are *dumb transport*. They move bytes and nothing else — no
transcription, no classification, no naming. That is the contract
`routes/inbox.py` states, and it is what let the Tines story be deleted in
2026-08-29 without rebuilding any of its logic: everything Tines did between
receiving the audio and filing the result already existed server-side.

⚠️ **Do not add cleverness to a Shortcut.** Anything a client computes is
computed once per client, drifts per device, and cannot be fixed without
touching every phone. The Tines story is the cautionary tale — it carried its
own transcription prompt, its own model pin and its own proper-noun list, all
of which went stale invisibly because nothing ever compared them to comar's.

## The request

    POST https://ingest.comar.ie/api/inbox/ingest
    Content-Type: application/json
    CF-Access-Client-Id:     <service token client id>
    CF-Access-Client-Secret: <service token secret>
    Authorization: Bearer    <comar client token>

    {
      "filename": "voice-note.m4a",
      "type":     "audio",
      "data":     "<base64 of the file>",
      "metadata": {
        "source":    "dictator",
        "recording": "<original filename>",
        "recorded":  "<ISO timestamp the recording was made>"
      }
    }

**Two credentials, two different jobs**, and it is worth keeping them straight:

- The **`CF-Access-*` pair** is the Cloudflare edge gate. It decides whether
  the request reaches the house at all. One per *device* — see
  `infra/docs/cloudflare-tunnel.md`.
- The **`Authorization` bearer** is a comar `client_tokens` row. It decides
  *whose* inbox the file lands in — `_resolve_caller_user_id` resolves it to a
  real user, so Sam's captures land in `u2` by identity. One per *person*.

There is no other credential. The shared `inbox_token` (the Tines relay's
webhook secret) and the dashboard `HOME_UI_TOKEN` used to be accepted here too,
attributed to user 1; both were removed on 2026-09-06 — lios has one
credential, the per-user bearer — so the route answers 401 to anything but a
`client_tokens` row.

⚠️ **Do not set `metadata.note`.** It is honoured if present and takes
precedence over the transcript, so a well-meaning client that fills it in with
a filename permanently suppresses the real transcript for that file.

### Minting a comar client token

    POST /api/auth/clients   {"user": "<user name>", "label": "dictator-<device>"}

Returns the token **once**. One per person per device; label them so a lost
phone is one obvious revocation.

## Building Dictator

1. **Record Audio** (or **Get Latest Voice Memo** / share-sheet input)
2. **Base64 Encode** the file
3. **Text** → the JSON body above, with the base64 substituted
4. **Get Contents of URL**
   - Method `POST`, Request Body `File`/`Text` with the JSON
   - Headers: the four above
5. **Handle the response** — see the two cases below

## Building Send-to-Comar

Identical, except it takes share-sheet input rather than recording, and sets
`"type": "file"` (or omits `type` entirely — unknown values fall through to
`/inbox/incoming/` and the server classifies by magic bytes). Set
`metadata.source` to `"send-to-comar"`.

## Two failure cases the Shortcuts must handle

Both exist because of decisions made elsewhere; neither is optional.

### 1. HTTP 429 → wait 10s, retry once

The Cloudflare rate limit is **3 requests / 10s per IP**, blocking for 10s —
the Free plan caps both windows at 10 seconds, so burst tolerance is 3, not
the ~18/min it works out to. Posting several files from the share sheet in one
go *will* hit this.

A 429 is not a failure. **Wait 10 seconds and retry.** Reporting it to the
user as an error trains them to distrust a capture path that is working fine.

### 2. Any other failure → save locally, notify locally

**This is the most important behaviour in this document**, and the reason is
architectural rather than defensive.

Everything in the capture path now runs *inside* comar: the transcription, the
retry, the push, the email. So if comar is down, nothing server-side can tell
you the capture failed — the alarm dies with the thing it is reporting on. The
retired Tines story had this property deliberately: its `Comar Push Failed`
action posted straight to Home Assistant, bypassing comar entirely.

The client is now the only place that independence can live:

- **Save the audio** to a known Files folder (iCloud, or a folder the Mac's
  comar-client daemon watches — then it drains automatically when comar
  returns).
- **Show a local notification** saying the capture was kept but not filed.

A voice note is unrecoverable — you cannot re-record the thought. Losing one
because a server was rebooting is the worst outcome this whole path has, and
it is entirely preventable on the client.

## Sam's setup

Same two Shortcuts, three different values:

| | |
|---|---|
| `CF-Access-*` | the `capture-sam-iphone` service token |
| `Authorization` | a comar client token minted for her user |
| everything else | identical |

Her captures then land in `u2` with no server-side change — no email
allow-list, no `IF(email = ...)` formula. That routing was a Tines expression;
here it falls out of which token was used.

Email goes to `sam@comar.ie` via `notifications`' `email_targets` map, and
her push routes by the `targets` map. Both are config, not code.

## Verifying a client end to end

    # should be 401 — reaches comar, rejected by comar
    curl -s -o /dev/null -w '%{http_code}\n' \
      -H "CF-Access-Client-Id: $ID" -H "CF-Access-Client-Secret: $SEC" \
      -X POST https://ingest.comar.ie/api/inbox/ingest

    # should be 200 with a summary
    curl -s -H "CF-Access-Client-Id: $ID" -H "CF-Access-Client-Secret: $SEC" \
      -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
      -X POST https://ingest.comar.ie/api/inbox/ingest \
      -d '{"filename":"probe.txt","type":"text","data":"aGVsbG8="}'

Then confirm the file is owned by the right person — a probe that lands in the
wrong user's inbox is a wrong bearer, not a broken endpoint.

For a real audio probe, expect: an immediate `summary` in the response, then a
push carrying the transcript's `title` within a minute or so (the ingest route
fires transcription as a background task; the `*/5` cron is only the retry
net), then the email with `transcript.txt` attached.

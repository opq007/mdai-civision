# AGENTS.md

Working notes for anyone (human or agent) changing this repository.

## What this project is

An OpenAI-compatible video generation API backed by ModelScope Civision's
web endpoints, plus an admin UI for managing accounts, proxies and magicube
budgets. The upstream contract is documented in [SPEC.md](SPEC.md) and was
verified against the live site.

## Core rule: never spend magicubes to test

**Magicubes are a real, non-renewable resource. Never run a live generation
just to check that something works, unless the user explicitly asks for it.**

This is the single most important rule in this file.

### Why

Magicube cost is driven by resolution and duration, and it escalates fast:

| Model | Resolution | Duration | Magicubes |
|---|---|---|---|
| Wan2.1-T2V-1.3B | 480×480 | 3s | ~2 |
| Wan2.1-T2V-1.3B | 720×1280 | 5s | ~23 |
| Wan2.2-T2V-A14B | 720×1280 | 5s | ~23 |
| MiniMax-H3 | 768×1280 | 10s | **~563** |

A single careless call can burn more magicubes than a test account holds.
The MiniMax-H3 figure alone is more than double the balance of a fresh
account.

### How to verify changes

Verify **without** generating a video:

1. **Parameter mapping** — assert on the resolved values, not on upstream
   behaviour. `resolve_size()` and `resolve_seconds()` are pure functions;
   call them with a model and check the result is in `model.sizes` /
   `model.seconds`. This is what the unit tests in `tests/test_mapping.py`
   do.
2. **Request shape** — the upstream payload is built in
   `app/civision/client.py`. Test the builder, not the network call.
3. **Quoting** — `POST /openapi/v1/aigc/agentic/estimate-quota` is free and
   returns the exact cost. Use it to confirm what a job *would* cost.
4. **Read-only endpoints** — `users/info`, `magicubes/balance`,
   `magicubes/spend/rates` and `defaultTemplateV2` cost nothing.

The core call path is identical for every model — pick account, submit,
poll, download. Model-specific differences are confined to:

- the model id (`checkpointModelVersionId`)
- the size set the model accepts
- the duration set the model accepts
- `guidanceScale` and `numInferenceSteps` defaults
- `videoInferenceArgs`, which only MiniMax-H3 accepts

So behaviour verified on the cheapest text-to-video model carries over to
the others.

### If a live call really is unavoidable

Only with the user's explicit go-ahead, and then:

- use the **cheapest** model — Wan2.1-T2V-1.3B (`WAN_VIDEO_2_1_T2V_1_3_B`,
  versionId 546) at **480×480** and **3 seconds**
- check the balance first, and quote the expected cost to the user
- one call, not a sweep
- report the magicubes spent

Text-to-image and image-to-video on the same account are cheaper still, and
exercise the same account/session/proxy/rotation machinery.

## Other rules

### The session is a cookie session

The signed-in state is carried by `m_session_id` and `h_uid`.
`csrf_session` and `csrf_token` are issued to **anonymous** visitors too and
prove nothing on their own. Writes need an `X-CSRF-TOKEN` header holding
the URL-decoded `csrf_token`.

### Two different field conventions upstream

- the submit endpoint uses camelCase (`predictType`, `basicDiffusionArgs`)
- the quota-estimate endpoint uses snake_case (`video_predict_task_request`)

Do not "simplify" one into the other.

### Upstream errors hide in HTTP 200

Many endpoints answer 200 with a failure inside the body. Check `Code`,
`Data.code` and `success` — never the status code alone. See
`CivisionClient._unwrap`.

### The upstream catalog is live data

Do not hard-code model lists, size sliders or durations. They are pulled from
`/api/v1/muse/predict/unauth/defaultTemplateV2` and cached for five minutes.
`tests/test_mapping.py` carries a trimmed copy for unit tests only.

### Chromium's cookie store is not durable

It drops `m_session_id` / `h_uid` across restarts. Session state therefore
lives in an explicit `storage_state.json` snapshot, re-applied with
`ctx.add_cookies()`. Keep it that way.

### `create_all` does not alter tables

New columns need an entry in `app/migrate.py`.

### Timestamps are UTC-aware

SQLite has no timezone support. `UtcDateTime` in `app/models.py` normalises
every timestamp on the way in and out. Use `utcnow()` and compare only
timezone-aware values.

### Keep the OpenAI error envelope

Clients parse `{"error": {"message", "type", "code", "param"}}` and expect
400 (not FastAPI's 422) for body validation failures. The handler in
`app/main.py` enforces this; do not remove it.

## Running things

```bash
pip install -e ".[dev]"
python -m pytest tests/ -q
python -m ruff check app/ tools/ tests/ --select F,E9
bash scripts/restart.sh          # restart the dev server on :8000
```

The dev server needs `.env` with at least `SECRET_KEY` and `ADMIN_PASSWORD`.

## Before committing

- `pytest` passes
- `ruff check --select F,E9` is clean
- no credentials in the diff: no real `m_session_id`, `h_uid`, `sk-civ-...`
  or `csrf_token` values, and no `.env`

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
| MiniMax-H3 | 768×1280 | 10s | **~113** |

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
   `tests/test_client_bodies.py` does this with a mock transport.
3. **Quoting** — `POST /openapi/v1/aigc/agentic/estimate-quota` is free and
   returns the exact cost. Use it to confirm what a job *would* cost.
4. **Read-only endpoints** — `users/info`, `magicubes/balance`,
   `magicubes/spend/rates` and `defaultTemplateV2` cost nothing.

#### The estimate endpoint is a *validator*, not just a price list

`estimate-quota` and the page's own
`/api/v1/muse/predict/getEstimateConsumeComputeQuota` parse the request body
with the same Jackson deserialisers the submit endpoint uses. So a **400 from
the estimator means upstream would reject that body** — the estimator is the
safe place to find out whether a request shape is legal.

That makes it the right tool for discovering the upstream schema. Send a field
with the wrong *type* and Jackson names the class it expected:

```
mode: 123          -> Cannot deserialize value of type
                      `VideoInferenceArgs$Mode` from number 123
                      (index value outside legal index range [0..1])
mode: "BOGUS"      -> ... not one of the values accepted for Enum class:
                      [REF2VA, FL2VA]
imageId: "abc"     -> Cannot deserialize value of type `java.lang.Long`
extraImageInputFrontArgs: "oops"
                   -> Cannot construct instance of
                      `ImageInputFrontArgs`
```

This is how the upstream `VideoInferenceArgs` and `ImageInputFrontArgs`
shapes were mapped without a single generation.

#### ⚠️ Do not probe the **submit** endpoint

The submit endpoint is the one endpoint that spends magicubes. It also
**accepts** a request before it decides to charge, so a "probe" that merely
tries to provoke a validation error can succeed and start a real generation.

**Use the estimator for every shape question.** If you do hit submit, the
task reserves its magicubes immediately and releases them only when the task
settles — a cancelled task can hold the reservation for a long time.

#### ⚠️ The test suite must not share the real database

`app.db` builds its engine at import time from `DATABASE_URL`. The default
points at `data/civision.db`, which is the **same file the running dev server
uses**. A test that creates a `video_jobs` row therefore leaves a job the
server's workers will pick up on the next restart — and a real MiniMax-H3
generation, at 34+ magicubes.

`tests/conftest.py` therefore points `DATABASE_URL` at a throwaway file
*before* anything imports `app.db`, and creates the schema there. Keep it
that way.

Two more traps in the same area:

- the routers bind `enqueue_job` / `require_api_key` at import time, so
  `monkeypatch.setattr("app.services.queue.enqueue_job", ...)` does **not**
  take effect. Patch the router's binding
  (`app.routers.videos.enqueue_job`) or use `app.dependency_overrides`.
- the test client does not run the app's lifespan, so `init_db` never fires.
  The session fixture in `tests/conftest.py` creates the schema.

### The core call path is identical for every model
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

### First-to-last-frame is a second image block, not a predict type

Upstream has **no** `FLF_2_VIDEO` predict type — the enum only accepts
`IMG_2_VIDEO`, `DIFFUSION`, `TXT_2_IMG`, `INPAINTING`, `TXT_2_AUDIO`,
`TXT_2_VIDEO` and `IMG_2_IMG`. `FLF_2_VIDEO` appears in the *catalog* as a
capability flag, which is what decides whether the page shows the switch.

The page implements first-to-last-frame as `IMG_2_VIDEO` plus a second image
block:

```json
"imageInputFrontArgs":     { "image": "<start url>", "stength": 1 },
"extraImageInputFrontArgs": { "image": "<end url>",   "stength": 1 }
```

`stength` is upstream's own spelling. Upstream ignores the field (any type is
accepted), but the page sends it, so the service sends it too.

### Never set `mode` in `videoInferenceArgs`

Upstream's `VideoInferenceArgs` is:

```
accelerateLora : Boolean
mode           : Mode            // [REF2VA, FL2VA]
references     : List<Reference>
```

Setting `mode` explicitly **breaks MiniMax-H3**:

```
videoInferenceArgs: {accelerateLora: true}                       -> accepted
videoInferenceArgs: {mode: "REF2VA", accelerateLora: true}       -> rejected
                         "MiniMax-H3 推理加速当前仅支持 FL2VA 模式"
```

The civision page never sets `mode` — it sends `{accelerateLora: P}` and
nothing else. Match the page: no `mode`, no `references`.

### The admin UI is HTML, the API is JSON

`/admin/*` renders pages; `/v1/*` serves OpenAI clients. A browser that
opens `/admin` without a session must be **redirected to `/admin/login`**,
not handed a JSON error envelope. The guard raises `AdminLoginRequired`
(the `app/main.py` handler turns it into a 303) — note that a dependency
that *returns* a `Response` does **not** short-circuit the route in FastAPI,
so raising is the only way to do it from a dependency.

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

### Only three models are exposed

`ALLOWED_MODELS` in `app/civision/mapping.py` holds the three upstream models
this service offers:

- `WAN_VIDEO_2_1_T2V_1_3_B` - Wan2.1-T2V-1.3B, text to video
- `WAN_VIDEO_2_1_I2V_14_B` - Wan2.1-I2V-14B-720P, image to video
- `MINIMAX_H3` - MiniMax-H3, text / image / first-last frame

The other five upstream models are filtered out. `sora-2` and `sora-2-pro`
are aliases onto the first and the third.

The cost table in `app/civision/costs.py` was measured with the free
`estimate-quota` endpoint. It is keyed by civision value, so a newly allowed
model has no costs until they are measured and added.

### Image-to-video needs an uploaded reference

`IMG_2_VIDEO` is rejected by upstream unless the request carries the first
frame. The service therefore uploads the caller's `input_reference` to
ModelScope's own storage first:

```
POST /api/v1/rm/uploadUrl   {FileName, Type}  -> Data.UploadUrl
PUT  <UploadUrl>            raw bytes          (x-oss-meta-author: aliy)
POST /api/v1/rm/downloadUrl {FileUrl, Type}    -> Data.DownloadUrl
```

`app/civision/upload.py` does this. Two traps:

- `image_input_front_args` is a **single object** with an `image` key, not a
  list. Upstream rejects the array form.
- the upload endpoints use the api/v1 envelope (`Data`), not the openapi one
  (`data`)

### Model quirks live on the model, not in the caller

MiniMax-H3 is the odd one out:

- it is the only model with a **10 second** duration
- it is the only model with **one size tier** (768-based, no 480 tier)
- it is the only model that accepts `videoInferenceArgs`; other models reject
  the block with `videoInferenceArgs 仅支持 MiniMax-H3`
- it has a **LoRA acceleration** switch that the civision page turns on by
  default, which pins `numInferenceSteps` to 4 and **halves the cost**

That last one is a trap. MiniMax-H3's *published* default is 20 steps, but
the page runs it with acceleration on. If the service omits
`accelerateLora`, the estimate comes out at roughly double what the page
quotes (68 instead of 34 for 3 seconds).

The rule that follows: **per-model behaviour belongs on the model object**,
resolved in `build_catalog`, not sprinkled through the request path. The
mapping layer already carries `accelerate_lora`, `default_steps` and
`default_guidance_scale` for exactly this reason.

### The estimate must mirror the submit

`estimate-quota` and the submit endpoint take the same request structure
(`video_predict_task_request`), differing only in field naming. So:

- every parameter of the estimate must match the submit
- the free estimate endpoint can confirm that upstream **accepts** a request
  body, without generating anything

Hard-coding anything in the estimate body (it previously pinned
`guidance_scale` to 7.5 and `num_inference_steps` to 50) makes the estimate
describe a different job than the one being submitted.

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

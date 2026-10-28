"""Mapping between the OpenAI-facing surface and civision's own vocabulary.

Three mappings live here:

* **model names** - OpenAI clients may say `sora-2`; civision knows
  `WAN_VIDEO_2_2_T2V_A_14_B`. Both resolve to a real model.
* **sizes** - each model supports a different set of width/height pairs
  (MiniMax-H3 for instance only offers the 768 tier), so the requested size
  is resolved against the *model's own* slider rather than a global table.
* **durations** - OpenAI offers 4/8/12 seconds; civision offers 3/5/10.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# OpenAI size -> (width, height).
#
# Only `720x1280` and `1280x720` exist in civision's large tier. The two
# "HD portrait/landscape" sizes are not expressible, so they are treated as
# requests for the 9:16 and 16:9 aspect ratio and resolved per model.
OPENAI_SIZE_MAP: dict[str, tuple[int, int]] = {
    "720x1280": (720, 1280),
    "1280x720": (1280, 720),
    "1024x1792": (480, 848),
    "1792x1024": (848, 480),
}

# The aspect ratio each OpenAI size is really asking for, used when the exact
# pixel size is not available for the chosen model.
OPENAI_SIZE_ASPECT: dict[str, str] = {
    "720x1280": "9:16",
    "1024x1792": "9:16",
    "1280x720": "16:9",
    "1792x1024": "16:9",
}

# Native civision sizes across the Wan models (two tiers, five ratios).
NATIVE_SIZES: dict[str, tuple[int, int]] = {
    "480x848": (480, 848),
    "480x640": (480, 640),
    "480x480": (480, 480),
    "640x480": (640, 480),
    "848x480": (848, 480),
    "720x1280": (720, 1280),
    "720x960": (720, 960),
    "720x720": (720, 720),
    "960x720": (960, 720),
    "1280x720": (1280, 720),
    # MiniMax-H3's tier
    "768x1280": (768, 1280),
    "768x960": (768, 960),
    "768x768": (768, 768),
    "960x768": (960, 768),
    "1280x768": (1280, 768),
}

DEFAULT_OPENAI_MODEL = "sora-2"
DEFAULT_PRO_MODEL = "sora-2-pro"

# The models this service exposes. Upstream offers eight video models; the
# rest are not worth the surface area.
#
#   wan2.1-t2v-1.3b  - cheapest text-to-video
#   wan2.1-i2v-14b   - image-to-video
#   minimax-h3       - MiniMax-H3 without LoRA acceleration
#   minimax-h3-turbo - the same model with LoRA acceleration
#
# Add a civision value here to expose it again.
ALLOWED_MODELS: tuple[str, ...] = (
    "WAN_VIDEO_2_1_T2V_1_3_B",
    "WAN_VIDEO_2_1_I2V_14_B",
    "MINIMAX_H3",
)

# The step count the civision page pins when LoRA acceleration is enabled.
ACCELERATED_STEPS = 4

# The step count MiniMax-H3 runs at when acceleration is off: its published
# default from the model catalog.
MINIMAX_DEFAULT_STEPS = 20

# The civision page offers 1-50 sampling steps for every video model and
# starts at 50. Fifty steps is slow on the Wan models, and upstream prices by
# step count, so this service runs them at 20 — the same step count as the
# unaccelerated MiniMax-H3 — unless the caller overrides
# `num_inference_steps`. The free estimate endpoint confirms 20 is accepted
# and quotes less: 480x480/3s goes 2 -> 1 magicube, 720x1280/5s 6 -> 2.
WAN_DEFAULT_STEPS = 20

# civision value -> the step count this service uses instead of the catalog's
# published default. Only Wan is overridden; MiniMax-H3's step count is
# decided by its acceleration variant below.
DEFAULT_STEP_OVERRIDES: dict[str, int] = {
    "WAN_VIDEO_2_1_T2V_1_3_B": WAN_DEFAULT_STEPS,
    "WAN_VIDEO_2_1_I2V_14_B": WAN_DEFAULT_STEPS,
}

# MiniMax-H3 has a "LoRA acceleration" switch on the civision page. On, it
# pins numInferenceSteps to 4 and halves the cost; off, the model runs at its
# published 20 steps. The two settings give noticeably different results, so
# they are exposed as two separate model names rather than a request flag.
#
# The alias table cannot express this: it maps a name onto a civision
# *value*, and both variants share the value MINIMAX_H3. So the variants are
# declared here and resolved alongside the real models.
#
# Each entry: public name -> (civision value, accelerate_lora, steps).
#
# `steps` is the value actually sent upstream, not a hint.
MODEL_VARIANTS: dict[str, tuple[str, bool, int]] = {
    # MiniMax-H3 with LoRA acceleration: 4 steps, half the price.
    "minimax-h3-turbo": ("MINIMAX_H3", True, ACCELERATED_STEPS),
    # MiniMax-H3 without acceleration: the model's own 20 steps.
    "minimax-h3": ("MINIMAX_H3", False, MINIMAX_DEFAULT_STEPS),
}

# OpenAI seconds -> civision `totalSeconds`. The civision page offers 3, 5
# and 10 seconds; 10 is only available for MiniMax-H3.
OPENAI_SECONDS_MAP: dict[int, int] = {4: 3, 8: 5, 12: 10}

# civision durations, in the order the page offers them.
CIVISION_SECONDS = (3, 5, 10)
DEFAULT_SECONDS = 5

# The OpenAI-facing names, and what they point at.
#
# These are the names a client of the OpenAI API is most likely to send, so
# they are mapped onto the models this service actually offers:
#
#   sora-2      -> minimax-h3-turbo  (MiniMax-H3 with LoRA acceleration)
#   sora-2-pro  -> minimax-h3        (MiniMax-H3 without acceleration)
#
# An alias target is a public model name, not a raw civision value, so that
# an alias can point at a *variant* of an upstream model.
DEFAULT_ALIASES: dict[str, str] = {
    "sora-2": "minimax-h3-turbo",
    "sora-2-pro": "minimax-h3",
}


@dataclass
class ResolvedModel:
    """A model the caller named, resolved to something civision can run."""

    requested: str
    civision_value: str
    version_id: int
    label: str
    supports_txt2video: bool = True
    supports_img2video: bool = False
    supports_flf2video: bool = False
    # (width, height) pairs this model accepts, and the tier each belongs to.
    sizes: list[tuple[int, int]] = field(default_factory=list)
    size_tiers: dict[tuple[int, int], str] = field(default_factory=dict)
    # The aspect-ratio label upstream gives each size.
    size_aspects: dict[tuple[int, int], str] = field(default_factory=dict)
    default_width: int = 720
    default_height: int = 1280
    # Durations this model accepts.
    seconds: tuple[int, ...] = CIVISION_SECONDS
    default_guidance_scale: float = 7.5
    default_steps: int = 50
    # MiniMax-H3 exposes a "LoRA acceleration" switch on the civision page.
    # On, it forces numInferenceSteps down to 4 and halves the magicube cost.
    # The two settings give different results, so they are two model names:
    # `minimax-h3-turbo` (on) and `minimax-h3` (off).
    #
    # None means "do not send a videoInferenceArgs block at all": the other
    # models reject it outright with "videoInferenceArgs 仅支持 MiniMax-H3".
    # True and False are sent explicitly, which is what the page does when
    # the switch is on and when it is off.
    accelerate_lora: bool | None = None
    # True when the caller named a variant of the upstream model rather than
    # the model itself. The two MiniMax-H3 names are both variants.
    is_variant: bool = False

    @property
    def supports_image_input(self) -> bool:
        return self.supports_img2video or self.supports_flf2video

    def has_size(self, width: int, height: int) -> bool:
        return (width, height) in self.size_tiers

    def tier_of(self, width: int, height: int) -> str | None:
        return self.size_tiers.get((width, height))

    def aspect_of(self, width: int, height: int) -> str | None:
        """The upstream aspect-ratio label for one of this model's sizes."""
        label = self.size_aspects.get((width, height))
        if label:
            return label
        return None

    def resolve_steps(self, requested: int | None) -> int:
        """The sampling step count to send upstream.

        LoRA acceleration pins the count: the page forces
        `numInferenceSteps` to 4 and hides the slider, so a caller cannot
        raise it. Otherwise an explicit request wins over the model default.
        """
        if self.accelerate_lora:
            return ACCELERATED_STEPS
        return requested or self.default_steps


# civision value -> friendly name. The upstream names encode the version in
# separate underscore-separated parts, which does not read well.
#   WAN_VIDEO_2_2_T2V_A_14_B -> wan2.2-t2v-a14b
#   WAN_VIDEO_2_1_T2V_1_3_B  -> wan2.1-t2v-1.3b
#   MINIMAX_H3               -> minimax-h3
_WAN_RE = re.compile(
    r"^WAN_VIDEO_(\d+)_(\d+)_(T2V|I2V|TI2V|FLF2V)_(?:(A)_)?(\d+)(?:_(\d+))?_B$",
    re.IGNORECASE,
)


def normalize_model_name(raw: str) -> str:
    """Turn a civision value into a friendly lower-case name."""
    s = (raw or "").strip().upper()
    if not s:
        return ""

    m = _WAN_RE.match(s)
    if m:
        major, minor, kind, a_flag, first, second = m.groups()
        if second:  # 1_3_B -> 1.3b
            size = f"{first}.{second}b"
        elif a_flag:  # A_14_B -> a14b
            size = f"a{first}b"
        else:  # 14_B -> 14b
            size = f"{first}b"
        return f"wan{major}.{minor}-{kind.lower()}-{size}"

    # Anything else: drop underscores and lower-case (MINIMAX_H3 -> minimax-h3).
    s = s.replace("_", "-").lower()
    return re.sub(r"-{2,}", "-", s)


def _aspect_ratio(width: int, height: int) -> str:
    """Reduce a width/height pair to one of the five civision ratios.

    Uses a 10% tolerance because upstream's own sizes are not exact and
    differ per model: the Wan "9 : 16" entry is 480x848 (ratio 0.566) while
    MiniMax-H3's is 768x1280 (ratio 0.600), against a nominal 0.5625.
    """
    for w, h in ((9, 16), (3, 4), (1, 1), (4, 3), (16, 9)):
        if abs(width / height - w / h) < 0.10:
            return f"{w}:{h}"
    return "1:1"


def _extract_sizes(
    model: dict,
) -> tuple[list[tuple[int, int]], dict[tuple[int, int], str], dict[tuple[int, int], str]]:
    """Read the size slider upstream publishes for a model.

    Returns the sizes, the tier each belongs to, and the aspect-ratio label
    upstream gives it.
    """
    sizes: list[tuple[int, int]] = []
    tiers: dict[tuple[int, int], str] = {}
    aspects: dict[tuple[int, int], str] = {}
    slider = model.get("videoSizeSlider") or {}
    for tier, entries in slider.items():
        for entry in entries or []:
            w, h = entry.get("width"), entry.get("height")
            if not w or not h:
                continue
            pair = (int(w), int(h))
            if pair not in sizes:
                sizes.append(pair)
            tiers[pair] = tier
            label = entry.get("label")
            if label:
                # Upstream writes ratios as "9 : 16".
                aspects[pair] = label.replace(" ", "")
    return sizes, tiers, aspects


def build_catalog(catalog: list[dict], aliases: dict[str, str]) -> list[ResolvedModel]:
    """Turn the upstream model list into resolved models.

    `aliases` maps an OpenAI-facing name onto another public model name (or
    a raw civision value, for entries stored in the database).
    """
    by_value = {m.get("value"): m for m in catalog if m.get("value")}

    def make(
        requested: str,
        target: dict,
        *,
        accelerate: bool | None = None,
        steps: int | None = None,
        is_variant: bool = False,
    ) -> ResolvedModel:
        support = set(target.get("supportType") or [])
        sizes, tiers, aspects = _extract_sizes(target)
        dp = (target.get("defaultParams") or {}).get("basicDiffusionArgs") or {}
        value = target.get("value", "")
        # MiniMax-H3 is the only model that offers the 10 second duration.
        seconds = CIVISION_SECONDS
        model_steps = int(dp.get("numInferenceSteps") or 50)
        override_steps = DEFAULT_STEP_OVERRIDES.get(value.upper())
        if override_steps is not None:
            model_steps = override_steps
        if value.upper() == "MINIMAX_H3":
            seconds = CIVISION_SECONDS
            # Without an explicit setting, the civision page turns LoRA
            # acceleration on for this model, which pins numInferenceSteps
            # to 4 and halves the cost.
            model_steps = ACCELERATED_STEPS
            if accelerate is None:
                accelerate = True
        else:
            seconds = tuple(s for s in CIVISION_SECONDS if s != 10)
            # The other models reject a videoInferenceArgs block outright.
            accelerate = None
        if steps is not None:
            model_steps = steps

        return ResolvedModel(
            requested=requested,
            civision_value=value,
            version_id=int(target.get("versionId") or 0),
            label=target.get("sub_label") or target.get("label") or value,
            supports_txt2video="TXT_2_VIDEO" in support,
            supports_img2video="IMG_2_VIDEO" in support,
            supports_flf2video="FLF_2_VIDEO" in support,
            sizes=sizes,
            size_tiers=tiers,
            size_aspects=aspects,
            default_width=int(dp.get("width") or (sizes[-1][0] if sizes else 720)),
            default_height=int(dp.get("height") or (sizes[-1][1] if sizes else 1280)),
            seconds=seconds,
            default_guidance_scale=float(dp.get("guidanceScale") or 7.5),
            default_steps=model_steps,
            accelerate_lora=accelerate,
            is_variant=is_variant,
        )

    resolved: list[ResolvedModel] = []

    # A model with declared variants is exposed *as* those variants, not
    # under its own name, so that "minimax-h3" means exactly one thing.
    variant_targets: set[str] = {
        value for value, _, _ in MODEL_VARIANTS.values()
    }

    for m in catalog:
        value = m.get("value")
        if not value or value not in ALLOWED_MODELS:
            continue
        if value in variant_targets:
            continue  # built below, one entry per variant
        resolved.append(make(normalize_model_name(value), m))

    for public_name, (value, accelerate, steps) in MODEL_VARIANTS.items():
        target = by_value.get(value)
        if not target or value not in ALLOWED_MODELS:
            continue
        resolved.append(
            make(
                public_name,
                target,
                accelerate=accelerate,
                steps=steps,
                is_variant=True,
            )
        )

    by_public_name = {m.requested: m for m in resolved}
    for alias, target_name in aliases.items():
        target_model = by_public_name.get(target_name)
        if target_model is None:
            # An alias stored in the database may point at a raw civision
            # value instead of a public name.
            raw = by_value.get(target_name)
            if not raw or target_name not in ALLOWED_MODELS:
                continue
            target_model = make(normalize_model_name(target_name), raw)
        resolved.append(
            make(
                alias,
                by_value[target_model.civision_value],
                accelerate=target_model.accelerate_lora,
                steps=target_model.default_steps,
                is_variant=target_model.is_variant,
            )
        )
    return resolved


def resolve_model(
    requested: str, resolved: list[ResolvedModel]
) -> ResolvedModel | None:
    """Find a model by its OpenAI name, its civision value, or its friendly name."""
    if not requested:
        return None
    key = requested.strip()
    for m in resolved:
        if m.requested == key or m.civision_value.lower() == key.lower():
            return m
    norm = normalize_model_name(key)
    for m in resolved:
        if m.requested == norm:
            return m
    return None


def resolve_size(
    size: str | None, model: ResolvedModel | None = None
) -> tuple[int, int]:
    """Map a requested size onto a size the chosen model actually supports.

    The exact size wins when the model supports it. Otherwise the request is
    treated as an aspect-ratio request and the model's own tier for that
    ratio is used, preferring the larger tier.
    """
    if not size:
        if model is not None:
            return (model.default_width, model.default_height)
        return OPENAI_SIZE_MAP["720x1280"]

    key = size.strip().lower().replace(" ", "").replace("×", "x")

    requested: tuple[int, int] | None = None
    if key in OPENAI_SIZE_MAP:
        requested = OPENAI_SIZE_MAP[key]
    elif key in NATIVE_SIZES:
        requested = NATIVE_SIZES[key]
    else:
        m = re.fullmatch(r"(\d+)x(\d+)", key)
        if m:
            pair = (int(m.group(1)), int(m.group(2)))
            if pair in set(NATIVE_SIZES.values()):
                requested = pair

    if requested is None:
        raise ValueError(
            f"unsupported size {size!r}; expected one of "
            f"{', '.join(sorted(OPENAI_SIZE_MAP))} or a native size"
        )

    if model is None or not model.sizes:
        return requested

    if model.has_size(*requested):
        return requested

    # The exact pair is unavailable for this model. Keep the aspect ratio and
    # pick the model's own size for it, preferring the larger tier.
    want_aspect = OPENAI_SIZE_ASPECT.get(key)
    if want_aspect is None:
        # A hand-written size. Match on the model's own aspect label first,
        # because upstream's label is authoritative (MiniMax-H3 calls
        # 768x1280 "9 : 16"), then fall back to comparing computed ratios.
        computed = _aspect_ratio(*requested)
        candidates = [
            s for s in model.sizes
            if model.aspect_of(*s) == computed
        ]
        if not candidates:
            candidates = [
                s for s in model.sizes
                if _aspect_ratio(*s) == computed
            ]
    else:
        candidates = [
            s for s in model.sizes if model.aspect_of(*s) == want_aspect
        ]
        if not candidates:
            candidates = [
                s for s in model.sizes
                if _aspect_ratio(*s) == _aspect_ratio(*requested)
            ]

    if not candidates:
        # No size shares the aspect ratio; fall back to the model default.
        return (model.default_width, model.default_height)

    def rank(pair: tuple[int, int]) -> tuple[int, int]:
        tier = model.tier_of(*pair) or ""
        # Prefer the large tier, then the largest pixel count.
        return (0 if tier == "large" else 1, -(pair[0] * pair[1]))

    return min(candidates, key=rank)


def resolve_seconds(
    seconds: str | int | None, model: ResolvedModel | None = None
) -> int:
    """Map an OpenAI duration onto a civision `totalSeconds` value.

    OpenAI offers 4/8/12 seconds; civision offers 3/5/10.
    """
    if seconds is None or seconds == "":
        return DEFAULT_SECONDS

    try:
        value = int(seconds)
    except (TypeError, ValueError):
        raise ValueError(f"unsupported seconds {seconds!r}") from None

    if value not in OPENAI_SECONDS_MAP:
        raise ValueError(
            f"unsupported seconds {value}; OpenAI supports 4, 8 and 12"
        )

    target = OPENAI_SECONDS_MAP[value]

    if model is not None and model.seconds:
        if target in model.seconds:
            return target
        # The model cannot do that length; use its longest supported one.
        return max(model.seconds)

    return target

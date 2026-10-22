"""Mapping between the OpenAI-facing surface and civision's own vocabulary.

Two mappings live here:

* **model names** - OpenAI clients may say `sora-2`; civision knows
  `WAN_VIDEO_2_2_T2V_A_14_B`. Both resolve to a real model.
* **sizes** - OpenAI offers `1024x1792`, which is not one of civision's
  width/height pairs, so it degrades to the closest available tier.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# OpenAI size -> (width, height). The two "portrait/landscape HD" sizes are
# not expressible in civision's 480/720 tiers, so they degrade to the small
# tier at the same aspect ratio.
OPENAI_SIZE_MAP: dict[str, tuple[int, int]] = {
    "720x1280": (720, 1280),
    "1280x720": (1280, 720),
    "1024x1792": (480, 848),
    "1792x1024": (848, 480),
}

# Native civision sizes (5 aspect ratios x 2 resolution tiers) that we also
# accept so callers can ask for something OpenAI does not expose.
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
}

DEFAULT_OPENAI_MODEL = "sora-2"
DEFAULT_PRO_MODEL = "sora-2-pro"

# Fallbacks when the alias table has not been seeded yet.
DEFAULT_ALIASES: dict[str, str] = {
    "sora-2": "WAN_VIDEO_2_2_T2V_A_14_B",
    "sora-2-pro": "WAN_VIDEO_2_2_I2V_A_14_B",
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

    @property
    def supports_image_input(self) -> bool:
        return self.supports_img2video or self.supports_flf2video


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


def build_catalog(catalog: list[dict], aliases: dict[str, str]) -> list[ResolvedModel]:
    """Turn the upstream model list into resolved models.

    `aliases` maps an OpenAI-facing name onto a civision value.
    """
    by_value = {m.get("value"): m for m in catalog if m.get("value")}

    resolved: list[ResolvedModel] = []
    for m in catalog:
        value = m.get("value")
        if not value:
            continue
        support = set(m.get("supportType") or [])
        resolved.append(
            ResolvedModel(
                requested=normalize_model_name(value),
                civision_value=value,
                version_id=int(m.get("versionId") or 0),
                label=m.get("sub_label") or m.get("label") or value,
                supports_txt2video="TXT_2_VIDEO" in support,
                supports_img2video="IMG_2_VIDEO" in support,
                supports_flf2video="FLF_2_VIDEO" in support,
            )
        )

    for alias, target in aliases.items():
        target_model = by_value.get(target)
        if not target_model:
            continue
        resolved.append(
            ResolvedModel(
                requested=alias,
                civision_value=target,
                version_id=int(target_model.get("versionId") or 0),
                label=f"{alias} -> {target_model.get('sub_label') or target}",
                supports_txt2video="TXT_2_VIDEO" in set(target_model.get("supportType") or []),
                supports_img2video="IMG_2_VIDEO" in set(target_model.get("supportType") or []),
                supports_flf2video="FLF_2_VIDEO" in set(target_model.get("supportType") or []),
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


def resolve_size(size: str | None) -> tuple[int, int]:
    """Map a requested size onto civision's width/height.

    Accepts the four OpenAI sizes plus civision's native ones. Anything
    unrecognised falls back to the OpenAI default.
    """
    if not size:
        return OPENAI_SIZE_MAP["720x1280"]
    key = size.strip().lower().replace(" ", "").replace("×", "x")
    if key in OPENAI_SIZE_MAP:
        return OPENAI_SIZE_MAP[key]
    if key in NATIVE_SIZES:
        return NATIVE_SIZES[key]
    m = re.fullmatch(r"(\d+)x(\d+)", key)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
        if (w, h) in set(NATIVE_SIZES.values()):
            return w, h
    raise ValueError(
        f"unsupported size {size!r}; expected one of "
        f"{', '.join(sorted(OPENAI_SIZE_MAP))} or a native size"
    )


def resolve_seconds(seconds: str | int | None) -> int:
    """Map a requested duration onto civision's `totalSeconds`."""
    if seconds is None or seconds == "":
        return 4
    try:
        value = int(seconds)
    except (TypeError, ValueError):
        raise ValueError(f"unsupported seconds {seconds!r}") from None
    if value not in (4, 8, 12):
        raise ValueError(
            f"unsupported seconds {value}; OpenAI supports 4, 8 and 12"
        )
    return value

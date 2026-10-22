"""The magicube cost table, measured from upstream.

Every value here was read from the free `estimate-quota` endpoint with the
same arguments the civision page sends, so the table reflects what a job
actually costs. The page quotes the same numbers.

Cost depends on the pixel count of the requested size and on the duration,
not on the exact width/height pair, so the table is keyed by size.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.civision.client import CivisionClient
from app.civision.mapping import ResolvedModel


@dataclass
class CostRow:
    model: str
    label: str
    width: int
    height: int
    seconds: int
    magicubes: float


@dataclass
class CostLine:
    """One size of one model, with the cost at each duration."""

    model: str
    label: str
    width: int
    height: int
    cost3: float | None
    cost5: float | None
    cost10: float | None
    # True for the first size of a model, so the template can print the model
    # name only once.
    first_of_model: bool = False


# Measured values, as of 2026-10-22.
#
# The three models the service exposes, with the magicubes a job costs at
# each supported size and duration.
#
# Wan2.1-T2V-1.3B (versionId 546)
#   720x1280  3s=3  5s=6
#   720x960   3s=3  5s=6
#   720x720   3s=3  5s=6
#   960x720   3s=3  5s=6
#   1280x720  3s=3  5s=6
#   480x848   3s=3  5s=6
#   480x640   3s=2  5s=3
#   480x480   3s=2  5s=3
#   640x480   3s=2  5s=3
#   848x480   3s=3  5s=6
#
# Wan2.1-I2V-14B (versionId 648)
#   720x1280  3s=17  5s=28
#   720x960   3s=17  5s=28
#   720x720   3s=17  5s=28
#   960x720   3s=17  5s=28
#   1280x720  3s=17  5s=28
#   480x848   3s=17  5s=28
#   480x640   3s=8   5s=13
#   480x480   3s=8   5s=13
#   640x480   3s=8   5s=13
#   848x480   3s=17  5s=28
#
# MiniMax-H3 (versionId 150867, LoRA acceleration on, 4 steps)
#   every size  3s=34  5s=56  10s=113
#
# Note: the civision page hides the 10 second option for the Wan models
# ("stay tuned"), but the endpoint accepts it: 720x1280 10s costs 11 for
# Wan2.1-T2V-1.3B and 480x480 10s costs 5.

MEASURED: dict[str, dict[tuple[int, int], dict[int, float]]] = {
    "WAN_VIDEO_2_1_T2V_1_3_B": {
        (720, 1280): {3: 3, 5: 6, 10: 11},
        (720, 960): {3: 3, 5: 6, 10: 11},
        (720, 720): {3: 3, 5: 6, 10: 11},
        (960, 720): {3: 3, 5: 6, 10: 11},
        (1280, 720): {3: 3, 5: 6, 10: 11},
        (480, 848): {3: 3, 5: 6, 10: 11},
        (480, 640): {3: 2, 5: 3, 10: 5},
        (480, 480): {3: 2, 5: 3, 10: 5},
        (640, 480): {3: 2, 5: 3, 10: 5},
        (848, 480): {3: 3, 5: 6, 10: 11},
    },
    "WAN_VIDEO_2_1_I2V_14_B": {
        (720, 1280): {3: 17, 5: 28, 10: 56},
        (720, 960): {3: 17, 5: 28},
        (720, 720): {3: 17, 5: 28},
        (960, 720): {3: 17, 5: 28},
        (1280, 720): {3: 17, 5: 28, 10: 56},
        (480, 848): {3: 17, 5: 28, 10: 56},
        (480, 640): {3: 8, 5: 13, 10: 25},
        (480, 480): {3: 8, 5: 13, 10: 25},
        (640, 480): {3: 8, 5: 13, 10: 25},
        (848, 480): {3: 17, 5: 28, 10: 56},
    },
    "MINIMAX_H3": {
        (768, 1280): {3: 34, 5: 56, 10: 113},
        (768, 960): {3: 34, 5: 56, 10: 113},
        (768, 768): {3: 34, 5: 56, 10: 113},
        (960, 768): {3: 34, 5: 56, 10: 113},
        (1280, 768): {3: 34, 5: 56, 10: 113},
    },
}

# Variants of one upstream model have their own prices, so the table is
# keyed by the *public* name for those. MiniMax-H3 without LoRA
# acceleration runs 20 steps instead of 4, which doubles the cost.
#
# Measured with the free estimator, 2026-10-22.
MEASURED_BY_NAME: dict[str, dict[tuple[int, int], dict[int, float]]] = {
    # With acceleration: 4 steps, the same prices as the base MiniMax-H3 row.
    "minimax-h3-turbo": {
        (768, 1280): {3: 34, 5: 56, 10: 113},
        (768, 960): {3: 34, 5: 56, 10: 113},
        (768, 768): {3: 34, 5: 56, 10: 113},
        (960, 768): {3: 34, 5: 56, 10: 113},
        (1280, 768): {3: 34, 5: 56, 10: 113},
    },
    # Without acceleration: 20 steps, twice the price.
    "minimax-h3": {
        (768, 1280): {3: 68, 5: 113, 10: 225},
        (768, 960): {3: 68, 5: 113, 10: 225},
        (768, 768): {3: 68, 5: 113, 10: 225},
        (960, 768): {3: 68, 5: 113, 10: 225},
        (1280, 768): {3: 68, 5: 113, 10: 225},
    },
}

MODEL_LABELS = {
    "WAN_VIDEO_2_1_T2V_1_3_B": "Wan2.1-T2V-1.3B (text to video)",
    "WAN_VIDEO_2_1_I2V_14_B": "Wan2.1-I2V-14B-720P (image to video)",
    "MINIMAX_H3": "MiniMax-H3 (text / image / first-last frame)",
}

# Public name -> the label to show for it. A variant gets its own label so
# the admin cost table can tell the two MiniMax-H3 variants apart.
VARIANT_LABELS = {
    "minimax-h3-turbo": "MiniMax-H3 turbo (LoRA acceleration, 4 steps)",
    "minimax-h3": "MiniMax-H3 (no acceleration, 20 steps)",
}


def _table_for(model: ResolvedModel) -> dict[tuple[int, int], dict[int, float]]:
    """The measured costs for one model.

    A variant has its own table, keyed by its public name, because two
    variants of one upstream model can cost different amounts.
    """
    if model.is_variant:
        return MEASURED_BY_NAME.get(model.requested, {})
    return MEASURED.get(model.civision_value, {})


def _label_for(model: ResolvedModel) -> str:
    if model.is_variant:
        return VARIANT_LABELS.get(model.requested, model.label)
    return model.label


def rows_for(model: ResolvedModel) -> list[CostRow]:
    """The measured cost rows for one model, one row per duration."""
    table = _table_for(model)
    if not table:
        return []
    rows: list[CostRow] = []
    for (width, height), by_seconds in table.items():
        for seconds, magicubes in by_seconds.items():
            rows.append(
                CostRow(
                    model=model.requested,
                    label=_label_for(model),
                    width=width,
                    height=height,
                    seconds=seconds,
                    magicubes=magicubes,
                )
            )
    return rows


def lines_for(model: ResolvedModel) -> list[CostLine]:
    """One row per size, with the cost at each duration."""
    table = _table_for(model)
    if not table:
        return []
    lines: list[CostLine] = []
    for index, ((width, height), by_seconds) in enumerate(table.items()):
        lines.append(
            CostLine(
                model=model.requested,
                label=_label_for(model),
                width=width,
                height=height,
                cost3=by_seconds.get(3),
                cost5=by_seconds.get(5),
                cost10=by_seconds.get(10),
                first_of_model=index == 0,
            )
        )
    return lines


def lookup(
    model: ResolvedModel, width: int, height: int, seconds: int
) -> float | None:
    """The measured cost of one combination, if it is known."""
    table = _table_for(model)
    if not table:
        return None
    entry = table.get((width, height))
    if not entry:
        return None
    return entry.get(seconds)


async def measure(
    client: CivisionClient, model: ResolvedModel, seconds: int
) -> dict[tuple[int, int], float]:
    """Ask upstream for the cost of each size at one duration.

    The estimate endpoint is free, so this is safe to call.
    """
    results: dict[tuple[int, int], float] = {}
    for width, height in model.sizes:
        try:
            quota = await client.estimate_quota(
                version_id=model.version_id,
                width=width,
                height=height,
                seconds=seconds,
                guidance_scale=model.default_guidance_scale,
                num_inference_steps=model.default_steps,
                accelerate_lora=model.accelerate_lora,
                image_input="https://example.com/reference.png"
                if model.supports_img2video and not model.supports_txt2video
                else None,
            )
        except Exception:  # noqa: BLE001
            continue
        results[(width, height)] = quota
    return results

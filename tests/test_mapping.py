"""Tests for the pieces that do not need the live upstream."""

from __future__ import annotations

import pytest

from app.civision.client import parse_cookie_header
from app.civision.mapping import (
    ALLOWED_MODELS,
    DEFAULT_ALIASES,
    build_catalog,
    normalize_model_name,
    resolve_model,
    resolve_seconds,
    resolve_size,
)

# A trimmed copy of the real upstream catalog. The two size tiers and the
# aspect labels matter: MiniMax-H3 offers only the 768 tier and calls
# 768x1280 "9 : 16".
SAMPLE_CATALOG = [
    {
        "value": "WAN_VIDEO_2_1_T2V_1_3_B",
        "sub_label": "Wan2.1-T2V-1.3B",
        "versionId": 546,
        "supportType": ["TXT_2_VIDEO"],
        "defaultParams": {
            "basicDiffusionArgs": {
                "guidanceScale": 7.5,
                "numInferenceSteps": 50,
                "width": 720,
                "height": 1280,
            }
        },
        "videoSizeSlider": {
            "large": [
                {"width": 720, "height": 1280, "label": "9 : 16"},
                {"width": 720, "height": 960, "label": "3 : 4"},
                {"width": 720, "height": 720, "label": "1 : 1"},
                {"width": 960, "height": 720, "label": "4 : 3"},
                {"width": 1280, "height": 720, "label": "16 : 9"},
            ],
            "small": [
                {"width": 480, "height": 848, "label": "9 : 16"},
                {"width": 480, "height": 640, "label": "3 : 4"},
                {"width": 480, "height": 480, "label": "1 : 1"},
                {"width": 640, "height": 480, "label": "4 : 3"},
                {"width": 848, "height": 480, "label": "16 : 9"},
            ],
        },
    },
    {
        "value": "WAN_VIDEO_2_1_I2V_14_B",
        "sub_label": "Wan2.1-I2V-14B-720P",
        "versionId": 648,
        "supportType": ["IMG_2_VIDEO"],
        "defaultParams": {
            "basicDiffusionArgs": {
                "guidanceScale": 7.5,
                "numInferenceSteps": 50,
                "width": 720,
                "height": 1280,
            }
        },
        "videoSizeSlider": {
            "large": [
                {"width": 720, "height": 1280, "label": "9 : 16"},
                {"width": 1280, "height": 720, "label": "16 : 9"},
            ],
            "small": [
                {"width": 480, "height": 848, "label": "9 : 16"},
                {"width": 848, "height": 480, "label": "16 : 9"},
            ],
        },
    },
    {
        "value": "MINIMAX_H3",
        "sub_label": "MiniMax-H3",
        "versionId": 150867,
        "supportType": ["TXT_2_VIDEO", "IMG_2_VIDEO", "FLF_2_VIDEO"],
        "defaultParams": {
            "basicDiffusionArgs": {
                "guidanceScale": 4,
                "numInferenceSteps": 20,
                "width": 1280,
                "height": 768,
            }
        },
        "videoSizeSlider": {
            "large": [
                {"width": 768, "height": 1280, "label": "9 : 16"},
                {"width": 768, "height": 960, "label": "3 : 4"},
                {"width": 768, "height": 768, "label": "1 : 1"},
                {"width": 960, "height": 768, "label": "4 : 3"},
                {"width": 1280, "height": 768, "label": "16 : 9"},
            ],
        },
    },
]


@pytest.fixture
def models():
    return build_catalog(SAMPLE_CATALOG, dict(DEFAULT_ALIASES))


class TestParseCookieHeader:
    def test_plain_pairs(self):
        assert parse_cookie_header("a=1; b=2") == {"a": "1", "b": "2"}

    def test_httponly_prefix_is_stripped(self):
        jar = parse_cookie_header("#HttpOnly_m_session_id=xyz; csrf_token=t")
        assert jar["m_session_id"] == "xyz"

    def test_newlines_and_blank_lines(self):
        assert parse_cookie_header("a=1\n\n  b=2  \n") == {"a": "1", "b": "2"}

    def test_cookie_header_prefix(self):
        assert parse_cookie_header("Cookie: a=1; b=2") == {"a": "1", "b": "2"}

    def test_values_may_contain_equals(self):
        assert parse_cookie_header("t=abc%3D")["t"] == "abc%3D"

    def test_empty(self):
        assert parse_cookie_header("") == {}


class TestNormalizeModelName:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("WAN_VIDEO_2_1_T2V_1_3_B", "wan2.1-t2v-1.3b"),
            ("WAN_VIDEO_2_1_T2V_14_B", "wan2.1-t2v-14b"),
            ("WAN_VIDEO_2_1_I2V_14_B", "wan2.1-i2v-14b"),
            ("WAN_VIDEO_2_1_FLF2V_14_B", "wan2.1-flf2v-14b"),
            ("WAN_VIDEO_2_2_TI2V_5_B", "wan2.2-ti2v-5b"),
            ("WAN_VIDEO_2_2_T2V_A_14_B", "wan2.2-t2v-a14b"),
            ("WAN_VIDEO_2_2_I2V_A_14_B", "wan2.2-i2v-a14b"),
            ("MINIMAX_H3", "minimax-h3"),
        ],
    )
    def test_names(self, raw, expected):
        assert normalize_model_name(raw) == expected


class TestResolveSize:
    """The requested size is resolved against the *model's own* slider."""

    def test_exact_match_is_kept(self, models):
        m = resolve_model("wan2.1-t2v-1.3b", models)
        assert resolve_size("720x1280", m) == (720, 1280)
        assert resolve_size("1280x720", m) == (1280, 720)

    def test_openai_portrait_degrades_to_the_ratio(self, models):
        m = resolve_model("wan2.1-t2v-1.3b", models)
        assert resolve_size("1024x1792", m) == (480, 848)
        assert resolve_size("1792x1024", m) == (848, 480)

    def test_minimax_maps_openai_sizes_to_its_own_tier(self, models):
        m = resolve_model("sora-2-pro", models)
        # MiniMax-H3 has no 480 tier, so the 9:16 slot is 768x1280.
        assert resolve_size("720x1280", m) == (768, 1280)
        assert resolve_size("1024x1792", m) == (768, 1280)
        assert resolve_size("1280x720", m) == (1280, 768)
        assert resolve_size("1792x1024", m) == (1280, 768)

    def test_minimax_square(self, models):
        m = resolve_model("sora-2-pro", models)
        assert resolve_size("480x480", m) == (768, 768)

    def test_native_sizes_pass_through(self, models):
        m = resolve_model("wan2.1-t2v-1.3b", models)
        assert resolve_size("480x480", m) == (480, 480)
        assert resolve_size("720x960", m) == (720, 960)
        assert resolve_size("960x720", m) == (960, 720)

    def test_size_from_another_model_is_remapped(self, models):
        """768x1280 is a MiniMax-H3 size; on a Wan model it becomes 9:16 large."""
        wan = resolve_model("wan2.1-t2v-1.3b", models)
        assert resolve_size("768x1280", wan) == (720, 1280)

    def test_result_is_always_supported_by_the_model(self, models):
        for m in models:
            for size in (
                "720x1280", "1280x720", "1024x1792", "1792x1024",
                "768x1280", "480x480", "720x960", None,
            ):
                w, h = resolve_size(size, m)
                assert m.has_size(w, h), f"{m.requested} {size} -> {w}x{h}"

    def test_default_uses_the_model_default(self, models):
        wan = resolve_model("wan2.1-t2v-1.3b", models)
        h3 = resolve_model("sora-2-pro", models)
        assert resolve_size(None, wan) == (720, 1280)
        assert resolve_size(None, h3) == (1280, 768)

    def test_without_a_model_the_request_is_passed_through(self):
        assert resolve_size("720x1280", None) == (720, 1280)
        assert resolve_size("1024x1792", None) == (480, 848)

    def test_unknown_size_raises(self):
        with pytest.raises(ValueError):
            resolve_size("99x99", None)


class TestResolveSeconds:
    """OpenAI's 4/8/12 map onto civision's 3/5/10."""

    def test_mapping(self):
        assert resolve_seconds("4") == 3
        assert resolve_seconds("8") == 5
        assert resolve_seconds("12") == 10

    def test_default(self):
        assert resolve_seconds(None) == 5

    @pytest.mark.parametrize("value", ["7", "0", "100"])
    def test_invalid(self, value):
        with pytest.raises(ValueError):
            resolve_seconds(value)

    def test_model_without_ten_seconds_caps_at_five(self, models):
        wan = resolve_model("wan2.1-t2v-1.3b", models)
        assert resolve_seconds("12", wan) == 5

    def test_minimax_supports_ten_seconds(self, models):
        h3 = resolve_model("sora-2-pro", models)
        assert resolve_seconds("12", h3) == 10

    def test_resolved_value_is_always_supported(self, models):
        for m in models:
            for value in ("4", "8", "12", None):
                assert resolve_seconds(value, m) in m.seconds


class TestModelResolution:
    def test_resolve_by_friendly_name(self, models):
        m = resolve_model("wan2.1-t2v-1.3b", models)
        assert m is not None and m.version_id == 546

    def test_resolve_by_civision_value(self, models):
        m = resolve_model("WAN_VIDEO_2_1_T2V_1_3_B", models)
        assert m is not None and m.version_id == 546

    def test_alias(self, models):
        m = resolve_model("sora-2", models)
        assert m is not None and m.version_id == 150867

    def test_sora_2_pro_points_at_minimax_h3(self, models):
        m = resolve_model("sora-2-pro", models)
        assert m is not None
        assert m.civision_value == "MINIMAX_H3"
        assert m.version_id == 150867

    def test_alias_to_unknown_model_is_ignored(self):
        built = build_catalog(SAMPLE_CATALOG, {"sora-2": "NOPE"})
        assert resolve_model("sora-2", built) is None

    def test_unknown_model(self, models):
        assert resolve_model("gpt-4", models) is None

    def test_capabilities(self, models):
        i2v = resolve_model("wan2.1-i2v-14b", models)
        assert i2v.supports_img2video and not i2v.supports_txt2video
        h3 = resolve_model("minimax-h3", models)
        assert h3.supports_txt2video and h3.supports_img2video and h3.supports_flf2video

    def test_model_defaults_come_from_upstream(self, models):
        h3 = resolve_model("minimax-h3", models)
        assert h3.default_guidance_scale == 4
        # MiniMax-H3 without acceleration runs at its published step count.
        assert h3.default_steps == 20
        turbo = resolve_model("minimax-h3-turbo", models)
        # With LoRA acceleration the steps are pinned to 4.
        assert turbo.default_steps == 4
        wan = resolve_model("wan2.1-t2v-1.3b", models)
        assert wan.default_guidance_scale == 7.5
        assert wan.default_steps == 50


class TestAccelerateLora:
    """MiniMax-H3 has a LoRA acceleration switch on the civision page.

    On, it pins numInferenceSteps to 4 and halves the magicube cost; off, the
    model runs at its published 20 steps. The two settings are exposed as two
    model names, because the results differ noticeably.
    """

    def test_turbo_enables_acceleration_with_four_steps(self, models):
        turbo = resolve_model("minimax-h3-turbo", models)
        assert turbo.accelerate_lora is True
        assert turbo.default_steps == 4

    def test_plain_minimax_disables_acceleration_with_twenty_steps(self, models):
        h3 = resolve_model("minimax-h3", models)
        assert h3.accelerate_lora is False
        assert h3.default_steps == 20

    def test_other_models_send_no_acceleration_block(self, models):
        """The Wan models reject a videoInferenceArgs block outright."""
        for name in ("wan2.1-t2v-1.3b", "wan2.1-i2v-14b"):
            m = resolve_model(name, models)
            assert m.accelerate_lora is None
            assert m.default_steps == 50

    def test_both_variants_are_the_same_upstream_model(self, models):
        turbo = resolve_model("minimax-h3-turbo", models)
        plain = resolve_model("minimax-h3", models)
        assert turbo.civision_value == plain.civision_value
        assert turbo.version_id == plain.version_id
        assert turbo.sizes == plain.sizes
        assert turbo.seconds == plain.seconds


class TestOpenAIAliases:
    """The OpenAI names point at the two MiniMax-H3 variants."""

    def test_sora_2_points_at_the_accelerated_variant(self, models):
        m = resolve_model("sora-2", models)
        assert m is not None
        assert m.accelerate_lora is True
        assert m.default_steps == 4

    def test_sora_2_pro_points_at_the_unaccelerated_variant(self, models):
        m = resolve_model("sora-2-pro", models)
        assert m is not None
        assert m.accelerate_lora is False
        assert m.default_steps == 20


class TestEstimateMatchesTheCivisionPage:
    """The numbers the civision page shows for MiniMax-H3 at 768x1280.

    These came from reading the estimate endpoint with the same arguments the
    browser sends: LoRA acceleration on, numInferenceSteps 4.
    """

    # (seconds, magicubes) as quoted by the civision page.
    H3_768x1280 = ((3, 34), (5, 56), (10, 113))

    def test_known_values(self):
        assert dict(self.H3_768x1280) == {3: 34, 5: 56, 10: 113}


class TestAllowList:
    """Only three models are exposed; upstream offers eight."""

    def test_only_allowed_models_are_exposed(self, models):
        exposed = {m.civision_value for m in models}
        assert exposed <= set(ALLOWED_MODELS)
        assert "WAN_VIDEO_2_1_T2V_1_3_B" in exposed
        assert "WAN_VIDEO_2_1_I2V_14_B" in exposed
        assert "MINIMAX_H3" in exposed

    def test_disallowed_models_are_hidden(self):
        catalog = SAMPLE_CATALOG + [
            {
                "value": "WAN_VIDEO_2_2_T2V_A_14_B",
                "sub_label": "Wan2.2-T2V-A14B",
                "versionId": 557,
                "supportType": ["TXT_2_VIDEO"],
                "defaultParams": {"basicDiffusionArgs": {}},
                "videoSizeSlider": {"large": []},
            }
        ]
        built = build_catalog(catalog, {})
        values = {m.civision_value for m in built}
        assert "WAN_VIDEO_2_2_T2V_A_14_B" not in values
        assert resolve_model("wan2.2-t2v-a14b", built) is None


class TestCostTable:
    """The measured magicube costs, keyed by civision model value."""

    def test_every_exposed_model_has_costs(self, models):
        from app.civision.costs import lines_for

        for m in models:
            if m.requested.startswith("sora-"):
                continue  # an alias points at a model that is already listed
            assert lines_for(m), f"no cost table for {m.requested}"

    def test_lookup_matches_the_table(self, models):
        from app.civision.costs import lookup

        wan = resolve_model("wan2.1-t2v-1.3b", models)
        assert lookup(wan, 480, 480, 3) == 2
        assert lookup(wan, 720, 1280, 5) == 6

        # The accelerated variant.
        turbo = resolve_model("minimax-h3-turbo", models)
        assert lookup(turbo, 768, 1280, 3) == 34
        assert lookup(turbo, 768, 1280, 10) == 113

        # The unaccelerated variant costs twice as much: 20 steps, not 4.
        h3 = resolve_model("minimax-h3", models)
        assert lookup(h3, 768, 1280, 3) == 68
        assert lookup(h3, 768, 1280, 5) == 113
        assert lookup(h3, 768, 1280, 10) == 225

    def test_the_two_variants_have_different_prices(self, models):
        from app.civision.costs import lookup

        turbo = resolve_model("minimax-h3-turbo", models)
        h3 = resolve_model("minimax-h3", models)
        assert lookup(turbo, 768, 1280, 3) != lookup(h3, 768, 1280, 3)

    def test_unknown_combination_has_no_cost(self, models):
        from app.civision.costs import lookup

        wan = resolve_model("wan2.1-t2v-1.3b", models)
        assert lookup(wan, 123, 456, 3) is None

"""Tests for the pieces that do not need the live upstream."""

from __future__ import annotations

import pytest

from app.civision.client import parse_cookie_header
from app.civision.mapping import (
    build_catalog,
    normalize_model_name,
    resolve_model,
    resolve_seconds,
    resolve_size,
)

SAMPLE_CATALOG = [
    {
        "value": "WAN_VIDEO_2_1_T2V_1_3_B",
        "sub_label": "Wan2.1-T2V-1.3B",
        "versionId": 546,
        "supportType": ["TXT_2_VIDEO"],
    },
    {
        "value": "WAN_VIDEO_2_2_I2V_A_14_B",
        "sub_label": "Wan2.2-I2V-A14B",
        "versionId": 647,
        "supportType": ["IMG_2_VIDEO"],
    },
    {
        "value": "MINIMAX_H3",
        "sub_label": "MiniMax-H3",
        "versionId": 150867,
        "supportType": ["TXT_2_VIDEO", "IMG_2_VIDEO", "FLF_2_VIDEO"],
    },
]


class TestParseCookieHeader:
    def test_plain_pairs(self):
        jar = parse_cookie_header("a=1; b=2")
        assert jar == {"a": "1", "b": "2"}

    def test_httponly_prefix_is_stripped(self):
        jar = parse_cookie_header("#HttpOnly_m_session_id=xyz; csrf_token=t")
        assert jar["m_session_id"] == "xyz"

    def test_newlines_and_blank_lines(self):
        raw = "a=1\n\n  b=2  \n"
        assert parse_cookie_header(raw) == {"a": "1", "b": "2"}

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
    @pytest.mark.parametrize(
        "size,expected",
        [
            ("720x1280", (720, 1280)),
            ("1280x720", (1280, 720)),
            ("1024x1792", (480, 848)),
            ("1792x1024", (848, 480)),
            ("480x480", (480, 480)),
            ("960x720", (960, 720)),
        ],
    )
    def test_known_sizes(self, size, expected):
        assert resolve_size(size) == expected

    def test_default(self):
        assert resolve_size(None) == (720, 1280)

    def test_uppercase_and_spaces(self):
        assert resolve_size(" 720X1280 ") == (720, 1280)

    def test_unknown_size_raises(self):
        with pytest.raises(ValueError):
            resolve_size("99x99")


class TestResolveSeconds:
    @pytest.mark.parametrize("value,expected", [("4", 4), ("8", 8), ("12", 12), (4, 4)])
    def test_valid(self, value, expected):
        assert resolve_seconds(value) == expected

    def test_default(self):
        assert resolve_seconds(None) == 4

    @pytest.mark.parametrize("value", ["7", "0", "100"])
    def test_invalid(self, value):
        with pytest.raises(ValueError):
            resolve_seconds(value)


class TestModelResolution:
    def test_build_and_resolve_by_friendly_name(self):
        models = build_catalog(SAMPLE_CATALOG, {})
        m = resolve_model("wan2.1-t2v-1.3b", models)
        assert m is not None
        assert m.version_id == 546

    def test_resolve_by_civision_value(self):
        models = build_catalog(SAMPLE_CATALOG, {})
        m = resolve_model("WAN_VIDEO_2_1_T2V_1_3_B", models)
        assert m is not None and m.version_id == 546

    def test_alias(self):
        models = build_catalog(SAMPLE_CATALOG, {"sora-2": "WAN_VIDEO_2_1_T2V_1_3_B"})
        m = resolve_model("sora-2", models)
        assert m is not None and m.version_id == 546

    def test_alias_to_unknown_model_is_ignored(self):
        models = build_catalog(SAMPLE_CATALOG, {"sora-2": "NOPE"})
        assert resolve_model("sora-2", models) is None

    def test_unknown_model(self):
        models = build_catalog(SAMPLE_CATALOG, {})
        assert resolve_model("gpt-4", models) is None

    def test_capabilities(self):
        models = build_catalog(SAMPLE_CATALOG, {})
        i2v = resolve_model("wan2.2-i2v-a14b", models)
        assert i2v.supports_img2video and not i2v.supports_txt2video
        h3 = resolve_model("minimax-h3", models)
        assert h3.supports_txt2video and h3.supports_img2video and h3.supports_flf2video

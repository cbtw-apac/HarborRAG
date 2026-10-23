"""Jira ``fields.<key>`` payload keys: accent folding and schema-stable ownership."""

from __future__ import annotations

from typing import Any

import pytest

from harborrag_adapters.connectors.jira.content import (
    custom_field_metadata,
    field_key,
    filterable_fields,
)


@pytest.mark.parametrize(
    ("name", "key"),
    [
        ("Skill Set", "skill_set"),
        ("Kỹ năng", "ky_nang"),
        ("Kinh nghiệm", "kinh_nghiem"),
        ("Số năm", "so_nam"),
        ("Đơn vị", "don_vi"),
        ("Résumé", "resume"),
        ("Ｆｕｌｌｗｉｄｔｈ", "fullwidth"),
        ("経験年数", ""),
    ],
)
def test_field_key_folds_accents_instead_of_splitting_on_them(name, key):
    assert field_key(name) == key


def _issue(fields: dict[str, Any], names: dict[str, str]) -> dict[str, Any]:
    return {
        "fields": fields,
        "names": names,
        "schema": {field_id: {"type": "option"} for field_id in names},
    }


def test_colliding_names_give_the_readable_key_to_one_field_on_every_issue():
    names = {"customfield_200": "team", "customfield_100": "Team"}
    only_second = _issue({"customfield_100": None, "customfield_200": {"value": "Platform"}}, names)
    both = _issue(
        {"customfield_200": {"value": "Platform"}, "customfield_100": {"value": "Payments"}},
        names,
    )

    assert filterable_fields(custom_field_metadata(only_second)) == {"customfield_200": "Platform"}
    assert filterable_fields(custom_field_metadata(both)) == {
        "team": "Payments",
        "customfield_200": "Platform",
    }


def test_a_prose_value_does_not_hand_its_key_to_a_colliding_field():
    # A field's kind follows its value: a multi-line value makes it prose.
    names = {"customfield_1": "Notes", "customfield_2": "notes"}
    prose = _issue({"customfield_1": "line one\nline two", "customfield_2": "x"}, names)
    prose["schema"] = {}

    assert filterable_fields(custom_field_metadata(prose)) == {"customfield_2": "x"}


def test_vietnamese_and_cjk_names_get_readable_or_field_id_keys():
    names = {
        "customfield_300": "Kỹ năng",
        "customfield_400": "Kinh nghiệm",
        "customfield_500": "経験年数",
    }
    issue = _issue(
        {
            "customfield_300": {"value": "Python"},
            "customfield_400": {"value": "5"},
            "customfield_500": {"value": "3"},
        },
        names,
    )

    assert filterable_fields(custom_field_metadata(issue)) == {
        "ky_nang": "Python",
        "kinh_nghiem": "5",
        "customfield_500": "3",
    }

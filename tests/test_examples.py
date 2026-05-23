import json
from pathlib import Path

import main

ROOT = Path(__file__).resolve().parents[1]


def test_static_examples_are_valid():
    examples = json.loads((ROOT / "static" / "examples.json").read_text(encoding="utf-8"))
    assert isinstance(examples, list)
    assert len(examples) >= 3

    seen_ids = set()
    for example in examples:
        example_id = example.get("id")
        assert example_id
        assert example_id not in seen_ids
        seen_ids.add(example_id)
        assert example.get("label")
        assert example.get("city_hint")
        assert example.get("objective") in main.SUPPORTED_OBJECTIVES
        assert 5 <= example.get("max_minutes", 0) <= main.MAX_MAX_MINUTES
        map_view = example.get("map_view")
        assert isinstance(map_view, dict)
        center = map_view.get("center")
        assert isinstance(center, dict)
        assert -180 <= center.get("lng", 999) <= 180
        assert -90 <= center.get("lat", 999) <= 90
        assert 1 <= map_view.get("zoom", 0) <= 18

        participants = example.get("participants")
        assert isinstance(participants, list)
        assert 1 <= len(participants) <= main.MAX_PARTICIPANTS
        for participant in participants:
            assert participant.get("address")
            assert participant.get("profile") in main.SUPPORTED_PROFILES

    assert "bay-area-driving" in seen_ids

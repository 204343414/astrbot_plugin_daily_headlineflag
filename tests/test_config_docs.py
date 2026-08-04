"""The README's config table must match _conf_schema.json.

Documentation drift here is not cosmetic: the table listed nine options that
had been removed, so anyone following it configured fields the plugin never
read and then wondered why nothing changed. A doc that is confidently wrong
costs more than a missing one.
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _schema_keys():
    return set(json.loads((ROOT / "_conf_schema.json").read_text("utf-8")))


def _documented_keys():
    readme = (ROOT / "README.md").read_text("utf-8")
    table = re.findall(r"^\| `([a-z_]+)` \|", readme, re.MULTILINE)
    return set(table)


def test_every_real_option_is_documented():
    missing = _schema_keys() - _documented_keys()
    assert not missing, f"README 少了配置项: {sorted(missing)}"


def test_no_documented_option_has_been_removed_from_the_schema():
    """The failure that actually happened: the table outlived the fields."""
    stale = _documented_keys() - _schema_keys()
    assert not stale, f"README 写了不存在的配置项: {sorted(stale)}"


def test_removed_options_are_not_silently_forgotten():
    """They stay mentioned as *removed*, because old guides still cite them
    and 'that option does nothing now' is the useful answer."""
    readme = (ROOT / "README.md").read_text("utf-8")
    for gone in ("news_type", "push_start_time", "group_only_push"):
        assert gone in readme, f"{gone} 应作为「已移除」被提到"

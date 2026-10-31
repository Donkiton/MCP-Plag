"""Preserve the real personal App registration while removing its optional skill.

This validates the received source snapshot as data. No App service mutations,
Telegram login or invented presentation fields are performed here.
"""
from pathlib import Path
import copy
import hashlib
import json

ROOT = Path(__file__).resolve().parents[1]
APP_VERSION = "1.0.2"
PLUGIN_ID = "plugin_asdk_app_6adde97bd2a88191986f132cf6d4c496"
APP_ID = "asdk_app_6adde97bd2a88191986f132cf6d4c496"
APP_PACKAGE_NAME = "dev-6adde97bd2a88191986f132cf6d4c496"
DELETE_PATHS = (
    "skills/tg-workflow/SKILL.md",
    "skills/tg-workflow/agents/openai.yaml",
    "skills/tg-workflow/assets/tg-skill-large.png",
    "skills/tg-workflow/assets/tg-skill-small.png",
)


def verify_app_source(write_report=False):
    baseline = json.loads((ROOT / "tools/app-registration-baseline.json").read_text("utf-8"))
    assert baseline["plugin_id"] == PLUGIN_ID and baseline["scope"] == "USER"
    contents = baseline["contents"]
    original_root = json.loads(contents["plugin.json"])
    original_legacy = json.loads(contents[".codex-plugin/plugin.json"])
    assert original_root["name"] == original_legacy["name"] == APP_PACKAGE_NAME
    assert original_root["version"] == original_legacy["version"] == "1.0.1"
    assert original_root["extensions"]["com.openai"]["interface"] == original_legacy["interface"]
    assert original_legacy["interface"]["defaultPrompt"] is None
    assert json.loads(contents[".app.json"])["apps"] == {APP_PACKAGE_NAME: {"id": APP_ID}}
    assert original_legacy["skills"] == "./skills"
    source_inventory = {entry["path"] for entry in baseline["source_inventory"]}
    expected_inventory = {".app.json", ".codex-plugin/plugin.json", "plugin.json"}
    assert source_inventory == expected_inventory | set(DELETE_PATHS)

    folder = ROOT / "plugin-app"
    candidate_root = json.loads((folder / "plugin.json").read_text("utf-8"))
    candidate_legacy = json.loads((folder / ".codex-plugin/plugin.json").read_text("utf-8"))
    expected_root = copy.deepcopy(original_root)
    expected_root["version"] = APP_VERSION
    expected_legacy = copy.deepcopy(original_legacy)
    expected_legacy["version"] = APP_VERSION
    del expected_legacy["skills"]
    assert candidate_root == expected_root, "App-owned root metadata changed outside the version bump"
    assert candidate_legacy == expected_legacy, "App-owned legacy metadata changed outside version/skill removal"
    assert candidate_root["extensions"]["com.openai"]["interface"] == candidate_legacy["interface"]
    assert candidate_root["extensions"]["com.openai"]["apps"] == candidate_legacy["apps"] == "./.app.json"
    assert (folder / ".app.json").read_bytes() == contents[".app.json"].encode("utf-8"), "App binding changed"
    assert not (folder / "mcp.json").exists() and not (folder / ".mcp.json").exists(), "Do not duplicate the App connection"
    assert not (folder / "skills").exists(), "The optional App-backed skill must be absent"
    assert "skills" not in candidate_legacy and "skills" not in candidate_root
    assert "onboardingSkill" not in candidate_root["extensions"]["com.openai"]
    actual = {p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()}
    assert actual == expected_inventory, "App-backed package must contain only binding and two manifests"

    # Removing the optional web skill must not remove the portable instructions.
    portable_skill = ROOT / "plugin/skills/tg-workflow/SKILL.md"
    assert portable_skill.is_file()
    assert portable_skill.read_text("utf-8").startswith("---\nname: tg-workflow\n")
    report = {
        "status": "preserved",
        "plugin_id": PLUGIN_ID,
        "expected_release_id": baseline["observed_release_id"],
        "version_before": original_root["version"], "version_after": APP_VERSION,
        "app_binding_preserved_byte_for_byte": True,
        "interface_preserved": True, "defaultPrompt_present_and_null": True,
        "name_preserved": True, "author_preserved": True, "description_preserved": True,
        "legacy_root_effective_configuration_equal": True,
        "new_mcp_configuration": False, "custom_skill_icons": 0,
        "optional_app_skill_removed": True, "portable_skill_preserved": True,
        "delete_paths": list(DELETE_PATHS),
        "reason": "The web UI rendered the optional skill as a generic cube despite declared skill icons. The working App remains; its redundant skill entry is removed.",
        "allowed_changes": ["Version 1.0.1 -> 1.0.2", "Remove the optional App-backed skill files and legacy skills declaration"],
        "files": {p.relative_to(folder).as_posix(): {"bytes": p.stat().st_size, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                  for p in sorted(folder.rglob("*")) if p.is_file()},
    }
    if write_report:
        (ROOT / "tools/app-preservation-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return report


if __name__ == "__main__":
    print(json.dumps(verify_app_source(write_report=True), ensure_ascii=False, indent=2))

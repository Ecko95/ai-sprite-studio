import hashlib
import importlib
import json
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ASSET_PREFIX = Path("src/ai_sprite_studio/assets/upstream")
PIXEL_REPO = "https://github.com/chongdashu/ai-pixel-snapped-game-sprites"
PIXEL_REVISION = "5f4c8b99a1de38a84af7dcc36af80622e5057cd6"
SPRITE_REPO = "https://github.com/aldegad/sprite-gen"
SPRITE_REVISION = "606db6d89beedace80863374859752739369ba19"

PROMPTS = {
    f"prompts/{number:02}-{name}.md"
    for number, name in enumerate(
        (
            "south-anchor",
            "pixel-snap",
            "directional-anchors",
            "action-spritesheet",
            "frame-recovery",
            "walk-cycle-i2v",
            "per-frame-snap",
            "runtime-normalize-and-align",
        ),
        start=1,
    )
}
REFERENCES = {
    "references/anchors/direction-anchors-nsew.png",
    "references/anchors/s-snapped-1024-chroma.png",
    "references/anchors/s-snapped-1024.png",
    "references/anchors/w-raw.png",
    "references/anchors/w-snapped-1024-chroma-canonical.png",
    "references/anchors/w-snapped-native-102x101.png",
    "references/diagrams/anchor-guide.png",
    "references/diagrams/post-generation-pipeline.png",
    "references/grids/alternating-1024x1024.png",
    "references/grids/alternating-2048x1536-4x3-pose-board.png",
    "references/poseboard/attack-w-bgclean-vs-fringe-cleaned.png",
    "references/poseboard/attack-w-generated-poseboard.png",
    "references/poseboard/attack-w-naive-grid-cells-only.png",
    "references/poseboard/attack-w-naive-grid-vs-recovered.png",
    "references/poseboard/attack-w-pixel-snapped-strike-frame.png",
    "references/poseboard/attack-w-recovered-vs-native-review.png",
    "references/problems/bleeding-naive-grid-animated.gif",
    "references/problems/drift-after-locked-attack.gif",
    "references/problems/drift-before-after-side-by-side.gif",
    "references/problems/drift-before-sliding-attack.gif",
    "references/problems/height-drift-attack.gif",
    "references/problems/mixel-vs-real-side-by-side.png",
    "references/problems/mixel-zoom-raw.png",
    "references/problems/real-pixels-zoom-snapped.png",
    "references/prompt-discipline/bad-detail-heavy-raw.png",
    "references/prompt-discipline/bad-detail-heavy-snapped-174x177.png",
    "references/prompt-discipline/good-restrictive-raw.png",
    "references/prompt-discipline/good-restrictive-snapped-96x96.png",
    "references/runtime/attack-w-preview.gif",
    "references/runtime/attack-w-runtime-spritesheet.png",
    "references/runtime/pirate-w-all-actions-combined.gif",
    "references/runtime/walk-w-preview.gif",
    "references/runtime/walk-w-runtime-spritesheet.png",
}
SPRITE_DOCS = {
    f"docs/{name}.md"
    for name in (
        "architecture",
        "chroma-alpha",
        "curation",
        "directional-anchor-workflow",
        "gen",
        "locomotion-curation",
        "pixel-perfect",
        "qa-motion",
        "run-contract",
        "sheet-slicing",
        "states-and-frames",
    )
}
CURATOR = {
    "scripts/curator/curator.css",
    "scripts/curator/curator.js",
    "scripts/curator/index.html",
}
EXPECTED = {
    PIXEL_REPO: (PIXEL_REVISION, "MIT", PROMPTS | REFERENCES | {"LICENSE"}),
    SPRITE_REPO: (
        SPRITE_REVISION,
        "Apache-2.0",
        {"SKILL.md", "LICENSE", "NOTICE"} | SPRITE_DOCS | CURATOR,
    ),
}


def test_locked_snapshot_has_complete_inventory_and_valid_hashes():
    lock = json.loads((ROOT / "upstream-lock.json").read_text())
    assert lock["version"] == 1
    entries = lock["entries"]
    assert len(entries) == 59

    destinations = set()
    for repository, (revision, spdx, expected_sources) in EXPECTED.items():
        repo_entries = [entry for entry in entries if entry["repository_url"] == repository]
        assert {entry["revision"] for entry in repo_entries} == {revision}
        assert {entry["source_path"] for entry in repo_entries} == expected_sources
        assert {entry["spdx_identifier"] for entry in repo_entries} == {spdx}

        slug = repository.rsplit("/", 1)[-1]
        for entry in repo_entries:
            expected_destination = (ASSET_PREFIX / slug / entry["source_path"]).as_posix()
            assert entry["destination_path"] == expected_destination
            destination = ROOT / entry["destination_path"]
            assert destination.is_file()
            assert hashlib.sha256(destination.read_bytes()).hexdigest() == entry["sha256"]
            destinations.add(entry["destination_path"])

    assert len(PROMPTS) == 8
    snapshot_files = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / ASSET_PREFIX).rglob("*")
        if path.is_file()
    }
    assert snapshot_files == destinations
    assert (ROOT / "THIRD_PARTY_NOTICES.md").is_file()


def test_offline_sync_check_accepts_locked_snapshot():
    result = subprocess.run(
        [sys.executable, ROOT / "scripts/sync_upstreams.py", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "Verified 59 upstream assets.\n"


def test_update_copies_from_the_locked_checkout_and_rewrites_hash(tmp_path):
    checkout = tmp_path / "checkouts" / "example"
    checkout.mkdir(parents=True)
    source = checkout / "asset.txt"
    source.write_bytes(b"upstream bytes\n")
    subprocess.run(["git", "init", "--quiet", checkout], check=True)
    subprocess.run(["git", "-C", checkout, "add", "asset.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            checkout,
            "-c",
            "user.name=Asset Test",
            "-c",
            "user.email=asset-test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--quiet",
            "-m",
            "fixture",
        ],
        check=True,
    )
    revision = subprocess.run(
        ["git", "-C", checkout, "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True)
    shutil.copyfile(ROOT / "scripts/sync_upstreams.py", project / "scripts/sync_upstreams.py")
    destination = "src/example/assets/upstream/example/asset.txt"
    (project / "upstream-lock.json").write_text(
        json.dumps(
            {
                "version": 1,
                "entries": [
                    {
                        "repository_url": "https://example.invalid/example",
                        "revision": revision,
                        "source_path": "asset.txt",
                        "destination_path": destination,
                        "sha256": "0" * 64,
                        "spdx_identifier": "MIT",
                    }
                ],
            }
        )
    )

    updated = subprocess.run(
        [sys.executable, project / "scripts/sync_upstreams.py", "--update", tmp_path / "checkouts"],
        cwd=project,
        capture_output=True,
        text=True,
    )
    assert updated.returncode == 0, updated.stdout + updated.stderr
    assert (project / destination).read_bytes() == source.read_bytes()
    lock = json.loads((project / "upstream-lock.json").read_text())
    assert lock["entries"][0]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()

    (project / destination).write_bytes(b"tampered\n")
    checked = subprocess.run(
        [sys.executable, project / "scripts/sync_upstreams.py", "--check"],
        cwd=project,
        capture_output=True,
        text=True,
    )
    assert checked.returncode == 1
    assert "hash mismatch" in checked.stderr


def test_project_and_pinned_sprite_gen_are_importable():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert pyproject["project"]["requires-python"] == ">=3.12,<3.13"
    assert pyproject["tool"]["uv"]["sources"]["sprite-gen"] == {
        "git": f"{SPRITE_REPO}.git",
        "rev": SPRITE_REVISION,
    }

    uv_lock = tomllib.loads((ROOT / "uv.lock").read_text())
    sprite_gen = next(package for package in uv_lock["package"] if package["name"] == "sprite-gen")
    assert sprite_gen["source"]["git"].endswith(
        f"sprite-gen.git?rev={SPRITE_REVISION}#{SPRITE_REVISION}"
    )
    assert importlib.import_module("ai_sprite_studio")
    assert importlib.import_module("sprite_gen")

"""Stack detection and persona scoring for projects (core/projects.py).

All projects live in a TemporaryDirectory; the real memory/projects.json is
never read or written.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import projects  # noqa: E402


@pytest.fixture(autouse=True)
def clean_detection_cache():
    projects.clear_cache()
    yield
    projects.clear_cache()


@pytest.fixture
def board(tmp_path, monkeypatch):
    memory = tmp_path / "memory"
    memory.mkdir()
    monkeypatch.setattr(projects, "PROJECTS_PATH", memory / "projects.json")
    return memory / "projects.json"


def write(project: Path, relative: str, content: str = "") -> Path:
    path = project / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def persona(pid, name, stacks):
    return {"id": pid, "name": name, "gender": "f", "stacks": stacks}


# ── detection ───────────────────────────────────────────────────────────────

def test_detect_package_json(tmp_path):
    project = tmp_path / "web"
    write(project, "package.json", json.dumps({
        "dependencies": {"react": "^18", "next": "^14"},
        "devDependencies": {"typescript": "^5"},
    }))
    assert projects.detect(project) == [
        "nextjs", "node", "react", "typescript"]


def test_detect_package_json_aliases_via_raw_text(tmp_path):
    project = tmp_path / "broken"
    write(project, "package.json", '{"dependencies": {"react-dom": "18", '
                                   '"@angular/core": "17", "svelte": "4"}}')
    tags = set(projects.detect(project))
    assert {"node", "react", "angular", "svelte"} <= tags


@pytest.mark.parametrize("relative,content,expected", [
    ("requirements.txt", "Django==5\npsycopg2\n", {"python", "django"}),
    ("requirements.txt", "fastapi==0.115\nuvicorn\n", {"python", "fastapi"}),
    ("requirements.txt", "Flask==3\n", {"python", "flask"}),
    ("pyproject.toml", '[project]\ndependencies = ["fastapi>=0.1"]\n',
     {"python", "fastapi"}),
    ("go.mod", "module example.com/x\n\ngo 1.24\n", {"go"}),
    ("Cargo.toml", '[package]\nname = "x"\n', {"rust"}),
    ("composer.json", '{"require": {"laravel/framework": "^11"}}',
     {"php", "laravel"}),
    ("Gemfile", "source 'https://rubygems.org'\ngem 'rails', '~> 7'\n",
     {"ruby", "rails"}),
    ("pom.xml", "<project><artifactId>spring-boot</artifactId></project>",
     {"java", "spring"}),
    ("App.csproj", "<Project Sdk=\"Microsoft.NET.Sdk\"></Project>", {"dotnet"}),
    ("Dockerfile", "FROM python:3.14\n", {"docker"}),
])
def test_detect_manifests(tmp_path, relative, content, expected):
    project = tmp_path / "proj"
    write(project, relative, content)
    assert set(projects.detect(project)) == expected


def test_detect_depth_two_is_bounded(tmp_path):
    project = tmp_path / "mono"
    write(project, "package.json", '{"dependencies": {"react": "18"}}')
    # Depth 2 counts: root/a/b/<manifest>.
    write(project, "apps/web/pyproject.toml",
          '[project]\ndependencies = ["fastapi"]\n')
    # Depth 3 is out of bounds.
    write(project, "apps/web/deep/Cargo.toml", '[package]\nname = "deep"\n')
    # Ignored directories are never scanned.
    write(project, "node_modules/pkg/package.json",
          '{"dependencies": {"vue": "3"}}')
    write(project, ".git/package.json", '{"dependencies": {"svelte": "4"}}')

    tags = set(projects.detect(project))
    assert {"node", "react", "python", "fastapi"} <= tags
    assert "rust" not in tags
    assert "vue" not in tags
    assert "svelte" not in tags


def test_detect_stops_after_max_files(tmp_path, monkeypatch):
    project = tmp_path / "huge"
    project.mkdir()
    for index in range(205):
        (project / f"a{index:03d}.txt").write_text("x", encoding="utf-8")
    # Sorts after the filler files, so the 200-file cap hides it.
    write(project, "package.json", '{"dependencies": {"react": "18"}}')
    assert projects.detect(project) == []

    monkeypatch.setattr(projects, "MAX_FILES", 1000)
    assert "react" in projects.detect(project)


def test_detect_missing_path_and_file_path(tmp_path):
    assert projects.detect(tmp_path / "nope") == []
    project = tmp_path / "app"
    write(project, "package.json", '{"dependencies": {"react": "18"}}')
    assert "react" in projects.detect(project / "package.json")


def test_detect_cache_invalidated_by_mtime(tmp_path):
    project = tmp_path / "cached"
    manifest = write(project, "package.json",
                     '{"dependencies": {"react": "18"}}')
    assert "react" in projects.detect(project)

    manifest.write_text('{"dependencies": {"vue": "3"}}', encoding="utf-8")
    import os
    stat = manifest.stat()
    os.utime(manifest, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))

    tags = set(projects.detect(project))
    assert "vue" in tags
    assert "react" not in tags


def test_normalize_stack_aliases():
    assert projects.normalize_stacks(
        ["Next.js", "reactjs", "TS", "nodejs", "golang", "C#"]
    ) == ["nextjs", "react", "typescript", "node", "go", "dotnet"]
    assert projects.normalize_stacks("ruby on rails") == ["rails"]
    assert projects.normalize_stacks("react, typescript; rust") == [
        "react", "typescript", "rust"]
    assert projects.normalize_stacks(None) == []


# ── board load/save ─────────────────────────────────────────────────────────

def test_board_load_save_and_malformed(board):
    assert projects.load() == {"projects": {}}
    projects.save({"projects": {"/tmp/x": {
        "name": "x", "stacks": ["python"], "persona": "py",
        "source": "pin", "detected_at": "2026-10-31T10:00:00-03:00"}}})
    stored = json.loads(board.read_text(encoding="utf-8"))
    assert stored["projects"]["/tmp/x"]["source"] == "pin"
    assert projects.load()["projects"]["/tmp/x"]["persona"] == "py"
    assert not [p for p in board.parent.iterdir() if p.suffix == ".tmp"]

    board.write_text("{broken", encoding="utf-8")
    assert projects.load() == {"projects": {}}
    projects.save(projects.load())
    assert json.loads(board.read_text(encoding="utf-8")) == {"projects": {}}


# ── resolution ──────────────────────────────────────────────────────────────

def react_project(tmp_path) -> Path:
    project = tmp_path / "frontend"
    write(project, "package.json", json.dumps({
        "dependencies": {"react": "^18", "next": "^14"},
        "devDependencies": {"typescript": "^5"},
    }))
    return project


def test_resolve_unique_best_match(tmp_path, board):
    project = react_project(tmp_path)
    people = [
        persona("fulana", "Fulana", ["react", "typescript", "nextjs"]),
        persona("beltrano", "Beltrano", ["python", "fastapi"]),
    ]
    result = projects.resolve(project, people)
    assert result["status"] == "match"
    assert result["persona"]["id"] == "fulana"
    assert set(result["stacks"]) == {"node", "react", "typescript", "nextjs"}


def test_resolve_tie_is_ambiguous(tmp_path, board):
    project = react_project(tmp_path)
    people = [
        persona("fulana", "Fulana", ["react", "typescript"]),
        persona("ciclana", "Ciclana", ["react", "nextjs"]),
    ]
    result = projects.resolve(project, people)
    assert result["status"] == "ambiguous"
    assert {p["id"] for p in result["candidates"]} == {"fulana", "ciclana"}
    assert "react" in result["stacks"]


def test_resolve_low_score_is_ambiguous(tmp_path, board):
    project = react_project(tmp_path)
    people = [persona("react-only", "React Only", ["react"])]
    result = projects.resolve(project, people)
    # Four detected tags require two matches; one is a low-confidence hit.
    assert result["status"] == "ambiguous"
    assert [p["id"] for p in result["candidates"]] == ["react-only"]


def test_resolve_none(tmp_path, board):
    project = react_project(tmp_path)
    result = projects.resolve(project, [persona("py", "Py", ["python"])])
    assert result["status"] == "none"
    assert "react" in result["stacks"]


def test_resolve_single_tag_project_needs_one_match(tmp_path, board):
    project = tmp_path / "service"
    write(project, "requirements.txt", "requests==2.32\n")
    result = projects.resolve(project, [persona("api", "Api", ["python"])])
    assert result["status"] == "match"
    assert result["persona"]["id"] == "api"


def test_link_pin_wins_over_detection(tmp_path, board):
    project = react_project(tmp_path)
    people = [
        persona("fulana", "Fulana", ["react", "typescript", "nextjs"]),
        persona("beltrano", "Beltrano", ["python"]),
    ]
    assert projects.resolve(project, people)["persona"]["id"] == "fulana"

    record = projects.link(project, "beltrano")
    assert record["source"] == "pin"
    assert record["persona"] == "beltrano"
    assert record["stacks"] == ["nextjs", "node", "react", "typescript"]
    stored = projects.load()["projects"]
    key = str(project.resolve())
    assert stored[key]["persona"] == "beltrano"
    assert stored[key]["source"] == "pin"

    result = projects.resolve(project, people)
    assert result["status"] == "match"
    assert result["persona"]["id"] == "beltrano"
    assert result["source"] == "pin"


def test_link_requires_persona(tmp_path, board):
    project = react_project(tmp_path)
    with pytest.raises(ValueError, match="persona"):
        projects.link(project, "")


def test_resolve_pin_to_missing_persona_falls_back(tmp_path, board):
    project = react_project(tmp_path)
    projects.link(project, "ghost")
    result = projects.resolve(
        project, [persona("fulana", "Fulana", ["react", "typescript"])])
    assert result["status"] == "match"
    assert result["persona"]["id"] == "fulana"

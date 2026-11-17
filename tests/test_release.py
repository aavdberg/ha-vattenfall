"""Execute the release workflow's Python preparation without publishing."""

import json
import os
import subprocess
from pathlib import Path
from zipfile import ZipFile

import pytest
import yaml


@pytest.fixture
def release_project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    component = root / "custom_components" / "vattenfall"
    component.mkdir(parents=True)
    (component / "manifest.json").write_text(json.dumps({"version": "2.3.4"}))
    (component / "__init__.py").write_text('"""Synthetic integration."""\n')
    (root / "pyproject.toml").write_text('[project]\nversion = "2.3.4"\n')
    (root / "README.md").write_text("Synthetic documentation, not an integration file")
    monkeypatch.chdir(root)
    subprocess.run(["git", "init", "-q"], check=True)
    subprocess.run(["git", "add", "."], check=True)
    commit()
    (component / "untracked-secret.txt").write_text("SYNTHETIC-DO-NOT-PACKAGE")
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("RELEASE_BRANCH", "dev")
    workflow = yaml.safe_load(
        (
            Path(__file__).parents[1] / ".github" / "workflows" / "release.yml"
        ).read_text()
    )
    run = next(
        step["run"]
        for step in workflow["jobs"]["release"]["steps"]
        if step.get("id") == "package"
    )
    lines = run.splitlines()
    assert lines[0] == "python - <<'PY'" and lines[-1] == "PY"
    return "\n".join(lines[1:-1]), output, tmp_path / "vattenfall.zip"


def commit():
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Synthetic CI",
            "-c",
            "user.email=ci@example.invalid",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "test: synthetic release fixture\n\n"
            "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>",
        ],
        check=True,
    )


def prepare(fixture):
    code, output, archive = fixture
    exec(compile(code, "<release-workflow>", "exec"), {})
    metadata = dict(line.split("=", 1) for line in output.read_text().splitlines())
    with ZipFile(archive) as package:
        assert set(package.namelist()) == {"__init__.py", "manifest.json"}
        manifest = json.loads(package.read("manifest.json"))
    assert manifest["version"] == metadata["tag"][1:]
    return metadata


def test_beta_versions_are_monotonic_and_commit_reruns_are_idempotent(release_project):
    assert prepare(release_project)["tag"] == "v2.3.4-beta.1"
    subprocess.run(["git", "tag", "v2.3.4-beta.1"], check=True)
    assert prepare(release_project)["tag"] == "v2.3.4-beta.1"
    commit()
    assert prepare(release_project)["tag"] == "v2.3.4-beta.2"


def test_stable_release_uses_base_and_never_moves_existing_tags(
    release_project, monkeypatch
):
    monkeypatch.setenv("RELEASE_BRANCH", "main")
    metadata = prepare(release_project)
    assert metadata == {"tag": "v2.3.4", "beta": "false", "skip": "false"}
    subprocess.run(["git", "tag", "v2.3.4"], check=True)
    assert prepare(release_project)["skip"] == "true"


@pytest.mark.parametrize("version", ["01.2.3", "1.2", "1.2.3-beta.1", "3.0.0"])
def test_invalid_or_mismatched_base_versions_cannot_publish(release_project, version):
    Path("custom_components/vattenfall/manifest.json").write_text(
        json.dumps({"version": version})
    )
    with pytest.raises(SystemExit):
        exec(compile(release_project[0], "<release-workflow>", "exec"), {})
    assert not Path(os.environ["RUNNER_TEMP"], "vattenfall.zip").exists()

"""Le shell de reconstruction doit conserver la première panne et sa preuve."""
import logging
import os
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parent / "src"))
from docker_eval import bentoml_runner
from docker_eval.bentoml_runner import BentoMLRunner


@pytest.fixture
def builder_shell(tmp_path, monkeypatch):
    source = tmp_path / "rendu"
    (source / "src").mkdir(parents=True)
    (source / "requirements.txt").write_text("pandas\n")
    for name in ("prepare_data", "train_model"):
        (source / "src" / f"{name}.py").write_text("pass\n")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    # Vrais processus : les sorties et codes traversent le shell du runner.
    commands = {
        "pip": 'phase=install_bentoml; case "$*" in *requirements.txt*) phase=install_requirements;; esac\necho "$phase evidence"\n[ "$FAIL_PHASE" != "$phase" ] || exit 17\n',
        "python": 'case "$1" in *prepare_data.py) phase=prepare;; *train_model.py) phase=train;; *) exec "' + sys.executable + '" "$@";; esac\necho "$phase evidence"\n[ "$FAIL_PHASE" != "$phase" ] || exit 18\n',
        "bentoml": 'echo "$1 evidence" >&2\nif [ "$1" = build ] && [ "$FAIL_BUILD" = 1 ]; then exit 19; fi\n[ "$FAIL_PHASE" != "$1" ] || exit 19\ncase "$1" in list) echo \'[{"tag":"svc:abc"}]\';; export) [ "$NO_ARTIFACT" = 1 ] || touch "$3";; esac\n',
        "chown": 'echo "cleanup evidence"\nexit "${CHOWN_EXIT:-0}"\n',
    }
    for name, body in commands.items():
        path = binaries / name
        path.write_text("#!/bin/sh\n" + body + "exit 0\n")
        path.chmod(0o755)
    real_run = subprocess.run

    def run(command, **kwargs):
        assert command[:2] == ["docker", "run"]
        mount = command[command.index("-v") + 1].rsplit(":", 1)[0]
        script = command[-1].replace("/src", mount)
        return real_run(["sh", "-c", script], **kwargs)

    monkeypatch.setenv("PATH", str(binaries) + os.pathsep + os.environ["PATH"])
    monkeypatch.setattr(bentoml_runner.subprocess, "run", run)
    return BentoMLRunner("370054", str(source), 60, logging.getLogger("t")), source


@pytest.mark.parametrize("phase,code,evidence", [
    ("install_bentoml", 17, "install_bentoml evidence"),
    ("install_requirements", 17, "install_requirements evidence"),
    ("build", 19, "build evidence"),
    ("list", 19, "list evidence"),
    ("export", 19, "export evidence"),
])
def test_panne_de_phase_conservee(builder_shell, monkeypatch, phase, code, evidence):
    runner, source = builder_shell
    monkeypatch.setenv("FAIL_PHASE", phase)
    result = runner._build_bento_from_source(str(source))
    assert result["success"] is False
    assert runner.steps[-1]["exit_code"] == code
    assert evidence in runner.steps[-1]["output"]
    assert "fault" not in result
    assert not (Path(runner._dossier_construction) / "service.bento").exists()


def test_export_sans_artefact_est_un_echec(builder_shell, monkeypatch):
    runner, source = builder_shell
    monkeypatch.setenv("NO_ARTIFACT", "1")
    result = runner._build_bento_from_source(str(source))
    assert result["success"] is False
    assert runner.steps[-1]["exit_code"] != 0


def test_construction_et_export_reussis(builder_shell):
    runner, source = builder_shell
    result = runner._build_bento_from_source(str(source))
    assert result["success"] is True
    assert runner.steps[-1]["exit_code"] == 0
    assert "install_requirements evidence" in runner.steps[-1]["output"]
    assert "export evidence" in runner.steps[-1]["output"]


def test_nettoyage_ne_masque_pas_la_panne(builder_shell, monkeypatch):
    runner, source = builder_shell
    monkeypatch.setenv("FAIL_PHASE", "build")
    monkeypatch.setenv("CHOWN_EXIT", "23")
    result = runner._build_bento_from_source(str(source))
    assert result["success"] is False
    assert runner.steps[-1]["exit_code"] == 19
    assert "cleanup evidence" in runner.steps[-1]["output"]


def test_code_non_nul_refuse_un_artefact_partiel(tmp_path, monkeypatch):
    source = tmp_path / "rendu"
    source.mkdir()
    runner = BentoMLRunner("370054", str(source), 60, logging.getLogger("t"))

    def run(command, **kwargs):
        mount = command[command.index("-v") + 1].rsplit(":", 1)[0]
        (Path(mount) / "service.bento").write_text("export partiel")
        return subprocess.CompletedProcess(command, 19, "export failed", "")

    monkeypatch.setattr(bentoml_runner.subprocess, "run", run)
    result = runner._build_bento_from_source(str(source))
    assert result["success"] is False
    assert runner.steps[-1]["exit_code"] == 19


def test_absence_de_preparation_facultative(builder_shell):
    runner, source = builder_shell
    (source / "requirements.txt").unlink()
    for path in (source / "src").glob("*.py"):
        path.unlink()
    result = runner._build_bento_from_source(str(source))
    assert result["success"] is True
    assert "install_requirements evidence" not in runner.steps[-1]["output"]


@pytest.mark.parametrize("phase,script", [
    ("prepare", "src/prepare_data.py"), ("train", "src/train_model.py"),
])
@pytest.mark.parametrize("build_failure", [False, True])
def test_preparation_facultative_ne_decide_pas_du_build(
        builder_shell, monkeypatch, phase, script, build_failure):
    runner, source = builder_shell
    monkeypatch.setenv("FAIL_PHASE", phase)
    if build_failure:
        monkeypatch.setenv("FAIL_BUILD", "1")
    result = runner._build_bento_from_source(str(source))
    assert result["success"] is (not build_failure)
    assert runner.steps[-1]["exit_code"] == (19 if build_failure else 0)
    output = runner.steps[-1]["output"]
    assert f"{script}: exit_code=18" in output
    assert f"avertissement: {script}" in output
    assert "build evidence" in output
    assert "fault" not in result

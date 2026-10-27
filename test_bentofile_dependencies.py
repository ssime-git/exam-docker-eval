"""Source reconstruction installs the dependencies actually declared by the copy.

The Docker boundary is simulated; the builder shell and preparatory Python
scripts really run, against a tiny fake package installer (no network/Docker).
"""
import json
import logging
import os
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parent / "src"))
from docker_eval import bentoml_runner as module
from docker_eval.bentoml_runner import BentoMLRunner

REAL_RUN = subprocess.run


def execute_builder(tmp_path, monkeypatch, bentofile, *, fail_install=False):
    source = tmp_path / "copy"
    (source / "src").mkdir(parents=True)
    (source / "bentofile.yaml").write_text(bentofile)
    (source / "requirements.txt").write_text("pytest\nrequests\n")
    (source / "runtime requirements.txt").write_text("pandas\nscikit-learn\n")
    (source / "src/prepare_data.py").write_text(
        "import pandas\nfrom pathlib import Path\nPath('prepared').touch()\n")
    (source / "src/train_model.py").write_text(
        "import sklearn\nfrom pathlib import Path\nPath('trained').touch()\n")
    tools = tmp_path / "tools"
    tools.mkdir()
    installer = tools / "pip"
    installer.write_text(
        "#!" + sys.executable + "\n"
        "import json, os, pathlib, sys\n"
        "with open(os.environ['PIP_LOG'], 'a') as f: f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if os.environ.get('FAIL_INSTALL') and ('pandas' in sys.argv or '-r' in sys.argv and sys.argv[-1] == 'runtime requirements.txt'): sys.exit(17)\n"
        "packages = sys.argv[1:]\n"
        "if '-r' in packages: packages = pathlib.Path(packages[packages.index('-r')+1]).read_text().splitlines()\n"
        "for dependency, module in [('pandas', 'pandas'), ('scikit-learn', 'sklearn')]:\n"
        " if any(p.startswith(dependency) for p in packages): pathlib.Path(module + '.py').touch()\n")
    installer.chmod(0o755)
    python = tools / "python"
    python.write_text("#!/bin/sh\nexec " + sys.executable + ' "$@"\n')
    python.chmod(0o755)
    bento = tools / "bentoml"
    bento.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        ' build) test -f prepared && test -f trained;;\n'
        """ list) printf '[{"tag":"svc:abc"}]';;\n"""
        ' export) touch "$3";;\n'
        'esac\n')
    bento.chmod(0o755)
    calls = []
    def docker_run(command, **kwargs):
        calls.append(command)
        mount = command[command.index("-v") + 1].rsplit(":", 1)[0]
        # Simulate the bind mount; all shell commands still run unchanged.
        script = command[-1].replace("/src", mount)
        env = dict(os.environ, PATH=str(tools) + ":" + os.environ["PATH"],
                   PIP_LOG=str(tmp_path / "pip.jsonl"), PYTHONPATH=mount)
        if fail_install:
            env["FAIL_INSTALL"] = "1"
        return REAL_RUN(["sh", "-c", script], capture_output=True, text=True, env=env)
    monkeypatch.setattr(module.subprocess, "run", docker_run)
    runner = BentoMLRunner("copy-test", str(source), 1800, logging.getLogger("bento"))
    result = runner._build_bento_from_source(str(source))
    installs = [json.loads(line) for line in (tmp_path / "pip.jsonl").read_text().splitlines()]
    return result, runner, calls, installs, source


def test_packages_installed_before_training_despite_test_only_requirements(tmp_path, monkeypatch):
    result, runner, calls, installs, source = execute_builder(tmp_path, monkeypatch, """
service: src.service:svc
python:
  packages: [pandas, scikit-learn]
""")
    assert result["success"], result
    assert any(args[-2:] == ["pandas", "scikit-learn"] for args in installs)
    assert "--- install bentofile.yaml python.packages: exit_code=0" in runner.steps[-1]["output"]
    assert not (source / "prepared").exists(), "original copy must stay untouched"


def test_declared_requirements_path_is_used(tmp_path, monkeypatch):
    result, runner, calls, installs, source = execute_builder(tmp_path, monkeypatch, """
service: src.service:svc
python:
  requirements_txt: "runtime requirements.txt"
""")
    assert result["success"], result
    assert any(args[-2:] == ["-r", "runtime requirements.txt"] for args in installs)


def test_failed_declared_install_preserves_cause_and_stops_before_preparation(tmp_path, monkeypatch):
    result, runner, calls, installs, source = execute_builder(tmp_path, monkeypatch, """
python:
  packages: [pandas, scikit-learn]
""", fail_install=True)
    assert not result["success"]
    assert runner.steps[-1]["exit_code"] == 17
    assert "install bentofile.yaml python.packages: exit_code=17" in runner.steps[-1]["output"]
    assert "--- src/prepare_data.py" not in runner.steps[-1]["output"]


def test_packages_are_arguments_not_shell_code(tmp_path, monkeypatch):
    result, runner, calls, installs, source = execute_builder(tmp_path, monkeypatch, """
python:
  packages: [pandas, scikit-learn, "odd; touch INJECTED"]
""")
    assert result["success"], result
    assert any(args[-1] == "odd; touch INJECTED" for args in installs)
    assert not any(source.rglob("INJECTED"))


@pytest.mark.parametrize("override,expected", [(None, "python:3.10-slim"), ("custom:builder", "custom:builder")])
def test_declared_python_version_and_explicit_builder_override(tmp_path, monkeypatch, override, expected):
    if override:
        monkeypatch.setenv("EXAM_BENTOML_BUILDER_IMAGE", override)
    else:
        monkeypatch.delenv("EXAM_BENTOML_BUILDER_IMAGE", raising=False)
    result, runner, calls, installs, source = execute_builder(tmp_path, monkeypatch, """
docker:
  python_version: "3.10"
python:
  packages: [pandas, scikit-learn]
""")
    assert calls[0][calls[0].index("sh") - 1] == expected

@pytest.mark.parametrize("bentofile", [
    "python: [pandas]\n",
    "python:\n  packages: pandas\n",
    "python:\n  requirements_txt: [requirements.txt]\n",
    "docker:\n  python_version: '3.10; touch INJECTED'\n",
    "python: [\n",
])
def test_invalid_configuration_is_visible_and_never_runs_docker(tmp_path, monkeypatch, bentofile):
    (tmp_path / "bentofile.yaml").write_text(bentofile)
    runner = BentoMLRunner("copy-test", str(tmp_path), 1800, logging.getLogger("bento"))
    def no_docker(*args, **kwargs):
        pytest.fail("invalid configuration must not start Docker")
    monkeypatch.setattr(module.subprocess, "run", no_docker)
    result = runner._build_bento_from_source(str(tmp_path))
    assert not result["success"]
    assert "configuration de construction illisible" in result["error"]
    assert runner.steps[-1]["exit_code"] == 1

def test_external_bentofile_symlink_never_read_on_host(tmp_path, monkeypatch):
    import builtins
    external = tmp_path / "external.yaml"
    external.write_text("python: {packages: [pandas]}\n")
    source = tmp_path / "copy"
    source.mkdir()
    (source / "bentofile.yaml").symlink_to(external)
    runner = BentoMLRunner("copy-test", str(source), 1800, logging.getLogger("bento"))
    original_open = builtins.open
    def guarded_open(file, *args, **kwargs):
        if os.path.basename(os.fspath(file)) == "bentofile.yaml":
            pytest.fail("external configuration must not be opened on the host")
        return original_open(file, *args, **kwargs)
    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(module.subprocess, "run",
                        lambda *args, **kwargs: pytest.fail("no Docker for external configuration"))
    result = runner._build_bento_from_source(str(source))
    assert not result["success"]
    assert "hors de la copie" in result["error"]

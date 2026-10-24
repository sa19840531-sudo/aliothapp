"""通过离线 Compose 和真实 Bash 流程验证部署镜像围栏。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_DIRECTORY = REPOSITORY_ROOT / "infra" / "scripts"
IMAGE_CHECK = SCRIPT_DIRECTORY / "validate-compose-images.py"
NEW_TAG = "221361e8d7b95cd1f3b7fac5dc8ae0932f56edb0"
OLD_TAG = "package-hash-20261009-185fd1968799"
SERVICE_IMAGES = {
    "alioth-migrate": "api",
    "alioth-initialize": "api",
    "alioth-api": "api",
    "alioth-worker": "api",
    "alioth-workstation": "workstation",
    "alioth-gateway": "gateway",
}


def _images(tag: str) -> dict[str, str]:
    return {service: f"alioth-local/{image}:{tag}" for service, image in SERVICE_IMAGES.items()}


def _check_images(config: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(IMAGE_CHECK),
            "--image-prefix",
            "alioth-local",
            "--image-tag",
            NEW_TAG,
        ],
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        input=config,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


@pytest.mark.parametrize("service", SERVICE_IMAGES)
def test_image_check_rejects_each_pinned_service(service: str) -> None:
    config = {"services": {name: {"image": image} for name, image in _images(NEW_TAG).items()}}
    config["services"][service]["image"] = _images(OLD_TAG)[service]

    result = _check_images(json.dumps(config))

    assert result.returncode == 1
    assert service in result.stderr
    assert _images(OLD_TAG)[service] in result.stderr


@pytest.mark.parametrize("config", ("not-json", "[]", '{"services": null}', '{"services": {}}'))
def test_image_check_rejects_missing_or_invalid_configuration(config: str) -> None:
    assert _check_images(config).returncode == 1


@pytest.mark.parametrize("case", ("old_process_tag", "pinned_override", "valid_override"))
def test_real_compose_configuration_preserves_image_precedence(tmp_path: Path, case: str) -> None:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("未安装 Docker Compose CLI；脚本执行与 JSON 围栏仍由独立回归验证")
    available = subprocess.run([docker, "compose", "version"], capture_output=True, check=False)
    if available.returncode != 0:
        pytest.skip("Docker Compose CLI 不可用")

    app_env = tmp_path / "app.env"
    app_env.write_text("", encoding="utf-8")
    compose_env = tmp_path / "compose.env"
    compose_env.write_text(
        f"CI_REGISTRY_IMAGE=alioth-local\nALIOTH_IMAGE_TAG={NEW_TAG}\n"
        f"ALIOTH_APP_ENV_FILE={app_env.as_posix()}\n"
        "ALIOTH_MOBILE_DEMO_TRUSTED_PROXY_ADDRESSES=127.0.0.1\n",
        encoding="utf-8",
    )
    command = [
        docker,
        "compose",
        "--env-file",
        str(compose_env),
        "-f",
        str(REPOSITORY_ROOT / "infra" / "docker-compose.production.yml"),
    ]
    environment = dict(os.environ)
    for key in ("ALIOTH_IMAGE_TAG", "CI_REGISTRY_IMAGE", "ALIOTH_APP_ENV_FILE", "COMPOSE_FILE"):
        environment.pop(key, None)
    if case == "old_process_tag":
        environment["ALIOTH_IMAGE_TAG"] = OLD_TAG
    else:
        environment["ALIOTH_IMAGE_TAG"] = NEW_TAG
        override = tmp_path / "override.yml"
        override.write_text(
            "services:\n  alioth-api:\n    command: [python, -m, services.api.app.worker]\n"
            "    volumes:\n      - type: bind\n"
            f"        source: {tmp_path.as_posix()}\n        target: /app-qc\n"
            + (f"    image: alioth-local/api:{OLD_TAG}\n" if case == "pinned_override" else ""),
            encoding="utf-8",
        )
        command.extend(["-f", str(override)])
    command.extend(["--profile", "ops", "config", "--format", "json"])

    rendered = subprocess.run(
        command,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    services = json.loads(rendered.stdout)["services"]
    checked = _check_images(rendered.stdout)

    assert checked.returncode == (0 if case == "valid_override" else 1)
    if case != "old_process_tag":
        assert services["alioth-api"]["command"] == ["python", "-m", "services.api.app.worker"]
        assert services["alioth-api"]["volumes"][0]["target"] == "/app-qc"


FAKE_CLI = r"""
import json
import os
import sys
from pathlib import Path

name, *arguments = sys.argv[1:]
state_path = Path(os.environ["ALIOTH_TEST_STATE"])
state = json.loads(state_path.read_text(encoding="utf-8"))
event = {"command": name, "arguments": arguments,
         "tag": os.environ.get("ALIOTH_IMAGE_TAG"),
         "prefix": os.environ.get("CI_REGISTRY_IMAGE")}
with Path(os.environ["ALIOTH_TEST_LOG"]).open("a", encoding="utf-8") as log:
    log.write(json.dumps(event) + "\n")

if name == "id":
    print("1001")
elif name == "git":
    if arguments[0] == "rev-parse":
        print(state["new_tag"])
    elif arguments[0] == "checkout" and state.get("remove_helper"):
        Path(state["helper_path"]).unlink()
elif name == "docker":
    if arguments[0] == "inspect":
        if arguments[-1] == "{{.State.Status}}":
            print("running")
        else:
            image = state["containers"].get(arguments[1])
            if image is None:
                raise SystemExit(1)
            print(image)
    elif arguments[0] == "compose":
        images = {service: f'{event["prefix"]}/{image}:{event["tag"]}'
                  for service, image in state["service_images"].items()}
        if state.get("pinned_service"):
            images[state["pinned_service"]] = state["pinned_image"]
        if "config" in arguments and "--format" in arguments:
            print(json.dumps({"services": {service: {"image": image}
                                            for service, image in images.items()}}))
        if "up" in arguments:
            state["containers"] = images
            if state.get("post_mismatch"):
                state["containers"]["alioth-api"] = state["post_mismatch"]
            state_path.write_text(json.dumps(state), encoding="utf-8")
"""


def _bash() -> str:
    if os.name == "nt":
        git_bash = Path("C:/Program Files/Git/bin/bash.exe")
        if git_bash.is_file():
            return str(git_bash)
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("未安装 Bash，无法运行部署脚本行为回归")
    return bash


def _run_deployment(
    tmp_path: Path,
    *,
    same_tag: bool = False,
    containers_current: bool = False,
    pinned_service: str | None = None,
    post_mismatch: bool = False,
    remove_helper: bool = False,
    env_mode: int = 0o600,
) -> tuple[subprocess.CompletedProcess[str], list[dict], Path]:
    root = tmp_path / "repository"
    scripts = root / "infra" / "scripts"
    scripts.mkdir(parents=True)
    for name in (
        "redeploy-local-production.sh",
        "verify-production.sh",
        "validate-compose-images.py",
    ):
        destination = scripts / name
        shutil.copyfile(SCRIPT_DIRECTORY / name, destination)
        destination.chmod(0o755)
    compose = root / "infra" / "docker-compose.production.yml"
    shutil.copyfile(REPOSITORY_ROOT / "infra" / "docker-compose.production.yml", compose)
    app_env = tmp_path / "app.env"
    app_env.write_text("", encoding="utf-8")
    compose_env = tmp_path / "compose.env"
    compose_env.write_text(
        f"CI_REGISTRY_IMAGE=alioth-local\nALIOTH_IMAGE_TAG={NEW_TAG if same_tag else OLD_TAG}\n"
        f"ALIOTH_APP_ENV_FILE={app_env.as_posix()}\n",
        encoding="utf-8",
    )
    compose_env.chmod(env_mode)
    override = tmp_path / "override.yml"
    override.write_text("services: {}\n", encoding="utf-8")
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps(
            {
                "new_tag": NEW_TAG,
                "service_images": SERVICE_IMAGES,
                "containers": _images(NEW_TAG if containers_current else OLD_TAG),
                "pinned_service": pinned_service,
                "pinned_image": _images(OLD_TAG).get(pinned_service),
                "post_mismatch": _images(OLD_TAG)["alioth-api"] if post_mismatch else None,
                "remove_helper": remove_helper,
                "helper_path": (scripts / "validate-compose-images.py").as_posix(),
            }
        ),
        encoding="utf-8",
    )
    log = tmp_path / "events.jsonl"
    fake_cli = tmp_path / "fake_cli.py"
    fake_cli.write_text(FAKE_CLI, encoding="utf-8")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for name in ("id", "git", "docker", "curl", "flock", "sudo"):
        wrapper = binaries / name
        wrapper.write_text(
            '#!/usr/bin/env bash\nexec "$ALIOTH_TEST_PYTHON" "$ALIOTH_TEST_FAKE_CLI" '
            f'{name} "$@"\n',
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
    python = binaries / "python3"
    python.write_text(
        '#!/usr/bin/env bash\nexec "$ALIOTH_TEST_PYTHON" "$@"\n',
        encoding="utf-8",
    )
    python.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
        "PYTHONIOENCODING": "utf-8",
        "ALIOTH_TEST_PYTHON": Path(sys.executable).as_posix(),
        "ALIOTH_TEST_BIN": (
            f"/{binaries.drive[0].lower()}{binaries.as_posix()[2:]}"
            if os.name == "nt"
            else binaries.as_posix()
        ),
        "ALIOTH_TEST_FAKE_CLI": fake_cli.as_posix(),
        "ALIOTH_TEST_STATE": state.as_posix(),
        "ALIOTH_TEST_LOG": log.as_posix(),
        "ALIOTH_IMAGE_TAG": OLD_TAG,
        "CI_REGISTRY_IMAGE": "wrong-inherited-prefix",
        "ALIOTH_COMPOSE_ENV": compose_env.as_posix(),
        "ALIOTH_COMPOSE_OVERRIDE_FILE": override.as_posix(),
        "ALIOTH_DEPLOYMENT_LOCK": (tmp_path / "deploy.lock").as_posix(),
        "ALIOTH_DEPLOYMENT_HISTORY": (tmp_path / "history.log").as_posix(),
        "ALIOTH_BACKUP_SCRIPT": (tmp_path / "backup.sh").as_posix(),
        "ALIOTH_FORCE_REDEPLOY": "0",
    }
    result = subprocess.run(
        [
            _bash(),
            "-c",
            'export PATH="$ALIOTH_TEST_BIN:$PATH"; exec bash "$@"',
            "deploy-test",
            (scripts / "redeploy-local-production.sh").as_posix(),
            "--branch",
            "dev",
            "--commit",
            NEW_TAG,
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    events = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    return result, events, compose_env


def test_deployment_binds_target_images_and_keeps_post_checks(tmp_path: Path) -> None:
    result, events, _ = _run_deployment(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    compose_calls = [
        event
        for event in events
        if event["command"] == "docker" and event["arguments"][0] == "compose"
    ]
    assert compose_calls
    assert all(
        event["tag"] == NEW_TAG and event["prefix"] == "alioth-local" for event in compose_calls
    )
    assert any("alioth-migrate" in event["arguments"] for event in compose_calls)
    assert any("alioth-initialize" in event["arguments"] for event in compose_calls)
    assert any("up" in event["arguments"] for event in compose_calls)
    assert any("{{.Config.Image}}" in event["arguments"] for event in events)
    assert "基础部署验收通过" in result.stdout


@pytest.mark.parametrize("mode", [0o600, 0o640])
def test_offline_image_tag_update_preserves_existing_env_permissions(tmp_path, mode):
    result, _, environment = _run_deployment(tmp_path, env_mode=mode)
    assert result.returncode == 0, result.stderr
    assert environment.stat().st_mode & 0o777 == mode


@pytest.mark.parametrize("service", SERVICE_IMAGES)
def test_pinned_override_fails_before_backup_stop_or_migration(
    tmp_path: Path,
    service: str,
) -> None:
    result, events, compose_env = _run_deployment(tmp_path, same_tag=True, pinned_service=service)

    assert result.returncode != 0
    assert service in result.stderr
    assert "部署编排镜像版本不一致" in result.stderr
    assert not any(event["command"] == "sudo" for event in events)
    assert not any(event["arguments"][0] == "build" for event in events)
    assert not any(
        "stop" in event["arguments"] or "up" in event["arguments"] or "run" in event["arguments"]
        for event in events
    )
    assert f"ALIOTH_IMAGE_TAG={NEW_TAG}\n" in compose_env.read_text(encoding="utf-8")


def test_same_tag_with_stale_container_continues_deployment(tmp_path: Path) -> None:
    result, events, _ = _run_deployment(tmp_path, same_tag=True)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "继续重建服务" in result.stdout
    assert any(event["arguments"][0] == "build" for event in events)
    assert any("up" in event["arguments"] for event in events)


def test_same_tag_skips_only_when_actual_containers_match(tmp_path: Path) -> None:
    result, events, _ = _run_deployment(tmp_path, same_tag=True, containers_current=True)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "当前部署已是最新版本" in result.stdout
    assert not any(event["arguments"][0] == "build" for event in events)
    assert not any(event["command"] == "sudo" for event in events)
    inspections = [event for event in events if "{{.Config.Image}}" in event["arguments"]]
    assert {event["arguments"][1] for event in inspections} == {
        "alioth-api",
        "alioth-worker",
        "alioth-workstation",
        "alioth-gateway",
    }


def test_checkout_older_commit_does_not_remove_running_image_validator(tmp_path: Path) -> None:
    result, _, _ = _run_deployment(tmp_path, remove_helper=True)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "基础部署验收通过" in result.stdout


def test_post_deployment_image_mismatch_still_fails(tmp_path: Path) -> None:
    result, events, _ = _run_deployment(tmp_path, post_mismatch=True)

    assert result.returncode != 0
    assert "容器镜像版本不一致: alioth-api" in result.stderr
    assert any("up" in event["arguments"] for event in events)
    assert not any("ps" in event["arguments"] for event in events)

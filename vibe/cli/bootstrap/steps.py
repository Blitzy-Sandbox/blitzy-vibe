from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import subprocess
import sys

from rich.console import Console
from rich.prompt import Prompt

console = Console()


@dataclass
class BootstrapContext:
    """Shared state across bootstrap steps."""

    environment: str = "dev"
    skip_make: bool = False
    run_tests_flag: bool = False
    blitzy_env_path_arg: Path | None = None
    blitzy_env_path: Path = field(default_factory=lambda: Path.home() / "Lab" / "Work" / "Blitzy")
    venv_path: Path | None = None


def deactivate_venv(ctx: BootstrapContext) -> None:
    """Step 1: Remove any active VIRTUAL_ENV from PATH and environment."""
    virtual_env = os.environ.get("VIRTUAL_ENV")
    if not virtual_env:
        console.print("    [dim]No active venv to deactivate[/]")
        return

    venv_bin = str(Path(virtual_env) / "bin")
    path_parts = os.environ.get("PATH", "").split(os.pathsep)
    new_path = os.pathsep.join(p for p in path_parts if p != venv_bin)
    os.environ["PATH"] = new_path
    del os.environ["VIRTUAL_ENV"]
    console.print(f"    [dim]Deactivated venv at {virtual_env}[/]")


def find_or_create_venv(ctx: BootstrapContext) -> None:
    """Step 2: Find existing venv or create .venv."""
    cwd = Path.cwd()
    candidates = sorted(cwd.glob("[.]venv*/bin/activate")) + sorted(
        cwd.glob("venv*/bin/activate")
    )

    if len(candidates) > 1:
        console.print("    [yellow]Multiple venvs found:[/]")
        for i, c in enumerate(candidates, 1):
            console.print(f"      {i}. {c.parent.parent}")
        choice = Prompt.ask(
            "    Select venv",
            choices=[str(i) for i in range(1, len(candidates) + 1)],
            default="1",
        )
        venv_dir = candidates[int(choice) - 1].parent.parent
    elif len(candidates) == 1:
        venv_dir = candidates[0].parent.parent
    else:
        venv_dir = cwd / ".venv"
        console.print(f"    [dim]No venv found, creating {venv_dir}[/]")
        subprocess.run(
            [sys.executable, "-m", "venv", str(venv_dir)],
            check=True,
        )

    ctx.venv_path = venv_dir
    os.environ["VIRTUAL_ENV"] = str(venv_dir)
    venv_bin = str(venv_dir / "bin")
    path = os.environ.get("PATH", "")
    if venv_bin not in path:
        os.environ["PATH"] = venv_bin + os.pathsep + path
    console.print(f"    [dim]Using venv: {venv_dir}[/]")


def set_blitzy_env_path(ctx: BootstrapContext) -> None:
    """Step 3: Resolve the Blitzy env files directory."""
    if ctx.blitzy_env_path_arg:
        ctx.blitzy_env_path = ctx.blitzy_env_path_arg.expanduser().resolve()
    elif env_path := os.environ.get("PATH_TO_BLITZY_ENV"):
        ctx.blitzy_env_path = Path(env_path).expanduser().resolve()
    # else: keep default ~/Lab/Work/Blitzy

    console.print(f"    [dim]Blitzy env path: {ctx.blitzy_env_path}[/]")


def load_env_file(ctx: BootstrapContext) -> None:
    """Step 4: Load environment-specific .env file using python-dotenv."""
    from dotenv import dotenv_values

    env_filenames = {
        "dev": "envfile",
        "qa": "envfileQA",
        "prod": "envfilePROD",
    }
    filename = env_filenames.get(ctx.environment, "envfile")
    env_file = ctx.blitzy_env_path / filename

    if not env_file.is_file():
        console.print(f"    [yellow]Env file not found: {env_file}[/]")
        return

    values = dotenv_values(env_file)
    loaded = 0
    for key, value in values.items():
        if value is not None:
            os.environ[key] = value
            loaded += 1
    console.print(f"    [dim]Loaded {loaded} vars from {env_file}[/]")


def load_env_config_yaml(ctx: BootstrapContext) -> None:
    """Step 5: Load env_config/env-{env}.yaml if it exists."""
    import yaml

    yaml_path = ctx.blitzy_env_path / "env_config" / f"env-{ctx.environment}.yaml"
    if not yaml_path.is_file():
        console.print(f"    [dim]No YAML config at {yaml_path}[/]")
        return

    with open(yaml_path) as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        console.print("    [yellow]YAML config is not a dict, skipping[/]")
        return

    loaded = 0
    for key, value in data.items():
        os.environ[str(key)] = str(value)
        loaded += 1
    console.print(f"    [dim]Loaded {loaded} vars from {yaml_path}[/]")


def set_postgres_port(ctx: BootstrapContext) -> None:
    """Step 6: Set POSTGRES_PORT based on environment."""
    port_map = {"dev": "5443", "qa": "5444", "prod": "5445"}
    port = port_map.get(ctx.environment, "5443")
    os.environ["POSTGRES_PORT"] = port
    console.print(f"    [dim]POSTGRES_PORT={port}[/]")


def set_local_development(ctx: BootstrapContext) -> None:
    """Step 7: Set LOCAL_DEVELOPMENT=true."""
    os.environ["LOCAL_DEVELOPMENT"] = "true"
    console.print("    [dim]LOCAL_DEVELOPMENT=true[/]")


def run_make_targets(ctx: BootstrapContext) -> None:
    """Step 8: Run make install-deployment-utils, make pre-setup, make init."""
    if ctx.skip_make:
        console.print("    [dim]Skipped (--skip-make)[/]")
        return

    makefile = Path.cwd() / "Makefile"
    if not makefile.is_file():
        console.print("    [dim]No Makefile found, skipping make targets[/]")
        return

    targets = ["install-deployment-utils", "pre-setup", "init"]
    for target in targets:
        console.print(f"    [dim]Running make {target}...[/]")
        result = subprocess.run(
            ["make", target],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            console.print(f"    [yellow]make {target} failed (exit {result.returncode})[/]")
            if result.stderr:
                console.print(f"    [dim]{result.stderr.strip()[:500]}[/]")
            # Non-fatal: continue with other targets


def run_tests(ctx: BootstrapContext) -> None:
    """Step 9: Run make test if --test flag was set."""
    if not ctx.run_tests_flag:
        console.print("    [dim]Skipped (no --test flag)[/]")
        return

    makefile = Path.cwd() / "Makefile"
    if not makefile.is_file():
        console.print("    [yellow]No Makefile found, cannot run tests[/]")
        return

    console.print("    [dim]Running make test...[/]")
    result = subprocess.run(["make", "test"])
    if result.returncode != 0:
        raise RuntimeError(f"make test failed with exit code {result.returncode}")

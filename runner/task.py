from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import shutil
from typing import Any, Mapping

BRIEF_FILENAME = "BENCHMARK_TASK.md"
DEFAULT_TIME_LIMIT_MINUTES = 30
DEFAULT_VALIDATOR_TIMEOUT_SECONDS = 600
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
DIGEST_SKIP_PARTS = frozenset({".git", "__pycache__"})


def _object(manifest: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = manifest.get(key, {})
    if not isinstance(value, dict):
        raise ValueError(f"task.json: '{key}' must be an object")
    return value


def _positive_number(manifest: Mapping[str, Any], key: str, default: int) -> int | float:
    value = manifest.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0:
        raise ValueError(f"task.json: '{key}' must be a positive number")
    return value


def _flag(section: Mapping[str, Any], section_name: str, key: str) -> bool:
    value = section.get(key, False)
    if not isinstance(value, bool):
        raise ValueError(f"task.json: '{section_name}.{key}' must be true or false")
    return value


@dataclass(frozen=True)
class TaskSpec:
    root: Path
    task_id: str
    title: str
    time_limit_minutes: int | float
    research_allowed: bool
    runtime_network_allowed: bool
    validator_timeout_seconds: int | float = DEFAULT_VALIDATOR_TIMEOUT_SECONDS

    @property
    def prompt_path(self) -> Path:
        return self.root / "task.md"

    @property
    def starter_path(self) -> Path:
        return self.root / "starter"

    @property
    def validator_path(self) -> Path:
        return self.root / "validator" / "run.py"

    @classmethod
    def load(cls, root: str | Path) -> "TaskSpec":
        path = Path(root).resolve()
        manifest_path = path / "task.json"
        if not manifest_path.is_file():
            raise ValueError(f"Missing task manifest: {manifest_path}")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise ValueError(f"task.json is not valid JSON: {error}") from None
        if not isinstance(manifest, dict):
            raise ValueError("task.json must contain an object")
        required = [path / "task.md", path / "starter", path / "validator" / "run.py"]
        missing = [str(item) for item in required if not item.exists()]
        if missing:
            raise ValueError("Task is missing: " + ", ".join(missing))
        if "task_id" not in manifest:
            raise ValueError("task.json: 'task_id' is required")
        task_id = manifest["task_id"]
        if not isinstance(task_id, str) or not SAFE_NAME.match(task_id):
            raise ValueError(
                "task.json: 'task_id' must be text made of letters, digits, '.', '_' and '-'"
            )
        title = manifest.get("title", task_id)
        if not isinstance(title, str):
            raise ValueError("task.json: 'title' must be text")
        _object(manifest, "runtime")
        network = _object(manifest, "network")
        return cls(
            root=path,
            task_id=task_id,
            title=title,
            time_limit_minutes=_positive_number(
                manifest, "time_limit_minutes", DEFAULT_TIME_LIMIT_MINUTES
            ),
            research_allowed=_flag(network, "network", "research_allowed"),
            runtime_network_allowed=_flag(network, "network", "runtime_network_allowed"),
            validator_timeout_seconds=_positive_number(
                manifest, "validator_timeout_seconds", DEFAULT_VALIDATOR_TIMEOUT_SECONDS
            ),
        )

    def digest(self) -> str:
        """Hash of every file name and every byte of the task folder, build caches aside."""
        digest = hashlib.sha256()
        for path in sorted(self.root.rglob("*")):
            relative = path.relative_to(self.root)
            if DIGEST_SKIP_PARTS & set(relative.parts) or path.suffix == ".pyc":
                continue
            if path.is_file():
                name = relative.as_posix().encode("utf-8")
                content = path.read_bytes()
                digest.update(len(name).to_bytes(8, "big") + name)
                digest.update(len(content).to_bytes(8, "big") + content)
        return digest.hexdigest()

    def prepare_workspace(self, workspace: Path) -> Path:
        workspace.mkdir(parents=True, exist_ok=True)
        for source in self.starter_path.iterdir():
            target = workspace / source.name
            if source.is_dir():
                shutil.copytree(source, target)
            else:
                shutil.copy2(source, target)
        (workspace / BRIEF_FILENAME).write_text(
            self.prompt_path.read_text(encoding="utf-8"), encoding="utf-8"
        )
        return workspace

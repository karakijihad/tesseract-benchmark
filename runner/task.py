from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import shutil


@dataclass(frozen=True)
class TaskSpec:
    root: Path
    task_id: str
    title: str
    time_limit_minutes: int
    research_allowed: bool
    runtime_network_allowed: bool

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
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        required = [path / "task.md", path / "starter", path / "validator" / "run.py"]
        missing = [str(item) for item in required if not item.exists()]
        if missing:
            raise ValueError("Task is missing: " + ", ".join(missing))
        runtime = manifest.get("runtime", {})
        network = manifest.get("network", {})
        return cls(
            root=path,
            task_id=str(manifest["task_id"]),
            title=str(manifest.get("title", manifest["task_id"])),
            time_limit_minutes=int(manifest.get("time_limit_minutes", 30)),
            research_allowed=bool(network.get("research_allowed", False)),
            runtime_network_allowed=bool(network.get("runtime_network_allowed", False)),
        )

    def digest(self) -> str:
        digest = hashlib.sha256()
        for path in sorted(self.root.rglob("*")):
            if path.is_file() and ".git" not in path.parts:
                digest.update(path.relative_to(self.root).as_posix().encode("utf-8"))
                digest.update(path.read_bytes())
        return digest.hexdigest()

    def prepare_workspace(self, workspace: Path) -> Path:
        workspace.mkdir(parents=True, exist_ok=False)
        for source in self.starter_path.iterdir():
            target = workspace / source.name
            if source.is_dir():
                shutil.copytree(source, target)
            else:
                shutil.copy2(source, target)
        (workspace / "BENCHMARK_TASK.md").write_text(
            self.prompt_path.read_text(encoding="utf-8"), encoding="utf-8"
        )
        return workspace

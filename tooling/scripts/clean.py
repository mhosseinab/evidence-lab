"""Remove only generated workspace files; retain environments and operator data."""
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[2]
CACHE_NAMES = {"__pycache__", ".pytest_cache", ".ruff_cache", ".task", "build", "dist"}


def main():
    roots = [ROOT, ROOT / "apps/evidence-lab", ROOT / "apps/dashboard", ROOT / "tooling"]
    targets = {root / name for root in roots for name in CACHE_NAMES}
    for root in (ROOT, ROOT / "apps/dashboard"):
        targets.update(root / "node_modules" / name for name in (".vite", ".vite-temp"))
    for root in (ROOT / "apps", ROOT / "tooling"):
        targets.update(path for path in root.rglob("*")
                       if "node_modules" not in path.parts and path.is_dir() and (path.name in CACHE_NAMES or path.name.endswith(".egg-info")))
    for path in sorted(targets, key=lambda value: len(value.parts), reverse=True):
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)


if __name__ == "__main__":
    main()

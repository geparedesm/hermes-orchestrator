"""Copy platform configuration without the regenerable local worker-image archive."""
from pathlib import Path
import shutil
import sys


def copy_config(source: Path, destination: Path) -> None:
    shutil.copytree(source, destination, dirs_exist_ok=True, symlinks=True,
                    ignore=shutil.ignore_patterns("image-cache"))


if __name__ == "__main__":
    copy_config(Path(sys.argv[1]), Path(sys.argv[2]))

"""Configuration backups retain pins and nested settings, but never copy the image recovery cache."""
import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "backup_config", Path(__file__).resolve().parents[2] / "scripts" / "backup_config.py")
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def test_configuration_backup_excludes_image_cache(tmp_path):
    source, target = tmp_path / "config", tmp_path / "backup"
    (source / "image-cache").mkdir(parents=True)
    (source / "image-cache" / "execution-images.tar").write_bytes(b"regenerable layers")
    (source / "images.lock.yaml").write_text("exact pins")
    (source / "nested").mkdir()
    (source / "nested" / "settings.yaml").write_text("settings")
    _MODULE.copy_config(source, target)
    assert (target / "images.lock.yaml").read_text() == "exact pins"
    assert (target / "nested" / "settings.yaml").read_text() == "settings"
    assert not (target / "image-cache").exists()
    assert (source / "image-cache" / "execution-images.tar").is_file()

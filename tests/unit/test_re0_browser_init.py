"""Re0 浏览器安全初始化：独立凭据、权限、幂等与不回显。"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path


def _main():
    path = Path(__file__).parents[2] / "scripts" / "init_re0_browser.py"
    spec = importlib.util.spec_from_file_location("init_re0_browser", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_first_init_creates_private_env_and_profile_and_second_hides_secret(tmp_path, capsys):
    first = _main()(tmp_path)
    first_output = capsys.readouterr().out
    assert first["created"] is True
    assert first["gui_password"] in first_output
    assert len(first["gui_password"]) >= 32

    env_path = tmp_path / "config" / "re0-browser.env"
    profile = tmp_path / "config" / "re0-browser"
    assert _mode(env_path) == 0o600
    assert _mode(profile) == 0o700
    content = env_path.read_text(encoding="utf-8")
    assert f"RE0_PUID={os.getuid()}" in content
    assert f"RE0_PGID={os.getgid()}" in content
    assert first["gui_user"] in content
    assert first["gui_password"] in content

    second = _main()(tmp_path)
    second_output = capsys.readouterr().out
    assert second["created"] is False
    assert "gui_password" not in second
    assert first["gui_password"] not in second_output
    assert env_path.read_text(encoding="utf-8") == content


def test_existing_env_is_never_overwritten(tmp_path):
    env_path = tmp_path / "config" / "re0-browser.env"
    env_path.parent.mkdir(parents=True)
    env_path.write_text("RE0_GUI_USER=custom\n", encoding="utf-8")

    result = _main()(tmp_path)
    assert result["created"] is False
    assert env_path.read_text(encoding="utf-8") == "RE0_GUI_USER=custom\n"

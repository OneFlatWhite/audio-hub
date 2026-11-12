"""Initialize private Re0 Chromium profile and independent Selkies credentials."""

from __future__ import annotations

import argparse
import os
import secrets
from pathlib import Path


def main(root: str | Path) -> dict:
    root = Path(root)
    config_dir = root / "config"
    env_path = config_dir / "re0-browser.env"
    profile = config_dir / "re0-browser"
    profile.mkdir(parents=True, exist_ok=True)
    profile.chmod(0o700)
    try:
        descriptor = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print(f"已存在，未修改：{env_path}")
        return {"created": False, "env_path": env_path}

    gui_user = f"re0admin-{secrets.token_urlsafe(6)}"
    gui_password = secrets.token_urlsafe(32)
    with os.fdopen(descriptor, "w", encoding="utf-8") as env_file:
        env_file.write(
            f"RE0_PUID={os.getuid()}\n"
            f"RE0_PGID={os.getgid()}\n"
            f"RE0_GUI_USER={gui_user}\n"
            f"RE0_GUI_PASSWORD={gui_password}\n"
        )
    env_path.chmod(0o600)
    print(f"已创建：{env_path}")
    print(f"RE0_GUI_USER={gui_user}")
    print(f"RE0_GUI_PASSWORD={gui_password}")
    print("远程桌面凭据仅在本次显示；浏览器配置目录已设为 0700。")
    return {
        "created": True,
        "env_path": env_path,
        "gui_user": gui_user,
        "gui_password": gui_password,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=Path.cwd(), type=Path)
    args = parser.parse_args()
    main(args.root)

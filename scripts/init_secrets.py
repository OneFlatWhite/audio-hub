#!/usr/bin/env python3
"""交互式初始化脚本：生成 Fernet 密钥 + 加密站点凭据 / qb API Key。

用法：
    python scripts/init_secrets.py

运行后会：
1. 生成密钥文件（若不存在）
2. 交互式输入各站点凭据与 qb API Key
3. 打印加密后的 YAML 片段，由你粘贴进 config/config.yaml

⚠️ NexusPHP 系站点收集的是**完整 RSS 地址**而非裸 passkey：
   passkey 内联在 RSS URL 里（见 adapters/nexusphp.py build_rss_url），
   且 SiteConfig 没有 passkey 字段，填裸 passkey 会被 Pydantic 静默丢弃。

也可以改用 Web 界面「设置 → 站点」填写，两条路径等价：
YAML 是基线，界面改动存数据库覆盖层，后者优先级更高。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 将 src 加入 path 以便导入 audio_hub
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from audio_hub.crypto import encrypt_value, ensure_key_file


def prompt(label: str, required: bool = False) -> str:
    """安全提示输入（不回显）。"""
    import getpass
    suffix = " (必填)" if required else " (留空跳过)"
    try:
        value = getpass.getpass(f"{label}{suffix}: ")
    except EOFError:
        value = ""
    if required and not value.strip():
        print(f"  ⚠️  {label} 不能为空，请重新输入。")
        return prompt(label, required)
    return value.strip()


def main() -> None:
    import os
    key_path_str = os.environ.get(
        "AUDIO_HUB_SECRETS_KEY",
        str(Path(__file__).resolve().parent.parent / "config" / "secrets.key"),
    )
    key_path = Path(key_path_str)

    print("=" * 60)
    print("  audio-hub 凭据加密初始化工具")
    print("=" * 60)
    print()

    # 1. 确保密钥文件存在
    key_path = ensure_key_file(key_path)
    print(f"✅ 密钥文件: {key_path}")
    print("   ⚠️  请妥善备份此文件！丢失后所有加密凭据不可恢复。")
    print()

    # 2. 收集凭据
    print("请输入各站点 passkey（输入时不回显）：")
    print("-" * 40)

    # ⚠️ 这里收集的必须是**完整 RSS URL**而非 passkey。
    #
    # 原实现收集 hhanclub_passkey 并输出 hhanclub_passkey_encrypted，
    # 但 SiteConfig（config.py）**没有 passkey 字段** —— 只有
    # name/enabled/rss_url/search_url/api_key/music_cat_id/... 。
    # Pydantic 默认忽略多余字段，故那些密文填进 YAML 会被静默丢弃，
    # 照做等于白做；而且它打印扁平键名，实际该嵌进 sites 列表对应项。
    #
    # NexusPHP 系的 passkey 内联在 RSS URL 里（nexusphp.py build_rss_url
    # 直接以 rss_url 为基址），mteam 用 Authorization header 故需 api_key。
    print("NexusPHP 系站点：请粘贴**完整 RSS 地址**（含 passkey 参数）")
    print("  形如 https://hhanclub.net/torrentrss.php?rows=50&linktype=dl&passkey=XXXX")
    print("  获取路径：站点控制面板 → RSS → 新建/复制 RSS 链接")
    print("-" * 40)

    nexus_sites = [
        ("hhanclub", "憨憨 (HhanClub)"),
        ("hdsky", "HDSky"),
        ("audiences", "Audiences"),
    ]
    site_creds: dict[str, dict[str, str]] = {}
    for name, label in nexus_sites:
        value = prompt(f"{label} 完整 RSS 地址")
        if value:
            if "passkey=" not in value:
                print(f"  ⚠️  {label} 的地址里没有 passkey= 参数，可能不是有效的 RSS 直链")
            site_creds.setdefault(name, {})["rss_url"] = encrypt_value(value, key_path)
            print(f"  ✅ {label} RSS 地址 → 已加密")
        else:
            print(f"  ⏭️  {label} → 跳过")

    print()
    print("馒头 (M-Team)：认证用存取令牌（Cookie 认证已于 2024 年废弃）")
    print("  获取路径：登录 → 控制台 → 实验室 → 存取令牌生成")
    print("-" * 40)
    value = prompt("M-Team API Key（存取令牌）")
    if value:
        site_creds.setdefault("mteam", {})["api_key"] = encrypt_value(value, key_path)
        print("  ✅ M-Team API Key → 已加密")
    else:
        print("  ⏭️  M-Team → 跳过")

    value = prompt("qBittorrent API Key")
    qb_cred = encrypt_value(value, key_path) if value else ""
    if value:
        print("  ✅ qBittorrent API Key → 已加密")
    else:
        print("  ⏭️  qBittorrent API Key → 跳过")

    # ── 输出正确的嵌套 YAML 片段 ──
    print()
    print("=" * 60)
    print("把下面的片段填入 config/config.yaml（注意缩进层级）")
    print("=" * 60)
    print()
    if qb_cred:
        print("qbittorrent:")
        print(f'  api_key_encrypted: "{qb_cred}"')
        print()
    if site_creds:
        print("sites:                      # 合并进已有的 sites 列表，勿重复建列表")
        for name, creds in site_creds.items():
            print(f"  - name: \"{name}\"")
            print("    enabled: true")
            for field, token in creds.items():
                print(f'    {field}_encrypted: "{token}"')
        print()
    print("说明：")
    print("  - _encrypted 后缀字段会在启动时自动解密到同名明文字段，")
    print("    原字段被移除，明文不落盘（见 config.py _decrypt_sensitive）")
    print("  - 也可改用 Web 界面「设置 → 站点」直接填写，效果等价")
    print(f"  - 环境变量: export AUDIO_HUB_SECRETS_KEY={key_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()

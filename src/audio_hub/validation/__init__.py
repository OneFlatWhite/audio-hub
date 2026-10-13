"""校验子包：完成后文件校验（扩展名白名单 / 空包 / 假无损）。"""

from .checker import ValidationResult, probe_flac, validate_dir

__all__ = ["ValidationResult", "probe_flac", "validate_dir"]

"""刮削联动子包：MusicTag Web webhook 与 Audiobookshelf 扫库触发。"""

from .trigger import trigger_audiobook, trigger_for_kind, trigger_music

__all__ = ["trigger_audiobook", "trigger_for_kind", "trigger_music"]

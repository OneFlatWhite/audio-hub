"""DownloaderRegistry：按 id 查、默认实例、启用排序、懒建缓存、dry_run 禁建、replace。"""

from __future__ import annotations

from audio_hub.config import DownloaderConfig
from audio_hub.downloaders import DownloaderRegistry


def _cfgs():
    return [
        DownloaderConfig(id="a", name="A", priority=1, enabled=True),
        DownloaderConfig(id="b", name="B", priority=5, enabled=True, is_default=True),
        DownloaderConfig(id="c", name="C", priority=9, enabled=False),
    ]


class _Factory:
    def __init__(self, fail_ids=()):
        self.calls: list[str] = []
        self.fail_ids = set(fail_ids)

    def __call__(self, cfg: DownloaderConfig):
        self.calls.append(cfg.id)
        if cfg.id in self.fail_ids:
            raise ConnectionError("down")
        return {"client_for": cfg.id}


class TestLookup:
    def test_get_and_contains(self):
        reg = DownloaderRegistry(_cfgs())
        assert reg.get("a").id == "a"
        assert reg.get("zzz") is None
        assert reg.get(None) is None
        assert reg.get("") is None
        assert "b" in reg and "zzz" not in reg
        assert len(reg) == 3
        assert [h.id for h in reg] == ["a", "b", "c"]

    def test_default_prefers_is_default(self):
        reg = DownloaderRegistry(_cfgs())
        assert reg.default().id == "b"

    def test_default_falls_back_to_top_enabled(self):
        cfgs = [c.model_copy(update={"is_default": False}) for c in _cfgs()]
        reg = DownloaderRegistry(cfgs)
        # c 优先级最高但禁用；b 是已启用里最高
        assert reg.default().id == "b"

    def test_default_none_when_empty(self):
        reg = DownloaderRegistry([])
        assert reg.default() is None
        assert reg.all_enabled() == []
        assert reg.resolve("x") is None

    def test_all_enabled_sorted_by_priority(self):
        reg = DownloaderRegistry(_cfgs())
        assert [h.id for h in reg.all_enabled()] == ["b", "a"]

    def test_resolve_falls_back_to_default(self):
        reg = DownloaderRegistry(_cfgs())
        assert reg.resolve("a").id == "a"
        assert reg.resolve("deleted").id == "b"
        assert reg.resolve(None).id == "b"

    def test_configs_property(self):
        reg = DownloaderRegistry(_cfgs())
        assert [c.id for c in reg.configs] == ["a", "b", "c"]


class TestClient:
    def test_client_lazy_and_cached(self):
        f = _Factory()
        reg = DownloaderRegistry(_cfgs(), client_factory=f)
        assert f.calls == [], "构造时不应连接任何 qb"
        h = reg.get("a")
        c1 = h.client
        c2 = h.client
        assert c1 == {"client_for": "a"} and c1 is c2
        assert f.calls == ["a"], "应只构建一次"

    def test_client_failure_cached_as_none(self):
        f = _Factory(fail_ids={"a"})
        reg = DownloaderRegistry(_cfgs(), client_factory=f)
        h = reg.get("a")
        assert h.client is None
        assert h.client is None
        assert f.calls == ["a"], "失败后不该每次访问都重试"
        h.reset_client()
        assert h.client is None
        assert f.calls == ["a", "a"]

    def test_dry_run_never_builds_client(self):
        f = _Factory()
        reg = DownloaderRegistry(_cfgs(), client_factory=f, dry_run=True)
        assert reg.dry_run is True
        assert reg.default().client is None
        assert f.calls == []

    def test_no_factory_means_none(self):
        reg = DownloaderRegistry(_cfgs())
        assert reg.get("a").client is None


class TestReplace:
    def test_replace_swaps_and_clears_cache(self):
        f = _Factory()
        reg = DownloaderRegistry(_cfgs(), client_factory=f)
        assert reg.get("a").client is not None
        reg.replace([DownloaderConfig(id="z", name="Z", is_default=True)])
        assert "a" not in reg and "z" in reg
        assert reg.default().id == "z"
        assert reg.get("z").client == {"client_for": "z"}
        assert f.calls == ["a", "z"]

    def test_replace_duplicate_ids_last_wins(self):
        reg = DownloaderRegistry([
            DownloaderConfig(id="d", name="first"),
            DownloaderConfig(id="d", name="second"),
        ])
        assert len(reg) == 1
        assert reg.get("d").config.name == "second"

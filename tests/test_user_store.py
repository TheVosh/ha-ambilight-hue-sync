"""Unit tests for UserStore — JSON-backed PSK credential persistence."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

# ---------------------------------------------------------------------------
# Bootstrap: load user_store without a real HA install
# ---------------------------------------------------------------------------

_base = Path(__file__).parent.parent / "custom_components" / "hue_entertainment"

_pkg_stub = types.ModuleType("hue_entertainment")
_pkg_stub.__path__ = [str(_base)]  # type: ignore[attr-defined]
_pkg_stub.__package__ = "hue_entertainment"
sys.modules.setdefault("hue_entertainment", _pkg_stub)


def _load(name: str, filename: str):
    path = _base / filename
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_user_store_mod = _load("hue_entertainment.user_store", "user_store.py")
UserStore = _user_store_mod.UserStore


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestUserStoreInMemory:
    def test_add_and_get_psk(self):
        store = UserStore()
        store.add("user1", "aabbccddeeff0011")
        assert store.get_psk("user1") == "aabbccddeeff0011"

    def test_unknown_returns_none(self):
        store = UserStore()
        assert store.get_psk("nobody") is None

    def test_users_property_returns_copy(self):
        store = UserStore()
        store.add("u1", "key1", "tv#test")
        users = store.users
        assert "u1" in users
        assert users["u1"]["clientkey"] == "key1"
        assert users["u1"]["devicetype"] == "tv#test"

    def test_multiple_users(self):
        store = UserStore()
        store.add("alice", "aaaabbbbccccdddd")
        store.add("bob", "1111222233334444")
        assert store.get_psk("alice") == "aaaabbbbccccdddd"
        assert store.get_psk("bob") == "1111222233334444"

    def test_overwrite_existing_user(self):
        store = UserStore()
        store.add("alice", "oldkey")
        store.add("alice", "newkey")
        assert store.get_psk("alice") == "newkey"

    def test_in_memory_mode_no_file_created(self, tmp_path):
        """UserStore(path=None) must not create any files."""
        store = UserStore(path=None)
        store.add("u", "k")
        assert list(tmp_path.iterdir()) == []


class TestUserStorePersistence:
    def test_persist_to_disk(self, tmp_path):
        """Users added to a file-backed store survive a new store instance."""
        db = tmp_path / "users.json"
        store1 = UserStore(path=db)
        store1.add("tv1", "deadbeefcafebabe", "philips#hue")

        store2 = UserStore(path=db)
        assert store2.get_psk("tv1") == "deadbeefcafebabe"

    def test_multiple_users_persist(self, tmp_path):
        db = tmp_path / "users.json"
        store1 = UserStore(path=db)
        store1.add("u1", "key1")
        store1.add("u2", "key2")

        store2 = UserStore(path=db)
        assert store2.get_psk("u1") == "key1"
        assert store2.get_psk("u2") == "key2"

    def test_overwrite_persists(self, tmp_path):
        db = tmp_path / "users.json"
        store1 = UserStore(path=db)
        store1.add("u", "oldkey")
        store1.add("u", "newkey")

        store2 = UserStore(path=db)
        assert store2.get_psk("u") == "newkey"

    def test_missing_file_starts_empty(self, tmp_path):
        db = tmp_path / "nonexistent.json"
        store = UserStore(path=db)
        assert store.get_psk("anyone") is None
        assert store.users == {}

    def test_parent_dir_created_automatically(self, tmp_path):
        db = tmp_path / "subdir" / "users.json"
        store = UserStore(path=db)
        store.add("u", "k")
        assert db.exists()


# ---------------------------------------------------------------------------
# get_by_devicetype()
# ---------------------------------------------------------------------------


class TestGetByDevicetype:
    def test_returns_matching_user(self):
        store = UserStore()
        store.add("user1", "key1", "tv#test")
        result = store.get_by_devicetype("tv#test")
        assert result == ("user1", "key1")

    def test_returns_none_when_no_match(self):
        store = UserStore()
        assert store.get_by_devicetype("tv#test") is None

    def test_returns_last_added_when_multiple(self):
        store = UserStore()
        store.add("user1", "key1", "tv#test")
        store.add("user2", "key2", "tv#test")
        result = store.get_by_devicetype("tv#test")
        assert result == ("user2", "key2")

    def test_different_devicetypes(self):
        store = UserStore()
        store.add("user1", "key1", "tv#samsung")
        store.add("user2", "key2", "tv#lg")
        assert store.get_by_devicetype("tv#samsung") == ("user1", "key1")
        assert store.get_by_devicetype("tv#lg") == ("user2", "key2")


# ---------------------------------------------------------------------------
# Corrupt / empty file handling
# ---------------------------------------------------------------------------


class TestCorruptFile:
    def test_corrupt_json_starts_empty(self, tmp_path):
        db = tmp_path / "users.json"
        db.write_text("{{bad json")
        store = UserStore(path=db)
        assert store.users == {}

    def test_empty_file_starts_empty(self, tmp_path):
        db = tmp_path / "users.json"
        db.write_text("")
        store = UserStore(path=db)
        assert store.users == {}


# ---------------------------------------------------------------------------
# HA Store integration (async_load / async_save)
# ---------------------------------------------------------------------------


class TestHaStoreIntegration:
    @pytest.mark.asyncio
    async def test_async_load_from_ha_store(self):
        mock_ha_store = AsyncMock()
        mock_ha_store.async_load.return_value = {
            "user1": {"clientkey": "abc", "devicetype": "test"},
        }
        store = UserStore(ha_store=mock_ha_store)
        await store.async_load()
        assert store.get_psk("user1") == "abc"

    @pytest.mark.asyncio
    async def test_async_save_to_ha_store(self):
        mock_ha_store = AsyncMock()
        store = UserStore(ha_store=mock_ha_store)
        store.add("user1", "abc", "test")
        await store.async_save()
        mock_ha_store.async_save.assert_called_once_with(
            {"user1": {"clientkey": "abc", "devicetype": "test"}}
        )

    @pytest.mark.asyncio
    async def test_async_load_none_data(self):
        mock_ha_store = AsyncMock()
        mock_ha_store.async_load.return_value = None
        store = UserStore(ha_store=mock_ha_store)
        await store.async_load()
        assert store.users == {}

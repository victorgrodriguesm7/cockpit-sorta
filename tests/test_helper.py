import base64
import hashlib
import contextlib
import json
import socket
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import helper

REAL_VERIFY_ROOT = helper.verify_root


MOVIE = {
    "id": 27205, "title": "A Origem", "original_title": "Inception", "runtime": 148,
    "poster_path": None, "genres": [{"id": 28, "name": "Ação"}, {"id": 878, "name": "Ficção científica"}],
}
SERIES = {
    "id": 1399, "name": "Série: Teste", "original_name": "Test Show",
    "episode_run_time": [42], "poster_path": None, "genres": [{"id": 18, "name": "Drama"}],
}
SEASON = {"episodes": [
    {"episode_number": 1, "name": "Piloto", "overview": "Primeiro", "air_date": "2020-01-01", "runtime": 41},
    {"episode_number": 2, "name": "Segundo", "overview": "Depois", "air_date": "2020-01-08", "runtime": 43},
]}


class HelperTests(unittest.TestCase):
    def test_tmdb_connection_prefers_ipv4_and_falls_back_to_ipv6(self):
        attempts = []

        class FakeSocket:
            def __init__(self, family):
                self.family = family

            def settimeout(self, _timeout):
                pass

            def connect(self, _target):
                attempts.append(self.family)
                if self.family == socket.AF_INET:
                    raise OSError("IPv4 unavailable")

            def close(self):
                pass

        def addresses(_host, _port, family, _kind):
            target = ("192.0.2.1", 443) if family == socket.AF_INET else ("2001:db8::1", 443, 0, 0)
            return [(family, socket.SOCK_STREAM, 0, "", target)]

        with mock.patch.object(helper.socket, "getaddrinfo", side_effect=addresses), \
             mock.patch.object(helper.socket, "socket", side_effect=lambda family, _kind, _proto: FakeSocket(family)):
            connection = helper.ipv4_first_connection(("api.themoviedb.org", 443), 5)
        self.assertEqual(attempts, [socket.AF_INET, socket.AF_INET6])
        self.assertEqual(connection.family, socket.AF_INET6)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "Midia"
        self.root.mkdir()
        self.root_id = "test-root"
        self.config_patch = mock.patch.object(helper, "CONFIG", self.base / "config.json")
        self.state_patch = mock.patch.object(helper, "STATE", self.base / "state")
        self.verify_patch = mock.patch.object(helper, "verify_root", return_value=self.root)
        self.tmdb_patch = mock.patch.object(helper, "tmdb_json", side_effect=self.tmdb)
        for patcher in (self.config_patch, self.state_patch, self.verify_patch, self.tmdb_patch):
            patcher.start()
            self.addCleanup(patcher.stop)
        helper.save_json(helper.CONFIG, {"tmdb_key": "test-key", "roots": [
            {"id": self.root_id, "label": "Teste", "mountpoint": str(self.base), "folder": "Midia", "uuid": "test-uuid"}
        ]})

    @staticmethod
    def tmdb(endpoint, _key, **_params):
        if endpoint == "genre/movie/list":
            return {"genres": [{"id": 28, "name": "Ação"}, {"id": 878, "name": "Ficção científica"},
                               {"id": 35, "name": "Comédia"}]}
        if endpoint == "genre/tv/list":
            return {"genres": [{"id": 18, "name": "Drama"}]}
        if endpoint == "movie/27205":
            return MOVIE
        if endpoint == "tv/1399":
            return SERIES
        if endpoint == "tv/1399/season/1":
            return SEASON
        raise AssertionError(endpoint)

    def test_new_db_matches_sorta_migrations(self):
        conn = helper.init_db(self.root)
        self.assertEqual(helper.db_version(conn), 4)
        rows = conn.execute("SELECT version,description,success,checksum FROM _sqlx_migrations ORDER BY version").fetchall()
        self.assertEqual([row["version"] for row in rows], [1, 2, 3, 4])
        for row, source in zip(rows, sorted(helper.MIGRATIONS.glob("*.sql"))):
            self.assertEqual(row["checksum"], hashlib.sha384(source.read_bytes()).digest())
            self.assertEqual(row["success"], 1)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(media)")}
        self.assertTrue({"catalogued_at", "is_new"}.issubset(columns))
        self.assertTrue(conn.execute("SELECT 1 FROM sqlite_master WHERE name='episodes'").fetchone())
        conn.close()

    def test_movie_preview_move_sidecar_and_catalog(self):
        incoming = self.root / "Entrada"
        incoming.mkdir()
        (incoming / "filme.mkv").write_bytes(b"video")
        (incoming / "filme.pt-BR.srt").write_text("legenda")
        (incoming / "outro.srt").write_text("nao mover")
        before = helper.scan(self.root)
        self.assertEqual(before["pending"], 1)
        request = {"root_id": self.root_id, "sources": ["Entrada/filme.mkv"], "media_type": "movie", "tmdb_id": 27205}
        preview = helper.plan(self.root, request)
        self.assertEqual(preview["folder_path"], "Movies/Ação/A Origem [tmdb-27205]")
        self.assertEqual(len(preview["moves"]), 2)
        result = helper.organize(self.root_id, {**request, "token": preview["token"]})
        self.assertEqual(result["files"], 1)
        target = self.root / "Movies" / "Ação" / "A Origem [tmdb-27205]"
        self.assertTrue((target / "A Origem [tmdb-27205].mkv").exists())
        self.assertTrue((target / "A Origem [tmdb-27205].pt-BR.srt").exists())
        self.assertTrue((incoming / "outro.srt").exists())
        self.assertEqual(helper.scan(self.root)["pending"], 0)
        with contextlib.closing(sqlite3.connect(self.root / "sorta.db")) as conn:
            row = conn.execute("SELECT media_type,folder_path,is_new,catalogued_at FROM media").fetchone()
            self.assertEqual(row[:3], ("movie", "Movies/Ação/A Origem [tmdb-27205]", 0))
            self.assertTrue(row[3].endswith("Z"))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM media_genres").fetchone()[0], 2)
        manifest = json.loads((self.root / "manifest.json").read_text())
        self.assertEqual(manifest["counts"], {"media_total": 1, "movies": 1, "series": 0})
        self.assertEqual(manifest["schema_version"], 4)

    def test_series_episode_order_and_new_episode(self):
        incoming = self.root / "Recebidos"
        incoming.mkdir()
        (incoming / "b.mkv").write_bytes(b"episode two")
        (incoming / "a.mkv").write_bytes(b"episode one")
        request = {"root_id": self.root_id, "sources": ["Recebidos/a.mkv", "Recebidos/b.mkv"],
                   "media_type": "tv", "tmdb_id": 1399, "season": 1, "start_episode": 1, "rename": True}
        preview = helper.plan(self.root, request)
        self.assertTrue(preview["moves"][0]["to"].endswith("S01E01.Piloto.mkv"))
        self.assertTrue(preview["moves"][1]["to"].endswith("S01E02.Segundo.mkv"))
        helper.organize(self.root_id, {**request, "token": preview["token"]})
        self.assertEqual(helper.scan(self.root)["pending"], 0)
        with contextlib.closing(sqlite3.connect(self.root / "sorta.db")) as conn:
            rows = conn.execute("SELECT season_number,episode_number,title,file_path FROM episodes ORDER BY episode_number").fetchall()
            self.assertEqual([row[2] for row in rows], ["Piloto", "Segundo"])
            self.assertTrue(all(row[3].startswith("Series/Série Teste [tmdb-1399]/Season 1/") for row in rows))
        (incoming / "c.mkv").write_bytes(b"episode three")
        next_request = {**request, "sources": ["Recebidos/c.mkv"], "start_episode": 3}
        next_preview = helper.plan(self.root, next_request)
        self.assertIsNotNone(next_preview["existing_media_id"])
        helper.organize(self.root_id, {**next_request, "token": next_preview["token"]})
        with contextlib.closing(sqlite3.connect(self.root / "sorta.db")) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM media").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0], 3)

    def test_preview_rejects_collision_and_changed_source(self):
        (self.root / "incoming.mkv").write_bytes(b"video")
        request = {"sources": ["incoming.mkv"], "media_type": "movie", "tmdb_id": 27205}
        preview = helper.plan(self.root, request)
        (self.root / "incoming.mkv").write_bytes(b"changed")
        with self.assertRaisesRegex(helper.SortaError, "prévia mudou"):
            helper.organize(self.root_id, {**request, "token": preview["token"]})
        self.assertTrue((self.root / "incoming.mkv").exists())
        self.assertFalse((self.root / "sorta.db").exists())
        (self.root / "Movies" / "Ação" / "A Origem [tmdb-27205]").mkdir(parents=True)
        with self.assertRaisesRegex(helper.SortaError, "pasta de destino já existe"):
            helper.plan(self.root, request)

    def test_existing_older_db_requires_desktop_migration(self):
        conn = helper.init_db(self.root)
        conn.execute("UPDATE settings SET value='3' WHERE key='schema_version'")
        conn.commit()
        conn.close()
        with self.assertRaisesRegex(helper.SortaError, "Sorta desktop atualizado"):
            helper.init_db(self.root)

    def test_multiple_disks_can_have_independent_roots(self):
        disks = [{"mountpoint": str(self.base), "uuid": "test-uuid", "path": "disk1", "size": 500_000_000_000},
                 {"mountpoint": str(self.base / "second"), "uuid": "other-uuid", "path": "disk2", "size": 1_000_000_000_000}]
        with mock.patch.object(helper, "list_disks", return_value=disks):
            other = helper.add_root({"mountpoint": str(self.base / "second"), "folder": "Filmes", "label": "Segundo HD"})
        roots = helper.config()["roots"]
        self.assertEqual(len(roots), 2)
        self.assertEqual(other["uuid"], "other-uuid")
        self.assertNotEqual(other["id"], self.root_id)
        with self.assertRaisesRegex(helper.SortaError, "sem '..'"):
            helper.add_root({"mountpoint": str(self.base), "folder": "../fora", "label": "Inseguro"})
        with mock.patch.object(helper, "list_disks", return_value=disks):
            with self.assertRaisesRegex(helper.SortaError, "sobrepõe"):
                helper.add_root({"mountpoint": str(self.base), "folder": "Midia/Filmes", "label": "Duplicado"})

    def test_invalid_root_id_cannot_escape_state_directory(self):
        with self.assertRaisesRegex(helper.SortaError, "Identificador"):
            helper.journal_path("../../outside")

    def test_mount_guard_rejects_other_uuid(self):
        root = helper.root_by_id(self.root_id)
        with mock.patch.object(helper, "find_mount", return_value={"uuid": "wrong"}):
            with self.assertRaisesRegex(helper.SortaError, "UUID diferente"):
                REAL_VERIFY_ROOT(root)

    def test_recovery_rolls_back_files_without_committed_row(self):
        source = self.root / "incoming.mkv"
        target = self.root / "Movies" / "Drama" / "Movie [tmdb-1]" / "Movie [tmdb-1].mkv"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"video")
        plan = {"tmdb_id": 1, "media_type": "movie", "folder_path": "Movies/Drama/Movie [tmdb-1]",
                "moves": [{"from": str(source), "to": str(target)}], "episodes": []}
        helper.save_json(helper.journal_path(self.root_id), {"plan": plan, "poster_new": None})
        result = helper.recover(self.root_id)
        self.assertEqual(result["status"], "rolled_back")
        self.assertTrue(source.exists())
        self.assertFalse(target.exists())
        self.assertFalse(helper.journal_path(self.root_id).exists())

    def test_recovery_preserves_committed_movie(self):
        (self.root / "film.mkv").write_bytes(b"video")
        request = {"root_id": self.root_id, "sources": ["film.mkv"], "media_type": "movie", "tmdb_id": 27205}
        preview = helper.plan(self.root, request)
        helper.organize(self.root_id, {**request, "token": preview["token"]})
        helper.save_json(helper.journal_path(self.root_id), {"plan": preview, "poster_new": None})
        result = helper.recover(self.root_id)
        self.assertEqual(result["status"], "committed")
        self.assertTrue((self.root / preview["moves"][0]["to"]).exists())

    def test_catalog_reads_local_poster_and_sorts_secondary_genres(self):
        (self.root / "film.mkv").write_bytes(b"video")
        request = {"root_id": self.root_id, "sources": ["film.mkv"], "media_type": "movie", "tmdb_id": 27205}
        preview = helper.plan(self.root, request)
        helper.organize(self.root_id, {**request, "token": preview["token"]})
        (self.root / "poster").mkdir()
        (self.root / "poster" / "27205.jpg").write_bytes(b"fake-jpeg")
        with contextlib.closing(sqlite3.connect(self.root / "sorta.db")) as conn:
            conn.execute("UPDATE media SET poster_path='poster/27205.jpg', poster_url='https://image.tmdb.org/t/p/w500/test.jpg'")
            conn.commit()
        library = helper.catalog_library(self.root)
        self.assertEqual(library["total"], 1)
        media = library["media"][0]
        self.assertEqual([g["id"] for g in media["genres"]], [28, 878])
        self.assertTrue(media["genres"][0]["is_primary"])
        poster = helper.catalog_poster(self.root, media["id"])
        self.assertTrue(poster["local"])
        self.assertEqual(base64.b64decode(poster["src"].split(",", 1)[1]), b"fake-jpeg")

    def test_primary_genre_change_moves_movie_and_updates_catalog(self):
        (self.root / "film.mkv").write_bytes(b"video")
        request = {"root_id": self.root_id, "sources": ["film.mkv"], "media_type": "movie", "tmdb_id": 27205}
        preview = helper.plan(self.root, request)
        result = helper.organize(self.root_id, {**request, "token": preview["token"]})
        edit = {"kind": "media-genres", "media_id": result["media_id"], "genre_ids": [878, 28]}
        edit_preview = helper.preview_edit(self.root, edit)
        self.assertEqual(len(edit_preview["moves"]), 1)
        applied = helper.apply_edit(self.root_id, {**edit, "token": edit_preview["token"]})
        self.assertEqual(applied["moved_folders"], 1)
        item = helper.catalog_library(self.root)["media"][0]
        self.assertTrue(item["folder_path"].startswith("Movies/Ficção científica/"))
        self.assertEqual([g["id"] for g in item["genres"]], [878, 28])
        self.assertEqual(helper.scan(self.root)["pending"], 0)
        self.assertTrue((self.root / item["folder_path"]).is_dir())

    def test_catalog_can_add_and_remove_tmdb_genres(self):
        (self.root / "film.mkv").write_bytes(b"video")
        request = {"root_id": self.root_id, "sources": ["film.mkv"], "media_type": "movie", "tmdb_id": 27205}
        preview = helper.plan(self.root, request)
        result = helper.organize(self.root_id, {**request, "token": preview["token"]})
        add = {"kind": "media-genres", "media_id": result["media_id"], "genre_ids": [28, 35, 878]}
        helper.apply_edit(self.root_id, {**add, "token": helper.preview_edit(self.root, add)["token"]})
        genres = helper.catalog_library(self.root)["media"][0]["genres"]
        self.assertEqual([g["id"] for g in genres], [28, 35, 878])
        self.assertEqual(genres[1]["canonical_name"], "Comédia")
        remove = {"kind": "media-genres", "media_id": result["media_id"], "genre_ids": [35, 878]}
        helper.apply_edit(self.root_id, {**remove, "token": helper.preview_edit(self.root, remove)["token"]})
        item = helper.catalog_library(self.root)["media"][0]
        self.assertEqual([g["id"] for g in item["genres"]], [35, 878])
        self.assertTrue(item["folder_path"].startswith("Movies/Comédia/"))
        self.assertEqual(helper.scan(self.root)["pending"], 0)

    def test_catalog_edit_rejects_existing_destination(self):
        (self.root / "film.mkv").write_bytes(b"video")
        request = {"root_id": self.root_id, "sources": ["film.mkv"], "media_type": "movie", "tmdb_id": 27205}
        preview = helper.plan(self.root, request)
        result = helper.organize(self.root_id, {**request, "token": preview["token"]})
        collision = self.root / "Movies" / "Ficção científica" / "A Origem [tmdb-27205]"
        collision.mkdir(parents=True)
        edit = {"kind": "media-genres", "media_id": result["media_id"], "genre_ids": [878, 28]}
        with self.assertRaisesRegex(helper.SortaError, "Destino já existe"):
            helper.preview_edit(self.root, edit)
        item = helper.catalog_library(self.root)["media"][0]
        self.assertTrue(item["folder_path"].startswith("Movies/Ação/"))

    def test_genre_translation_moves_movie_and_preserves_compatibility(self):
        (self.root / "film.mkv").write_bytes(b"video")
        request = {"root_id": self.root_id, "sources": ["film.mkv"], "media_type": "movie", "tmdb_id": 27205}
        preview = helper.plan(self.root, request)
        helper.organize(self.root_id, {**request, "token": preview["token"]})
        edit = {"kind": "genre-translation", "genre_id": 28, "media_type": "movie", "translated": "Aventura"}
        edit_preview = helper.preview_edit(self.root, edit)
        helper.apply_edit(self.root_id, {**edit, "token": edit_preview["token"]})
        item = helper.catalog_library(self.root)["media"][0]
        self.assertTrue(item["folder_path"].startswith("Movies/Aventura/"))
        self.assertEqual(item["genres"][0]["translated_name"], "Aventura")
        self.assertEqual(helper.scan(self.root)["pending"], 0)
        self.assertEqual(len(helper.catalog_settings(self.root)["genres"]), 2)

    def test_season_translation_updates_episode_paths(self):
        (self.root / "episode.mkv").write_bytes(b"video")
        request = {"root_id": self.root_id, "sources": ["episode.mkv"], "media_type": "tv", "tmdb_id": 1399,
                   "season": 1, "start_episode": 1}
        preview = helper.plan(self.root, request)
        helper.organize(self.root_id, {**request, "token": preview["token"]})
        edit = {"kind": "season-label", "label": "Temporada"}
        edit_preview = helper.preview_edit(self.root, edit)
        self.assertEqual(len(edit_preview["moves"]), 1)
        helper.apply_edit(self.root_id, {**edit, "token": edit_preview["token"]})
        with contextlib.closing(sqlite3.connect(self.root / "sorta.db")) as conn:
            path = conn.execute("SELECT file_path FROM episodes").fetchone()[0]
        self.assertIn("/Temporada 1/", path)
        self.assertTrue((self.root / path).is_file())
        self.assertEqual(helper.catalog_settings(self.root)["labels"]["season_label"], "Temporada")
        self.assertEqual(helper.scan(self.root)["pending"], 0)

    def test_metadata_recovery_rolls_back_uncommitted_folder_move(self):
        conn = helper.init_db(self.root)
        conn.close()
        source = self.root / "Series" / "Show" / "Season 1"
        target = self.root / "Series" / "Show" / "Temporada 1"
        target.mkdir(parents=True)
        (target / "episode.mkv").write_bytes(b"video")
        helper.save_json(helper.journal_path(self.root_id), {"kind": "metadata", "operation_id": "not-committed",
                                                              "moves": [{"from": str(source), "to": str(target)}]})
        self.assertEqual(helper.recover(self.root_id)["status"], "rolled_back")
        self.assertTrue((source / "episode.mkv").exists())
        self.assertFalse(target.exists())

    def test_metadata_recovery_keeps_committed_folder_move(self):
        (self.root / "film.mkv").write_bytes(b"video")
        request = {"root_id": self.root_id, "sources": ["film.mkv"], "media_type": "movie", "tmdb_id": 27205}
        preview = helper.plan(self.root, request)
        result = helper.organize(self.root_id, {**request, "token": preview["token"]})
        edit = {"kind": "media-genres", "media_id": result["media_id"], "genre_ids": [878, 28]}
        edit_preview = helper.preview_edit(self.root, edit)
        helper.apply_edit(self.root_id, {**edit, "token": edit_preview["token"]})
        with contextlib.closing(sqlite3.connect(self.root / "sorta.db")) as conn:
            operation_id = conn.execute("SELECT value FROM settings WHERE key='cockpit_sorta_last_edit'").fetchone()[0]
        helper.save_json(helper.journal_path(self.root_id), {"kind": "metadata", "operation_id": operation_id,
                                                              "moves": edit_preview["moves"]})
        self.assertEqual(helper.recover(self.root_id)["status"], "committed")
        self.assertTrue(Path(edit_preview["moves"][0]["to"]).exists())


if __name__ == "__main__":
    unittest.main()

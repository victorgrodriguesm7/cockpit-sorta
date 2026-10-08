#!/usr/bin/env python3
"""Cockpit Sorta bridge. One JSON request on stdin, one JSON response on stdout."""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # Windows development tests; the deployed helper runs on Linux.
    fcntl = None
    import msvcrt


APP_VERSION = "0.1.0"
SCHEMA_VERSION = 4
VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".mov", ".wmv", ".m4v", ".webm"}
SIDECAR_EXTS = {".srt", ".ass", ".ssa", ".sub", ".vtt", ".nfo"}
SKIP_DIRS = {
    "system volume information", ".trash", ".trashes", ".spotlight-v100",
    ".fseventsd", "lost+found", "found.000", "node_modules", "poster",
}
CONFIG = Path(os.environ.get("COCKPIT_SORTA_CONFIG", "~/.config/cockpit-sorta/config.json")).expanduser()
STATE = CONFIG.parent / "state"
MIGRATIONS = Path(__file__).resolve().parent / "migrations"


class SortaError(Exception):
    pass


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def save_json(path: Path, value: Any, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, mode)
        else:
            os.chmod(temporary, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def config() -> dict[str, Any]:
    if not CONFIG.exists():
        return {"roots": [], "tmdb_key": ""}
    data = json.loads(CONFIG.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("roots"), list):
        raise SortaError("Configuração do Cockpit Sorta inválida.")
    return data


def root_by_id(root_id: str) -> dict[str, str]:
    for root in config()["roots"]:
        if root["id"] == root_id:
            return root
    raise SortaError("Pasta de catálogo não cadastrada.")


def find_mount(mountpoint: str) -> dict[str, str] | None:
    result = subprocess.run(
        ["findmnt", "-J", "-M", mountpoint, "-o", "TARGET,UUID,SOURCE,FSTYPE"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        return None
    filesystems = json.loads(result.stdout).get("filesystems", [])
    return filesystems[0] if filesystems else None


def verify_root(root: dict[str, str]) -> Path:
    mountpoint = Path(root["mountpoint"])
    mounted = find_mount(str(mountpoint))
    if not mounted or str(mounted.get("uuid", "")).casefold() != root["uuid"].casefold():
        raise SortaError(f"Disco indisponível ou UUID diferente: {mountpoint}")
    path = mountpoint / root["folder"]
    if not path.is_dir() or path.is_symlink():
        raise SortaError(f"Pasta de catálogo indisponível: {path}")
    real_mount, real_path = mountpoint.resolve(), path.resolve()
    if real_path != real_mount and not real_path.is_relative_to(real_mount):
        raise SortaError("Pasta de catálogo fora do disco selecionado.")
    # Reject a second filesystem mounted below the selected disk.
    nearest = subprocess.run(
        ["findmnt", "-n", "-o", "TARGET", "-T", str(path)],
        capture_output=True, text=True, check=False,
    )
    if nearest.returncode or Path(nearest.stdout.strip()).resolve() != real_mount:
        raise SortaError("A pasta está em outra montagem.")
    return real_path


def list_disks() -> list[dict[str, Any]]:
    result = subprocess.run(
        ["lsblk", "-J", "-b", "-o", "NAME,PATH,SIZE,FSTYPE,UUID,MOUNTPOINT,LABEL,TYPE"],
        capture_output=True, text=True, check=True,
    )
    disks: list[dict[str, Any]] = []

    def walk(node: dict[str, Any]) -> None:
        mountpoint = node.get("mountpoint")
        if mountpoint and node.get("uuid") and mountpoint not in ("/", "/boot", "/boot/efi"):
            disks.append({key: node.get(key) for key in ("path", "size", "fstype", "uuid", "mountpoint", "label")})
        for child in node.get("children", []):
            walk(child)

    for device in json.loads(result.stdout).get("blockdevices", []):
        walk(device)
    return disks


def add_root(request: dict[str, Any]) -> dict[str, str]:
    mountpoint = str(request.get("mountpoint", ""))
    folder = str(request.get("folder", "")).strip().replace("\\", "/")
    label = str(request.get("label", "")).strip()
    if not label or len(label) > 80:
        raise SortaError("Informe um nome para o catálogo (até 80 caracteres).")
    if not folder or folder.startswith("/") or any(part in (".", "..", "") for part in folder.split("/")):
        raise SortaError("Informe uma pasta relativa ao disco, sem '..'.")
    disk = next((d for d in list_disks() if d["mountpoint"] == mountpoint), None)
    if disk is None:
        raise SortaError("Selecione um disco montado da lista.")
    root = {"id": uuid.uuid4().hex, "label": label, "mountpoint": mountpoint,
            "uuid": str(disk["uuid"]), "folder": folder}
    verify_root(root)
    data = config()
    for registered in data["roots"]:
        if registered["mountpoint"] != mountpoint:
            continue
        old, new = registered["folder"].casefold(), folder.casefold()
        if old == new or old.startswith(new + "/") or new.startswith(old + "/"):
            raise SortaError("Esta pasta coincide ou se sobrepõe a outro catálogo do disco.")
    data["roots"].append(root)
    save_json(CONFIG, data)
    return root


def remove_root(root_id: str) -> None:
    data = config()
    roots = [r for r in data["roots"] if r["id"] != root_id]
    if len(roots) == len(data["roots"]):
        raise SortaError("Pasta não cadastrada.")
    if journal_path(root_id).exists():
        raise SortaError("Resolva a operação pendente antes de remover o catálogo.")
    data["roots"] = roots
    save_json(CONFIG, data)


def db_connect(root: Path, readonly: bool = False) -> sqlite3.Connection | None:
    path = root / "sorta.db"
    if not path.exists():
        return None
    if readonly:
        uri = path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    else:
        conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def db_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()
        return int(row[0]) if row else 0
    except (sqlite3.Error, ValueError):
        return 0


def init_db(root: Path) -> sqlite3.Connection:
    existing = db_connect(root)
    if existing:
        version = db_version(existing)
        if version != SCHEMA_VERSION:
            existing.close()
            raise SortaError(f"Banco com esquema {version}. Abra este disco no Sorta desktop atualizado antes de organizar (esperado: {SCHEMA_VERSION}).")
        return existing
    dest = root / "sorta.db"
    fd, name = tempfile.mkstemp(prefix=".sorta-new-", suffix=".db", dir=root)
    os.close(fd)
    temporary = Path(name)
    try:
        conn = sqlite3.connect(temporary)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("CREATE TABLE _sqlx_migrations (version BIGINT PRIMARY KEY, description TEXT NOT NULL, installed_on TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, success BOOLEAN NOT NULL, checksum BLOB NOT NULL, execution_time BIGINT NOT NULL)")
        for migration in sorted(MIGRATIONS.glob("*.sql")):
            raw = migration.read_bytes()
            conn.executescript(raw.decode("utf-8"))
            version, description = migration.stem.split("_", 1)
            conn.execute("INSERT INTO _sqlx_migrations(version,description,success,checksum,execution_time) VALUES(?,?,1,?,0)",
                         (int(version), description.replace("_", " "), hashlib.sha384(raw).digest()))
            conn.commit()
        conn.execute("INSERT INTO settings(key,value) VALUES('schema_version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(SCHEMA_VERSION),))
        conn.commit()
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise SortaError("Falha ao criar o banco SQLite.")
        conn.close()
        if dest.exists():
            raise SortaError("O banco foi criado por outro programa. Tente novamente.")
        os.replace(temporary, dest)
        opened = db_connect(root)
        assert opened is not None
        return opened
    finally:
        if temporary.exists():
            temporary.unlink()


def db_labels(conn: sqlite3.Connection | None) -> dict[str, str]:
    labels = {"movies_folder_label": "Movies", "series_folder_label": "Series", "season_label": "Season"}
    if conn:
        for row in conn.execute("SELECT key,value FROM settings WHERE key IN ('movies_folder_label','series_folder_label','season_label')"):
            labels[row["key"]] = row["value"]
    return labels


def journal_path(root_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", root_id):
        raise SortaError("Identificador de catálogo inválido.")
    return STATE / f"{root_id}.journal.json"


@contextlib.contextmanager
def root_lock(root_id: str):
    journal_path(root_id)  # Validate before building any state path.
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(STATE / f"{root_id}.lock", "a+b") as stream:
        if fcntl:
            fcntl.flock(stream, fcntl.LOCK_EX)
        else:
            stream.write(b"\0")
            stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            if fcntl:
                fcntl.flock(stream, fcntl.LOCK_UN)
            else:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def skip_video(path: Path) -> bool:
    name = path.name.casefold()
    return path.suffix.casefold() not in VIDEO_EXTS or ".original." in name or ".compressing." in name


def scan(root: Path) -> dict[str, Any]:
    conn = db_connect(root, readonly=True)
    movie_folders: set[str] = set()
    tv_folders: set[str] = set()
    episode_files: set[str] = set()
    has_episodes = False
    if conn:
        for row in conn.execute("SELECT folder_path,media_type FROM media"):
            (movie_folders if row["media_type"] == "movie" else tv_folders).add(row["folder_path"].casefold())
        has_episodes = bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='episodes'").fetchone())
        if has_episodes:
            episode_files = {row[0].casefold() for row in conn.execute("SELECT file_path FROM episodes WHERE file_path IS NOT NULL")}
        conn.close()
    results: list[dict[str, Any]] = []
    total = linked = errors = 0

    def on_error(_error: OSError) -> None:
        nonlocal errors
        errors += 1

    for directory, dirs, files in os.walk(root, topdown=True, followlinks=False, onerror=on_error):
        dirs[:] = [d for d in dirs if not d.startswith("$") and d.casefold() not in SKIP_DIRS and not (Path(directory) / d).is_symlink()]
        video_files = [f for f in files if not skip_video(Path(f))]
        for filename in video_files:
            path = Path(directory) / filename
            if path.is_symlink():
                continue
            try:
                stat = path.stat()
            except OSError:
                errors += 1
                continue
            total += 1
            relative = path.relative_to(root).as_posix()
            parent = path.parent.relative_to(root).as_posix().casefold()
            movie_linked = parent in movie_folders and path.stem.casefold() == path.parent.name.casefold()
            tv_linked = relative.casefold() in episode_files or (not has_episodes and any(parent == p or parent.startswith(p + "/") for p in tv_folders))
            if movie_linked or tv_linked:
                linked += 1
                continue
            if len(results) < 5000:
                guess = "tv" if re.search(r"(?i)S\d{1,3}E\d{1,3}", filename) or len(video_files) > 1 or re.search(r"(?i)^(season|temporada|s\d)\s*\d*", path.parent.name) else "movie"
                results.append({"path": relative, "size": stat.st_size, "kind": guess})
    results.sort(key=lambda row: row["path"].casefold())
    version_conn = db_connect(root, readonly=True)
    version = db_version(version_conn) if version_conn else None
    if version_conn:
        version_conn.close()
    return {"files": results, "total": total, "linked": linked, "pending": total - linked,
            "truncated": total - linked > len(results), "errors": errors, "schema_version": version}


def sanitize(value: str) -> str:
    value = unicodedata.normalize("NFC", value)
    value = "".join(" " if ord(ch) < 32 or ch in '<>:"/\\|?*' else ch for ch in value)
    value = " ".join(value.split()).rstrip(". ")
    return value or "_"


def resolve_file(root: Path, relative: str) -> Path:
    rel = Path(relative)
    if rel.is_absolute() or not rel.parts or any(part in (".", "..") for part in rel.parts):
        raise SortaError("Caminho de origem inválido.")
    path = root / rel
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
        raise SortaError(f"Arquivo indisponível: {relative}")
    if skip_video(path):
        raise SortaError(f"Arquivo não é vídeo: {relative}")
    return path


def tmdb_json(endpoint: str, key: str, **params: str | int) -> dict[str, Any]:
    if not key:
        raise SortaError("Configure a chave TMDB nas configurações.")
    query = urllib.parse.urlencode({"api_key": key, "language": "pt-BR", **params})
    url = f"https://api.themoviedb.org/3/{endpoint}?{query}"
    request = urllib.request.Request(url, headers={"User-Agent": "Cockpit-Sorta/0.1", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise SortaError("Chave TMDB inválida.") from exc
        raise SortaError(f"TMDB respondeu HTTP {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise SortaError(f"Falha ao consultar o TMDB: {exc}") from exc


def tmdb_search(query: str) -> list[dict[str, Any]]:
    if len(query.strip()) < 2:
        raise SortaError("Digite pelo menos dois caracteres.")
    raw = tmdb_json("search/multi", config().get("tmdb_key", ""), query=query.strip(), include_adult="false")
    return [{"id": r["id"], "type": r["media_type"], "title": r.get("title") or r.get("name") or r.get("original_title") or r.get("original_name"),
             "year": (r.get("release_date") or r.get("first_air_date") or "")[:4],
             "poster": poster_url(r.get("poster_path"), "w185")}
            for r in raw.get("results", []) if r.get("media_type") in ("movie", "tv")][:30]


def poster_url(path: str | None, width: str = "w500") -> str | None:
    return f"https://image.tmdb.org/t/p/{width}{path}" if path and path.startswith("/") else None


def sidecar_moves(source: Path, target: Path) -> list[dict[str, str]]:
    moves = []
    old = source.stem
    new = target.stem
    for sibling in source.parent.iterdir():
        if not sibling.is_file() or sibling.is_symlink() or sibling == source or sibling.suffix.casefold() not in SIDECAR_EXTS:
            continue
        stem = sibling.stem
        if stem.casefold() == old.casefold():
            suffix = ""
        elif stem.casefold().startswith(old.casefold() + "."):
            suffix = stem[len(old):]
        else:
            continue
        moves.append({"from": str(sibling), "to": str(target.with_name(new + suffix + sibling.suffix))})
    return moves


def check_conflicts(moves: list[dict[str, str]]) -> None:
    destinations: set[str] = set()
    for move in moves:
        src, dest = Path(move["from"]), Path(move["to"])
        key = unicodedata.normalize("NFC", str(dest)).casefold()
        if key in destinations:
            raise SortaError(f"Dois arquivos teriam o mesmo destino: {dest}")
        destinations.add(key)
        if src == dest:
            raise SortaError("O arquivo já está no destino.")
        if dest.exists():
            raise SortaError(f"Destino já existe: {dest}")
        if dest.parent.exists() and any(p.name.casefold() == dest.name.casefold() for p in dest.parent.iterdir()):
            raise SortaError(f"Nome conflitante no destino: {dest}")


def plan(root: Path, request: dict[str, Any]) -> dict[str, Any]:
    conn = db_connect(root, readonly=True)
    try:
        return _plan(root, request, conn)
    finally:
        if conn:
            conn.close()


def _plan(root: Path, request: dict[str, Any], conn: sqlite3.Connection | None) -> dict[str, Any]:
    kind = request.get("media_type")
    tmdb_id = request.get("tmdb_id")
    sources = request.get("sources")
    if kind not in ("movie", "tv") or not isinstance(tmdb_id, int) or tmdb_id <= 0:
        raise SortaError("Escolha um filme ou série do TMDB.")
    if not isinstance(sources, list) or not sources or len(sources) > 100 or any(not isinstance(s, str) for s in sources):
        raise SortaError("Selecione de 1 a 100 vídeos.")
    if kind == "movie" and len(sources) != 1:
        raise SortaError("Selecione um vídeo para o filme.")
    if len(set(sources)) != len(sources):
        raise SortaError("A seleção contém arquivos repetidos.")
    files = [resolve_file(root, source) for source in sources]
    if conn and db_version(conn) != SCHEMA_VERSION:
        raise SortaError(f"Banco com esquema diferente de {SCHEMA_VERSION}. Abra este disco no Sorta desktop atualizado antes de organizar.")
    labels = db_labels(conn)
    key = config().get("tmdb_key", "")
    details = tmdb_json(f"{kind}/{tmdb_id}", key)
    title = str(details.get("title") or details.get("name") or details.get("original_title") or details.get("original_name") or "").strip()
    if not title:
        raise SortaError("O TMDB não retornou um título.")
    original_title = details.get("original_title") or details.get("original_name")
    genres = [{"id": g["id"], "name": g["name"]} for g in details.get("genres", [])]
    folder_name = f"{sanitize(title)} [tmdb-{tmdb_id}]"
    existing = None
    if conn:
        existing = conn.execute("SELECT id,folder_path FROM media WHERE tmdb_id=? AND media_type=?", (tmdb_id, kind)).fetchone()
    if kind == "movie" and existing:
        raise SortaError("Este filme já está catalogado neste disco.")
    if kind == "tv" and existing:
        folder = root / existing["folder_path"]
        if not folder.is_dir() or not folder.resolve().is_relative_to(root):
            raise SortaError("A pasta da série catalogada está ausente.")
    elif kind == "movie":
        genre = genres[0]["name"] if genres else None
        if genre and conn:
            translated = conn.execute("SELECT translated_name FROM genres WHERE id=? AND media_type='movie'", (genres[0]["id"],)).fetchone()
            if translated and translated[0]:
                genre = translated[0]
        folder = root / sanitize(labels["movies_folder_label"])
        if genre:
            folder /= sanitize(genre)
        folder /= folder_name
    else:
        folder = root / sanitize(labels["series_folder_label"]) / folder_name
    if not existing and folder.exists():
        raise SortaError(f"A pasta de destino já existe: {folder}")
    moves: list[dict[str, str]] = []
    episodes: list[dict[str, Any]] = []
    season = int(request.get("season", 1))
    start = int(request.get("start_episode", 1))
    if kind == "tv" and (season < 0 or season > 999 or start < 0 or start + len(files) > 10000):
        raise SortaError("Temporada ou episódio fora do intervalo.")
    season_data: dict[int, dict[str, Any]] = {}
    if kind == "tv":
        try:
            raw = tmdb_json(f"tv/{tmdb_id}/season/{season}", key)
            season_data = {int(ep["episode_number"]): ep for ep in raw.get("episodes", [])}
        except SortaError:
            pass  # Sorta desktop also falls back to numeric episode names.
        folder = folder / f"{sanitize(labels['season_label'])} {season}"
    for index, source in enumerate(files):
        if kind == "movie":
            target = folder / f"{folder_name}{source.suffix}"
        else:
            number = start + index
            episode = season_data.get(number, {})
            episode_title = str(episode.get("name") or "").strip()
            if request.get("rename", True):
                base = f"S{season:02}E{number:02}"
                if episode_title:
                    base += "." + sanitize(episode_title)
                target = folder / (base + source.suffix)
            else:
                target = folder / sanitize(source.name)
            episodes.append({"season_number": season, "episode_number": number,
                             "title": episode_title or None, "overview": episode.get("overview"),
                             "air_date": episode.get("air_date"), "runtime_minutes": episode.get("runtime"),
                             "still_url": poster_url(episode.get("still_path"), "w300"),
                             "file_path": target.relative_to(root).as_posix()})
            if conn and existing and conn.execute("SELECT 1 FROM episodes WHERE media_id=? AND season_number=? AND episode_number=?", (existing["id"], season, number)).fetchone():
                raise SortaError(f"S{season:02}E{number:02} já está catalogado.")
        moves.append({"from": str(source), "to": str(target)})
        moves.extend(sidecar_moves(source, target))
    check_conflicts(moves)
    stats = [{"path": source.relative_to(root).as_posix(), "size": source.stat().st_size,
              "mtime_ns": source.stat().st_mtime_ns} for source in files]
    runtime = details.get("runtime") if kind == "movie" else next(iter(details.get("episode_run_time") or []), None)
    result = {"media_type": kind, "tmdb_id": tmdb_id, "title": title, "original_title": original_title,
              "runtime_minutes": runtime, "genres": genres, "folder_path": (folder if kind == "movie" else folder.parent).relative_to(root).as_posix(),
              "poster_url": poster_url(details.get("poster_path")), "poster_path_tmdb": details.get("poster_path"),
              "existing_media_id": existing["id"] if existing else None, "is_new": bool(request.get("is_new")),
              "moves": moves, "episodes": episodes, "sources": stats}
    result["token"] = hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return result


def backup_db(root: Path, root_id: str) -> str | None:
    source = root / "sorta.db"
    if not source.exists():
        return None
    target = CONFIG.parent / "backups" / root_id
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    dest = target / f"sorta-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}.db"
    with sqlite3.connect(source) as old, sqlite3.connect(dest) as copy:
        old.backup(copy)
    os.chmod(dest, 0o600)
    return str(dest)


def write_manifest(root: Path, conn: sqlite3.Connection) -> None:
    counts = dict(conn.execute("SELECT media_type,COUNT(*) FROM media GROUP BY media_type").fetchall())
    save_json(root / "manifest.json", {"schema_version": SCHEMA_VERSION, "app_version": APP_VERSION,
              "generated_at": utc_now(), "counts": {"media_total": sum(counts.values()),
              "movies": counts.get("movie", 0), "series": counts.get("tv", 0)}}, mode=0o644)


def download_poster(url: str | None) -> bytes | None:
    if not url:
        return None
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Cockpit-Sorta/0.1"}), timeout=15) as response:
            data = response.read(5_000_001)
        return data if len(data) <= 5_000_000 else None
    except (urllib.error.URLError, TimeoutError):
        return None


def rollback_moves(moves: list[dict[str, str]]) -> None:
    for move in reversed(moves):
        source, target = Path(move["from"]), Path(move["to"])
        if target.exists() and not source.exists():
            source.parent.mkdir(parents=True, exist_ok=True)
            os.rename(target, source)
        elif target.exists() and source.exists():
            raise SortaError(f"Recuperação requer intervenção: origem e destino existem ({source}).")
    for parent in sorted({Path(m["to"]).parent for m in moves}, key=lambda p: len(p.parts), reverse=True):
        with contextlib.suppress(OSError):
            parent.rmdir()


def committed(conn: sqlite3.Connection, journal: dict[str, Any]) -> bool:
    plan_data = journal["plan"]
    row = conn.execute("SELECT id,folder_path FROM media WHERE tmdb_id=? AND media_type=?",
                       (plan_data["tmdb_id"], plan_data["media_type"])).fetchone()
    if not row or row["folder_path"] != plan_data["folder_path"]:
        return False
    if plan_data["media_type"] == "movie":
        return True
    return all(conn.execute("SELECT 1 FROM episodes WHERE media_id=? AND season_number=? AND episode_number=? AND file_path=?",
                            (row["id"], ep["season_number"], ep["episode_number"], ep["file_path"])).fetchone()
               for ep in plan_data["episodes"])


def recover(root_id: str) -> dict[str, str]:
    root_by_id(root_id)
    with root_lock(root_id):
        root = verify_root(root_by_id(root_id))
        path = journal_path(root_id)
        if not path.exists():
            return {"status": "none"}
        journal = json.loads(path.read_text(encoding="utf-8"))
        conn = db_connect(root)
        if conn and committed(conn, journal):
            write_manifest(root, conn)
            status = "committed"
        else:
            rollback_moves(journal["plan"]["moves"])
            poster = journal.get("poster_new")
            if poster:
                with contextlib.suppress(FileNotFoundError):
                    (root / poster).unlink()
            status = "rolled_back"
        if conn:
            conn.close()
        path.unlink()
        return {"status": status}


def organize(root_id: str, request: dict[str, Any]) -> dict[str, Any]:
    with root_lock(root_id):
        root = verify_root(root_by_id(root_id))
        if journal_path(root_id).exists():
            raise SortaError("Há uma operação pendente. Use Recuperar antes de continuar.")
        preview = plan(root, request)
        if preview["token"] != request.get("token"):
            raise SortaError("A prévia mudou. Revise os caminhos antes de confirmar.")
        verify_root(root_by_id(root_id))
        backup = backup_db(root, root_id)
        verify_root(root_by_id(root_id))
        conn = init_db(root)
        poster_bytes = None if preview["existing_media_id"] else download_poster(preview["poster_url"])
        poster_rel = f"poster/{preview['tmdb_id']}.jpg" if poster_bytes else None
        poster_new = poster_rel if poster_rel and not (root / poster_rel).exists() else None
        journal = {"plan": preview, "poster_new": poster_new, "backup": backup, "created_at": utc_now()}
        save_json(journal_path(root_id), journal)
        moved: list[dict[str, str]] = []
        committed_db = False
        try:
            conn.execute("BEGIN IMMEDIATE")
            check_conflicts(preview["moves"])
            for move in preview["moves"]:
                verify_root(root_by_id(root_id))
                source, target = Path(move["from"]), Path(move["to"])
                if not source.is_file() or source.is_symlink():
                    raise SortaError(f"Origem mudou: {source}")
                target.parent.mkdir(parents=True, exist_ok=True)
                if source.stat().st_dev != target.parent.stat().st_dev:
                    raise SortaError("Origem e destino estão em sistemas de arquivos diferentes.")
                os.rename(source, target)
                moved.append(move)
            if poster_new and poster_bytes:
                poster = root / poster_new
                poster.parent.mkdir(parents=True, exist_ok=True)
                poster.write_bytes(poster_bytes)
            media_id = preview["existing_media_id"]
            if not media_id:
                cursor = conn.execute(
                    "INSERT INTO media(tmdb_id,media_type,title,original_title,runtime_minutes,poster_path,poster_url,folder_path,is_new,catalogued_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (preview["tmdb_id"], preview["media_type"], preview["title"], preview["original_title"], preview["runtime_minutes"],
                     poster_rel, preview["poster_url"], preview["folder_path"], int(preview["is_new"]), utc_now()))
                media_id = cursor.lastrowid
                for index, genre in enumerate(preview["genres"]):
                    conn.execute("INSERT INTO genres(id,media_type,canonical_name) VALUES(?,?,?) ON CONFLICT(id,media_type) DO UPDATE SET canonical_name=excluded.canonical_name",
                                 (genre["id"], preview["media_type"], genre["name"]))
                    conn.execute("INSERT INTO media_genres(media_id,genre_id,media_type,is_primary) VALUES(?,?,?,?)",
                                 (media_id, genre["id"], preview["media_type"], int(index == 0)))
            for ep in preview["episodes"]:
                conn.execute("INSERT INTO episodes(media_id,season_number,episode_number,title,overview,air_date,runtime_minutes,still_path,still_url,file_path) VALUES(?,?,?,?,?,?,?,?,?,?)",
                             (media_id, ep["season_number"], ep["episode_number"], ep["title"], ep["overview"], ep["air_date"],
                              ep["runtime_minutes"], None, ep["still_url"], ep["file_path"]))
            conn.commit()
            committed_db = True
            verify_root(root_by_id(root_id))
            write_manifest(root, conn)
            journal_path(root_id).unlink()
            return {"media_id": media_id, "folder_path": preview["folder_path"], "files": len(preview["sources"]), "backup": backup}
        except Exception:
            if not committed_db:
                conn.rollback()
                try:
                    verify_root(root_by_id(root_id))
                    rollback_moves(moved)
                    if poster_new:
                        with contextlib.suppress(FileNotFoundError):
                            (root / poster_new).unlink()
                    journal_path(root_id).unlink()
                except Exception:
                    pass  # Keep the journal for explicit recovery.
            raise
        finally:
            conn.close()


def dispatch(command: str, request: dict[str, Any]) -> Any:
    if command == "status":
        data = config()
        roots = []
        for root in data["roots"]:
            try:
                path = verify_root(root)
                available, error = True, None
                database = (path / "sorta.db").exists()
            except SortaError as exc:
                available, error, database = False, str(exc), False
            roots.append({**root, "path": str(Path(root["mountpoint"]) / root["folder"]),
                          "available": available, "error": error, "database": database,
                          "pending_operation": journal_path(root["id"]).exists()})
        return {"roots": roots, "tmdb_configured": bool(data.get("tmdb_key"))}
    if command == "disks":
        return list_disks()
    if command == "add-root":
        return add_root(request)
    if command == "remove-root":
        remove_root(str(request.get("root_id", "")))
        return {"removed": True}
    if command == "set-key":
        key = str(request.get("key", "")).strip()
        if key and len(key) > 300:
            raise SortaError("Chave TMDB inválida.")
        data = config()
        data["tmdb_key"] = key
        save_json(CONFIG, data)
        return {"configured": bool(key)}
    if command == "search":
        return tmdb_search(str(request.get("query", "")))
    root_id = str(request.get("root_id", ""))
    if command == "recover":
        return recover(root_id)
    root = verify_root(root_by_id(root_id))
    if command == "scan":
        return scan(root)
    if command == "preview":
        if journal_path(root_id).exists():
            raise SortaError("Resolva a operação pendente antes de organizar.")
        return plan(root, request)
    if command == "organize":
        return organize(root_id, request)
    raise SortaError("Comando desconhecido.")


def main() -> int:
    try:
        command = sys.argv[1] if len(sys.argv) == 2 else ""
        request = json.load(sys.stdin)
        if not isinstance(request, dict):
            raise SortaError("Requisição inválida.")
        result = dispatch(command, request)
        json.dump({"ok": True, "result": result}, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return 0
    except (SortaError, ValueError, KeyError, sqlite3.Error, OSError) as exc:
        json.dump({"ok": False, "error": str(exc)}, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Verified dual-mode GeoSite/GeoIP publisher for Remnawave.

``original`` publishes unmodified RoscomVPN release assets. ``custom`` replaces
only the whitelist categories with vahellame data. Remnawave writes are fully
disabled when PUBLISH_TO_REMNA=false (the default for custom mode).
"""

from __future__ import annotations

import argparse
import base64
import binascii
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import signal
import tempfile
import threading
import time
from urllib.parse import urlparse

import requests
import urllib3

from geodata import (
    inspect_geoip,
    inspect_geosite,
    replace_geoip_whitelist,
    replace_geosite_whitelist,
)


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("routing-updater")
STOP = threading.Event()
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
DEFAULT_PROFILE_URL = (
    "https://raw.githubusercontent.com/hydraponique/roscomvpn-routing/"
    "refs/heads/main/HAPP/JSONSUB.DEEPLINK"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean value: {value!r}")


def parse_positive_int(name: str, default: int, minimum: int = 1) -> int:
    value = int(os.getenv(name, str(default)))
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


@dataclass(frozen=True)
class Config:
    mode: str
    publish_to_remna: bool
    output_dir: Path
    source_profile_url: str
    geoip_public_url: str | None
    geosite_public_url: str | None
    deeplink_prefix: str
    routing_header_name: str
    check_interval: int
    keep_releases: int
    min_geosite_rules: int
    min_geoip_rules: int
    geoip_categories: tuple[str, ...]
    max_shrink_fraction: float
    custom_name_suffix: str
    remna_base_url: str | None
    remna_token: str | None
    cookie: str | None

    @classmethod
    def from_env(cls) -> "Config":
        mode = os.getenv("GEODATA_MODE", "original").strip().lower()
        if mode not in {"original", "custom"}:
            raise ValueError("GEODATA_MODE must be original or custom")
        publish = parse_bool(os.getenv("PUBLISH_TO_REMNA"), default=mode == "original")
        base_dir = Path(os.getenv("ROUTING_ASSETS_DIR", "/opt/remnawave/downloads"))
        output_dir = Path(os.getenv("MODE_OUTPUT_DIR", str(base_dir / mode)))
        categories = tuple(
            part.strip().lower()
            for part in os.getenv("GEOIP_WHITELIST_CATEGORIES", "other,vk,yandex").split(",")
            if part.strip()
        )
        shrink = float(os.getenv("MAX_WHITELIST_SHRINK_FRACTION", "0.50"))
        if not 0 <= shrink < 1:
            raise ValueError("MAX_WHITELIST_SHRINK_FRACTION must be in [0, 1)")
        config = cls(
            mode=mode,
            publish_to_remna=publish,
            output_dir=output_dir,
            source_profile_url=os.getenv("GITHUB_RAW_URL", DEFAULT_PROFILE_URL).strip(),
            geoip_public_url=os.getenv("GEOIP_PUBLIC_URL") or None,
            geosite_public_url=os.getenv("GEOSITE_PUBLIC_URL") or None,
            deeplink_prefix=os.getenv("DEEPLINK_PREFIX", "happ://routing/add/"),
            routing_header_name=os.getenv("ROUTING_HEADER_NAME", "routing"),
            check_interval=parse_positive_int(
                "GEODATA_CHECK_INTERVAL", int(os.getenv("CHECK_INTERVAL", "300")), 60
            ),
            keep_releases=parse_positive_int("KEEP_RELEASES", 5, 2),
            min_geosite_rules=parse_positive_int("MIN_WHITELIST_RULES", 100),
            min_geoip_rules=parse_positive_int("MIN_GEOIP_WHITELIST_RULES", 1),
            geoip_categories=categories,
            max_shrink_fraction=shrink,
            custom_name_suffix=os.getenv("CUSTOM_PROFILE_NAME_SUFFIX", "Custom Whitelist"),
            remna_base_url=(os.getenv("REMNA_BASE_URL") or "").rstrip("/") or None
            if publish
            else None,
            remna_token=(os.getenv("REMNA_TOKEN") or None) if publish else None,
            cookie=(os.getenv("COOKIE") or None) if publish else None,
        )
        config.validate()
        return config

    def validate(self) -> None:
        if urlparse(self.source_profile_url).scheme != "https":
            raise ValueError("GITHUB_RAW_URL must use HTTPS")
        if self.mode == "custom" and (
            not self.geoip_public_url or not self.geosite_public_url
        ):
            raise ValueError("Custom mode requires GEOIP_PUBLIC_URL and GEOSITE_PUBLIC_URL")
        for name, value in (
            ("GEOIP_PUBLIC_URL", self.geoip_public_url),
            ("GEOSITE_PUBLIC_URL", self.geosite_public_url),
        ):
            if value and urlparse(value).scheme != "https":
                raise ValueError(f"{name} must use HTTPS")
        if not self.geoip_categories or len(set(self.geoip_categories)) != len(
            self.geoip_categories
        ):
            raise ValueError("GEOIP_WHITELIST_CATEGORIES must be non-empty and unique")
        if {"trash", "category-public-dns"}.intersection(self.geoip_categories):
            raise ValueError("Forbidden GeoIP category selected")
        if self.publish_to_remna and (not self.remna_base_url or not self.remna_token):
            raise ValueError(
                "REMNA_BASE_URL and REMNA_TOKEN are required when PUBLISH_TO_REMNA=true"
            )


def fetch(url: str, limit: int = MAX_DOWNLOAD_BYTES) -> bytes:
    if urlparse(url).scheme != "https":
        raise ValueError("Only HTTPS source downloads are supported")
    headers = {"User-Agent": "remna-routing-updater/2.0"}
    if urlparse(url).hostname == "api.github.com" and os.getenv("GITHUB_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"]
    for attempt in range(3):
        try:
            with requests.get(url, headers=headers, timeout=60, stream=True) as response:
                response.raise_for_status()
                if urlparse(response.url).scheme != "https":
                    raise ValueError("Source redirected to a non-HTTPS URL")
                content = bytearray()
                for chunk in response.iter_content(64 * 1024):
                    if chunk:
                        content.extend(chunk)
                    if len(content) > limit:
                        raise ValueError("Downloaded file exceeds size limit")
            if not content:
                raise ValueError("Downloaded file is empty")
            return bytes(content)
        except (requests.RequestException, TimeoutError, OSError):
            if attempt == 2:
                raise
            time.sleep(2**attempt)
    raise AssertionError("unreachable")


def parse_checksum(checksum_data: bytes, filename: str) -> str:
    try:
        lines = checksum_data.decode("ascii").splitlines()
    except UnicodeDecodeError as exc:
        raise ValueError("Checksum file is not ASCII") from exc
    candidates: list[tuple[str, str | None]] = []
    for line in lines:
        match = re.fullmatch(r"\s*([0-9a-fA-F]{64})(?:\s+[*]?(.+?))?\s*", line)
        if match:
            candidates.append((match.group(1).lower(), match.group(2)))
    named = [digest for digest, name in candidates if name and Path(name).name == filename]
    if len(named) == 1:
        return named[0]
    if len(candidates) == 1 and candidates[0][1] in (None, "", filename):
        return candidates[0][0]
    raise ValueError(f"No unambiguous checksum for {filename}")


def release_asset(repo: str, filename: str) -> tuple[bytes, dict]:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("Invalid GitHub repository name")
    release = json.loads(
        fetch(f"https://api.github.com/repos/{repo}/releases/latest", 2 * 1024 * 1024)
    )
    if release.get("draft") or release.get("prerelease"):
        raise ValueError(f"Latest {repo} release is not stable")
    assets = {asset.get("name"): asset for asset in release.get("assets", [])}
    binary = assets.get(filename)
    if not binary:
        raise ValueError(f"Release {repo}@{release.get('tag_name')} lacks {filename}")
    checksum_asset = next(
        (
            assets.get(name)
            for name in (
                filename + ".sha256",
                filename + ".sha256sum",
                "SHA256SUMS",
                "sha256sums.txt",
            )
            if assets.get(name)
        ),
        None,
    )
    if not checksum_asset:
        raise ValueError(f"Release {repo}@{release.get('tag_name')} lacks a checksum asset")
    expected = parse_checksum(fetch(checksum_asset["browser_download_url"], 64 * 1024), filename)
    data = fetch(binary["browser_download_url"])
    digest = sha256(data)
    if digest != expected:
        raise ValueError(f"SHA256 mismatch for {repo}/{filename}")
    api_digest = binary.get("digest")
    if api_digest and api_digest.lower() != f"sha256:{digest}":
        raise ValueError(f"GitHub asset digest mismatch for {repo}/{filename}")
    return data, {
        "repository": repo,
        "tag": release["tag_name"],
        "published_at": release.get("published_at"),
        "sha256": digest,
        "asset_url": binary["browser_download_url"],
        "checksum_url": checksum_asset["browser_download_url"],
        "github_asset_digest": api_digest,
    }


def add_base64_padding(value: str) -> str:
    return value + "=" * (-len(value) % 4)


def extract_deeplink_payload(payload: str) -> str:
    normalized = payload.strip()
    for prefix in ("happ://routing/add/", "happ://routing/onadd/"):
        if normalized.startswith(prefix):
            return normalized[len(prefix) :]
    return normalized


def decode_deeplink(payload: str) -> dict:
    payload = extract_deeplink_payload(payload)
    if payload.startswith("{"):
        decoded = json.loads(payload)
        if not isinstance(decoded, dict):
            raise ValueError("Routing profile must be a JSON object")
        return decoded
    normalized = add_base64_padding(payload)
    for decoder in (
        lambda value: base64.b64decode(value, validate=True),
        base64.urlsafe_b64decode,
    ):
        try:
            decoded = json.loads(decoder(normalized).decode("utf-8"))
            if not isinstance(decoded, dict):
                raise ValueError("Routing profile must be a JSON object")
            return decoded
        except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
            continue
    raise ValueError("Failed to decode routing profile as JSON/base64 JSON")


def encode_deeplink(payload: dict, prefix: str) -> str:
    compact = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    encoded = base64.b64encode(compact.encode()).decode("ascii")
    return prefix + encoded


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def atomic_text(path: Path, value: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.chmod(mode)
    temporary.replace(path)


def atomic_json(path: Path, value: dict, mode: int = 0o644) -> None:
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n", mode)


def source_profile(config: Config) -> dict:
    return decode_deeplink(fetch(config.source_profile_url, 2 * 1024 * 1024).decode("utf-8"))


def build_databases(config: Config) -> tuple[dict[str, bytes], dict]:
    geosite_base, geosite_base_info = release_asset(
        "hydraponique/roscomvpn-geosite", "geosite.dat"
    )
    geoip_base, geoip_base_info = release_asset(
        "hydraponique/roscomvpn-geoip", "geoip.dat"
    )
    _, base_site_counts = inspect_geosite(geosite_base)
    _, base_ip_counts = inspect_geoip(geoip_base)
    site_counts = {code: count for code, (_, count) in base_site_counts.items()}
    ip_counts = {code: count for code, (_, count) in base_ip_counts.items()}
    sources: dict[str, dict] = {
        "geosite_base": geosite_base_info,
        "geoip_base": geoip_base_info,
    }
    if config.mode == "original":
        geosite, geoip = geosite_base, geoip_base
    else:
        geosite_donor, geosite_donor_info = release_asset(
            "vahellame/russia-whitelist-geosite", "geosite.dat"
        )
        geoip_donor, geoip_donor_info = release_asset(
            "vahellame/russia-whitelist-geoip", "geoip.dat"
        )
        geosite, site_counts = replace_geosite_whitelist(
            geosite_base, geosite_donor, config.min_geosite_rules
        )
        geoip, ip_counts = replace_geoip_whitelist(
            geoip_base, geoip_donor, config.geoip_categories, config.min_geoip_rules
        )
        sources.update(
            geosite_whitelist=geosite_donor_info,
            geoip_whitelist=geoip_donor_info,
        )
        _, merged_site_categories = inspect_geosite(geosite)
        for code in base_site_counts.keys() - {"whitelist"}:
            if base_site_counts[code][0].raw != merged_site_categories[code][0].raw:
                raise ValueError(f"GeoSite category {code} was unexpectedly modified")
        _, merged_ip_categories = inspect_geoip(geoip)
        for code in base_ip_counts.keys() - {"whitelist"}:
            if base_ip_counts[code][0].raw != merged_ip_categories[code][0].raw:
                raise ValueError(f"GeoIP category {code} was unexpectedly modified")
    if config.mode == "original" and (geosite != geosite_base or geoip != geoip_base):
        raise AssertionError("Original mode altered release bytes")
    if not site_counts.get("whitelist") or not ip_counts.get("whitelist"):
        raise ValueError("Published databases must contain non-empty whitelist categories")
    return {"geosite.dat": geosite, "geoip.dat": geoip}, {
        "sources": sources,
        "counts": {"geosite": site_counts, "geoip": ip_counts},
        "geoip_whitelist_source_categories": list(config.geoip_categories)
        if config.mode == "custom"
        else [],
    }


def check_shrink(config: Config, state: dict, metadata: dict) -> None:
    if config.mode != "custom" or state.get("mode") != "custom":
        return
    previous = state.get("counts") or {}
    current = metadata["counts"]
    for family in ("geosite", "geoip"):
        old_count = ((previous.get(family) or {}).get("whitelist"))
        new_count = current[family]["whitelist"]
        if old_count and new_count < old_count * (1 - config.max_shrink_fraction):
            raise ValueError(
                f"{family} whitelist shrank unexpectedly: {old_count} -> {new_count}"
            )


def prepare_profile(
    config: Config, profile: dict, databases: dict[str, bytes], state: dict
) -> tuple[dict, str, str, int]:
    prepared = json.loads(json.dumps(profile))
    if not prepared.get("Geoipurl") or not prepared.get("Geositeurl"):
        raise ValueError("Source profile lacks Geoipurl or Geositeurl")
    if config.geoip_public_url:
        prepared["Geoipurl"] = config.geoip_public_url
    if config.geosite_public_url:
        prepared["Geositeurl"] = config.geosite_public_url
    if config.mode == "custom":
        original_name = str(prepared.get("Name") or "Routing")
        if config.custom_name_suffix.lower() not in original_name.lower():
            prepared["Name"] = f"{original_name} — {config.custom_name_suffix}"
        direct_ip = prepared.get("DirectIp")
        if not isinstance(direct_ip, list):
            raise ValueError("Source profile DirectIp must be an array")
        if not any(str(item).lower() == "geoip:whitelist" for item in direct_ip):
            insert_at = next(
                (
                    index + 1
                    for index, item in enumerate(direct_ip)
                    if str(item).lower() == "geoip:private"
                ),
                len(direct_ip),
            )
            direct_ip.insert(insert_at, "geoip:whitelist")
    prepared.pop("LastUpdated", None)
    fingerprint = sha256(
        json.dumps(
            {
                "profile": prepared,
                "geosite": sha256(databases["geosite.dat"]),
                "geoip": sha256(databases["geoip.dat"]),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )
    if state.get("content_fingerprint") == fingerprint and state.get("last_updated"):
        last_updated = int(state["last_updated"])
    else:
        previous = int(state.get("last_updated") or 0)
        last_updated = max(int(time.time()), previous + 1)
    prepared["LastUpdated"] = str(last_updated)
    deeplink = encode_deeplink(prepared, config.deeplink_prefix)
    return prepared, deeplink, fingerprint, last_updated


def snapshot_id(files: dict[str, bytes]) -> str:
    return sha256(
        json.dumps(
            {name: sha256(data) for name, data in sorted(files.items())},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )


def activate(root: Path, snapshot: Path) -> None:
    temporary = root / "current.next"
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(snapshot.relative_to(root), target_is_directory=True)
    temporary.replace(root / "current")


def publish_snapshot(root: Path, files: dict[str, bytes], manifest: dict, keep: int) -> tuple[str, bool]:
    required = {"geosite.dat", "geoip.dat", "routing.json", "routing.deeplink"}
    if set(files) != required:
        raise ValueError(f"Snapshot must contain exactly {sorted(required)}")
    digest = snapshot_id(files)
    current = root / "current"
    if current.is_dir() and current.resolve().name == digest:
        for name, data in files.items():
            if sha256((current / name).read_bytes()) != sha256(data):
                raise ValueError("Active snapshot is corrupt")
        return digest, False
    releases = root / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    snapshot = releases / digest
    staging = Path(tempfile.mkdtemp(prefix=".build-", dir=root))
    try:
        for name, data in files.items():
            (staging / name).write_bytes(data)
        for name in ("geosite.dat", "geoip.dat"):
            checksum = sha256(files[name])
            (staging / f"{name}.sha256").write_text(checksum + "\n", encoding="ascii")
            (staging / f"{name}.sha256sum").write_text(
                f"{checksum}  {name}\n", encoding="ascii"
            )
        atomic_json(staging / "manifest.json", manifest)
        staging.chmod(0o755)
        for child in staging.iterdir():
            child.chmod(0o644)
        if snapshot.exists():
            for name, data in files.items():
                if sha256((snapshot / name).read_bytes()) != sha256(data):
                    raise ValueError("Existing immutable snapshot is corrupt")
        else:
            staging.rename(snapshot)
        activate(root, snapshot)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    older = sorted(
        (
            path
            for path in releases.iterdir()
            if path.is_dir()
            and path.name != digest
            and re.fullmatch(r"[0-9a-f]{64}", path.name)
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for old in older[keep - 1 :]:
        try:
            shutil.rmtree(old)
        except OSError:
            log.warning("Could not prune old snapshot %s", old.name, exc_info=True)
    return digest, True


class RemnaClient:
    def __init__(self, config: Config):
        if not config.remna_base_url or not config.remna_token:
            raise ValueError("Remnawave client requires URL and token")
        self.api_url = config.remna_base_url + "/subscription-settings"
        self.verify = config.remna_base_url.startswith("https://")
        self.routing_header_name = config.routing_header_name
        self.headers = {
            "Accept": "application/json",
            "Authorization": "Bearer " + config.remna_token,
        }
        if config.cookie:
            self.headers["Cookie"] = config.cookie
        if not self.verify:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            self.headers["X-Forwarded-Proto"] = "https"
            self.headers["X-Forwarded-For"] = "127.0.0.1"

    def get(self) -> dict:
        response = requests.get(
            self.api_url, headers=self.headers, timeout=30, verify=self.verify
        )
        response.raise_for_status()
        result = response.json()
        value = result.get("response", result)
        if not isinstance(value, dict):
            raise ValueError("Unexpected Remnawave settings response")
        return value

    def current_routing(self, settings: dict) -> str:
        headers = settings.get("customResponseHeaders")
        if isinstance(headers, dict):
            return str(headers.get(self.routing_header_name) or "")
        return str(settings.get("happRouting") or "")

    def patch(self, settings: dict, routing: str) -> None:
        payload: dict = {"uuid": settings["uuid"]}
        headers = settings.get("customResponseHeaders")
        if isinstance(headers, dict):
            updated_headers = headers.copy()
            updated_headers[self.routing_header_name] = routing
            payload["customResponseHeaders"] = updated_headers
        else:
            payload["happRouting"] = routing
        response = requests.patch(
            self.api_url,
            headers={**self.headers, "Content-Type": "application/json"},
            json=payload,
            timeout=30,
            verify=self.verify,
        )
        response.raise_for_status()
        confirmed = self.get()
        if self.current_routing(confirmed) != routing:
            raise ValueError("Remnawave did not return the newly written routing value")


def sync_remnawave(config: Config, state: dict, fingerprint: str, deeplink: str) -> bool:
    if not config.publish_to_remna:
        return False
    client = RemnaClient(config)
    settings = client.get()
    if client.current_routing(settings) == deeplink:
        state["remna_synced_fingerprint"] = fingerprint
        state["remna_synced_at"] = utc_now()
        return False
    client.patch(settings, deeplink)
    state["remna_synced_fingerprint"] = fingerprint
    state["remna_synced_at"] = utc_now()
    return True


def update_once(config: Config) -> dict:
    root = config.output_dir
    root.mkdir(parents=True, exist_ok=True)
    state_path = root / "state.json"
    state = read_json(state_path)
    databases, metadata = build_databases(config)
    check_shrink(config, state, metadata)
    profile = source_profile(config)
    prepared, deeplink, fingerprint, last_updated = prepare_profile(
        config, profile, databases, state
    )
    files = {
        **databases,
        "routing.json": (json.dumps(prepared, ensure_ascii=False, indent=2) + "\n").encode(),
        "routing.deeplink": (deeplink + "\n").encode(),
    }
    manifest = {
        "schema": 1,
        "mode": config.mode,
        "built_at": utc_now(),
        "snapshot_id": snapshot_id(files),
        "content_fingerprint": fingerprint,
        "last_updated": last_updated,
        "files": {
            name: {"sha256": sha256(data), "bytes": len(data)}
            for name, data in files.items()
        },
        **metadata,
    }
    digest, changed = publish_snapshot(root, files, manifest, config.keep_releases)
    state.update(
        schema=1,
        mode=config.mode,
        content_fingerprint=fingerprint,
        last_updated=last_updated,
        snapshot_id=digest,
        counts=metadata["counts"],
        published_at=utc_now(),
    )
    atomic_json(state_path, state, 0o600)
    patched = sync_remnawave(config, state, fingerprint, deeplink)
    atomic_json(state_path, state, 0o600)
    status = {
        "schema": 1,
        "mode": config.mode,
        "publish_to_remna": config.publish_to_remna,
        "last_check": utc_now(),
        "last_success": utc_now(),
        "error": None,
        "changed": changed,
        "remna_patched": patched,
        "snapshot_id": digest,
        "counts": metadata["counts"],
        "sources": metadata["sources"],
    }
    atomic_json(root / "status.json", status)
    log.info(
        "%s %s snapshot %s; GeoSite whitelist=%d, GeoIP whitelist=%d, Remnawave PATCH=%s",
        "Published" if changed else "Verified",
        config.mode,
        digest[:12],
        metadata["counts"]["geosite"]["whitelist"],
        metadata["counts"]["geoip"]["whitelist"],
        patched,
    )
    return status


def record_error(config: Config, exc: Exception) -> None:
    status_path = config.output_dir / "status.json"
    status = read_json(status_path)
    status.update(
        schema=1,
        mode=config.mode,
        publish_to_remna=config.publish_to_remna,
        last_check=utc_now(),
        error=f"{type(exc).__name__}: {exc}",
    )
    atomic_json(status_path, status)


def check_health(config: Config) -> None:
    status = read_json(config.output_dir / "status.json")
    if status.get("error") or not status.get("last_success"):
        raise ValueError("No successful update status")
    age = time.time() - datetime.fromisoformat(status["last_success"]).timestamp()
    if age > max(config.check_interval * 3, 3600):
        raise ValueError("Last successful check is stale")
    current = config.output_dir / "current"
    for name in ("geosite.dat", "geoip.dat", "routing.json", "routing.deeplink"):
        if not (current / name).is_file():
            raise ValueError(f"Published {name} is unavailable")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="run one update cycle")
    parser.add_argument("--healthcheck", action="store_true")
    args = parser.parse_args()
    config = Config.from_env()
    if args.healthcheck:
        check_health(config)
        return 0
    config.output_dir.mkdir(parents=True, exist_ok=True)
    log.info(
        "Starting mode=%s publish_to_remna=%s output=%s interval=%ds",
        config.mode,
        config.publish_to_remna,
        config.output_dir,
        config.check_interval,
    )
    if not config.publish_to_remna:
        log.info("Remnawave API access is disabled for this instance")
    with (config.output_dir / ".updater.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.error("Another updater owns %s", config.output_dir)
            return 1
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda *_: STOP.set())
        while not STOP.is_set():
            try:
                update_once(config)
            except Exception as exc:
                log.exception("Update failed; active snapshot was retained")
                record_error(config, exc)
                if args.once:
                    return 1
            else:
                if args.once:
                    return 0
            STOP.wait(config.check_interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

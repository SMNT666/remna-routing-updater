import base64
import binascii
import json
import os
import time
import logging
from pathlib import Path

import requests
import urllib3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

REMNA_BASE_URL = os.environ["REMNA_BASE_URL"].rstrip("/")
REMNA_API_URL = f"{REMNA_BASE_URL}/subscription-settings"
REMNA_TOKEN = os.environ["REMNA_TOKEN"]
COOKIE = os.environ.get("COOKIE")
GITHUB_RAW_URL = os.environ.get(
    "GITHUB_RAW_URL",
    "https://raw.githubusercontent.com/hydraponique/roscomvpn-happ-routing/refs/heads/main/HAPP/DEFAULT.DEEPLINK",
)
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "300"))  # seconds
ROUTING_ASSETS_DIR = Path(
    os.environ.get("ROUTING_ASSETS_DIR", "/opt/remnawave/downloads")
)
GEOIP_FILE_PATH = ROUTING_ASSETS_DIR / "geoip.dat"
GEOSITE_FILE_PATH = ROUTING_ASSETS_DIR / "geosite.dat"
GEOIP_PUBLIC_URL = os.environ.get("GEOIP_PUBLIC_URL")
GEOSITE_PUBLIC_URL = os.environ.get("GEOSITE_PUBLIC_URL")
DEEPLINK_PREFIX = os.environ.get("DEEPLINK_PREFIX", "happ://routing/add/")
SSL_VERIFY = REMNA_BASE_URL.startswith("https://")

REMNA_HEADERS = {
    "Accept": "application/json",
    "Authorization": f"Bearer {REMNA_TOKEN}",
}

if COOKIE:
    REMNA_HEADERS["Cookie"] = COOKIE

if not SSL_VERIFY:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    REMNA_HEADERS["X-Forwarded-Proto"] = "https"
    REMNA_HEADERS["X-Forwarded-For"] = "127.0.0.1"

def get_remna_settings() -> dict:
    resp = requests.get(
        REMNA_API_URL,
        headers=REMNA_HEADERS,
        timeout=30,
        verify=SSL_VERIFY,
    )
    resp.raise_for_status()
    return resp.json()


def patch_remna_settings(payload: dict) -> dict:
    resp = requests.patch(
        REMNA_API_URL,
        headers={**REMNA_HEADERS, "Content-Type": "application/json"},
        json=payload,
        timeout=30,
        verify=SSL_VERIFY,
    )
    resp.raise_for_status()
    return resp.json()


def get_github_deeplink() -> str:
    resp = requests.get(GITHUB_RAW_URL, timeout=30)
    resp.raise_for_status()
    return resp.text.strip()


def add_base64_padding(value: str) -> str:
    return value + ("=" * (-len(value) % 4))


def extract_deeplink_payload(payload: str) -> str:
    normalized_payload = payload.strip()
    prefixes = (
        "happ://routing/add/",
        "happ://routing/onadd/",
    )

    for prefix in prefixes:
        if normalized_payload.startswith(prefix):
            return normalized_payload[len(prefix):]

    return normalized_payload


def decode_deeplink(payload: str) -> dict:
    payload = extract_deeplink_payload(payload)

    if payload.startswith("{"):
        return json.loads(payload)

    normalized_payload = add_base64_padding(payload)
    decoders = (
        lambda value: base64.b64decode(value, validate=True),
        base64.urlsafe_b64decode,
    )

    for decoder in decoders:
        try:
            decoded_payload = decoder(normalized_payload).decode("utf-8")
            return json.loads(decoded_payload)
        except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
            continue

    raise ValueError("Failed to decode deeplink payload as JSON/base64 JSON")


def encode_deeplink(payload: dict) -> str:
    compact_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    encoded_payload = base64.b64encode(compact_json.encode("utf-8")).decode("ascii")
    return f"{DEEPLINK_PREFIX}{encoded_payload}"


def download_file(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_destination = destination.with_name(f"{destination.name}.tmp")

    try:
        with requests.get(url, timeout=60, stream=True) as resp:
            resp.raise_for_status()
            with temp_destination.open("wb") as file_handle:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        file_handle.write(chunk)
        temp_destination.replace(destination)
    finally:
        if temp_destination.exists():
            temp_destination.unlink()

    log.info("Synced %s -> %s", url, destination)


def transform_deeplink(source_deeplink: str) -> str:
    payload = decode_deeplink(source_deeplink)

    geoip_source_url = payload.get("Geoipurl")
    geosite_source_url = payload.get("Geositeurl")

    if not geoip_source_url:
        raise ValueError("Geoipurl is missing in source deeplink")
    if not geosite_source_url:
        raise ValueError("Geositeurl is missing in source deeplink")

    download_file(geoip_source_url, GEOIP_FILE_PATH)
    download_file(geosite_source_url, GEOSITE_FILE_PATH)

    if GEOIP_PUBLIC_URL:
        payload["Geoipurl"] = GEOIP_PUBLIC_URL
    if GEOSITE_PUBLIC_URL:
        payload["Geositeurl"] = GEOSITE_PUBLIC_URL

    return encode_deeplink(payload)


def get_last_updated_value(payload: str) -> str | None:
    decoded_payload = decode_deeplink(payload)
    last_updated = decoded_payload.get("LastUpdated")

    if last_updated in (None, ""):
        return None

    return str(last_updated)


def should_update_routing(
    source_deeplink: str,
    prepared_deeplink: str,
    current_routing: str,
) -> bool:
    source_last_updated = get_last_updated_value(source_deeplink)
    if source_last_updated is None:
        log.warning("Source deeplink does not contain LastUpdated; skipping update")
        return False

    if not current_routing:
        log.info("Current happRouting is empty; update required")
        return True

    try:
        current_last_updated = get_last_updated_value(current_routing)
    except Exception:
        log.warning("Current happRouting could not be decoded; update required")
        return True

    if current_last_updated is None:
        log.warning("Current happRouting does not contain LastUpdated; update required")
        return True

    if source_last_updated != current_last_updated:
        log.info(
            "LastUpdated changed: current=%s, new=%s",
            current_last_updated,
            source_last_updated,
        )
        return True

    if prepared_deeplink != current_routing:
        log.info(
            "Skipping update because LastUpdated is unchanged (%s)",
            source_last_updated,
        )

    return False


def main():
    log.info("Starting routing update monitor")
    log.info("Remna API: %s", REMNA_API_URL)
    log.info("GitHub URL: %s", GITHUB_RAW_URL)
    log.info("Check interval: %ds", CHECK_INTERVAL)
    log.info("Geo assets dir: %s", ROUTING_ASSETS_DIR)
    log.info("GeoIP file: %s", GEOIP_FILE_PATH)
    log.info("Geosite file: %s", GEOSITE_FILE_PATH)
    log.info("Deeplink prefix: %s", DEEPLINK_PREFIX)
    if GEOIP_PUBLIC_URL:
        log.info("GeoIP URL override: %s", GEOIP_PUBLIC_URL)
    if GEOSITE_PUBLIC_URL:
        log.info("Geosite URL override: %s", GEOSITE_PUBLIC_URL)

    # Fetch current settings on startup
    settings = get_remna_settings()
    data = settings.get("response", settings)
    settings_uuid = data["uuid"]
    current_routing = data.get("happRouting", "") or ""
    log.info("Settings UUID: %s", settings_uuid)
    log.info("Current happRouting loaded (%d chars)", len(current_routing))

    while True:
        try:
            github_deeplink = get_github_deeplink()
            log.info("Fetched GitHub deeplink (%d chars)", len(github_deeplink))
            remna_deeplink = transform_deeplink(github_deeplink)
            log.info("Prepared Remna deeplink (%d chars)", len(remna_deeplink))

            if should_update_routing(github_deeplink, remna_deeplink, current_routing):
                log.info("Routing changed! Updating Remna...")
                result = patch_remna_settings({
                    "uuid": settings_uuid,
                    "happRouting": remna_deeplink,
                })
                current_routing = remna_deeplink
                log.info("Successfully updated happRouting in Remna")
                log.debug("Patch response: %s", result)
            else:
                log.info("No changes detected")

        except Exception:
            log.exception("Error during check cycle")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()

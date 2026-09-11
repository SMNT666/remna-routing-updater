import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import app
from test_geodata import category, network, rule


def config(root: Path, mode="custom", publish=False):
    return app.Config(
        mode=mode,
        publish_to_remna=publish,
        output_dir=root / mode,
        source_profile_url="https://example.test/profile",
        geoip_public_url=f"https://example.test/files/{mode}/geoip.dat",
        geosite_public_url=f"https://example.test/files/{mode}/geosite.dat",
        deeplink_prefix="happ://routing/add/",
        routing_header_name="routing",
        check_interval=300,
        keep_releases=3,
        min_geosite_rules=1,
        min_geoip_rules=1,
        geoip_categories=("other", "vk", "yandex"),
        max_shrink_fraction=0.5,
        custom_name_suffix="Custom Whitelist",
        remna_base_url="http://remnawave:3000/api" if publish else None,
        remna_token="test-token" if publish else None,
        cookie=None,
    )


SITE_BASE = category("PRIVATE", rule("localhost")) + category(
    "WHITELIST", rule("old.example")
)
SITE_DONOR = category("WHITELIST", rule("new.example"), rule("full.example", 3))
IP_BASE = category("DIRECT", network("192.0.2.0/24")) + category(
    "PRIVATE", network("10.0.0.0/8")
) + category("WHITELIST", network("198.51.100.0/24"))
IP_DONOR = category("other", network("203.0.113.0/24")) + category(
    "vk", network("203.0.113.0/24")
) + category("yandex", network("2001:db8::/32"))
PROFILE = {
    "Name": "RoscomVPN",
    "Geoipurl": "https://upstream/ip",
    "Geositeurl": "https://upstream/site",
    "DirectIp": ["geoip:private", "geoip:direct"],
    "DirectSites": ["geosite:whitelist"],
    "LastUpdated": "123",
}


class ConfigTests(unittest.TestCase):
    def test_custom_defaults_to_no_panel_and_needs_no_credentials(self):
        environment = {
            "GEODATA_MODE": "custom",
            "GITHUB_RAW_URL": "https://example.test/profile",
            "GEOIP_PUBLIC_URL": "https://example.test/custom/geoip.dat",
            "GEOSITE_PUBLIC_URL": "https://example.test/custom/geosite.dat",
            "ROUTING_ASSETS_DIR": "/tmp/assets",
        }
        with patch.dict(os.environ, environment, clear=True):
            actual = app.Config.from_env()
        self.assertFalse(actual.publish_to_remna)
        self.assertEqual(actual.output_dir, Path("/tmp/assets/custom"))
        self.assertIsNone(actual.remna_token)

    def test_forbidden_categories_and_publish_without_token_rejected(self):
        base = {
            "GEODATA_MODE": "custom",
            "GITHUB_RAW_URL": "https://example.test/profile",
            "GEOIP_PUBLIC_URL": "https://example.test/ip",
            "GEOSITE_PUBLIC_URL": "https://example.test/site",
        }
        with patch.dict(os.environ, {**base, "GEOIP_WHITELIST_CATEGORIES": "other,trash"}, clear=True):
            with self.assertRaises(ValueError):
                app.Config.from_env()
        with patch.dict(os.environ, {**base, "PUBLISH_TO_REMNA": "true"}, clear=True):
            with self.assertRaises(ValueError):
                app.Config.from_env()


class DownloadTests(unittest.TestCase):
    def test_checksum_formats_and_github_asset_digest(self):
        data = b"binary fixture"
        digest = hashlib.sha256(data).hexdigest()
        release = json.dumps(
            {
                "tag_name": "20260911",
                "published_at": "2026-09-11T00:00:00Z",
                "draft": False,
                "prerelease": False,
                "assets": [
                    {
                        "name": "geosite.dat",
                        "browser_download_url": "https://example.test/data",
                        "digest": "sha256:" + digest,
                    },
                    {
                        "name": "geosite.dat.sha256sum",
                        "browser_download_url": "https://example.test/hash",
                    },
                ],
            }
        ).encode()
        with patch.object(
            app, "fetch", side_effect=[release, f"{digest}  release/geosite.dat\n".encode(), data]
        ):
            actual, metadata = app.release_asset("owner/repo", "geosite.dat")
        self.assertEqual(actual, data)
        self.assertEqual(metadata["tag"], "20260911")
        with self.assertRaises(ValueError):
            app.parse_checksum(
                (("0" * 64) + "  one.dat\n" + ("1" * 64) + "  two.dat\n").encode(),
                "different.dat",
            )

    def test_wrong_checksum_is_rejected(self):
        data = b"bad"
        release = json.dumps(
            {
                "tag_name": "1",
                "draft": False,
                "prerelease": False,
                "assets": [
                    {"name": "geoip.dat", "browser_download_url": "https://example.test/data"},
                    {
                        "name": "geoip.dat.sha256",
                        "browser_download_url": "https://example.test/hash",
                    },
                ],
            }
        ).encode()
        with patch.object(app, "fetch", side_effect=[release, b"0" * 64, data]):
            with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
                app.release_asset("owner/repo", "geoip.dat")


class ProfileTests(unittest.TestCase):
    def test_custom_name_urls_rule_and_stable_last_updated(self):
        with tempfile.TemporaryDirectory() as temporary:
            cfg = config(Path(temporary))
            databases = {"geosite.dat": b"site", "geoip.dat": b"ip"}
            first, link, fingerprint, stamp = app.prepare_profile(cfg, PROFILE, databases, {})
            self.assertIn("Custom Whitelist", first["Name"])
            self.assertEqual(first["DirectIp"], ["geoip:private", "geoip:whitelist", "geoip:direct"])
            self.assertEqual(first["Geoipurl"], cfg.geoip_public_url)
            state = {"content_fingerprint": fingerprint, "last_updated": stamp}
            second, second_link, _, second_stamp = app.prepare_profile(
                cfg, {**PROFILE, "LastUpdated": "999999"}, databases, state
            )
            self.assertEqual(stamp, second_stamp)
            self.assertEqual(link, second_link)
            changed = {**databases, "geoip.dat": b"new ip only"}
            _, _, changed_fingerprint, changed_stamp = app.prepare_profile(
                cfg, PROFILE, changed, state
            )
            self.assertNotEqual(fingerprint, changed_fingerprint)
            self.assertGreaterEqual(changed_stamp, stamp)


class PublicationTests(unittest.TestCase):
    def files(self, suffix=b""):
        return {
            "geosite.dat": b"site" + suffix,
            "geoip.dat": b"ip" + suffix,
            "routing.json": b"{}\n" + suffix,
            "routing.deeplink": b"happ://x\n" + suffix,
        }

    def test_pair_is_atomic_noop_and_immutable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_files = self.files()
            first_id, changed = app.publish_snapshot(root, first_files, {}, 3)
            self.assertTrue(changed)
            self.assertEqual((root / "current").resolve().name, first_id)
            second_id, changed = app.publish_snapshot(root, first_files, {"ignored": True}, 3)
            self.assertEqual(first_id, second_id)
            self.assertFalse(changed)
            with patch.object(app, "activate", side_effect=OSError("switch failed")):
                with self.assertRaises(OSError):
                    app.publish_snapshot(root, self.files(b"2"), {}, 3)
            self.assertEqual((root / "current").resolve().name, first_id)
            self.assertEqual((root / "current" / "geoip.dat").read_bytes(), b"ip")

    def test_requires_complete_pair_and_profiles(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ValueError):
                app.publish_snapshot(Path(temporary), {"geosite.dat": b"only"}, {}, 3)

    def test_mode_roots_are_independent(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            original = config(base, "original")
            custom = config(base, "custom")
            app.publish_snapshot(original.output_dir, self.files(b"o"), {}, 3)
            app.publish_snapshot(custom.output_dir, self.files(b"c"), {}, 3)
            self.assertEqual((original.output_dir / "current/geoip.dat").read_bytes(), b"ipo")
            self.assertEqual((custom.output_dir / "current/geoip.dat").read_bytes(), b"ipc")


class RemnaTests(unittest.TestCase):
    def test_patch_preserves_unrelated_headers_and_confirms_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            cfg = config(Path(temporary), "original", publish=True)
            client = app.RemnaClient(cfg)
            settings = {
                "uuid": "settings-uuid",
                "customResponseHeaders": {"announce": "keep", "x-policy": "2"},
            }
            response = Mock()
            response.raise_for_status.return_value = None
            with patch.object(app.requests, "patch", return_value=response) as request_patch, patch.object(
                client,
                "get",
                return_value={
                    **settings,
                    "customResponseHeaders": {
                        **settings["customResponseHeaders"],
                        "routing": "new-routing",
                    },
                },
            ):
                client.patch(settings, "new-routing")
            payload = request_patch.call_args.kwargs["json"]
            self.assertEqual(payload["uuid"], "settings-uuid")
            self.assertEqual(
                payload["customResponseHeaders"],
                {"announce": "keep", "x-policy": "2", "routing": "new-routing"},
            )

    def test_matching_panel_value_does_not_patch(self):
        with tempfile.TemporaryDirectory() as temporary:
            cfg = config(Path(temporary), "original", publish=True)
            with patch.object(
                app.RemnaClient,
                "get",
                return_value={"uuid": "x", "customResponseHeaders": {"routing": "same"}},
            ), patch.object(app.RemnaClient, "patch") as request_patch:
                state = {}
                self.assertFalse(app.sync_remnawave(cfg, state, "fingerprint", "same"))
            request_patch.assert_not_called()
            self.assertEqual(state["remna_synced_fingerprint"], "fingerprint")


class UpdateTests(unittest.TestCase):
    def release_side_effect(self, repo, filename):
        mapping = {
            "hydraponique/roscomvpn-geosite": SITE_BASE,
            "hydraponique/roscomvpn-geoip": IP_BASE,
            "vahellame/russia-whitelist-geosite": SITE_DONOR,
            "vahellame/russia-whitelist-geoip": IP_DONOR,
        }
        data = mapping[repo]
        return data, {"repository": repo, "tag": "test", "sha256": app.sha256(data)}

    def test_original_is_byte_identical_to_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            cfg = config(Path(temporary), "original")
            with patch.object(app, "release_asset", side_effect=self.release_side_effect):
                databases, _ = app.build_databases(cfg)
            self.assertEqual(databases, {"geosite.dat": SITE_BASE, "geoip.dat": IP_BASE})

    def test_custom_changes_only_whitelists(self):
        with tempfile.TemporaryDirectory() as temporary:
            cfg = config(Path(temporary))
            with patch.object(app, "release_asset", side_effect=self.release_side_effect):
                databases, metadata = app.build_databases(cfg)
            self.assertEqual(metadata["counts"]["geosite"]["whitelist"], 2)
            self.assertEqual(metadata["counts"]["geoip"]["whitelist"], 2)
            self.assertNotEqual(databases["geosite.dat"], SITE_BASE)
            self.assertNotEqual(databases["geoip.dat"], IP_BASE)

    def test_custom_never_touches_remna_api(self):
        with tempfile.TemporaryDirectory() as temporary:
            cfg = config(Path(temporary), publish=False)
            metadata = {
                "sources": {},
                "counts": {
                    "geosite": {"whitelist": 2},
                    "geoip": {"whitelist": 2},
                },
                "geoip_whitelist_source_categories": ["other", "vk", "yandex"],
            }
            with patch.object(app, "build_databases", return_value=({"geosite.dat": b"s", "geoip.dat": b"i"}, metadata)), patch.object(
                app, "source_profile", return_value=copy.deepcopy(PROFILE)
            ), patch.object(app.requests, "get") as get, patch.object(app.requests, "patch") as request_patch:
                status = app.update_once(cfg)
            self.assertFalse(status["remna_patched"])
            get.assert_not_called()
            request_patch.assert_not_called()

    def test_failed_patch_is_retried_after_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            cfg = config(Path(temporary), "original", publish=True)
            metadata = {
                "sources": {},
                "counts": {
                    "geosite": {"whitelist": 1},
                    "geoip": {"whitelist": 1},
                },
                "geoip_whitelist_source_categories": [],
            }
            common = [
                patch.object(app, "build_databases", return_value=({"geosite.dat": b"s", "geoip.dat": b"i"}, metadata)),
                patch.object(app, "source_profile", return_value=copy.deepcopy(PROFILE)),
            ]
            for item in common:
                item.start()
                self.addCleanup(item.stop)
            with patch.object(app.RemnaClient, "get", return_value={"uuid": "x", "customResponseHeaders": {}}), patch.object(
                app.RemnaClient, "patch", side_effect=RuntimeError("API down")
            ) as first_patch:
                with self.assertRaises(RuntimeError):
                    app.update_once(cfg)
            self.assertEqual(first_patch.call_count, 1)
            state = app.read_json(cfg.output_dir / "state.json")
            self.assertNotIn("remna_synced_fingerprint", state)
            with patch.object(app.RemnaClient, "get", return_value={"uuid": "x", "customResponseHeaders": {}}), patch.object(
                app.RemnaClient, "patch"
            ) as retry_patch:
                app.update_once(cfg)
            self.assertEqual(retry_patch.call_count, 1)

    def test_shrink_guard_skips_first_migration_then_enforces(self):
        with tempfile.TemporaryDirectory() as temporary:
            cfg = config(Path(temporary))
            metadata = {"counts": {"geosite": {"whitelist": 1}, "geoip": {"whitelist": 1}}}
            app.check_shrink(cfg, {}, metadata)
            state = {
                "mode": "custom",
                "counts": {"geosite": {"whitelist": 10}, "geoip": {"whitelist": 10}},
            }
            with self.assertRaises(ValueError):
                app.check_shrink(cfg, state, metadata)


if __name__ == "__main__":
    unittest.main()

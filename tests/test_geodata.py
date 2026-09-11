import ipaddress
import unittest

import geodata


def vint(number):
    output = bytearray()
    while number > 127:
        output.append((number & 127) | 128)
        number >>= 7
    output.append(number)
    return bytes(output)


def blob(number, value):
    return vint(number * 8 + 2) + vint(len(value)) + value


def rule(value, kind=3, attributes=b""):
    return b"\x08" + vint(kind) + blob(2, value.encode()) + attributes


def category(code, *items, reverse=False):
    body = blob(1, code.encode()) + b"".join(blob(2, item) for item in items)
    if reverse:
        body += b"\x18\x01"
    return blob(1, body)


def network(value):
    item = ipaddress.ip_network(value)
    return blob(1, item.network_address.packed) + b"\x10" + vint(item.prefixlen)


class GeoSiteTests(unittest.TestCase):
    def test_only_whitelist_is_replaced_and_attributes_survive(self):
        attribute = blob(3, blob(1, b"curator") + b"\x10\x01")
        base = category("PRIVATE", rule("localhost")) + category(
            "WHITELIST", rule("old.example")
        )
        donor_entry = category(
            "WHITELIST", rule("exact.example", 3, attribute), rule("suffix.example", 2)
        )
        donor = donor_entry + category("ADS", rule("ad.example"))
        unknown = blob(99, b"future-field")
        merged, counts = geodata.replace_geosite_whitelist(
            base + unknown, donor, minimum=1
        )
        self.assertEqual(merged, category("PRIVATE", rule("localhost")) + donor_entry + unknown)
        self.assertEqual(counts, {"private": 1, "whitelist": 2})
        self.assertIn(attribute, merged)

    def test_missing_empty_duplicate_and_truncated_rejected(self):
        base = category("WHITELIST", rule("old.example"))
        valid = category("WHITELIST", rule("new.example"))
        for donor in (
            b"",
            valid[:-1],
            valid + valid,
            category("OTHER", rule("new.example")),
            category("WHITELIST"),
        ):
            with self.subTest(donor=donor), self.assertRaises(ValueError):
                geodata.replace_geosite_whitelist(base, donor, minimum=1)

    def test_invalid_wire_and_domain_types_rejected(self):
        for value in (b"\x00", b"\x0a\x80", b"\x0a\x01", b"\x0b", b"\x80" * 11):
            with self.subTest(value=value), self.assertRaises(ValueError):
                geodata.inspect_geosite(value)
        with self.assertRaises(ValueError):
            geodata.replace_geosite_whitelist(
                category("WHITELIST", rule("old")),
                category("WHITELIST", rule("new", 9)),
                minimum=1,
            )


class GeoIPTests(unittest.TestCase):
    def setUp(self):
        self.base = (
            category("DIRECT", network("192.0.2.0/24"))
            + category("PRIVATE", network("10.0.0.0/8"))
            + category("WHITELIST", network("198.51.100.0/24"))
        )
        self.donor = (
            category("other", network("203.0.113.0/24"))
            + category("vk", network("203.0.113.0/24"))
            + category("yandex", network("2001:db8::/32"))
            + category("trash", network("192.88.99.0/24"))
            + category("category-public-dns", network("8.8.8.8/32"))
        )

    def test_union_deduplicates_ipv4_ipv6_and_preserves_everything_else(self):
        merged, counts = geodata.replace_geoip_whitelist(self.base, self.donor)
        original = geodata.inspect_geoip(self.base)[1]
        actual = geodata.inspect_geoip(merged)[1]
        self.assertEqual(counts, {"direct": 1, "private": 1, "whitelist": 2})
        for code in ("direct", "private"):
            self.assertEqual(original[code][0].raw, actual[code][0].raw)
        whitelist = geodata.fields(actual["whitelist"][0].value)
        networks = {
            geodata.cidr(field.value)
            for field in geodata.matching(whitelist, 2, 2)
        }
        self.assertEqual(
            networks,
            {
                geodata.cidr(network("203.0.113.0/24")),
                geodata.cidr(network("2001:db8::/32")),
            },
        )

    def test_missing_inverted_unknown_and_bad_cidr_rejected(self):
        with self.assertRaises(ValueError):
            geodata.replace_geoip_whitelist(self.base, category("other", network("1.1.1.0/24")))
        with self.assertRaises(ValueError):
            geodata.replace_geoip_whitelist(
                self.base,
                category("other", network("1.1.1.0/24"), reverse=True),
                ("other",),
            )
        donor_unknown = blob(
            1, blob(1, b"other") + blob(2, network("1.1.1.0/24")) + b"\x20\x01"
        )
        with self.assertRaises(ValueError):
            geodata.replace_geoip_whitelist(self.base, donor_unknown, ("other",))
        for bad in (
            blob(1, b"bad"),
            blob(1, b"\x00" * 4) + b"\x10\x21",
            blob(1, b"\xc0\x00\x02\x01") + b"\x10\x18",
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                geodata.inspect_geoip(category("other", bad))


if __name__ == "__main__":
    unittest.main()

"""Lossless Xray/V2Ray GeoSite and GeoIP protobuf transformations.

The parser intentionally works at protobuf wire level.  Replacing one category
therefore preserves every untouched category byte-for-byte, including unknown
fields and serialized domain attributes.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import re


@dataclass(frozen=True)
class Field:
    number: int
    wire: int
    value: bytes | int
    raw: bytes


def varint(data: bytes, position: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 70, 7):
        if position >= len(data):
            raise ValueError("Truncated protobuf varint")
        byte = data[position]
        position += 1
        if shift == 63 and byte > 1:
            raise ValueError("Protobuf varint overflow")
        value |= (byte & 127) << shift
        if byte < 128:
            return value, position
    raise ValueError("Invalid protobuf varint")


def fields(data: bytes) -> list[Field]:
    result: list[Field] = []
    position = 0
    while position < len(data):
        start = position
        key, position = varint(data, position)
        number, wire = key >> 3, key & 7
        if not 0 < number < 2**29:
            raise ValueError("Invalid protobuf field number")
        if wire == 0:
            value, position = varint(data, position)
        elif wire in (1, 2, 5):
            if wire == 2:
                length, position = varint(data, position)
            else:
                length = 8 if wire == 1 else 4
            end = position + length
            if end > len(data):
                raise ValueError("Truncated protobuf field")
            value = data[position:end]
            position = end
        else:
            raise ValueError(f"Unsupported protobuf wire type {wire}")
        result.append(Field(number, wire, value, data[start:position]))
    return result


def matching(message: list[Field], number: int, wire: int) -> list[Field]:
    found = [field for field in message if field.number == number]
    if any(field.wire != wire for field in found):
        raise ValueError(f"Wrong wire type for field {number}")
    return found


def _text(value: bytes) -> str:
    return value.decode("utf-8", errors="strict")


def domain(message: bytes) -> tuple[int, str]:
    parsed = fields(message)
    types = matching(parsed, 1, 0)
    values = matching(parsed, 2, 2)
    if len(types) > 1 or len(values) != 1:
        raise ValueError("Ambiguous or missing domain value/type")
    kind = types[0].value if types else 0
    value = _text(values[0].value)
    if kind not in (0, 1, 2, 3) or not value or "\x00" in value:
        raise ValueError("Invalid domain rule")
    for attribute in matching(parsed, 3, 2):
        attrs = fields(attribute.value)
        keys = matching(attrs, 1, 2)
        bools = matching(attrs, 2, 0)
        ints = matching(attrs, 3, 0)
        if len(keys) != 1 or not _text(keys[0].value) or len(bools) + len(ints) > 1:
            raise ValueError("Invalid domain attribute")
    return int(kind), value


def inspect_geosite(data: bytes) -> tuple[list[Field], dict[str, tuple[Field, int]]]:
    outer = fields(data)
    categories: dict[str, tuple[Field, int]] = {}
    for entry in matching(outer, 1, 2):
        parsed = fields(entry.value)
        codes = matching(parsed, 1, 2)
        if len(codes) != 1:
            raise ValueError("Missing or duplicate GeoSite category code")
        code = _text(codes[0].value).lower()
        if not re.fullmatch(r"[a-z0-9_-]+", code) or code in categories:
            raise ValueError(f"Invalid or duplicate GeoSite category: {code!r}")
        domains = matching(parsed, 2, 2)
        for entry_domain in domains:
            domain(entry_domain.value)
        categories[code] = (entry, len(domains))
    if not categories:
        raise ValueError("GeoSite contains no categories")
    return outer, categories


# Compatibility name used by the supplied prototype tests.
inspect = inspect_geosite


def replace_geosite_whitelist(
    base: bytes, donor: bytes, minimum: int = 100
) -> tuple[bytes, dict[str, int]]:
    outer, original = inspect_geosite(base)
    _, replacement = inspect_geosite(donor)
    if "whitelist" not in original or "whitelist" not in replacement:
        raise ValueError("Both GeoSite inputs must contain whitelist")
    new_entry, count = replacement["whitelist"]
    if count < minimum:
        raise ValueError(f"GeoSite whitelist too small: {count} < {minimum}")
    old_entry = original["whitelist"][0]
    merged = b"".join(new_entry.raw if item is old_entry else item.raw for item in outer)
    _, checked = inspect_geosite(merged)
    if set(checked) != set(original):
        raise ValueError("GeoSite category set changed")
    for code, (entry, _) in original.items():
        expected = new_entry.raw if code == "whitelist" else entry.raw
        if checked[code][0].raw != expected:
            raise ValueError(f"Unexpected change in GeoSite category {code}")
    return merged, {code: item_count for code, (_, item_count) in checked.items()}


# Compatibility name used by the supplied prototype.
replace_whitelist = replace_geosite_whitelist


def encode_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("Cannot encode a negative varint")
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def encoded_field(number: int, value: bytes) -> bytes:
    return encode_varint(number * 8 + 2) + encode_varint(len(value)) + value


def cidr(value: bytes) -> tuple[bytes, int]:
    parsed = fields(value)
    addresses = matching(parsed, 1, 2)
    prefixes = matching(parsed, 2, 0)
    if len(addresses) != 1 or len(prefixes) > 1:
        raise ValueError("Missing or duplicate CIDR address/prefix")
    address = addresses[0].value
    prefix = prefixes[0].value if prefixes else 0
    if len(address) not in (4, 16) or not 0 <= prefix <= len(address) * 8:
        raise ValueError("Invalid CIDR address length or prefix")
    ipaddress.ip_network((ipaddress.ip_address(address), prefix), strict=True)
    return address, int(prefix)


def inspect_geoip(data: bytes) -> tuple[list[Field], dict[str, tuple[Field, int]]]:
    outer = fields(data)
    categories: dict[str, tuple[Field, int]] = {}
    for entry in matching(outer, 1, 2):
        parsed = fields(entry.value)
        codes = matching(parsed, 1, 2)
        reverse = matching(parsed, 3, 0)
        if len(codes) != 1 or len(reverse) > 1 or any(
            field.value not in (0, 1) for field in reverse
        ):
            raise ValueError("Invalid GeoIP category code/reverse_match")
        code = _text(codes[0].value).lower()
        if not re.fullmatch(r"[a-z0-9_-]+", code) or code in categories:
            raise ValueError(f"Invalid or duplicate GeoIP category: {code!r}")
        networks = matching(parsed, 2, 2)
        for network in networks:
            cidr(network.value)
        categories[code] = (entry, len(networks))
    if not categories:
        raise ValueError("GeoIP contains no categories")
    return outer, categories


def replace_geoip_whitelist(
    base: bytes,
    donor: bytes,
    selected: tuple[str, ...] = ("other", "vk", "yandex"),
    minimum: int = 1,
) -> tuple[bytes, dict[str, int]]:
    outer, original = inspect_geoip(base)
    _, replacement = inspect_geoip(donor)
    normalized = tuple(code.strip().lower() for code in selected)
    forbidden = {"trash", "category-public-dns"}
    if "whitelist" not in original or not normalized or len(set(normalized)) != len(normalized):
        raise ValueError("Missing base whitelist or invalid GeoIP source category selection")
    if forbidden.intersection(normalized):
        raise ValueError("trash and category-public-dns cannot be imported into whitelist")

    # Exact CIDR tuples are deduplicated.  Networks are deliberately not
    # collapsed because that could broaden the allowed address space.
    networks: dict[tuple[bytes, int], bytes] = {}
    for code in normalized:
        if code not in replacement:
            raise ValueError(f"Missing GeoIP source category {code!r}")
        parsed = fields(replacement[code][0].value)
        if any(field.value for field in matching(parsed, 3, 0)):
            raise ValueError(f"Cannot union inverted GeoIP category {code}")
        if any(field.number not in (1, 2, 3) for field in parsed):
            raise ValueError(f"Unknown GeoIP category semantics in {code}")
        for network in matching(parsed, 2, 2):
            networks.setdefault(cidr(network.value), network.raw)
    if len(networks) < minimum:
        raise ValueError(f"GeoIP whitelist too small: {len(networks)} < {minimum}")

    whitelist_message = encoded_field(1, b"WHITELIST") + b"".join(
        networks[key] for key in sorted(networks)
    )
    new_entry = encoded_field(1, whitelist_message)
    old_entry = original["whitelist"][0]
    merged = b"".join(new_entry if item is old_entry else item.raw for item in outer)
    _, checked = inspect_geoip(merged)
    if set(checked) != set(original):
        raise ValueError("GeoIP category set changed")
    for code, (entry, _) in original.items():
        expected = new_entry if code == "whitelist" else entry.raw
        if checked[code][0].raw != expected:
            raise ValueError(f"Unexpected change in GeoIP category {code}")
    return merged, {code: item_count for code, (_, item_count) in checked.items()}

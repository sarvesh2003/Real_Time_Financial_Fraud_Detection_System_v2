"""Write record-free MaxMind DB files for credential-free test stacks.

Each database has a one-node search tree whose records both mean "no data",
so every lookup succeeds with empty fields. They let go-enricher start and
exercise its real GeoIP reader without MaxMind credentials; they say nothing
about real GeoIP results. Format: https://maxmind.github.io/MaxMind-DB/

Usage: python tests/fixtures/geoip/make_empty_mmdb.py   (rewrites both files)
"""

from pathlib import Path

HERE = Path(__file__).resolve().parent
METADATA_MARKER = b"\xab\xcd\xefMaxMind.com"
UTF8, UINT16, UINT32, MAP, UINT64, ARRAY = 2, 5, 6, 7, 9, 11


def control(kind: int, size: int) -> bytes:
    if size < 29:
        size_field, size_bytes = size, b""
    elif size < 29 + 256:
        size_field, size_bytes = 29, bytes([size - 29])
    else:
        raise ValueError("fixture values are short; longer sizes are not implemented")
    if kind <= 7:
        return bytes([kind << 5 | size_field]) + size_bytes
    # An extended type is stored in the byte after the control byte.
    return bytes([size_field, kind - 7]) + size_bytes


def unsigned(kind: int, value: int) -> bytes:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return control(kind, len(raw)) + raw


def encode(value) -> bytes:
    if isinstance(value, tuple):  # (type, int) for an explicit integer width
        return unsigned(*value)
    if isinstance(value, str):
        raw = value.encode("utf-8")
        return control(UTF8, len(raw)) + raw
    if isinstance(value, list):
        return control(ARRAY, len(value)) + b"".join(encode(item) for item in value)
    if isinstance(value, dict):
        return control(MAP, len(value)) + b"".join(encode(k) + encode(v) for k, v in value.items())
    raise TypeError(type(value))


def empty_database(database_type: str) -> bytes:
    node_count, record_size = 1, 24
    # Both records equal node_count, which the format defines as "not found".
    tree = node_count.to_bytes(3, "big") * 2
    metadata = {
        "binary_format_major_version": (UINT16, 2),
        "binary_format_minor_version": (UINT16, 0),
        "build_epoch": (UINT64, 1793059200),  # fixed, so the file is reproducible
        "database_type": database_type,
        "description": {"en": "Record-free test fixture; not real GeoIP data"},
        "ip_version": (UINT16, 6),
        "languages": ["en"],
        "node_count": (UINT32, node_count),
        "record_size": (UINT16, record_size),
    }
    return tree + bytes(16) + METADATA_MARKER + encode(metadata)


if __name__ == "__main__":
    for name in ("GeoLite2-City", "GeoLite2-ASN"):
        (HERE / f"{name}.mmdb").write_bytes(empty_database(name))
        print(f"wrote {HERE / name}.mmdb")

from dzll_launcher.live import parse_dayz_rules_mods


def _escape(buf: bytes) -> bytes:
    out = bytearray()
    for b in buf:
        if b <= 0x03:
            out.append(0x01)
            out.append(b)
        else:
            out.append(b)
    return bytes(out)


def _build_payload(mods: list[tuple[int, int, str]]) -> bytes:
    """mods: list of (workshop_id, id_len, name)."""
    buf = bytearray()
    buf.append(2)  # protocol version
    buf += bytes([0, 0, 0])  # flags
    buf.append(len(mods))  # mod count
    for workshop_id, id_len, name in mods:
        buf += b"\x00\x01\x02\x03"  # hash (deliberately includes escapable bytes)
        buf.append(id_len)
        buf += workshop_id.to_bytes(id_len, "little")
        name_bytes = name.encode("utf-8")
        buf.append(len(name_bytes))
        buf += name_bytes
    return bytes(buf)


def _chunk_into_rules(escaped: bytes, chunk_count: int) -> dict:
    n = len(escaped)
    size = max(1, (n + chunk_count - 1) // chunk_count)
    pieces = [escaped[i:i + size] for i in range(0, n, size)]
    total = len(pieces)
    rules = {bytes([idx, total]): piece for idx, piece in enumerate(pieces, start=1)}
    rules[b"dedicated"] = b"1"
    rules[b"island"] = b"chernarusplus"
    return rules


def test_single_mod_round_trips_through_chunking_and_escaping():
    payload = _build_payload([(1559212036, 4, "DayZ-Expansion-Core")])
    rules = _chunk_into_rules(_escape(payload), chunk_count=1)
    assert parse_dayz_rules_mods(rules) == [
        {"steamWorkshopId": 1559212036, "name": "DayZ-Expansion-Core"}
    ]


def test_multiple_mods_split_across_several_chunks():
    payload = _build_payload(
        [
            (123456, 4, "TestMod"),
            (999, 2, "Second"),
            (7, 1, "Third"),
        ]
    )
    rules = _chunk_into_rules(_escape(payload), chunk_count=4)
    assert parse_dayz_rules_mods(rules) == [
        {"steamWorkshopId": 123456, "name": "TestMod"},
        {"steamWorkshopId": 999, "name": "Second"},
        {"steamWorkshopId": 7, "name": "Third"},
    ]


def test_missing_chunk_yields_no_mods_rather_than_garbage():
    payload = _build_payload([(123456, 4, "TestMod")])
    rules = _chunk_into_rules(_escape(payload), chunk_count=3)
    # Drop the middle chunk to simulate an incomplete/garbled response.
    del rules[b"\x02\x03"]
    assert parse_dayz_rules_mods(rules) == []


def test_non_chunked_rules_without_binary_payload_yield_no_mods():
    rules = {"dedicated": "1", "island": "chernarusplus"}
    assert parse_dayz_rules_mods(rules) == []


def test_not_a_dict_returns_empty_list():
    assert parse_dayz_rules_mods(None) == []
    assert parse_dayz_rules_mods([]) == []


def test_duplicate_workshop_ids_are_deduplicated():
    payload = _build_payload([(42, 1, "First"), (42, 1, "FirstAgain")])
    rules = _chunk_into_rules(_escape(payload), chunk_count=1)
    assert parse_dayz_rules_mods(rules) == [{"steamWorkshopId": 42, "name": "First"}]

from dzll_launcher.live import parse_dayz_rules_mods


# Index-in-table escaping, matching the real DayZServer binary: 0x01 <N>
# decodes via a 4-entry lookup, not literally to N. Index 3 maps to 0xFF,
# not 0x03 - that mismatch was the root cause of a real parsing bug (one
# Workshop ID decoded wrong) confirmed against a live server's own mod
# config.
_ESCAPE_TABLE = (0x00, 0x01, 0x02, 0xFF)


def _escape(buf: bytes) -> bytes:
    out = bytearray()
    for b in buf:
        if b in _ESCAPE_TABLE:
            out.append(0x01)
            out.append(_ESCAPE_TABLE.index(b))
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
        buf += b"\x00\x01\x02\xff"  # hash (deliberately includes every escapable byte)
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


def test_workshop_id_byte_requiring_0xff_escape_decodes_correctly():
    # Regression test: a Workshop ID whose bytes include 0xFF (e.g. the real
    # 2116157322 = 0x7E21FF8A) was previously decoded as 2116092810
    # (0x7E21038A) because escape index 3 was assumed to map to literal
    # 0x03 instead of 0xFF.
    workshop_id = 2116157322
    assert workshop_id.to_bytes(4, "little") == bytes.fromhex("8aff217e")
    payload = _build_payload([(workshop_id, 4, "DayZ-Expansion-Licensed")])
    rules = _chunk_into_rules(_escape(payload), chunk_count=2)
    assert parse_dayz_rules_mods(rules) == [
        {"steamWorkshopId": workshop_id, "name": "DayZ-Expansion-Licensed"}
    ]


def test_full_real_server_rules_response_matches_actual_mod_config():
    # Exact raw_rules captured (via DZLL_MODS_DEBUG=1) from a live DayZ
    # server, cross-checked against that server's own serverDZ.cfg mod
    # list. All 32 Workshop IDs must match exactly.
    rules = {
        b"\x01\n": b"\x02\x08\x01\x02\x01\x02 >\xce#\xb4\x04\xe26E\xe3\x10TriageZ Mod Pack\x1b\x18\xdb\x1e\x04\xea\xd4\x1eb\x08CodeLock\xf6lG\xa2\x04g*f\xad\tFurnitureq\x94\xba\xe2\x04\x8e\xfa\t\xad\tGive&Take\xac\xc2w\x82\x04qJ\xb1\xd3\x15Sakhal Houses Adapted\xd62\xe2\xb9\x04\xe6\xb3",
        b"\x02\n": b',\x9f\x17Building FortificationsWC\xa0K\x04\xed\x91y\xa6\x16DayZ-Expansion-Weapons\x98\xf2\xa7\x18\x04\xb6\xe7!~\x0eDayZ-Expansion$\x1f7\xec\x04\x10\x8dy\xa6\x19DayZ-Expansion-Map-Assetsr\xde"\xb5\x04\x8a\x01\x03!~',
        b"\x03\n": b"\x17DayZ-Expansion-Licensed\x17\x84\x86T\x04\\\xde\x99\x88\x13DayZ-Expansion-Core\xa7.\xe7\x10\x04 \x9e\xb6\x97\x0eDabs Framework\xc7\xbd\xa6s\x04\xe7$\xf5\xa9\x1dTACSAT Radio - Updated (50km)\xf8\x99\xb0\x81\x04\xe2_\xcd\xdd\x17I",
        b"\x04\n": b"mmersive Hearing Focusrp\xb0\x14\x04\xb7&|\xdf\x0eNoobPercentHudAp\n\xed\x04\xab\\p\xdd\x16Multiplix Territory V2\x05\x83,\xd7\x04z\xd9\t\xc4\x1dZenarchist's Enormous PackageVA\x8f\x97\x04\xecr\xae\xdc\x15",
        b"\x05\n": b"Zenarchist's Core Mod\x9c\xe9\xfe\xbd\x04\x8f34\x97\x16SkyZ - Skybox Overhaul\x17.\xa1\x8a\x04S\xdd\xee\xc4&Early Winter or late Fall in Chernarus\x12n\x8e\x07\x04\xaeC\x9e\xc8\x19Disable campfire",
        b"\x06\n": b" climbingXF^\xec\x042\x01\x01\xaa\xde\x17Asmond Vanilla Clothing?,\x10\x01\x01\x04\xa3Z\xfc\x91\x0eMagObfuscation\x9e\x81\xcb\xac\x04\xd6$&\xda\x0bDeath Groan\xd7\\\xbdv\x04Q\xde\xea\xde\x10Animal Sound FixB\x17\xb9x\x04\x81Z\xb5\xb4\x10In",
        b"\x07\n": b"ediaInfectedAI7\x05-\x0e\x04\x1b\xf7\x8d\xd9\x17Terje Compatibility COT\xd7\x85\xa6\x86\x04\x90'9]\x16Community Online Tools\xd1\xd6n\xc9\x04\xea\xf5\x8d\xd9\x12Terje Start Screen\x9b-\x1d+\x04\xa0\xee\x8d\xd9\x0eTerje Medi",
        b"\x08\n": b"cine7\xcfe\x80\x04B\xed\x8d\xd9\nTerje Core\xc1\xc6\xafh\x04\x04\xb0\xef\\\x13Community Framework\x19\x04dayz\x0eJacob_Mango_V3\nCodeLockv3\tExpansion\x0cInclementDab\x06ruffaz\x03dab\x17Buildin",
        b"\t\n": b"g-Fortifications\x03DIW\x0baffenb3rtV2\tSrPomidor\x06Inedia\nZenarchist\x05cynep\x08CampFire\x05Terje\x08Zodiacal\tMultiplix\x037ka\x06Asmond\x06KoT9pA\nNo0bM4st",
        b"\n\n": b"3r\x03VPP\x06gomods\x0cNotABananaV3",
        b"allowedBuild": b"0", b"clientPort": b"0", b"dedicated": b"1",
        b"island": b"chernarusplus", b"language": b"65545", b"platform": b"?",
        b"requiredBuild": b"0", b"requiredVersion": b"129", b"timeLeft": b"15",
    }
    real_ids = {
        1559212036, 3649957186, 3649957536, 3649959402, 1564026768, 3649959707,
        3031784065, 3739934289, 3659932886, 2449234595, 3735683378, 3365815214,
        3303988563, 2536780687, 3702420204, 3288979834, 3715128491, 3749455543,
        3721224162, 2851415271, 2545327648, 2291785308, 2116157322, 2792983824,
        2116151222, 2792985069, 2670506982, 3551611505, 2903112334, 2909153895,
        1646187754, 3812964066,
    }
    mods = parse_dayz_rules_mods(rules)
    assert len(mods) == 32
    assert {mod["steamWorkshopId"] for mod in mods} == real_ids

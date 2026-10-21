"""Test helper: writes a minimal Outlook .msg (Compound File Binary v3, MS-CFB + MS-OXMSG) with SYNTHETIC
content, so the .msg reader can be tested without real mail. Streams under 4096 bytes go in the mini
stream, as the format requires."""
from __future__ import annotations

import struct
from datetime import datetime, timezone

SECTOR, MINI, CUTOFF = 512, 64, 4096
FREE, END, FATSECT, NOSTREAM = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD, 0xFFFFFFFF


def _filetime(d: datetime) -> int:
    return int((d - datetime(1601, 1, 1, tzinfo=timezone.utc)).total_seconds() * 10_000_000)


def msg_bytes(subject: str, body: str, sender: str, sender_email: str, to: str, message_id: str,
              sent: datetime, in_reply_to: str = "", attachments: list[tuple[str, bytes]] = ()) -> bytes:
    def text(code: str, value: str) -> tuple[str, bytes]:
        return f"__substg1.0_{code}001F", value.encode("utf-16-le")

    root = dict([text("0037", subject), text("1000", body), text("0C1A", sender), text("0C1F", sender_email),
                 text("0E04", to), text("1035", message_id)] + ([text("1042", in_reply_to)] if in_reply_to else []))
    props = bytearray(32)
    struct.pack_into("<II", props, 16, 0, len(attachments))
    props += struct.pack("<HHIQ", 0x0040, 0x0039, 6, _filetime(sent))       # PR_CLIENT_SUBMIT_TIME
    root["__properties_version1.0"] = bytes(props)
    storages = {}
    for i, (name, data) in enumerate(attachments):
        storages[f"__attach_version1.0_#{i:08X}"] = dict([text("3707", name), ("__substg1.0_37010102", data)])
    return _cfb(root, storages)


def _cfb(root_streams: dict[str, bytes], storages: dict[str, dict[str, bytes]]) -> bytes:
    # directory entries: (name, type, data, children)
    entries = [["Root Entry", 5, b"", []]]
    def add(name, kind, data=b""):
        entries.append([name, kind, data, []])
        return len(entries) - 1
    for name, data in root_streams.items():
        entries[0][3].append(add(name, 2, data))
    for sname, streams in storages.items():
        sid = add(sname, 1)
        entries[0][3].append(sid)
        for name, data in streams.items():
            entries[sid][3].append(add(name, 2, data))

    # mini stream for the small streams, regular sectors for the others
    mini_data, minifat, start = bytearray(), [], {}
    big = []
    for i, (name, kind, data, _) in ((i, e[:4]) for i, e in enumerate(entries)):
        if kind != 2:
            continue
        if len(data) < CUTOFF:
            first = len(mini_data) // MINI
            n = max(1, -(-len(data) // MINI))
            minifat += [first + k + 1 for k in range(n - 1)] + [END]
            start[i] = first if data else END
            mini_data += data + b"\0" * (n * MINI - len(data))
        else:
            big.append(i)

    dir_bytes = b"".join(_dir_entry(e, entries) for e in entries)
    dir_bytes += b"\0" * (-len(dir_bytes) % SECTOR)
    minifat_bytes = b"".join(struct.pack("<I", x) for x in minifat)
    minifat_bytes += b"\xff" * (-len(minifat_bytes) % SECTOR)
    mini_bytes = bytes(mini_data) + b"\0" * (-len(mini_data) % SECTOR)
    blobs = [("dir", dir_bytes), ("minifat", minifat_bytes), ("mini", mini_bytes)] + \
            [(i, entries[i][2] + b"\0" * (-len(entries[i][2]) % SECTOR)) for i in big]
    n_data = sum(len(b) // SECTOR for _, b in blobs)
    n_fat = 1
    while n_fat * (SECTOR // 4) < n_data + n_fat:
        n_fat += 1
    assert n_fat <= 109
    fat, sect, first_sect = [], 0, {}
    for key, b in blobs:
        n = len(b) // SECTOR
        if n == 0:
            first_sect[key] = END
            continue
        first_sect[key] = sect
        fat += [sect + k + 1 for k in range(n - 1)] + [END]
        sect += n
    fat_start = sect
    fat += [FATSECT] * n_fat
    fat += [FREE] * (n_fat * (SECTOR // 4) - len(fat))

    for i in big:
        start[i] = first_sect[i]
    entries[0][2] = bytes(mini_data)
    start[0] = first_sect["mini"]
    dir_bytes = b"".join(_dir_entry(e, entries, start.get(k, END)) for k, e in enumerate(entries))
    dir_bytes += b"\0" * (-len(dir_bytes) % SECTOR)

    header = bytearray(SECTOR)
    header[0:8] = bytes.fromhex("D0CF11E0A1B11AE1")
    struct.pack_into("<HHHHH", header, 24, 0x003E, 0x0003, 0xFFFE, 9, 6)
    struct.pack_into("<I", header, 44, n_fat)
    struct.pack_into("<I", header, 48, first_sect["dir"])
    struct.pack_into("<I", header, 56, CUTOFF)
    struct.pack_into("<II", header, 60, first_sect["minifat"], len(minifat_bytes) // SECTOR if minifat else 0)
    struct.pack_into("<II", header, 68, END, 0)
    difat = [fat_start + k for k in range(n_fat)] + [FREE] * (109 - n_fat)
    struct.pack_into("<109I", header, 76, *difat)
    body = b"".join(dir_bytes if k == "dir" else b for k, b in blobs)
    return bytes(header) + body + b"".join(struct.pack("<I", x) for x in fat)


def _dir_entry(e, entries, start_sector: int = END) -> bytes:
    name, kind, data, children = e[:4]
    out = bytearray(128)
    enc = name.encode("utf-16-le") + b"\0\0"
    out[0:len(enc)] = enc
    struct.pack_into("<HBB", out, 64, len(enc), kind, 1)
    struct.pack_into("<III", out, 68, NOSTREAM, NOSTREAM, NOSTREAM)
    if children:
        # children as a chain of right siblings, in the order of the format (length, then upper case)
        order = sorted(children, key=lambda i: (len(entries[i][0]), entries[i][0].upper()))
        struct.pack_into("<I", out, 76, order[0])
        for a, b in zip(order, order[1:]):
            entries[a].append(b)
    right = e[4] if len(e) > 4 else NOSTREAM
    struct.pack_into("<I", out, 72, right)
    size = len(data) if kind in (2, 5) else 0
    struct.pack_into("<IQ", out, 116, start_sector if kind in (2, 5) and size else (END if kind != 1 else 0), size)
    return bytes(out)

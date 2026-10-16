# -*- coding: utf-8 -*-
"""Parse pktmon pcapng: reassemble TCP streams to 120.24.174.5:5432 and
extract PostgreSQL protocol messages (Startup, Query, Parse)."""
import struct, sys, io

PCAPNG = sys.argv[1] if len(sys.argv) > 1 else "hbo_capture.pcapng"

# ---------- minimal pcapng reader ----------
def read_pcapng(path):
    pkts = []  # (ts_us, if_linktype, data)
    with open(path, "rb") as f:
        data = f.read()
    off = 0
    linktypes = {}
    endian = "<"
    while off + 12 <= len(data):
        btype, blen = struct.unpack_from(endian + "II", data, off)
        body = data[off + 8: off + 8 + blen - 12]
        if btype == 0x0A0D0D0A:  # SHB
            bom = body[:4]
            endian = "<" if bom == b"\x4d\x3c\x2b\x1a" else ">"
        elif btype == 0x00000001:  # IDB
            linktype, _res, _snap = struct.unpack_from(endian + "HHI", body, 0)
            linktypes[len(linktypes)] = linktype
        elif btype == 0x00000006:  # EPB
            iface, tsh, tsl, caplen, _orig = struct.unpack_from(endian + "IIIII", body, 0)
            pkt = body[20:20 + caplen]
            ts = (tsh << 32 | tsl)
            pkts.append((ts, linktypes.get(iface, 1), pkt))
        elif btype == 0x00000002:  # obsolete PB
            ts_sec, ts_usec, caplen = struct.unpack_from(endian + "III", body, 0)
            pkt = body[12:12 + caplen]
            pkts.append((ts_sec * 1000000 + ts_usec, 1, pkt))
        off += blen
        if blen < 12:
            break
    return pkts

# ---------- extract TCP payload from Ethernet/RAW ----------
def tcp_payload(linktype, pkt):
    if linktype == 1:  # ethernet
        if len(pkt) < 14: return None
        eth_type = struct.unpack_from(">H", pkt, 12)[0]
        ipoff = 14
        while eth_type in (0x8100, 0x88A8) and len(pkt) >= ipoff + 4:
            eth_type = struct.unpack_from(">H", pkt, ipoff + 2)[0]
            ipoff += 4
        if eth_type != 0x0800: return None
    elif linktype in (101, 228):  # raw ip / ipv4
        ipoff = 0
    else:
        return None
    ip = pkt[ipoff:]
    if len(ip) < 20: return None
    ihl = (ip[0] & 0x0F) * 4
    proto = ip[9]
    if proto != 6: return None
    src = ".".join(map(str, ip[12:16])); dst = ".".join(map(str, ip[16:20]))
    tcp = ip[ihl:]
    if len(tcp) < 20: return None
    sport, dport, seq = struct.unpack_from(">HHI", tcp, 0)
    doff = (tcp[12] >> 4) * 4
    payload = tcp[doff:]
    return (src, sport, dst, dport, seq, payload)

# ---------- reassemble streams ----------
pkts = read_pcapng(PCAPNG)
print(f"packets: {len(pkts)}", file=sys.stderr)

streams = {}  # key -> {seq_base, segments {seq: bytes}, order}
count_tcp = 0
for ts, lt, pkt in pkts:
    r = tcp_payload(lt, pkt)
    if not r: continue
    src, sport, dst, dport, seq, payload = r
    if dport != 5432 and sport != 5432: continue
    if not payload: continue
    count_tcp += 1
    key = (sport if dport == 5432 else dport)  # per client port
    s = streams.setdefault(key, {"c2s": {}, "s2c": {}, "first": ts, "last": ts})
    s["last"] = ts
    if dport == 5432:
        s["c2s"][seq] = payload
    else:
        s["s2c"][seq] = payload

print(f"tcp payload pkts: {count_tcp}, streams: {len(streams)}", file=sys.stderr)

def assemble(segments):
    if not segments: return b"", {}
    # find base: the seq with the lowest; handle wrap by starting from min
    base = min(segments)
    out = io.BytesIO()
    pos = base
    chunks = {}  # stream_offset -> seq (for mapping)
    for seq in sorted(segments):
        d = segments[seq]
        if seq < pos:
            d = d[pos - seq:]
            if not d: continue
        elif seq > pos:
            pos = seq  # gap: skip (jump)
        chunks[pos - base] = seq
        out.write(d)
        pos += len(d)
    return out.getvalue(), chunks

# ---------- PG protocol parse ----------
def parse_pg(buf, chunks, direction, port, fp):
    off = 0
    startup_done = False
    while off + 5 <= len(buf):
        if not startup_done and direction == "c2s":
            # StartupMessage: no type byte
            length = struct.unpack_from(">I", buf, off)[0]
            if 8 <= length <= 2048 and off + length <= len(buf):
                body = buf[off + 4: off + length]
                proto = struct.unpack_from(">I", body, 0)[0]
                if proto in (0x00030000, 0x00019604, 0x00019608, 0x00030004):
                    params = body[4:].split(b"\x00")
                    kv = {}
                    for i in range(0, len(params) - 1, 2):
                        if params[i]: kv[params[i].decode("ascii", "replace")] = params[i+1].decode("utf-8", "replace")
                    fp.write(f"\n=== [port {port}] STARTUP ===\n")
                    for k, v in kv.items(): fp.write(f"  {k} = {v}\n")
                    off += length
                    startup_done = True
                    continue
            startup_done = True  # fall through to typed messages
        mtype = buf[off:off+1]
        length = struct.unpack_from(">I", buf, off + 1)[0]
        if length < 4 or off + 1 + length > len(buf):
            break
        body = buf[off + 5: off + 1 + length]
        if mtype == b"Q":
            sql = body[:-1].decode("utf-8", "replace")
            fp.write(f"\n=== [port {port}] QUERY ===\n{sql}\n")
        elif mtype == b"P":  # Parse: stmt\0 query\0 ...
            parts = body.split(b"\x00", 2)
            if len(parts) >= 2:
                sql = parts[1].decode("utf-8", "replace")
                fp.write(f"\n=== [port {port}] PARSE({parts[0].decode()}) ===\n{sql}\n")
        elif mtype == b"p":
            fp.write(f"\n=== [port {port}] PASSWORD msg (len={length}) ===\n")
        off += 1 + length

with open("pg_messages.txt", "w", encoding="utf-8") as fp:
    for port, s in sorted(streams.items(), key=lambda kv: kv[1]["first"]):
        c2s, _ = assemble(s["c2s"])
        s2c, _ = assemble(s["s2c"])
        if not c2s: continue
        fp.write(f"\n########## stream port={port} c2s={len(c2s)}B s2c={len(s2c)}B ##########")
        parse_pg(c2s, None, "c2s", port, fp)
print("done -> pg_messages.txt", file=sys.stderr)

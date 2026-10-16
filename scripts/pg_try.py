# -*- coding: utf-8 -*-
"""Minimal PostgreSQL client: connect with MD5 or SCRAM-SHA-256 password auth, run a test query."""
import socket, struct, hashlib, hmac, os, sys

HOST, PORT = "120.24.174.5", 5432
USER = "dba_app"
PASSWORD = sys.argv[1] if len(sys.argv) > 1 else "123123"
DATABASE = "hydata"

def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("closed")
        buf += chunk
    return buf

def recv_msg(sock):
    t = recv_exact(sock, 1)
    (ln,) = struct.unpack(">I", recv_exact(sock, 4))
    return t, recv_exact(sock, ln - 4)

def send_msg(sock, t, body):
    sock.sendall(t + struct.pack(">I", 4 + len(body)) + body)

def md5_auth(sock, salt):
    inner = hashlib.md5((PASSWORD + USER).encode()).hexdigest()
    outer = hashlib.md5(inner.encode() + salt).hexdigest()
    send_msg(sock, b"p", b"md5" + outer.encode() + b"\x00")

def scram_sha_256(sock):
    import base64
    client_nonce = os.urandom(18).hex()
    cfirst = f"n,,n=,r={client_nonce}"
    body = b"SCRAM-SHA-256" + b"\x00" + struct.pack(">I", len(cfirst)) + cfirst.encode()
    send_msg(sock, b"p", body)
    t, sfirst_b = recv_msg(sock)
    sfirst = sfirst_b[4:].decode()   # skip R message code (11)
    attrs = dict(kv.split("=", 1) for kv in sfirst.split(","))
    salt = base64.b64decode(attrs["s"])
    iters = int(attrs["i"])
    full_nonce = attrs["r"]
    salted = hashlib.pbkdf2_hmac("sha256", PASSWORD.encode(), salt, iters)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    wo_proof = f"c=biws,r={full_nonce}"
    auth_msg = f"{cfirst},{sfirst},{wo_proof}"
    sig = hmac.new(stored_key, auth_msg.encode(), hashlib.sha256).digest()
    proof = bytes(a ^ b for a, b in zip(client_key, sig))
    final = wo_proof + ",p=" + base64.b64encode(proof).decode()
    send_msg(sock, b"p", final.encode())

def main():
    sock = socket.create_connection((HOST, PORT), timeout=15)
    params = b"user\x00" + USER.encode() + b"\x00database\x00" + DATABASE.encode() + b"\x00\x00"
    sock.sendall(struct.pack(">I", 8 + len(params)) + struct.pack(">I", 196608) + params)
    while True:
        t, body = recv_msg(sock)
        if t == b"R":
            (code,) = struct.unpack_from(">I", body, 0)
            if code == 0:
                print("AUTH OK — password works!")
                break
            elif code == 5:
                md5_auth(sock, body[4:8])
            elif code == 10:
                # SASL mechanisms list
                mechs = body[4:].rstrip(b"\x00").decode(errors="replace")
                print("SASL mechanisms:", mechs)
                scram_sha_256(sock)
            elif code in (2, 11, 12):
                pass  # continuation handled inside scram
            elif code == 3:
                send_msg(sock, b"p", PASSWORD.encode() + b"\x00")
            else:
                print("auth code", code, body[:60])
        elif t == b"E":
            print("AUTH FAILED:", body.decode("utf-8", "replace")[:200])
            return
    # run test query
    q = b"SELECT current_user, current_database(), now()"
    sock.sendall(b"Q" + struct.pack(">I", 4 + len(q) + 1) + q + b"\x00")
    while True:
        t, body = recv_msg(sock)
        if t == b"D":
            print("query result:", body)
        if t == b"Z":
            break
        if t == b"E":
            print("query error:", body[:200])
            break
    sock.close()

if __name__ == "__main__":
    main()

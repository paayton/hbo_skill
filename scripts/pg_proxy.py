# -*- coding: utf-8 -*-
"""
PostgreSQL MITM proxy for HBO client (single-reader design).
- 127.0.0.1:15432 -> 120.24.174.5:5432, passthrough + SQL log.
- inject_queue.txt: one SQL per line; results -> inject_results.jsonl.
Only s2c reads from the server; injected-query responses are consumed there.
"""
import asyncio, json, os, struct, time

HERE = os.path.dirname(os.path.abspath(__file__))
REAL_HOST, REAL_PORT = "120.24.174.5", 5432
LISTEN = ("127.0.0.1", 15432)
QUEUE = os.path.join(HERE, "inject_queue.txt")
RESULTS = os.path.join(HERE, "inject_results.jsonl")
SQLLOG = os.path.join(HERE, "sql_log.txt")
PLOG = os.path.join(HERE, "proxy.log")

def plog(msg):
    with open(PLOG, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")

def log_sql(tag, sql):
    with open(SQLLOG, "a", encoding="utf-8") as f:
        f.write(f"\n### {tag} ###\n{sql}\n")

def append_result(obj):
    with open(RESULTS, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")

def decode_row(body):
    (n,) = struct.unpack_from(">H", body, 0)
    p = 2
    row = []
    for _ in range(n):
        (ln,) = struct.unpack_from(">i", body, p)
        p += 4
        if ln == -1:
            row.append(None)
        else:
            row.append(body[p:p+ln].decode("utf-8", "replace"))
            p += ln
    return row

def decode_rowdesc(body):
    (n,) = struct.unpack_from(">H", body, 0)
    p = 2
    cols = []
    for _ in range(n):
        end = body.index(b"\x00", p)
        cols.append(body[p:end].decode("utf-8", "replace"))
        p = end + 1 + 18
    return cols

class Conn:
    def __init__(self, cr, cw, sr, sw):
        self.cr, self.cw, self.sr, self.sw = cr, cw, sr, sw
        self.authed = False
        self.client_inflight = 0
        self.injecting = None   # dict(columns,rows,error) while our query runs
        self.inject_pending = False
        self.c2s_buf = b""
        self.s2c_buf = b""
        self.startup_pending = True
        self.dead = False

async def try_start_inject(c: Conn):
    """Called when connection idle (authed, no inflight): run one queued SQL."""
    if c.injecting or c.client_inflight > 0 or not c.authed:
        return
    try:
        with open(QUEUE, encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return
    for idx, line in enumerate(lines):
        sql = line.strip()
        if not sql:
            continue
        # remove line from queue first (avoid re-run on crash)
        try:
            with open(QUEUE, encoding="utf-8") as f:
                cur = f.readlines()
            del cur[idx]
            with open(QUEUE, "w", encoding="utf-8") as f:
                f.writelines(cur)
        except Exception as e:
            plog(f"queue update failed: {e}")
            return
        plog(f"inject: {sql[:90]}")
        data = b"Q" + struct.pack(">I", 4 + len(sql.encode()) + 1) + sql.encode() + b"\x00"
        c.sw.write(data)
        await c.sw.drain()
        c.injecting = {"sql": sql, "columns": [], "rows": [], "error": None,
                       "ts": time.strftime("%H:%M:%S")}
        return

async def c2s_loop(c: Conn):
    try:
        while True:
            data = await c.cr.read(65536)
            if not data:
                break
            c.sw.write(data)
            await c.sw.drain()
            c.c2s_buf += data
            # parse client messages: track query inflight
            buf = c.c2s_buf
            off = 0
            while True:
                if c.startup_pending:
                    if len(buf) < 4: break
                    (length,) = struct.unpack_from(">I", buf, 0)
                    if length < 8 or length > 1 << 20 or len(buf) < length:
                        break
                    c.startup_pending = False
                    off = length
                    continue
                if len(buf) - off < 5: break
                mtype = buf[off:off+1]
                (length,) = struct.unpack_from(">I", buf, off + 1)
                if length < 4 or off + 1 + length > len(buf): break
                body = buf[off+5: off+1+length]
                if mtype == b"Q":
                    log_sql("CLIENT-Q", body[:-1].decode("utf-8", "replace"))
                    c.client_inflight += 1
                elif mtype == b"P":
                    parts = body.split(b"\x00", 2)
                    log_sql("CLIENT-P", (parts[1] if len(parts) > 1 else b"?").decode("utf-8", "replace"))
                    c.client_inflight += 1
                off += 1 + length
            c.c2s_buf = buf[off:]
            # a client query may have been fully sent and answered already; nothing else here
    except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
        pass
    finally:
        c.dead = True
        try: c.sw.write_eof()
        except Exception: pass

async def s2c_loop(c: Conn):
    try:
        while True:
            data = await c.sr.read(65536)
            if not data:
                break
            c.s2c_buf += data
            buf = c.s2c_buf
            out = bytearray()   # bytes to forward to client
            off = 0
            while True:
                if len(buf) - off < 5: break
                mtype = buf[off:off+1]
                (length,) = struct.unpack_from(">I", buf, off + 1)
                if length < 4 or off + 1 + length > len(buf): break
                body = buf[off+5: off+1+length]
                msg = buf[off: off+1+length]
                if c.injecting is not None:
                    # consume our query's response
                    if mtype == b"T":
                        c.injecting["columns"] = decode_rowdesc(body)
                    elif mtype == b"D":
                        c.injecting["rows"].append(decode_row(body))
                    elif mtype == b"E":
                        parts = body.split(b"\x00")
                        c.injecting["error"] = parts[2].decode("utf-8", "replace") if len(parts) > 2 else "error"
                    elif mtype == b"Z":
                        res = c.injecting
                        c.injecting = None
                        append_result(res)
                        plog(f"inject done: {len(res['rows'])} rows, err={res['error']}")
                        asyncio.ensure_future(try_start_inject(c))
                    # all other messages while injecting are ignored (rare)
                else:
                    out += msg
                    if mtype == b"R" and len(body) >= 4:
                        (code,) = struct.unpack_from(">I", body, 0)
                        if code == 0 and not c.authed:
                            c.authed = True
                            plog("auth OK")
                            asyncio.ensure_future(try_start_inject(c))
                    elif mtype == b"Z":
                        if c.client_inflight > 0:
                            c.client_inflight -= 1
                off += 1 + length
            c.s2c_buf = buf[off:]
            if out:
                c.cw.write(bytes(out))
                await c.cw.drain()
    except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
        pass
    finally:
        c.dead = True
        if c.injecting is not None:
            r = c.injecting; r["error"] = "connection lost"
            append_result(r)
        try: c.cw.write_eof()
        except Exception: pass

async def handle_client(cr, cw):
    try:
        await _handle_client(cr, cw)
    except Exception as e:
        plog(f"handle_client crashed: {type(e).__name__}: {e}")

async def _handle_client(cr, cw):
    try:
        sr, sw = await asyncio.open_connection(REAL_HOST, REAL_PORT)
    except Exception as e:
        plog(f"connect real failed: {e}")
        cw.close(); return
    c = Conn(cr, cw, sr, sw)
    plog("client connected")
    async def poller():
        while not c.dead:
            await asyncio.sleep(0.5)
            try:
                await try_start_inject(c)
            except Exception as e:
                plog(f"poller error: {e}")
    t0 = asyncio.create_task(poller())
    t1 = asyncio.create_task(c2s_loop(c))
    t2 = asyncio.create_task(s2c_loop(c))
    await asyncio.wait([t1, t2], return_when=asyncio.FIRST_COMPLETED)
    for t in (t0, t1, t2): t.cancel()
    cw.close(); sw.close()
    plog("client disconnected")

async def main():
    server = await asyncio.start_server(handle_client, *LISTEN)
    plog(f"proxy listening on {LISTEN} -> {REAL_HOST}:{REAL_PORT}")
    async with server:
        await server.serve_forever()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

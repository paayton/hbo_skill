# -*- coding: utf-8 -*-
"""HBO 取数环境管理：自检 / 切代理 / 恢复收尾。

用法（Windows，改注册表需管理员权限）：
  python scripts/hbo_env.py check      # 自检：DSN 指向、代理端口、连接状态
  python scripts/hbo_env.py to-proxy   # 备份并切换 DSN 到 127.0.0.1:15432，启动代理
  python scripts/hbo_env.py restore    # 恢复 DSN 到真实库，关闭代理（取数后必须执行）

每一步都会打印「成功标志」和「下一步该做什么」，照着输出走即可。
"""
import ctypes, os, re, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
DSN_KEY = r'HKLM\SOFTWARE\WOW6432Node\ODBC\ODBC.INI\HYTPGS'
REAL_SERVER, REAL_PORT = '120.24.174.5', '5432'
PROXY_SERVER, PROXY_PORT = '127.0.0.1', '15432'
BACKUP_REG = os.path.join(HERE, '..', 'dsn_backup.reg')


def run(cmd):
    """以参数列表形式调用，避开 Git Bash 引号问题。"""
    p = subprocess.run(cmd, capture_output=True)
    out = (p.stdout + p.stderr).decode('gbk', 'replace')
    return p.returncode, out


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def dsn_now():
    """返回 (Servername, Port)，读不到返回 (None, None)。"""
    vals = {}
    for v in ('Servername', 'Port'):
        rc, out = run(['reg', 'query', DSN_KEY, '/v', v])
        m = re.search(v + r'\s+REG_SZ\s+(\S+)', out)
        vals[v] = m.group(1) if m else None
    return vals['Servername'], vals['Port']


def proxy_pid():
    """监听 15432 的进程 PID，没有返回 None。"""
    rc, out = run(['netstat', '-ano'])
    for line in out.splitlines():
        if ':15432' in line and 'LISTENING' in line:
            parts = line.split()
            return parts[-1]
    return None


def proxy_has_client():
    """代理上是否有已建立的 HBO 客户端连接。"""
    rc, out = run(['netstat', '-ano'])
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[1].endswith(':15432') and 'ESTABLISHED' in line:
            return True
    return False


def set_dsn(server, port):
    for v, d in (('Servername', server), ('Port', port)):
        rc, out = run(['reg', 'add', DSN_KEY, '/v', v, '/t', 'REG_SZ', '/d', d, '/f'])
        if rc != 0:
            print('改注册表失败：', out.strip())
            print('原因通常是权限不足——请用「管理员身份运行」的终端再试。')
            sys.exit(1)


def cmd_check():
    srv, port = dsn_now()
    pid = proxy_pid()
    print('=== HBO 取数环境自检 ===')
    print('DSN 当前指向: %s:%s' % (srv or '?', port or '?'))
    print('代理(15432): %s' % ('正在监听，PID=%s' % pid if pid else '未运行'))
    print('HBO 客户端已连上代理: %s' % ('是' if proxy_has_client() else '否'))
    print()
    if (srv, port) == (REAL_SERVER, REAL_PORT) and not pid:
        print('状态：正常（未在取数）。要取数就执行：python scripts/hbo_env.py to-proxy')
    elif (srv, port) == (PROXY_SERVER, PROXY_PORT) and pid:
        print('状态：取数模式。HBO 客户端需【完全退出再打开】并登录，然后跑 fetch_hbo.py 取数。')
        print('取数完成后必须执行：python scripts/hbo_env.py restore')
    else:
        print('状态：异常（DSN 与代理不一致）！HBO 会连不上数据库。')
        print('修复：python scripts/hbo_env.py restore')


def cmd_to_proxy():
    if not is_admin():
        print('需要管理员权限。请右键终端选「以管理员身份运行」后再试。')
        sys.exit(1)
    srv, port = dsn_now()
    if (srv, port) == (PROXY_SERVER, PROXY_PORT) and proxy_pid():
        print('已是取数模式，无需重复切换。')
        return
    # 1. 备份
    rc, out = run(['reg', 'export', DSN_KEY, os.path.abspath(BACKUP_REG), '/y'])
    print('1. DSN 已备份到 dsn_backup.reg' if rc == 0 else '1. 备份失败（不阻断）：' + out.strip())
    # 2. 切 DSN
    set_dsn(PROXY_SERVER, PROXY_PORT)
    print('2. DSN 已切到 127.0.0.1:15432')
    # 3. 起代理（独立进程，不占用本终端）
    if not proxy_pid():
        flags = 0x00000008 | 0x08000000  # DETACHED_PROCESS | CREATE_NO_WINDOW
        subprocess.Popen([sys.executable, os.path.join(HERE, 'pg_proxy.py')],
                         cwd=HERE, creationflags=flags, close_fds=True)
        time.sleep(1)
    if proxy_pid():
        print('3. 代理已启动，正在监听 15432')
    else:
        print('3. 代理启动失败！看 scripts/proxy_err.log / proxy.log')
        sys.exit(1)
    print()
    print('下一步：让使用人【完全退出 HBO 客户端再打开】并正常登录。')
    print('登录成功后跑：python scripts/hbo_env.py check 确认「客户端已连上代理: 是」')


def cmd_restore():
    if not is_admin():
        print('需要管理员权限。请右键终端选「以管理员身份运行」后再试。')
        sys.exit(1)
    pid = proxy_pid()
    if pid:
        run(['taskkill', '/F', '/PID', pid])
        time.sleep(1)
        print('1. 代理已关闭' if not proxy_pid() else '1. 代理关闭失败，PID=' + pid)
    else:
        print('1. 代理本来就没在运行')
    set_dsn(REAL_SERVER, REAL_PORT)
    srv, port = dsn_now()
    if (srv, port) == (REAL_SERVER, REAL_PORT):
        print('2. DSN 已恢复为 %s:%s' % (REAL_SERVER, REAL_PORT))
        print()
        print('收尾完成。提醒使用人：HBO 客户端要【完全退出再打开】才能重新直连真实库。')
    else:
        print('2. DSN 恢复失败，当前是 %s:%s，请手工检查！' % (srv, port))
        sys.exit(1)


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'check'
    {'check': cmd_check, 'to-proxy': cmd_to_proxy, 'restore': cmd_restore}.get(
        cmd, lambda: print('未知命令 %s，用 check / to-proxy / restore' % cmd))()

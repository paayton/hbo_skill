# -*- coding: utf-8 -*-
"""HBO 取数一条龙：对账单 -> SONO -> 注入 SQL -> fees.csv / jobs.csv。

前提：已执行 hbo_env.py to-proxy，且使用人已重启 HBO 并登录
     （hbo_env.py check 显示「客户端已连上代理: 是」）。

用法：
  python scripts/fetch_hbo.py --invoice 对账单.xlsx --client 猩航
  python scripts/fetch_hbo.py --invoice 对账单.xlsx --clientid 1263631068

产出（写在对账单所在目录）：
  fees.csv  HBO 费用明细（rptype=1 应付、已剔除作废行）
  jobs.csv  SONO -> 工作号映射
  并打印下一步该执行的 reconcile.py 命令。

已知口径（2026-10-14 实测）：
  - 工作单表 job_shipping_2026（老单在 job_shipping），sono 匹配，isdeleted='Y' 是作废单
  - 费用表 stl_arap_2026，feeid 关联 fil_feeitem 取 feename_c
  - 费用表没有 payno 列，用 paypass 近似：'N'/空 = 未付款，其他值 = 已付款剔除（语义未和业务确认，遇大量剔除先问用户）
"""
import argparse, csv, json, os, re, socket, sys, time

import openpyxl

HERE = os.path.dirname(os.path.abspath(__file__))
QUEUE = os.path.join(HERE, 'inject_queue.txt')
RESULTS = os.path.join(HERE, 'inject_results.jsonl')

# 客户简称 -> (clientid, settle_abbr)。识别不了的客户不要猜，问用户后把 clientid 直接传给 --clientid。
CLIENTS = {
    '猩航': ('85500691068', 'SHXINHAN'),
    '奥吉': ('1280701068', 'STACIAGYE'),
    '万海': ('1249221068', 'SHEUNISHG'),
}

# 文件人员 = 委托单 sal_ordinfo.documentby（工作单界面显示的"文件人员"，2026-10-15 用用户标注
# 15 个工作号对照验证 15/15 命中；lastupduser 是"最后修改人"，不是文件人员）。
# job_shipping_2026 / job_shipping 都按 jobid 关联；2026 委托单没有时兜底历史表 _sal_ordinfo，
# 再兜底 lastupduser。documentby 为 0/空表示未填。
# 注意：sal_ordinfo 一个 jobid 可能有多行（分单），必须先 GROUP BY jobid 收拢，直接 JOIN 会笛卡尔积爆行。
_DOC_JOIN = ("LEFT JOIN (SELECT jobid, MAX(NULLIF(documentby, '0')) AS documentby "
             "FROM public.sal_ordinfo_2026 GROUP BY jobid) s ON s.jobid = j.jobid "
             "LEFT JOIN (SELECT jobid, MAX(NULLIF(documentby, '0')) AS documentby "
             "FROM public._sal_ordinfo GROUP BY jobid) s2 ON s2.jobid = j.jobid ")
_DOC_SEL = ("COALESCE(NULLIF(s.documentby, '0'), NULLIF(s2.documentby, '0'), j.lastupduser) AS lastupduser")
SQL_JOBS = ("SELECT j.jobid, j.sono, j.blnoh, j.vessel, j.voyage, j.createuser, " + _DOC_SEL + ", j.isdeleted "
            "FROM {table} j " + _DOC_JOIN + "WHERE j.sono IN ({inlist});")
SQL_JOBS_SUFFIX = ("SELECT j.jobid, j.sono, j.blnoh, j.vessel, j.voyage, j.createuser, " + _DOC_SEL + ", j.isdeleted "
                   "FROM {table} j " + _DOC_JOIN + "WHERE rtrim(j.sono) ~ '({inlist})$';")
SQL_FEES = ("SELECT a.jobid, a.rptype, a.clientid, f.feename_c, a.cyid, a.amt, a.amtstl, "
            "a.rpdate, a.paypass, a.billno, a.createuser, a.isdeleted "
            "FROM stl_arap_2026 a LEFT JOIN fil_feeitem f ON f.feeid = a.feeid "
            "WHERE a.clientid = '{clientid}' AND a.jobid IN "
            "(SELECT jobid FROM job_shipping_2026 WHERE sono IN ({inlist}));")
SQL_FEES_JOBS = ("SELECT a.jobid, a.rptype, a.clientid, f.feename_c, a.cyid, a.amt, a.amtstl, "
                 "a.rpdate, a.paypass, a.billno, a.createuser, a.isdeleted "
                 "FROM stl_arap_2026 a LEFT JOIN fil_feeitem f ON f.feeid = a.feeid "
                 "WHERE a.clientid = '{clientid}' AND a.jobid IN ({inlist});")


def norm_sono(raw, sonos):
    """把 HBO 原始 sono（可能带船司前缀/尾部空格，如 'COSU6461707920 '）映射回对账单的 SONO。"""
    s = (raw or '').strip()
    if s in sonos:
        return s
    for t in sonos:
        if s.endswith(t):
            return t
    return None


def die(msg):
    print('【失败】' + msg)
    sys.exit(1)


def proxy_alive():
    s = socket.socket()
    s.settimeout(2)
    try:
        s.connect(('127.0.0.1', 15432))
        return True
    except OSError:
        return False
    finally:
        s.close()


def read_sonos(path):
    """从对账单/开票明细提取唯一 SONO 列表（自动定位表头行，跳过合计页脚，拆开逗号合并单元格）。

    返回 (sonos, book_map)：
      - sonos    唯一 SONO 列表。猩航/奥吉的列叫 SONO/提单号；万海账单叫 So_No。
      - book_map {Book_No: So_No}（万海账单有 Book_No 列，HBO 录单可能录订舱号，兜底反查用）。
    """
    ws = openpyxl.load_workbook(path).active
    header_row = c_sono = c_book = None
    for r in range(1, min(11, ws.max_row + 1)):
        vals = [str(c.value or '').strip() for c in ws[r]]
        for i, v in enumerate(vals):
            lv = v.lower()
            if v == 'SONO' or v == '提单号' or 'SONO' in v or lv == 'so_no':
                header_row, c_sono = r, i + 1
            elif lv == 'book_no':
                c_book = i + 1
        if header_row:
            break
    if header_row is None:
        die('前 10 行内找不到「提单号/SONO/So_No」列，这不是支持的对账单格式。把表头截图发给用户确认。')
    sonos, book_map = [], {}
    for r in range(header_row + 1, ws.max_row + 1):
        v = ws.cell(r, c_sono).value
        if not v:
            continue
        for s in str(v).replace('，', ',').split(','):
            s = s.strip()
            if s and '合计' not in s and s not in sonos:
                sonos.append(s)
                if c_book:
                    b = ws.cell(r, c_book).value
                    if b:
                        book_map[str(b).strip()] = s
    if not sonos:
        die('对账单里没读到任何 SONO，请人工检查文件。')
    return sonos, book_map


def queue_and_wait(sqls, expect, timeout=300):
    """把 SQL 写入队列，等 expect 条新结果。返回新结果列表。"""
    baseline = 0
    if os.path.exists(RESULTS):
        with open(RESULTS, encoding='utf-8') as f:
            baseline = sum(1 for _ in f)
    with open(QUEUE, 'w', encoding='utf-8') as f:
        f.write('\n'.join(sqls) + '\n')
    print('已注入 %d 条 SQL，等待 HBO 会话执行（基线 %d 行）...' % (len(sqls), baseline))
    t0 = time.time()
    while time.time() - t0 < timeout:
        if os.path.exists(RESULTS):
            with open(RESULTS, encoding='utf-8') as f:
                lines = f.readlines()
            if len(lines) >= baseline + expect:
                return [json.loads(x) for x in lines[baseline:baseline + expect]]
        time.sleep(3)
    die('等了 %d 秒没等到结果。排查：1) hbo_env.py check 是否显示「客户端已连上代理: 是」'
        '（不是的话让用户重启 HBO 再登录）；2) 看 scripts/proxy.log 最后几行。' % timeout)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--invoice', required=True, help='对账单/开票明细 xlsx 路径')
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--client', help='客户简称：' + ' / '.join(CLIENTS))
    g.add_argument('--clientid', help='HBO 客户 ID（简称表里没有时用）')
    ap.add_argument('--settle-abbr', default='', help='配合 --clientid 使用的客户英文简称')
    ap.add_argument('--timeout', type=int, default=300)
    a = ap.parse_args()

    if a.client:
        clientid, abbr = CLIENTS[a.client]
    else:
        clientid, abbr = a.clientid, a.settle_abbr

    if not proxy_alive():
        die('代理没在监听 15432。先执行：python scripts/hbo_env.py to-proxy，'
            '再让用户【完全退出 HBO 再打开】并登录。')

    outdir = os.path.dirname(os.path.abspath(a.invoice))
    sonos, book_map = read_sonos(a.invoice)
    print('从 %s 读到 %d 个唯一 SONO' % (a.invoice, len(sonos)))
    inlist = ','.join("'%s'" % s for s in sonos)

    # 第 1 轮：工作单映射 + 费用明细
    jobs_res, fees_res = queue_and_wait(
        [SQL_JOBS.format(table='job_shipping_2026', inlist=inlist),
         SQL_FEES.format(clientid=clientid, inlist=inlist)], expect=2, timeout=a.timeout)
    if 'jobid' not in jobs_res['columns'] or 'sono' not in jobs_res['columns']:
        jobs_res, fees_res = fees_res, jobs_res  # 顺序保险
    for name, res in (('工作单', jobs_res), ('费用', fees_res)):
        if res['error']:
            die('%s查询报错：%s。把 scripts/inject_results.jsonl 最后一条发给用户排查。' % (name, res['error']))
    print('工作单查到 %d 行，费用查到 %d 行' % (len(jobs_res['rows']), len(fees_res['rows'])))

    # 第 2 轮：缺失 SONO 依次兜底：2026 表后缀匹配 → 老表精确 → 老表后缀
    # （奥吉的 sono 在 HBO 里带船司前缀，如 'COSU6461707920 '，精确 IN 查不到）
    ci = jobs_res['columns'].index
    fallback_jobids = []

    def matched():
        return {norm_sono(r[ci('sono')], sonos) for r in jobs_res['rows']} - {None}

    def suffix_inlist(items):
        return '|'.join(re.escape(s) for s in items)

    missing = [s for s in sonos if s not in matched()]
    if missing:
        print('%d 个 SONO 精确查无，改用后缀匹配（前缀形如 COSU...）...' % len(missing))
        r = queue_and_wait([SQL_JOBS_SUFFIX.format(
            table='job_shipping_2026', inlist=suffix_inlist(missing))],
            expect=1, timeout=a.timeout)[0]
        if r['error']:
            die('后缀匹配查询报错：%s。把 scripts/inject_results.jsonl 最后一条发给用户排查。' % r['error'])
        before = len(jobs_res['rows'])
        jobs_res['rows'] += r['rows']
        fallback_jobids += [x[ci('jobid')] for x in r['rows']]
        print('后缀匹配补到 %d 行工作单' % (len(jobs_res['rows']) - before))

    missing = [s for s in sonos if s not in matched()]
    if missing and book_map:
        books = [b for b, s in book_map.items() if s in missing]
        if books:
            print('%d 个 SONO 仍未命中，改用 Book_No（订舱号）反查（万海录单可能录的是订舱号）...' % len(missing))
            binlist = ','.join("'%s'" % b for b in books)
            rs = queue_and_wait(
                [SQL_JOBS.format(table='job_shipping_2026', inlist=binlist),
                 SQL_JOBS_SUFFIX.format(table='job_shipping_2026',
                                        inlist='|'.join(re.escape(b) for b in books))],
                expect=2, timeout=a.timeout)
            added = 0
            for r in rs:
                if r['error']:
                    die('Book_No 反查报错：%s。把 scripts/inject_results.jsonl 最后一条发给用户排查。' % r['error'])
                for row in r['rows']:
                    raw = (row[ci('sono')] or '').strip()
                    hit = raw if raw in book_map else None
                    if not hit:
                        for b in book_map:
                            if raw.endswith(b):
                                hit = b
                                break
                    if hit and book_map[hit] not in matched():
                        row[ci('sono')] = book_map[hit]  # 归一化回对账单 So_No
                        jobs_res['rows'].append(row)
                        fallback_jobids.append(row[ci('jobid')])
                        added += 1
            print('Book_No 反查补到 %d 行工作单' % added)

    missing = [s for s in sonos if s not in matched()]
    if missing:
        print('%d 个 SONO 在 job_shipping_2026 查无，补查老表 job_shipping...' % len(missing))
        old = queue_and_wait(
            [SQL_JOBS.format(table='job_shipping',
                             inlist=','.join("'%s'" % s for s in missing)),
             SQL_JOBS_SUFFIX.format(table='job_shipping',
                                    inlist=suffix_inlist(missing))],
            expect=2, timeout=a.timeout)
        for r in old:
            if r['error']:
                die('老表查询报错：%s。把 scripts/inject_results.jsonl 最后一条发给用户排查。' % r['error'])
            jobs_res['rows'] += r['rows']
            fallback_jobids += [x[ci('jobid')] for x in r['rows']]
        still = [s for s in sonos if s not in matched()]
        if still:
            print('仍有 %d 个 SONO 查无工作单（对账时标红）：%s' % (len(still), ', '.join(still)))

    # 兜底轮找到的工作单，第 1 轮费用子查询没覆盖，按 jobid 补拉费用
    if fallback_jobids:
        r = queue_and_wait([SQL_FEES_JOBS.format(
            clientid=clientid,
            inlist=','.join("'%s'" % j for j in dict.fromkeys(fallback_jobids)))],
            expect=1, timeout=a.timeout)[0]
        if r['error']:
            die('补拉费用报错：%s。把 scripts/inject_results.jsonl 最后一条发给用户排查。' % r['error'])
        fees_res['rows'] += r['rows']
        print('按 jobid 补拉费用 %d 行' % len(r['rows']))

    # 文件人员（jobs.csv 的 doc 列）已在 SQL 里取 sal_ordinfo.documentby（见 SQL_JOBS 注释），
    # 这里仍是数字工号 -> 英文代码（usercode，如 JANELIAO）查 app_users。
    lu_ids = sorted({str(r[ci('lastupduser')]) for r in jobs_res['rows']
                     if str(r[ci('lastupduser')] or '') not in ('', '0')})
    usercode_by_id = {}
    if lu_ids:
        ru = queue_and_wait([
            "SELECT userid, usercode FROM app_users WHERE CAST(userid AS TEXT) IN (%s);"
            % ','.join("'%s'" % x for x in lu_ids)], expect=1, timeout=a.timeout)[0]
        if ru['error']:
            print('警告：app_users 查询报错 %s，文件人员先填数字工号' % ru['error'])
        else:
            usercode_by_id = {str(uid): (uc or '') for uid, uc in ru['rows']}

    # 写 jobs.csv（剔除作废单 isdeleted='Y'；sono 归一化回对账单口径；jobno 用 blnoh 工作号如 GOSZX…）
    jobs_csv = os.path.join(outdir, 'jobs.csv')
    n = 0
    seen = set()
    blnoh_by_jobid = {}
    with open(jobs_csv, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        w.writerow(['sono', 'jobno', 'doc'])
        for r in jobs_res['rows']:
            if r[ci('isdeleted')] == 'Y':
                continue
            if str(r[ci('jobid')]).strip() in ('0', ''):
                continue  # jobid=0 的是假单/占位记录（如分单后缀 B/C），无费用，不进映射
            blnoh_by_jobid[r[ci('jobid')]] = r[ci('blnoh')] or r[ci('jobid')]
            s = norm_sono(r[ci('sono')], sonos) or (r[ci('sono')] or '').strip()
            key = (s, r[ci('jobid')])
            if key in seen:
                continue
            seen.add(key)
            lu = str(r[ci('lastupduser')] or '')
            w.writerow([s, r[ci('blnoh')] or r[ci('jobid')],
                        usercode_by_id.get(lu, lu)])
            n += 1

    # 写 fees.csv（剔除作废费用；paypass 'N'/空 -> payno 空）
    fi = fees_res['columns'].index
    fees_csv = os.path.join(outdir, 'fees.csv')
    m = 0
    with open(fees_csv, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        w.writerow(['jobno', 'rptype', 'feename_c', 'cyid', 'amt', 'amtstl',
                    'rpdate', 'payno', 'documentby', 'settle_abbr'])
        for r in fees_res['rows']:
            if r[fi('isdeleted')] == 'Y':
                continue
            paypass = r[fi('paypass')]
            payno = '' if paypass in ('N', None, '') else str(paypass)
            w.writerow([blnoh_by_jobid.get(r[fi('jobid')], r[fi('jobid')]), r[fi('rptype')], r[fi('feename_c')], r[fi('cyid')],
                        r[fi('amt')], r[fi('amtstl')], str(r[fi('rpdate')])[:10], payno,
                        r[fi('createuser')], abbr])
            m += 1
    print('已写出 %s（%d 行）、%s（%d 行）' % (jobs_csv, n, fees_csv, m))

    out_xlsx = os.path.join(outdir, 'output', a.client or abbr or '其他',
                            os.path.splitext(os.path.basename(a.invoice))[0] + '_对账结果.xlsx')
    os.makedirs(os.path.dirname(out_xlsx), exist_ok=True)
    print()
    print('下一步（照抄执行）：')
    print('  python scripts/reconcile.py --invoice "%s" --fees "%s" --jobs "%s" --out "%s"'
          % (a.invoice, fees_csv, jobs_csv, out_xlsx))
    print('注意：开票明细（猩航）不要加 --cutoff；对账单（奥吉）才加 --cutoff 确认日。')
    print('对账完成后必须收尾：python scripts/hbo_env.py restore')


if __name__ == '__main__':
    main()

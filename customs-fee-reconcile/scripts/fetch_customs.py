# -*- coding: utf-8 -*-
"""报关费取数：rows.csv -> 注入 SQL -> jobs.csv / fees.csv。

前提：hbo_env.py to-proxy 已执行、使用人已重启 HBO 并登录（与 fetch_hbo.py 同一套代理）。

口径（2026-10-20 探库定版）：
  - 结算单位：上海宇中报关行 SHAYUNSHG = clientid 1332541068（crm_client.clicode 解析验证：
    cliname_c = SHANGHAI YUNSHU CUSTOMS DECLARATION OFFICE）
  - 费用名目：fil_feeitem.feename_c = '清关费/报关费'（feeid=1347041068；字典里另有
    进口清查费/出口报关费等，实测该客户名下只出现'清关费/报关费'，reconcile 侧再过滤一道）
  - 应付 rptype=1；isdeleted='Y' 剔除；paypass='N'/空=未销账，其他=已销账剔除（用户定版沿用）
  - HBO 无报关单号数据（job_declinfo 全空，2026-10-20 验证），匹配只能靠提运单号(sono)
  - 工作号显示 = blnoh；jobid=0 的是假单/分单占位，无费用，剔除

用法：
  python fetch_customs.py --scripts-dir "F:\\hbo-reconcile-main\\hbo-reconcile-main\\scripts" \
      --rows 账单_rows.csv
产出（rows.csv 所在目录）：jobs.csv（sono,jobno,jobid）、
fees.csv（jobid,rptype,feename_c,amt,paypass,rpdate,usercode；名目/应付/未作废/未销账
已在 SQL 层过滤，fees.csv 每行都是有效费用）
"""
import argparse
import csv
import json
import os
import sys

CLIENTID = '1332541068'      # SHAYUNSHG 上海宇中报关行
FEENAME = '清关费/报关费'
FEEID = '1347041068'         # fil_feeitem.feename_c='清关费/报关费'（2026-10-20 探库验证）

# 工作单一条 SQL 搞定三种匹配（精确 / 去尾空格 / 后缀正则），不再分两条往返
# serviceby = 委托单 sal_ordinfo 的"操作"（客服，用户 2026-10-21 16:31 在 HBO 界面核对定版；
# 注意不是 documentby 文件人员、不是 opr_decby 报关操作——这两个字段实测多为空）。
# GROUP BY jobid 收拢防分单笛卡尔积；0/空=未填
_DOC_JOIN = ("LEFT JOIN (SELECT jobid, MAX(NULLIF(serviceby, '0')) AS serviceby "
             "FROM public.sal_ordinfo_2026 GROUP BY jobid) s ON s.jobid = j.jobid "
             "LEFT JOIN (SELECT jobid, MAX(NULLIF(serviceby, '0')) AS serviceby "
             "FROM public._sal_ordinfo GROUP BY jobid) s2 ON s2.jobid = j.jobid ")
SQL_JOBS = ("SELECT j.jobid, j.sono, j.blnoh, "
            "COALESCE(NULLIF(s.serviceby, '0'), NULLIF(s2.serviceby, '0')) AS op, "
            "j.isdeleted FROM {table} j " + _DOC_JOIN +
            "WHERE j.sono IN ({inlist}) OR rtrim(j.sono) IN ({inlist}) "
            "OR rtrim(j.sono) ~ '({alts})$';")
# 费用在 SQL 层锁定名目/应付/未作废/未销账，不 JOIN 费用字典、不拉无关名目，
# 传回来的每一行都是要的（reconcile 侧仍保留同样过滤做双保险，兼容旧 fees.csv）
SQL_FEES = ("SELECT a.jobid, a.rptype, a.amt, a.paypass, a.rpdate, a.createuser "
            "FROM stl_arap_2026 a "
            "WHERE a.clientid = '{cid}' AND a.feeid = '{feeid}' AND a.rptype = '1' "
            "AND (a.isdeleted IS NULL OR a.isdeleted <> 'Y') "
            "AND (a.paypass IS NULL OR a.paypass IN ('N','')) "
            "AND a.jobid IN ({jobids});")


def inlist(items):
    return ','.join("'%s'" % str(x).replace("'", '') for x in items)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--scripts-dir', required=True, help='工作区 hbo-reconcile-main/scripts 目录')
    ap.add_argument('--rows', required=True, help='customs_bill.py 产出的 rows.csv')
    ap.add_argument('--timeout', type=int, default=300)
    a = ap.parse_args()
    sys.path.insert(0, a.scripts_dir)
    import fetch_hbo as fh

    if not fh.proxy_alive():
        print('【失败】代理没在监听 15432。先 hbo_env.py to-proxy，让用户重启 HBO 再登录。')
        sys.exit(1)

    with open(a.rows, encoding='utf-8-sig') as f:
        rd = csv.DictReader(f)
        rows_in = list(rd)
        sonos = sorted({r['sono'].strip() for r in rows_in if r['sono'].strip()})
        # 箱号兜底用：sono -> 该行箱号列表（集装箱号列可能多箱，按非字母数字切分）
        import re as _re0
        boxes_by_sono = {}
        if 'container' in (rd.fieldnames or []):
            for r in rows_in:
                so = r['sono'].strip()
                for b in _re0.split(r'[^0-9A-Za-z]+', r.get('container') or ''):
                    if len(b) >= 6:
                        boxes_by_sono.setdefault(so, []).append(b.upper())
    if not sonos:
        print('【失败】rows.csv 里没有 sono')
        sys.exit(1)
    print('提运单号 %d 个' % len(sonos))

    jobs_rows = []
    for table in ('job_shipping_2026', 'job_shipping'):
        if jobs_rows:
            break
        rs = fh.queue_and_wait(
            [SQL_JOBS.format(table=table, inlist=inlist(sonos),
                             alts='|'.join(s.replace("'", '') for s in sonos))],
            expect=1, timeout=a.timeout)
        for res in rs:
            if res['error']:
                print('【失败】%s 查询报错：%s' % (table, res['error']))
                sys.exit(1)
            jobs_rows += res['rows']
    # 老表兜底（2026 表查无的 sono 才去老表）
    found = {(r[1] or '').strip() for r in jobs_rows}
    miss = [s for s in sonos if s not in found and not any(x.endswith(s) for x in found)]
    if miss:
        rs = fh.queue_and_wait(
            [SQL_JOBS.format(table='job_shipping', inlist=inlist(miss),
                             alts='|'.join(s.replace("'", '') for s in miss))],
            expect=1, timeout=a.timeout)
        for res in rs:
            if res['error']:
                print('【失败】老表查询报错：%s' % res['error'])
                sys.exit(1)
            jobs_rows += res['rows']

    # 箱号兜底反查（用户方法，优先级高于工作号尾段；用户 2026-10-21 定版"箱号是兜底方案"）：
    # 提运单号查无的，用账单箱号到 job_containers_2026 反查工作单。实证：WA666V20064
    # 的费用实际记在 sono=WA666V20057 的 GOSZX26011720 下（80 元），箱号一查即中；
    # 而工作号尾段反查会误配（GOSZX26020064 号码巧合但无费用）。
    found = {(r[1] or '').strip() for r in jobs_rows}
    miss = [s for s in sonos if s not in found and not any(x.endswith(s) for x in found)]
    box_rows = []   # (bill_sono, jobid, blnoh)
    box_miss = [s for s in miss if boxes_by_sono.get(s)]
    if box_miss:
        cond = ' OR '.join("c.cntno LIKE '%%%s%%'" % b
                           for s in box_miss for b in boxes_by_sono[s])
        rs = fh.queue_and_wait(
            [("SELECT c.cntno, j.jobid, j.sono, j.blnoh, j.isdeleted "
              "FROM job_containers_2026 c JOIN job_shipping_2026 j ON j.jobid = c.jobid "
              "WHERE %s;" % cond)],
            expect=1, timeout=a.timeout)
        for res in rs:
            if res['error']:
                print('【失败】箱号兜底查询报错：%s' % res['error'])
                sys.exit(1)
            hits_by_sono = {}
            for cntno, jobid, j_sono, blnoh, isdel in res['rows']:
                if isdel == 'Y' or str(jobid) in ('0', ''):
                    continue
                for s in box_miss:
                    if any(b in (cntno or '').upper() for b in boxes_by_sono[s]):
                        hits_by_sono.setdefault(s, {})[str(jobid)] = (blnoh or '').strip()
            for s, jobs in hits_by_sono.items():
                for jobid, blnoh in sorted(jobs.items()):
                    box_rows.append((s, jobid, blnoh or jobid))
                    print('箱号兜底命中：%s -> %s (%s)' % (s, blnoh, jobid))
        found |= {s for s, _, _ in box_rows}
        miss = [s for s in miss if s not in found]

    # 工作号尾段兜底反查（末位兜底；已有箱号兜底，此路只在箱号也查无时启用。
    # 注意：尾段反查有误配风险（实证 WA666V20064 误配 GOSZX26020064），
    # 命中工作单若名下无费用，行内标注"尾段反查命中，建议箱号人工复核"由 reconcile 侧处理）
    tail_rows = []   # (bill_sono, jobid, blnoh)
    if miss:
        import re as _re
        tail_by_sono = {}
        for s in miss:
            mt = _re.search(r'(\d{5,})$', s)
            if mt:
                tail_by_sono[s] = mt.group(1)
        if tail_by_sono:
            cond = ' OR '.join("blnoh LIKE '%%%s%%'" % t for t in set(tail_by_sono.values()))
            rs = fh.queue_and_wait(
                ["SELECT jobid, sono, blnoh, isdeleted FROM job_shipping_2026 WHERE %s;" % cond],
                expect=1, timeout=a.timeout)
            for res in rs:
                if res['error']:
                    print('【失败】工作号兜底查询报错：%s' % res['error'])
                    sys.exit(1)
                rows_by_tail = {}
                for jobid, sono, blnoh, isdel in res['rows']:
                    if isdel == 'Y' or str(jobid) in ('0', ''):
                        continue
                    blnoh = (blnoh or '').strip()
                    for s, t in tail_by_sono.items():
                        if t in blnoh:
                            rows_by_tail.setdefault(s, {})[str(jobid)] = blnoh
                for s, jobs in rows_by_tail.items():
                    # 优先采纳工作号【以尾段数字结尾】的（GOSZX26080096 型）；
                    # 同尾段多个（GOSHA/GOSZX 分部）全采纳——子集和判定容忍系统多记；
                    # 无结尾命中时仅唯一子串命中才采纳；其余歧义放弃
                    ends = {j: b for j, b in jobs.items()
                            if b.endswith(tail_by_sono[s])}
                    pick = ends if ends else (jobs if len(jobs) == 1 else {})
                    if pick:
                        for jobid, blnoh in sorted(pick.items()):
                            tail_rows.append((s, jobid, blnoh or jobid))
                            print('工作号兜底命中：%s -> %s (%s)' % (s, blnoh, jobid))
                    else:
                        print('工作号兜底歧义放弃：%s 命中 %d 个不同工作号'
                              % (s, len({b for b in jobs.values()})))

    bl_by_jobid = {}
    seen = set()
    op_by_jobid = {}          # jobid -> 文件人员数字工号（主查询 SELECT 带 doc）
    main_rows = []             # (sono, blnoh, jobid)
    out_rows = []              # (sono, blnoh, jobid) 含箱号/尾段兜底
    for jobid, sono, blnoh, op, isdel in jobs_rows:
        jobid, sono, blnoh = str(jobid), (sono or '').strip(), (blnoh or '').strip()
        if isdel == 'Y' or jobid in ('0', ''):
            continue  # 作废单、假单剔除
        key = (sono, jobid)
        if key in seen:
            continue
        seen.add(key)
        bl_by_jobid[jobid] = blnoh or jobid
        if op and str(op) not in ('0', 'None', ''):
            op_by_jobid[jobid] = str(op)
        main_rows.append((sono, blnoh or jobid, jobid))
    for s, jobid, blnoh in box_rows + tail_rows:
        key = (s, jobid)
        if key in seen:
            continue
        seen.add(key)
        bl_by_jobid[jobid] = blnoh
        out_rows.append((s, blnoh, jobid))   # sono 写账单单号，建立映射

    # 箱号/尾段兜底命中的 jobid 补查操作(serviceby)（sal_ordinfo 按 jobid 收拢，同 _DOC_JOIN 口径）
    need_op = [j for j in bl_by_jobid if j not in op_by_jobid]
    if need_op:
        rs = fh.queue_and_wait(
            ["SELECT jobid, MAX(NULLIF(serviceby, '0')) AS serviceby FROM "
             "public.sal_ordinfo_2026 WHERE jobid IN (%s) GROUP BY jobid;"
             % inlist(need_op),
             "SELECT jobid, MAX(NULLIF(serviceby, '0')) AS serviceby FROM "
             "public._sal_ordinfo WHERE jobid IN (%s) GROUP BY jobid;"
             % inlist(need_op)],
            expect=2, timeout=a.timeout)
        for res in rs:
            if res['error']:
                continue   # 补查失败不致命，doc 留空
            for jobid, d in res['rows']:
                if d and str(d) not in ('0', 'None', '') and str(jobid) not in op_by_jobid:
                    op_by_jobid[str(jobid)] = str(d)

    # 操作数字工号 -> 英文代码（app_users），写进 jobs.csv 的 op 列
    op_ids = sorted({d for d in op_by_jobid.values() if d not in ('0', 'None', '')})
    op_ucode = {}
    if op_ids:
        ru = fh.queue_and_wait(
            ["SELECT userid, usercode FROM app_users WHERE CAST(userid AS TEXT) IN (%s);"
             % ','.join("'%s'" % x for x in op_ids)],
            expect=1, timeout=a.timeout)[0]
        if not ru['error']:
            op_ucode = {str(uid): (uc or '') for uid, uc in ru['rows']}

    outdir = os.path.dirname(os.path.abspath(a.rows))
    jobs_csv = os.path.join(outdir, 'jobs.csv')
    n = 0
    with open(jobs_csv, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        w.writerow(['sono', 'jobno', 'jobid', 'op'])
        for sono, blnoh, jobid in main_rows + out_rows:
            w.writerow([sono, blnoh, jobid,
                        op_ucode.get(op_by_jobid.get(jobid, ''),
                                      op_by_jobid.get(jobid, ''))])
            n += 1
    print('jobs.csv %d 行（覆盖 %d/%d 个运单；op=工作单操作serviceby英文代码）'
          % (n, len({r[0] for r in seen}), len(sonos)))

    jobids = sorted(bl_by_jobid)
    fees_csv = os.path.join(outdir, 'fees.csv')
    m = 0
    if jobids:
        res = fh.queue_and_wait(
            [SQL_FEES.format(cid=CLIENTID, feeid=FEEID, jobids=inlist(jobids))],
            expect=1, timeout=a.timeout)[0]
        if res['error']:
            print('【失败】费用查询报错：%s' % res['error'])
            sys.exit(1)
        rows = res['rows']   # SQL 已锁定名目/应付/未作废/未销账
        # 录入人数字工号 -> 英文代码（app_users）
        ucode = {}
        op_ids = sorted({str(r[5]) for r in rows
                         if str(r[5] or '') not in ('', '0', 'None')})
        if op_ids:
            ru = fh.queue_and_wait(
                ["SELECT userid, usercode FROM app_users WHERE CAST(userid AS TEXT) IN (%s);"
                 % ','.join("'%s'" % x for x in op_ids)],
                expect=1, timeout=a.timeout)[0]
            if not ru['error']:
                ucode = {str(uid): (uc or '') for uid, uc in ru['rows']}
        with open(fees_csv, 'w', newline='', encoding='utf-8-sig') as f:
            w = csv.writer(f)
            w.writerow(['jobid', 'rptype', 'feename_c', 'amt', 'paypass', 'rpdate', 'usercode'])
            for jobid, rptype, amt, paypass, rpdate, createuser in rows:
                op = ucode.get(str(createuser), str(createuser or ''))
                w.writerow([str(jobid), rptype or '', FEENAME, amt or '',
                            '' if paypass in ('N', None, '') else str(paypass),
                            str(rpdate or '')[:10], op])
                m += 1
    else:
        open(fees_csv, 'w', encoding='utf-8-sig').write(
            'jobid,rptype,feename_c,amt,paypass,rpdate,usercode\n')
    print('fees.csv %d 行（结算单位 SHAYUNSHG=%s，名目 %s，操作=createuser 英文代码）'
          % (m, CLIENTID, FEENAME))
    print('下一步：python reconcile_customs.py --bill <账单.xls> --rows "%s" --jobs "%s" '
          '--fees "%s" --groups <groups.json> --out <输出.xlsx>'
          % (a.rows, jobs_csv, fees_csv))
    print('收尾：python "%s" restore' % os.path.join(a.scripts_dir, 'hbo_env.py'))


if __name__ == '__main__':
    main()

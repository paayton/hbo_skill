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
SQL_JOBS = ("SELECT jobid, sono, blnoh, isdeleted FROM {table} "
            "WHERE sono IN ({inlist}) OR rtrim(sono) IN ({inlist}) "
            "OR rtrim(sono) ~ '({alts})$';")
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
        sonos = sorted({r['sono'].strip() for r in csv.DictReader(f) if r['sono'].strip()})
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

    # 工作号兜底反查（用户 2026-10-21 定版，实例：账单 WA666V20064 在 HBO 的 sono 是拼后
    # 实际单号 CLL60201390Z，查无；但工作号 blnoh 仍含客户SO尾段数字 GOSZX26020064）。
    # 规则：sono 查无的运单，取末尾数字段（≥5位）在 job_shipping_2026.blnoh 里模糊匹配，
    # 唯一命中才采纳（多命中=歧义放弃），jobs.csv 里 sono 写账单单号以建立映射。
    found = {(r[1] or '').strip() for r in jobs_rows}
    miss = [s for s in sonos if s not in found and not any(x.endswith(s) for x in found)]
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
    outdir = os.path.dirname(os.path.abspath(a.rows))
    jobs_csv = os.path.join(outdir, 'jobs.csv')
    n = 0
    with open(jobs_csv, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        w.writerow(['sono', 'jobno', 'jobid'])
        for jobid, sono, blnoh, isdel in jobs_rows:
            jobid, sono, blnoh = str(jobid), (sono or '').strip(), (blnoh or '').strip()
            if isdel == 'Y' or jobid in ('0', ''):
                continue  # 作废单、假单剔除
            key = (sono, jobid)
            if key in seen:
                continue
            seen.add(key)
            bl_by_jobid[jobid] = blnoh or jobid
            w.writerow([sono, blnoh or jobid, jobid])
            n += 1
        for s, jobid, blnoh in tail_rows:
            key = (s, jobid)
            if key in seen:
                continue
            seen.add(key)
            bl_by_jobid[jobid] = blnoh
            w.writerow([s, blnoh, jobid])   # sono 写账单单号，建立映射
            n += 1
    print('jobs.csv %d 行（覆盖 %d/%d 个运单）' % (n, len({r[0] for r in seen}), len(sonos)))

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

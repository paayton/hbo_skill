# -*- coding: utf-8 -*-
"""一次性排查：不限 clientid 拉取指定工作单的费用，看"缺失"费用是否挂在其他客户编码下。
用法：python scripts/probe_fees.py（需先 hbo_env.py to-proxy 且 HBO 客户端已登录连上代理）
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fetch_hbo import queue_and_wait, die

# 猩航对账剩余异常行的工作号 → jobid（来自本次 fetch 缓存的工作单查询结果）
JOBIDS = {
    'GOSHA26090008': '255180451068', 'GOSZX26090901': '257289381068',
    'GOSZX26081897': '252386461068', 'GOSZX26090557': '256206291068',
    'GOSZX26090558': '256207021068', 'GOSZX26090676': '256531571068',
    'GOSHA26090030': '256678251068', 'GOSHA26090045': '257702941068',
    'GOSZX26090220': '255022421068', 'GOSZX26090221': '255023181068',
}
IDS = ','.join(JOBIDS.values())

SQL = ("SELECT a.jobid, a.rptype, a.clientid, f.feename_c, a.cyid, a.amt, a.amtstl, "
       "a.rpdate, a.paypass, a.isdeleted "
       "FROM stl_arap_2026 a "
       "LEFT JOIN fil_feeitem f ON f.feeid = a.feeid "
       "WHERE a.jobid IN (%s) ORDER BY a.jobid, a.cyid;" % IDS)
SQL_CLIENT = ("SELECT clientid, cliname_c, cliabbr FROM crm_client WHERE clientid IN "
              "(SELECT DISTINCT clientid FROM stl_arap_2026 WHERE jobid IN (%s));" % IDS)


def main():
    rs = queue_and_wait(['ROLLBACK;', SQL, SQL_CLIENT], expect=3, timeout=300)
    r, rc = rs[1], rs[2]
    if r.get('error'):
        die('费用查询报错：%s' % r['error'])
    if rc.get('error'):
        die('客户查询报错：%s（可把 SQL_CLIENT 的 cname 换成实际列名）' % rc['error'])
    cols = r['columns']
    rows = [dict(zip(cols, row)) for row in r['rows']]
    name_by_id = {str(d[0]): (d[1] or d[2] or d[0]) for d in rc['rows']}
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'probe_result.json')
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    jn_by_id = {v: k for k, v in JOBIDS.items()}
    print('共 %d 行，已写 %s' % (len(rows), out))
    for jobid in JOBIDS.values():
        jn = jn_by_id[jobid]
        fs = [x for x in rows if str(x['jobid']) == jobid and x['isdeleted'] == 'N']
        for cur in ('CNY', 'USD'):
            sel = [x for x in fs if x['cyid'] == cur and x['rptype'] == '1']
            tot = sum(float(x['amt']) for x in sel)
            print('%s %s 应付合计 %g (%d 笔)' % (jn, cur, tot, len(sel)))
            for x in sel:
                nm = name_by_id.get(str(x['clientid']), x['clientid'])
                if str(x['clientid']) != '85500691068':
                    print('   ⚠ 非猩航编码: [%s] %s %s %s amtstl=%s paypass=%s %s'
                          % (nm, x['feename_c'], cur, x['amt'],
                             x['amtstl'], x['paypass'], str(x['rpdate'])[:10]))


if __name__ == '__main__':
    main()

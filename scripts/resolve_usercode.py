# -*- coding: utf-8 -*-
"""把 HBO 数字工号（createuser/documentby，如 136707461068）翻译成英文代码（如 JANELIAO）。

前提：代理已起、HBO 客户端已重开登录。
用法：python scripts/resolve_usercode.py 136707461068 1137161068 174953591068
"""
import sys
import importlib.util
import os

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location('fetch_hbo', os.path.join(HERE, 'fetch_hbo.py'))
fh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fh)


def main():
    ids = [x.strip() for x in sys.argv[1:] if x.strip()]
    if not ids:
        fh.die('用法：python scripts/resolve_usercode.py <数字工号> [...]')
    # 1) 找用户/员工相关表
    res = fh.queue_and_wait([
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='public' AND (table_name ILIKE '%user%' "
        "OR table_name ILIKE '%employee%' OR table_name ILIKE '%staff%') ORDER BY 1;"],
        expect=1)[0]
    if res['error']:
        fh.die('查表清单报错：%s' % res['error'])
    tables = [r[0] for r in res['rows']]
    print('候选表(%d): %s' % (len(tables), ', '.join(tables)))
    if not tables:
        fh.die('没找到用户相关表')

    inlist = ','.join("'%s'" % t for t in tables)
    res = fh.queue_and_wait([
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name IN (%s) ORDER BY 1, 2;" % inlist],
        expect=1)[0]
    if res['error']:
        fh.die('查列清单报错：%s' % res['error'])
    cols = {}
    for t, c in res['rows']:
        cols.setdefault(t, []).append(c)
    for t in tables:
        print('  %s: %s' % (t, ', '.join(cols.get(t, []))))

    # 2) 在每张表里用数字 id 定位行，打印全部列值，人工确认哪列是英文代码
    id_in = ','.join("'%s'" % x for x in ids)
    hits = []
    for t in tables:
        idcols = [c for c in cols.get(t, [])
                  if c.lower() in ('userid', 'user_id', 'pkid', 'empid', 'createuser', 'id', 'uid')]
        for c in idcols:
            sql = 'SELECT * FROM "%s" WHERE CAST("%s" AS TEXT) IN (%s) LIMIT 10;' % (t, c, id_in)
            r = fh.queue_and_wait([sql], expect=1)[0]
            if not r['error'] and r['rows']:
                hits.append((t, c, r))
    for t, c, r in hits:
        print()
        print('命中 %s（id 列 %s）：' % (t, c))
        print('  columns: %s' % r['columns'])
        for row in r['rows']:
            print('  row: %s' % row)
    if not hits:
        fh.die('数字工号 %s 在用户表里没命中，把候选表清单发给用户确认哪张是用户表。' % ', '.join(ids))


if __name__ == '__main__':
    main()

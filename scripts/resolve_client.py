# -*- coding: utf-8 -*-
"""按英文简称反查 HBO clientid（万海 SHEUNISHG 等新客户用）。

前提：代理已起、HBO 客户端已重开登录（hbo_env.py check 显示「客户端已连上代理: 是」）。
用法：python scripts/resolve_client.py SHEUNISHG
"""
import sys
import importlib.util
import os

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location('fetch_hbo', os.path.join(HERE, 'fetch_hbo.py'))
fh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fh)


def main():
    abbr = sys.argv[1].strip().upper()
    # 1) 找客户/结算相关表
    res = fh.queue_and_wait([
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='public' AND (table_name ILIKE '%client%' "
        "OR table_name ILIKE '%custom%' OR table_name ILIKE '%settle%') ORDER BY 1;"],
        expect=1)[0]
    if res['error']:
        fh.die('查表清单报错：%s' % res['error'])
    tables = [r[0] for r in res['rows']]
    print('候选表(%d): %s' % (len(tables), ', '.join(tables)))
    if not tables:
        fh.die('没找到客户相关表')

    # 2) 看每张候选表的列，找含简称/代码的文本列做全表模糊匹配
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

    # 3) 优先在列名像简称/代码的列里精确匹配；找不到再对所有文本列 ILIKE
    hits = []
    tried = set()
    for t in tables:
        for c in cols.get(t, []):
            if any(k in c.lower() for k in ('abbr', 'code', 'name', 'sname', 'ename')):
                sql = "SELECT '%s' AS tbl, '%s' AS col, * FROM \"%s\" WHERE UPPER(CAST(\"%s\" AS TEXT)) LIKE '%%%s%%' LIMIT 5;" % (t, c, t, c, abbr)
                if sql not in tried:
                    tried.add(sql)
                    r = fh.queue_and_wait([sql], expect=1)[0]
                    if not r['error'] and r['rows']:
                        hits.append((t, c, r))
    if not hits:
        print('列名精确匹配没命中，对所有文本列做模糊匹配...')
        for t in tables:
            for c in cols.get(t, []):
                sql = "SELECT '%s' AS tbl, '%s' AS col, * FROM \"%s\" WHERE UPPER(CAST(\"%s\" AS TEXT)) LIKE '%%%s%%' LIMIT 5;" % (t, c, t, c, abbr)
                if sql in tried:
                    continue
                tried.add(sql)
                r = fh.queue_and_wait([sql], expect=1)[0]
                if not r['error'] and r['rows']:
                    hits.append((t, c, r))
    for t, c, r in hits:
        print()
        print('命中 %s.%s：' % (t, c))
        print('  columns: %s' % r['columns'])
        for row in r['rows']:
            print('  row: %s' % row)
    if not hits:
        fh.die('按简称 %s 没查到任何客户记录。让用户在 HBO 客户资料页确认英文简称。' % abbr)


if __name__ == '__main__':
    main()

# -*- coding: utf-8 -*-
"""宇中报关费账单解析：.xls -> rows.csv。

表结构（2026-02~09 八份样例一致）：
  第 1 行标题（文件名），第 2 行表头，数据行，最后一行「总计」。
  关键列：提运单号(->HBO sono)、报关单号(HBO 无此数据，仅展示)、客户自编号(=操作员)、
         报关费(CNY)/联单费/查验费/加班费/合计(CNY)。

用法：python customs_bill.py 账单.xls [--out rows.csv]
"""
import argparse
import os

import pandas as pd


def parse_bill(path):
    df = pd.read_excel(path, header=None)
    hdr_row = None
    for i in range(min(6, len(df))):
        vals = [str(v).strip() for v in df.iloc[i].tolist()]
        if '提运单号' in vals:
            hdr_row = i
            header = vals
            break
    if hdr_row is None:
        raise SystemExit('前 6 行内找不到「提运单号」列，不是宇中报关费账单格式，把表头截图发给用户确认。')

    def col(name):
        return header.index(name)

    c_seq, c_sono = col('序号'), col('提运单号')
    c_op = col('客户自编号') if '客户自编号' in header else None
    c_decl = col('报关单号') if '报关单号' in header else None
    c_total = col('合计(CNY)') if '合计(CNY)' in header else None
    c_fee = col('报关费(CNY)') if '报关费(CNY)' in header else None
    rows = []
    for _, r in df.iloc[hdr_row + 1:].iterrows():
        seq = str(r[c_seq]).strip()
        if not seq or seq == 'nan' or '总计' in seq or '合计' in seq:
            continue
        sono = str(r[c_sono]).strip()
        if not sono or sono == 'nan':
            continue
        def clean(v):
            s = str(v).strip()
            return '' if s in ('nan', 'None') else s

        rows.append({
            'seq': seq,
            'sono': sono,
            'declno': clean(r[c_decl]) if c_decl is not None else '',
            'operator': clean(r[c_op]) if c_op is not None else '',
            'decl_fee': r[c_fee] if c_fee is not None else '',
            'total': r[c_total] if c_total is not None else '',
        })
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('bill')
    ap.add_argument('--out', default=None)
    a = ap.parse_args()
    rows = parse_bill(a.bill)
    out = a.out or os.path.splitext(a.bill)[0] + '_rows.csv'
    pd.DataFrame(rows).to_csv(out, index=False, encoding='utf-8-sig')
    total = pd.to_numeric(pd.DataFrame(rows)['total'], errors='coerce').sum()
    print('解析 %d 行，合计 %.2f 元 -> %s' % (len(rows), total, out))


if __name__ == '__main__':
    main()

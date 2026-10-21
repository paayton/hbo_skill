# -*- coding: utf-8 -*-
"""合并各月对账结果 xlsx 为一份表单：汇总 sheet + 每月一个 sheet。

用法：
  python merge_months.py --out 合并.xlsx 02月=path.xlsx 03月=path.xlsx ...

汇总 sheet 列：月份 / 账单行数 / 账单总额(CNY) / 一致 / 差额 / 系统金额无 / 查无工作单 /
              红色拼单行 / 黄色异常行 / 异常说明
"""
import argparse
import copy

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill

RED_TAIL = 'FF0000'
YEL_TAIL = 'FFFF00'


def fill_kind(cell):
    f = cell.fill
    if not f or not f.fill_type:
        return None
    rgb = str(f.start_color.rgb or '')
    if rgb.endswith(RED_TAIL):
        return 'red'
    if rgb.endswith(YEL_TAIL):
        return 'yellow'
    return None


def copy_sheet(src_ws, dst_ws):
    for row in src_ws.iter_rows():
        for c in row:
            nc = dst_ws.cell(c.row, c.column, c.value)
            if c.has_style:
                nc.fill = copy.copy(c.fill)
                nc.font = copy.copy(c.font)
                nc.alignment = copy.copy(c.alignment)
                nc.border = copy.copy(c.border)
    for rng in src_ws.merged_cells.ranges:
        dst_ws.merge_cells(str(rng))
    for col, dim in src_ws.column_dimensions.items():
        if dim.width:
            dst_ws.column_dimensions[col].width = dim.width
    for r, dim in src_ws.row_dimensions.items():
        if dim.height:
            dst_ws.row_dimensions[r].height = dim.height


def month_stats(ws):
    hdr = None
    colmap = {}
    for i in range(1, 8):
        vals = [str(c.value or '').strip() for c in ws[i]]
        if '提运单号' in vals:
            hdr = i
            colmap = {v: j + 1 for j, v in enumerate(vals) if v}
            break
    c_total = colmap.get('合计(CNY)')
    c_abn = colmap.get('异常金额')
    n_rows = total = red = yellow = 0
    abns = {}
    for i in range(hdr + 1, ws.max_row + 1):
        seq = str(ws.cell(i, 1).value or '').strip()
        if not seq or seq == '总计':
            continue
        n_rows += 1
        if c_total:
            v = ws.cell(i, c_total).value
            try:
                total += float(v or 0)
            except (TypeError, ValueError):
                pass
        k = fill_kind(ws.cell(i, 1))
        if k == 'red':
            red += 1
        elif k == 'yellow':
            yellow += 1
        if c_abn:
            a = str(ws.cell(i, c_abn).value or '').strip()
            if a.startswith('差额'):
                abns['差额'] = abns.get('差额', 0) + 1
            elif a.startswith('系统金额无'):
                abns['系统金额无'] = abns.get('系统金额无', 0) + 1
            elif a.startswith('系统查无'):
                abns['系统查无'] = abns.get('系统查无', 0) + 1
    diff = abns.get('差额', 0)
    zero = abns.get('系统金额无', 0)
    nojob = abns.get('系统查无', 0)
    ok = n_rows - diff - zero - nojob
    return dict(rows=n_rows, total=total, ok=ok, diff=diff, zero=zero,
                nojob=nojob, red=red, yellow=yellow)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', required=True)
    ap.add_argument('months', nargs='+', help='月份=文件路径')
    a = ap.parse_args()

    pairs = []
    for m in a.months:
        name, _, path = m.partition('=')
        pairs.append((name, path))

    wb = Workbook()
    ws_sum = wb.active
    ws_sum.title = '汇总'
    hdr_font = Font(bold=True)
    hdr_fill = PatternFill(start_color='D9E1F2', fill_type='solid')
    headers = ['月份', '账单行数', '账单总额(CNY)', '一致', '差额', '系统金额无',
               '查无工作单', '红色拼单行', '黄色异常行']
    for j, h in enumerate(headers, 1):
        c = ws_sum.cell(1, j, h)
        c.font = hdr_font
        c.fill = hdr_fill
        c.alignment = Alignment(horizontal='center')
    r = 1
    for name, path in pairs:
        src = load_workbook(path)
        ss = src.active
        st = month_stats(ss)
        r += 1
        vals = [name, st['rows'], round(st['total'], 2), st['ok'], st['diff'],
                st['zero'], st['nojob'], st['red'], st['yellow']]
        for j, v in enumerate(vals, 1):
            ws_sum.cell(r, j, v)
        ds = wb.create_sheet(name)
        copy_sheet(ss, ds)
    ws_sum.freeze_panes = 'A2'
    for col, w in zip('ABCDEFGHI', (8, 9, 14, 7, 7, 11, 11, 11, 11)):
        ws_sum.column_dimensions[col].width = w
    wb.save(a.out)
    print('合并完成 -> %s（%d 个月 + 汇总）' % (a.out, len(pairs)))


if __name__ == '__main__':
    main()

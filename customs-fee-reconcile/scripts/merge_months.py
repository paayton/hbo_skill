# -*- coding: utf-8 -*-
"""合并各月对账结果 xlsx 为一份表单：只保留一个 sheet「全部」——所有月份账单行
放在一起（首列加月份区分）。

各月账单的费用列布局不同（02月快递费/其他、03月单证费系列、05月查验费……），
**必须按表头名对齐列**（操作/操作员视为同列，其他/其他(CNY) 视为同列），
工作号/操作列的合并单元格随映射平移保留。

用法：
  python merge_months.py --out 合并.xlsx 02月=path.xlsx 03月=path.xlsx ...
"""
import argparse
import copy

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

HDR_FILL = PatternFill(start_color='D9E1F2', fill_type='solid')
HDR_FONT = Font(bold=True)
CENTER = Alignment(horizontal='center')

# 固定列骨架：前缀 + 合计/结果后缀；中间费用列按首次出现顺序填充
PREFIX = ['序号', '申报时间', '业务编号', '经营单位', '客户自编号', '船名航次',
          '提运单号', '报关单号', '合同号', '集装箱号', '商品项数',
          '报关费(CNY)', '联单费(CNY)']
SUFFIX = ['合计(CNY)', '工作号', '异常金额', '操作']


def norm_h(h):
    """表头归一：去 (CNY)/（CNY）、操作员→操作，用于跨月对齐"""
    h = (h or '').strip().replace('（CNY）', '').replace('(CNY)', '').strip()
    if h == '操作员':
        h = '操作'
    return h


def copy_cell(src, dst):
    dst.value = src.value
    if src.has_style:
        dst.fill = copy.copy(src.fill)
        dst.font = copy.copy(src.font)
        dst.alignment = copy.copy(src.alignment)
        dst.border = copy.copy(src.border)


def canonical_headers(pairs):
    """遍历各月表头，生成规范列序：PREFIX + 中间费用列(首现序) + SUFFIX。"""
    mid, display = [], {}
    for _, path in pairs:
        ws = load_workbook(path).active
        hr = next(r for r in range(1, 10)
                  if any('提运单' in str(c.value) for c in ws[r] if c.value))
        for c in ws[hr]:
            if not c.value:
                continue
            n = norm_h(str(c.value))
            if n and n not in [norm_h(x) for x in PREFIX + SUFFIX] and n not in mid:
                mid.append(n)
                display[n] = str(c.value).strip()
    disp = {norm_h(x): x for x in PREFIX + SUFFIX}
    cols = PREFIX + [display[n] for n in mid] + SUFFIX
    return cols, disp


def append_month(all_ws, src_ws, month, row_off, cols):
    """把 src_ws 按表头名对齐追加进 all_ws（首列月份）。返回新行偏移。"""
    nrows, ncols = src_ws.max_row, src_ws.max_column
    hr = next(r for r in range(1, 10)
              if any('提运单' in str(c.value) for c in src_ws[r] if c.value))
    src_hdr = {norm_h(str(c.value)): c.column for c in src_ws[hr] if c.value}
    dst_col = {norm_h(h): i + 2 for i, h in enumerate(cols)}   # +1：首列是月份

    for i in range(1, nrows + 1):
        r = row_off + i
        mc = all_ws.cell(r, 1, month if i >= 2 else None)
        if i == hr:   # 表头行：写规范表头
            mc.value = '月份'
            mc.fill = copy.copy(HDR_FILL)
            mc.font = copy.copy(HDR_FONT)
            mc.alignment = copy.copy(CENTER)
            for ci, h in enumerate(cols):
                all_ws.cell(r, ci + 2, h)
            continue
        for j in range(1, ncols + 1):
            copy_cell(src_ws.cell(i, j), all_ws.cell(r, j + 1))
        if i == 1:    # 原标题行（文件名），跳过对齐
            continue
        # 数据/总计行：按表头名归位（先清掉位置拷贝，再写对齐值）
        snap = [all_ws.cell(r, j + 1).value for j in range(1, ncols + 1)]
        for j in range(2, ncols + 2):
            all_ws.cell(r, j).value = None
        for j in range(1, ncols + 1):
            sh = norm_h(str(src_ws.cell(hr, j).value) if src_ws.cell(hr, j).value else '')
            if sh in dst_col and snap[j - 1] is not None:
                all_ws.cell(r, dst_col[sh]).value = snap[j - 1]

    # 合并单元格平移（仅数据区的单列纵向合并，且起止列都能映射）
    for rng in src_ws.merged_cells.ranges:
        if rng.min_row <= hr or rng.min_col != rng.max_col:
            continue
        sh = norm_h(str(src_ws.cell(hr, rng.min_col).value)
                    if src_ws.cell(hr, rng.min_col).value else '')
        if sh not in dst_col:
            continue
        dc = dst_col[sh]
        all_ws.merge_cells(start_row=rng.min_row + row_off, start_column=dc,
                           end_row=rng.max_row + row_off, end_column=dc)
    return row_off + nrows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', required=True)
    ap.add_argument('months', nargs='+', help='月份=文件路径')
    a = ap.parse_args()

    pairs = []
    for m in a.months:
        name, _, path = m.partition('=')
        pairs.append((name, path))

    cols, _ = canonical_headers(pairs)
    wb = Workbook()
    all_ws = wb.active
    all_ws.title = '全部'
    row_off = 0
    for name, path in pairs:
        src = load_workbook(path)
        row_off = append_month(all_ws, src.active, name, row_off, cols)
    all_ws.freeze_panes = 'A3'
    all_ws.column_dimensions['A'].width = 8
    for i, h in enumerate(cols, 2):
        w = 20 if h in ('经营单位',) else (16 if h in ('船名航次',) else 11)
        if h in ('提运单号', '业务编号'):
            w = 18
        all_ws.column_dimensions[get_column_letter(i)].width = w

    wb.save(a.out)
    print('合并完成 -> %s（全部 sheet %d 行，%d 个月，%d 列）'
          % (a.out, row_off, len(pairs), len(cols) + 1))


if __name__ == '__main__':
    main()

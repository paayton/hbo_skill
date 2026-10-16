# -*- coding: utf-8 -*-
"""HBO 对账主脚本（通用版）

用法：
  python reconcile.py --invoice 对账单.xlsx --fees 费用.csv --jobs 工作单.csv --out 输出.xlsx

输入：
  --invoice  开票明细 xlsx，须含表头：SONO、主单号、币种、费用总金额
             （SONO 单元格可用逗号合并多个单号，如 "177A,177B"）
  --fees     HBO 费用明细 csv，表头须含：jobno, feename_c, cyid, amt, documentby
  --jobs     SONO→工作号映射 csv，表头须含：sono, jobno

口径（与人工对账对齐，2026-10-13 定版）：
  - 严格按 SONO 匹配工作号；一个 SONO 只对应一个工作号
  - 主池只取应付费用（rptype=1）；rptype=0 是应收方向（万海负数票面去应收池按绝对值匹配，见下）
  - 对账单是快照：--cutoff 之前的费用才参与（晚于对账日录入的属于下期）
  - 人民币行：人民币列 = HBO 该工作号全部 CNY 费用合计
  - 美金行：美金列 = HBO 海运费 + 巴拿马运河费（与开票可比口径）
  - 判定优先级：
      0. 有付款申请号（payno 列非空）的费用 = 已付款，剔除不对账；对账的是未付款部分
      1. 全额一致 → 不标注
      2. 不一致 → 子集凑数：某几笔费用之和恰好等于票面金额 → 黄色"可凑出"备注
         （差额通常是：亏损、电放费/MBL电放费、重复录入的操作费）
      3. 凑不出 → 红色真异常，填异常文件人员（HBO 工号）+ 异常原因
  - SONO 在 HBO 查无工作单 → 红色，原因"查无工作单"

输出：原表 + 工作号 / 人民币 / 美金 / 异常文件人员 / 异常原因 五列
"""
import argparse, csv, collections, os
import openpyxl
from openpyxl.styles import PatternFill

MAIN_FEE = {'海运费', '巴拿马运河费'}

def cents(x): return int(round(float(x) * 100))

def subset_sum(amounts, target):
    """amounts: [(金额, 标注)]；返回能凑出 target 的标注列表，凑不出返回 None"""
    t = cents(target)
    items = [(cents(a), n) for a, n in amounts]
    dp = {0: ()}
    for i, (c, n) in enumerate(items):
        for s in list(dp.keys()):
            ns = s + c
            if ns > t or ns in dp:
                continue
            dp[ns] = dp[s] + (i,)
            if ns == t:
                return [items[j][1] for j in dp[ns]]
    return None

def find_col(header, *keys):
    for k in keys:
        for i, h in enumerate(header):
            if h and k in str(h):
                return i + 1
    raise SystemExit(f'找不到列: {keys}，实际表头: {header}')

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--invoice', required=True)
    ap.add_argument('--fees', required=True)
    ap.add_argument('--jobs', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--cutoff', default='',
                    help='对账单日期 YYYY-MM-DD：晚于该日录入的费用剔除（奥吉取确认日，如 2026-08-14）')
    a = ap.parse_args()

    # 硬校验：--fees 必须是 HBO 费用数据，防止把客户对账单/开票明细误当系统数据
    with open(a.fees, encoding='utf-8-sig') as fh:
        fees_header = next(csv.reader(fh))
    HBO_MARKERS = {'jobno', 'settle_abbr', 'documentby', 'rpdate', 'feename_c', 'rptype'}
    if len(HBO_MARKERS & set(h.strip() for h in fees_header)) < 3:
        raise SystemExit(
            '拒绝运行：--fees %s 缺少 HBO 费用表特征字段（jobno/settle_abbr/documentby/rpdate 等），\n'
            '它看起来不是 HBO 系统数据。对账的另一方永远是 HBO 系统费用：\n'
            '  让用户登录 HBO，走 scripts/pg_proxy.py 现拉费用明细（见 references/hbo-system.md）。\n'
            '绝不能用对账单/开票明细等客户侧 Excel 代替 HBO 数据。' % a.fees)

    jobs = {j['jobno'].strip(): j for j in csv.DictReader(open(a.jobs, encoding='utf-8-sig'))}
    jobs_by_sono = collections.defaultdict(list)  # 一个 SONO 可能挂多个工作单（分单 A/B/C 后缀、历史单），全部保留
    for j in csv.DictReader(open(a.jobs, encoding='utf-8-sig')):
        jobs_by_sono[j['sono'].strip()].append(j)
    jobs_doc = {j['jobno'].strip(): (j.get('doc') or '').strip() for j in jobs.values()}
    fees_by_job = collections.defaultdict(list)
    fees_ar_by_job = collections.defaultdict(list)  # rptype=0 应收：万海负数票面（折让/退款）去这个池匹配
    doc_by_job = {}
    for f in csv.DictReader(open(a.fees, encoding='utf-8-sig')):
        # 只取应付费用（rptype=1）进主池；rptype=0 是应收方向，单独存（万海负数票面用），其他方向丢弃
        if 'rptype' in f and f['rptype'].strip() == '0':
            ar = True
        elif 'rptype' in f and f['rptype'].strip() not in ('1', ''):
            continue
        else:
            ar = False
        # 对账单是快照：晚于对账日录入的费用属于下期（--cutoff YYYY-MM-DD）
        if a.cutoff and f.get('rpdate', '')[:10] > a.cutoff:
            continue
        (fees_ar_by_job if ar else fees_by_job)[f['jobno']].append(f)
        doc_by_job.setdefault(f['jobno'], f.get('documentby', ''))

    wb = openpyxl.load_workbook(a.invoice)
    ws = wb.active
    # 表头行自动定位：前 10 行里找到含"提单号/SONO"的行（奥吉对账单在第 5 行，猩航开票明细在第 1 行）
    header_row = None
    for r in range(1, min(11, ws.max_row + 1)):
        vals = [str(c.value or '').strip() for c in ws[r]]
        if any(v in ('提单号', 'SONO', 'So_No') or 'SONO' in v for v in vals):
            header_row = r
            header = vals
            break
    if header_row is None:
        raise SystemExit('前 10 行内找不到含"提单号/SONO"的表头行')
    wanhai = any(str(h).strip().lower() == 'so_no' for h in header)
    if wanhai:
        # 万海账单（用户日常格式）：原表 + RMB（前置处理列）+ 工作号/人民币/美金/文件异常人员
        # 只核 RMB 与 Ttl_Usd；票面金额在系统「未付款且未销账」应付费用中存在（单笔或几笔之和）即一致；
        # 异常行整行黄填，人民币/美金列写「系统金额无X」，文件异常人员填英文代码；两项均 0 的行整行跳过。
        c_sono = find_col(header, 'So_No')
        if any(str(h).strip() in ('RMB', 'RMB对账金额') for h in header):
            c_inv_cny = [n + 1 for n, h in enumerate(header) if str(h).strip() in ('RMB', 'RMB对账金额')][0]
            c_mix = None
        else:
            c_inv_cny = find_col(header, 'TTL_CNY')
            try:
                c_mix = find_col(header, 'Mix_Cny')
            except SystemExit:
                c_mix = None
        c_inv_usd = find_col(header, 'Ttl_Usd')
        # 输出列头对齐用户日常习惯：前置处理列统一叫 RMB
        ws.cell(header_row, c_inv_cny, 'RMB')
        # 运费账单识别（如 D666_00 "Outstanding"）：PP/CC OFT 有值 = 本账结海运费，美元池要含海运费；
        # D 系列半月账单 OFT 全 0，运费另结，美元池剔除海运费
        freight_bill = False
        for cn in ('PP OFT', 'CC OFT'):
            try:
                cc = find_col(header, cn)
            except SystemExit:
                continue
            for rr in range(header_row + 1, ws.max_row + 1):
                v = ws.cell(rr, cc).value
                try:
                    if v not in (None, '') and abs(float(v)) > 0.005:
                        freight_bill = True
                        break
                except (TypeError, ValueError):
                    continue
            if freight_bill:
                break
    else:
        c_sono = find_col(header, 'SONO', '提单号')
        c_cur = find_col(header, '币种')
        c_amt = find_col(header, '费用总金额', '金额')
    # 输出统一（2026-10-16 用户定版，适用所有账单）：原账单一个字不动，追加 4 列；
    # 高亮规则：美金海运费异常 → 红；其他累加不出来 → 黄 + 异常文件人员；一致/可凑出（部分开票）→ 不高亮
    xinghang = any(h in header for h in ('船名航次', '主单号', 'ETD'))
    userfmt = True
    base_col = ws.max_column
    new_headers = ['工作号', '人民币', '美金', '异常文件人员']
    for col, h in zip(range(base_col + 1, base_col + 1 + len(new_headers)), new_headers):
        ws.cell(header_row, col, h)  # 新列表头必须写在表头行，不能写死第 1 行（奥吉对账单表头在第 5 行）
    c_job, c_rmb, c_usd, c_doc = base_col + 1, base_col + 2, base_col + 3, base_col + 4
    c_reason = None

    yellow = PatternFill('solid', fgColor='FFFF00')  # 其他金额异常：亮黄整行
    red = PatternFill('solid', fgColor='FFC7CE')     # 美金海运费异常：红整行

    def mark(row, fill, person=None):
        """整行高亮 + 写异常文件人员（None=不写）。"""
        if person:
            ws.cell(row, c_doc, person)
        for c in range(1, c_doc + 1):
            ws.cell(row, c).fill = fill

    n_ok = n_sub = n_bad = n_yellow = n_missing = n_paid_rows = n_skip = 0
    for row in range(header_row + 1, ws.max_row + 1):
        sono = str(ws.cell(row, c_sono).value or '').strip()
        if not sono or '合计' in sono:  # 跳过空行和"分币种合计"页脚
            continue
        if wanhai:
            inv_cny = float(ws.cell(row, c_inv_cny).value or 0)
            if c_mix:
                inv_cny -= float(ws.cell(row, c_mix).value or 0)
            inv_usd = float(ws.cell(row, c_inv_usd).value or 0)
            # 万海口径：票面 RMB 与 Ttl_Usd 都是 0 → 整行跳过，不查工作号、不标注
            if abs(inv_cny) < 0.01 and abs(inv_usd) < 0.01:
                n_skip += 1
                continue
        if not wanhai:
            cur = str(ws.cell(row, c_cur).value or '').strip()
            inv = ws.cell(row, c_amt).value
            if inv is None:
                continue
            inv = float(inv)
        sonos = [s.strip() for s in sono.replace('，', ',').split(',') if s.strip()]
        all_jns = dict.fromkeys(j['jobno'] for s in sonos for j in jobs_by_sono.get(s, []))
        # 显示与人员归属优先取「有费用且无分单后缀」的主单（如 GOSZX26082310，而不是 GOSZX26082310C）
        jobnos = sorted(all_jns, key=lambda jn: (not fees_by_job.get(jn), jn[-1:].isalpha(), jn))

        if not jobnos:
            # 查无工作单：统一亮黄（其他异常类），人民币/币种列写明，无文件人员可填
            if wanhai:
                ws.cell(row, c_rmb, '系统查无此工作单')
            else:
                cur0 = str(ws.cell(row, c_cur).value or '').strip().upper()
                ws.cell(row, c_usd if cur0 == 'USD' else c_rmb, '系统查无此工作单')
            mark(row, yellow)
            n_missing += 1
            continue

        disp = [jn for jn in jobnos if fees_by_job.get(jn)] or jobnos[:1]  # 只显示有费用的主单，假单/分单后缀不显示
        ws.cell(row, c_job, ', '.join(disp))

        if wanhai:
            def payno(f):
                return (f.get('payno') or f.get('付款申请号') or '').strip()

            def settled(f):
                a = f.get('amtstl')
                try:
                    return a not in (None, '') and abs(float(f['amt']) - float(a)) < 0.01
                except (TypeError, ValueError):
                    return False

            fs_cny = [f for jn in jobnos for f in fees_by_job.get(jn, []) if f['cyid'] == 'CNY']
            # 美元池：半月账单剔除海运费（运费另结）；运费账单（PP/CC OFT 有值）含海运费
            fs_usd = [f for jn in jobnos for f in fees_by_job.get(jn, []) if f['cyid'] == 'USD'
                      and (freight_bill or f['feename_c'].strip() != '海运费')]
            # 负数票面（折让/退款）= 应收方向（用户定版 2026-10-14，如 -200 配应收 200）：去 rptype=0 池匹配正数
            fs_cny_ar = [f for jn in jobnos for f in fees_ar_by_job.get(jn, []) if f['cyid'] == 'CNY'
                         and (freight_bill or f['feename_c'].strip() != '海运费')]
            fs_usd_ar = [f for jn in jobnos for f in fees_ar_by_job.get(jn, []) if f['cyid'] == 'USD'
                         and (freight_bill or f['feename_c'].strip() != '海运费')]
            p_cny = [f for f in (fs_cny_ar if inv_cny < -0.005 else fs_cny)
                     if not payno(f) and not settled(f)]  # 未付款且未销账
            p_usd = [f for f in (fs_usd_ar if inv_usd < -0.005 else fs_usd)
                     if not payno(f) and not settled(f)]

            def find_amount(inv, p):
                """票面金额在对应方向池里是否存在（单笔或几笔之和，按绝对值匹配：负数票面配应收正数）。
                命中返回明细列表，否则 None。"""
                if abs(inv) < 0.01:
                    return []
                amt_list = [(abs(float(f['amt'])), '%s %g' % (f['feename_c'].strip(), float(f['amt'])))
                            for f in p]
                tgt = abs(inv)
                for a, n in amt_list:  # 单笔命中优先
                    if a == tgt:
                        return [n]
                return subset_sum(amt_list, tgt)

            cny_bad = find_amount(inv_cny, p_cny) is None
            usd_bad = find_amount(inv_usd, p_usd) is None
            bad = []
            if cny_bad:
                bad.append((c_rmb, inv_cny))
            if usd_bad:
                bad.append((c_usd, inv_usd))
            if bad:
                for ccol, inv in bad:
                    ws.cell(row, ccol, '系统金额无%g' % inv)
                # 高亮定版（2026-10-16）：美金侧仅在「运费账单（本账结海运费）且美元池涉海运费」时红色+GRACECHEN；
                # 半月账单美元异常（AMS/电放等）→ 黄 + 文件人员；人民币侧异常 → 黄 + 文件人员；两侧异常人员并列
                persons = []
                usd_freight_bad = False
                if usd_bad:
                    usd_all = [f for jn in jobnos for f in fees_by_job.get(jn, []) if f['cyid'] == 'USD']
                    usd_freight_bad = freight_bill and any('海运费' in f['feename_c'] for f in usd_all)
                    persons.append('GRACECHEN' if usd_freight_bad
                                   else (jobs_doc.get(jobnos[0], '') or doc_by_job.get(jobnos[0], '')))
                if cny_bad:
                    persons.insert(0, jobs_doc.get(jobnos[0], '') or doc_by_job.get(jobnos[0], ''))
                mark(row, red if usd_freight_bad else yellow, '/'.join(p for p in persons if p))
                if usd_freight_bad:
                    n_bad += 1
                else:
                    n_yellow += 1
            else:
                n_ok += 1
            continue

        if xinghang:
            # 猩航（用户日常格式，与万海一致，2026-10-15 定版）：票面金额在系统「未付款且未销账」
            # 同币种费用里存在（单笔或几笔之和）即一致；异常 → 金额写进该行币种列（系统金额无X），
            # 文件异常人员：人民币侧=工作单文件人员（lastupduser 英文代码），美金侧（涉海运费）=Grace，整行亮黄
            cur_code = 'CNY' if cur.upper() in ('RMB', 'CNY') else 'USD'

            def payno(f):
                return (f.get('payno') or f.get('付款申请号') or '').strip()

            def settled(f):
                a = f.get('amtstl')
                try:
                    return a not in (None, '') and abs(float(f['amt']) - float(a)) < 0.01
                except (TypeError, ValueError):
                    return False

            pool = [f for jn in jobnos for f in fees_by_job.get(jn, []) if f['cyid'] == cur_code]
            pool = [f for f in pool if not payno(f)]  # 猩航口径（10-13 定版）：已付款不进对账；未销账的仍留在池里
            if abs(inv) < 0.01:  # 票面 0 = 无费用可核，一致（与旧口径相同）
                n_ok += 1
                continue
            c_out = c_rmb if cur_code == 'CNY' else c_usd
            sub = subset_sum([(float(f['amt']), '%s %g' % (f['feename_c'].strip(), float(f['amt'])))
                              for f in pool], inv)
            if sub is None:
                ws.cell(row, c_out, '系统金额无%g' % inv)
                # 高亮定版（2026-10-16）：美金侧异常且费用池涉海运费 → 红 + GRACECHEN；其余 → 黄 + 文件人员
                if cur_code == 'USD' and any('海运费' in f['feename_c'] for f in pool):
                    mark(row, red, 'GRACECHEN')
                    n_bad += 1
                else:
                    mark(row, yellow, jobs_doc.get(jobnos[0], '') or doc_by_job.get(jobnos[0], ''))
                    n_yellow += 1
            else:
                n_ok += 1
            continue

        cur_code = 'CNY' if cur.upper() in ('RMB', 'CNY') else 'USD'
        raw_fees = [f for jn in jobnos for f in fees_by_job.get(jn, []) if f['cyid'] == cur_code]
        # 有付款申请号的费用 = 已进入付款流程，不参与对账
        def payno(f):
            return (f.get('payno') or f.get('付款申请号') or '').strip()
        paid = [f for f in raw_fees if payno(f)]
        all_fees = [f for f in raw_fees if not payno(f)]
        n_paid_rows += len(paid)
        full = round(sum(float(f['amt']) for f in all_fees), 2)

        # 人民币/美金列平时留空（2026-10-16 用户定版），只有异常行才写金额
        if abs(full - inv) < 0.01:
            n_ok += 1
            continue

        sub = subset_sum([(float(f['amt']), '%s %g' % (f['feename_c'].strip(), float(f['amt'])))
                          for f in all_fees], inv)
        if sub is not None:
            n_sub += 1  # 可凑出（部分开票）= 正常，不高亮
            continue

        c_out = c_rmb if cur_code == 'CNY' else c_usd
        if not all_fees:
            ws.cell(row, c_out, '系统金额无%g' % inv)
        else:
            ws.cell(row, c_out, '差额%g' % round(inv - full, 2))
        # 高亮定版（2026-10-16）：美金侧异常且费用涉海运费 → 红 + GRACECHEN；其余累加不出来 → 黄 + 文件人员
        if cur_code == 'USD' and any('海运费' in f['feename_c'] for f in all_fees):
            mark(row, red, 'GRACECHEN')
            n_bad += 1
        else:
            mark(row, yellow, jobs_doc.get(jobnos[0], '') or doc_by_job.get(jobnos[0], ''))
            n_yellow += 1

    out_parent = os.path.dirname(os.path.abspath(a.out))
    os.makedirs(out_parent, exist_ok=True)
    wb.save(a.out)
    total = n_ok + n_sub + n_bad + n_yellow + n_missing + n_skip
    print('saved', a.out)
    print('一致 %d / 可凑出(不高亮) %d / 异常(黄) %d / 美金海运费异常(红) %d / 查无工作单(黄) %d / 跳过(票面双零) %d / 合计 %d'
          % (n_ok, n_sub, n_yellow, n_bad, n_missing, n_skip, total))
    if n_paid_rows:
        print('已付款未计入对账的费用笔数: %d' % n_paid_rows)

    # 自动体检：异常比例过高 = 大概率口径错了（误加 cutoff / 客户简称错 / 取数缺列），不是真实业务异常
    if total and (n_bad + n_yellow) / total > 0.3:
        print()
        print('!! 疑似口径错误，请勿交付 !!')
        print('异常 %d/%d（%.0f%%）超过 30%% 阈值。真实对账中异常应该很少（通常为 0）。' % (n_bad + n_yellow, total, 100.0 * (n_bad + n_yellow) / total))
        print('按以下顺序排查后重跑：')
        print('  1. 开票明细（猩航）是不是误加了 --cutoff？（开票明细禁用 cutoff，费用常在开票后才录入）')
        print('  2. fetch_hbo.py 的 --client 是不是选错了客户？（费用会查空，人民币列全是 0）')
        print('  3. fees.csv 里费用是否大面积缺失或币种对不上？（打开抽查几行）')
        print('排查不清就把统计和 2-3 条异常行截图发给用户确认，不要直接交付。')

if __name__ == '__main__':
    main()

# -*- coding: utf-8 -*-
"""万海网页版子单查询（拼单分组）：通过 Kimi WebBridge 驱动用户真实浏览器。

页面：https://es.wanhai.com/ces-api/#/BKG/e-Booking/BKGlist （订舱列表，按客户SO查询）
逻辑（用户 2026-10-20 定版）：运单号查出的行带「查看子单」= 母单，点进去列表里的
所有客户SO 共享同一个工作号（拼单组）；子单号直接查查不到（必须经由母单）。
非万海平台订的运单（如 COSU 前缀）查无 —— 组信息留空，reconcile 侧按单票处理。

前提：kimi-webbridge 守护进程可用（http://127.0.0.1:10086），用户浏览器已登录万海网页版。

用法：python wanhai_groups.py --out groups.json --sonos SO1 SO2 ...
  或：python wanhai_groups.py --out groups.json --sonos-file sonos.txt
输出 JSON 数组：[{sono, found, group, sub_rows?}]，group=该运单所属组的全部客户SO；
found=false 且 group=null = 网页查无（可能非万海平台订舱）。
"""
import argparse
import json
import time
import urllib.request

BRIDGE = 'http://127.0.0.1:10086/command'
SESSION = 'customs-fee-reconcile'
URL = 'https://es.wanhai.com/ces-api/#/BKG/e-Booking/BKGlist'

EVAL_SET_SO = """(so=>{
const labels=[...document.querySelectorAll('label,span,div')].filter(e=>e.children.length===0&&e.innerText.trim()==='客户SO');
if(!labels.length) return 'no-label';
let input=null, p=labels[0];
for(let i=0;i<6&&p;i++){ if(p.querySelector('input')){input=p.querySelector('input');break;} p=p.parentElement; }
if(!input) return 'no-input';
const setter=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;
setter.call(input,so);
input.dispatchEvent(new Event('input',{bubbles:true}));
input.dispatchEvent(new Event('change',{bubbles:true}));
return 'filled';
})"""

EVAL_CLICK_QUERY = """(()=>{
const btns=[...document.querySelectorAll('button')].filter(b=>b.innerText.replace(/\\s/g,'').includes('查询'));
if(!btns.length) return 'no-btn';
btns[0].click();
return 'clicked';
})()"""

EVAL_CLICK_SUB = """(()=>{
const els=[...document.querySelectorAll('a,button,span,div')].filter(e=>e.children.length===0&&e.innerText.trim()==='查看子单');
if(!els.length) return 'none';
els[0].click();
return 'clicked';
})()"""

EVAL_READ_TABLE = """(()=>{
const rows=[...document.querySelectorAll('table tr')].map(tr=>
  [...tr.querySelectorAll('td,th')].map(td=>td.innerText.trim().replace(/\\n/g,'|'))
).filter(r=>r.some(x=>x));
return JSON.stringify(rows);
})()"""


def wb(action, args):
    body = json.dumps({'action': action, 'args': args, 'session': SESSION}).encode('utf-8')
    req = urllib.request.Request(BRIDGE, data=body, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=90) as r:
        d = json.loads(r.read().decode('utf-8'))
    if not d.get('ok'):
        raise RuntimeError(str(d))
    return d['data']


def ev(code):
    return json.loads(wb('evaluate', {'code': code}).get('value', 'null'))


def parse_rows(raw):
    rows = json.loads(raw) if isinstance(raw, str) else raw
    if not rows:
        return [], None
    header = rows[0]
    if '客户SO' not in header:
        return [], None
    i_so, i_type, i_bl = header.index('客户SO'), (
        header.index('子母单') if '子母单' in header else None), header.index('提单号')
    out = []
    for r in rows[1:]:
        if len(r) <= i_so or not r[i_so].strip():
            continue
        out.append({'type': r[i_type].strip() if i_type is not None and len(r) > i_type else '',
                    'blno': r[i_bl].strip().replace('查看子单', '') if len(r) > i_bl else '',
                    'so': r[i_so].strip()})
    return out, header


def wait_table(pred, timeout=30.0):
    t0 = time.time()
    raw = '[]'
    while time.time() - t0 < timeout:
        raw = ev(EVAL_READ_TABLE)
        if isinstance(raw, str) and pred(raw):
            break
        time.sleep(1.0)
    return raw


def query_one(sono):
    res = {'sono': sono, 'found': False, 'group': None}
    wb('navigate', {'url': URL})
    time.sleep(6)
    if wb('evaluate', {'code': EVAL_SET_SO + "('" + sono + "')"}) .get('value') != 'filled':
        res['error'] = '客户SO输入框定位失败'
        return res
    wb('evaluate', {'code': EVAL_CLICK_QUERY})
    raw = wait_table(lambda t: sono in t, timeout=30)
    rows, _ = parse_rows(raw)
    if not rows:
        return res  # 网页查无（可能非万海平台订舱）
    res['found'] = True
    time.sleep(2)  # 等操作列渲染
    if wb('evaluate', {'code': EVAL_CLICK_SUB}).get('value') == 'clicked':
        raw2 = wait_table(lambda t: '子母单' in t and '母单' in t, timeout=30)
        sub_rows, hdr2 = parse_rows(raw2)
        if sub_rows and hdr2 and '子母单' in hdr2:
            res['group'] = [x['so'] for x in sub_rows]
            res['sub_rows'] = sub_rows
            return res
    res['group'] = [sono]
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', required=True)
    ap.add_argument('--sonos', nargs='*', default=[])
    ap.add_argument('--sonos-file', default=None)
    ap.add_argument('--retry-misses', type=int, default=1,
                    help='found=false 的运单重试轮数（万海页面偶发慢加载）')
    a = ap.parse_args()
    sonos = list(a.sonos)
    if a.sonos_file:
        sonos += [x.strip() for x in open(a.sonos_file, encoding='utf-8') if x.strip()]
    sonos = list(dict.fromkeys(sonos))
    if not sonos:
        raise SystemExit('没有运单号')

    out = []
    for s in sonos:
        for attempt in range(a.retry_misses + 1):
            try:
                r = query_one(s)
            except Exception as e:
                r = {'sono': s, 'error': str(e)}
            if r.get('found') or attempt == a.retry_misses:
                break
        out.append(r)
        print(json.dumps(r, ensure_ascii=False), flush=True)
        # 增量落盘：超时被杀也不丢已完成的结果
        with open(a.out, 'w', encoding='utf-8') as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
    n_grp = len({tuple(x['group']) for x in out if x.get('group')})
    print('完成：%d 个运单，%d 个拼单组（含单票组），网页查无 %d 个 -> %s'
          % (len(out), n_grp, len([x for x in out if not x.get('found')]), a.out))


if __name__ == '__main__':
    main()

# 华谊 HBO 系统知识库

## 连接信息

- 服务器：`120.24.174.5:5432`（PostgreSQL），库名 `hydata`
- 应用账号：`dba_app`（密码未知，不走直连）
- HBO 客户端走本机 32 位 ODBC DSN：**HYTPGS**（驱动 psqlodbc35w.dll，注册表 `HKLM\SOFTWARE\WOW6432Node\ODBC\ODBC.INI\HYTPGS`，键 Servername/Port）
- 客户端地址 `http://120.24.174.5`（页面很卡，批量操作必须走数据层）
- 登录账号是操作员账号，与数据库账号无关。**当前操作员账号：`SHACC1`（2026-10-15 用户提供，密码变更后更新此处）**

## 环境管理（用脚本，不要手工敲 reg 命令）

Git Bash 下 `reg` 命令引号有坑，一律走 `scripts/hbo_env.py`（管理员终端）：

- `python scripts/hbo_env.py check` — 自检 DSN 指向 / 代理端口 / 客户端连接，并提示下一步
- `python scripts/hbo_env.py to-proxy` — 自动备份注册表到 dsn_backup.reg、切 DSN 到 `127.0.0.1:15432`、后台启动 `pg_proxy.py`
- `python scripts/hbo_env.py restore` — 关代理、恢复 DSN 到 `120.24.174.5:5432`（取数后**必须**执行）

DSN 只对**新连接**生效：切完代理必须让使用人**完全退出 HBO 客户端再打开登录**；restore 后同样要重开 HBO 才能直连真实库。

## 取数（fetch_hbo.py 一条龙）

`scripts/fetch_hbo.py --invoice <xlsx> --client <简称>` 自动完成：提 SONO → 写注入队列 → 等结果 → 产出 `fees.csv` / `jobs.csv` → 打印 reconcile 命令。正常对账不需要手工写 SQL。

底层原理（排障时才需要懂）：`pg_proxy.py` 是中间人代理，HBO 客户端的已认证会话经它透传；写入 `scripts/inject_queue.txt` 的 SQL 借用该会话执行，结果追加到 `scripts/inject_results.jsonl`，日志在 `scripts/proxy.log`。

## 真实表结构（2026-10-14 探库验证，1188 张表里的关键 4 张）

> SKILL.md 旧版里的「工作单表」「费用表」是占位名，直接查会报 `C42P01`（表不存在）。真实表名如下：

| 表 | 行数量级 | 说明 |
|---|---|---|
| `job_shipping_2026` | 2.6 万 | 当年工作单（订舱/提单）。关键列：`jobid, sono, blnoh（工作号/提单号）, blnom（主单号）, vessel, voyage, lastupduser（最后修改人，非文件人员）, isdeleted` |
| `job_shipping` | 13.8 万 | 历年老工作单，2026 表查无时兜底查它 |
| `stl_arap_2026` | 36.9 万 | 当年费用。关键列：`jobid, rptype, clientid, feeid, cyid, amt, amtstl, rpdate, paypass, billno, agenbillid, createuser, isdeleted`（老费用在 `stl_arap`） |
| `fil_feeitem` | — | 费用名目字典：`feeid → feename_c` |

要点：
- **`isdeleted='Y'` 是作废行，必须剔除**（否则同一 SONO 会查出作废单+正常单两条，费用对到作废单上）
- 费用表**没有 payno 列**。目前用 `paypass` 近似：`'N'`/空 = 未付款，其他 = 已付款剔除。此语义未经业务确认（观测到的全是 'N'），若某次出现大量"已付款剔除"，先和用户确认再交付
- `createuser` 是数字工号（如 146833351068），和 `data/usercode_map.json` 里的字母工号是两套编码， map 不上的直接原样填

取数 SQL 模板（fetch_hbo.py 内置，排障参考）：

```sql
-- 1) SONO → 工作号
SELECT jobid, sono, blnoh, vessel, voyage, createuser, isdeleted
FROM job_shipping_2026 WHERE sono IN (...);

-- 2) 费用明细（join 名目字典）
SELECT a.jobid, a.rptype, a.clientid, f.feename_c, a.cyid, a.amt, a.amtstl,
       a.rpdate, a.paypass, a.billno, a.createuser, a.isdeleted
FROM stl_arap_2026 a LEFT JOIN fil_feeitem f ON f.feeid = a.feeid
WHERE a.clientid = '<客户ID>' AND a.jobid IN (SELECT jobid FROM job_shipping_2026 WHERE sono IN (...));
```

## 客户简称 → clientid（应付费用）

| 客户 | clientid | settle_abbr |
|---|---|---|
| 猩航（SHXINHAN） | 85500691068 | SHXINHAN |
| 奥吉（STACIAGYE） | 1280701068 | STACIAGYE |
| 万海（SHEUNISHG = 宇中上海分公司账户，万海应付费用挂此） | 1249221068 | SHEUNISHG |
| 外代（CHIOCESHG，万海舱单费挂此） | 1218891068 | CHIOCESHG |
| COSCO（COSCOSGH） | 1315921068 | COSCOSGH |
| 海管家（HAIGUANJ） | 75766901068 | HAIGUANJ |

> 后三家 2026-10-16 从 `crm_client.clicode` 解析（缩写列叫 **clicode**，不是 settle_abbr；同表 cliabbr 同值），缓存在 `skills/manifest-fee-reconcile/assets/clientids.json`。crm_client 里还有 264757371068、76013581068 等其他挂过"舱单费"的结算单位，遇到对不上的费用先想是不是别家供应商的。

> 万海 SHEUNISHG 2026-10-14 验证：crm_client 中该编码即「SHENZHEN UNIWIN INTERNATIONAL LOGISTICS LTD SHANGHAI BRANCH」，万海账单的本地费用（码头费/文件费/封条费/电放费等）应付全部挂此 clientid、rptype=1（71 票金额精确命中验证）；同工作单上其他 clientid 的 rptype=1 费用是第三方供应商，不取。

> 奥吉 clientid 2026-10-14 修正：旧值 1263631068 是应收方向/其他主体，应付费用实测归属 1280701068（18 票里 13 票金额精确命中验证）。
> 另：奥吉的 sono 在 HBO 里带船司前缀和尾空格（如 `COSU6461707920 `），精确 IN 查不到，fetch_hbo.py 已内置 rtrim+后缀正则兜底。

公司简称识别不了时问用户，不要猜。

## 对账单 vs 开票明细（角色别搞反）

- **对账单**（如"奥吉对账单8.14"）：客户给华谊的应付明细，是**对账的主体**，匹配键是它的提单号列（=SONO；奥吉是 10 位数字如 6461707920）
- **开票明细**（如"宇中7.26开票明细"）：宇中开给客户的票，仅供参考（看票面金额构成），**不是对账的另一方**
- 对账的另一方永远是 **HBO 系统里的费用**。拿到两份 Excel 时，不要互相对，各自汇总后分别去对 HBO

## cutoff（快照日期）规则 —— 两种文件相反

- **开票明细（猩航）：禁用 cutoff**。费用常在开票后几天才录入 HBO（实测 6.28 的票，费用 6.30–7.3 才入账）；用 cutoff 会把它们剔掉，造成大批量"系统 0"的假异常（2026-10-14 实测 475/687 笔被误剔）
- **对账单（奥吉）：用 `--cutoff 确认日`**（如 2026-08-14），剔除晚于对账日录入的费用（它们属于下期）
- **对账单（万海）：禁用 cutoff**（2026-10-14 D666 实测：68/323 笔费用在账单日 10/10 之后才录入，加 cutoff 误剔出 13 个假异常）。万海账单出账早于 HBO 录单滞后，同猩航逻辑
- 奥吉更精确的做法：对账单在 HBO 里有记录（表 `job_agenbill`，对账单号如 AC260814DJPU8499），按 `agenbillid` 关联 `stl_arap_2026` 的费用行可 100% 复现对账单口径，不用日期粗筛

## 万海账单口径（2026-10-14 与用户对版，同日晚按用户纠正修订）

- 账单是万海 EDI 明细（列如 So_No/Book_No/Ttl_Usd/TTL_CNY(KP)/Mix_Cny，HALF MONTHLY PAYMENT）
- **匹配键 So_No**（如 WI666V80118）；HBO 的 sono 带船司前缀，fetch_hbo.py 后缀匹配已覆盖；录单偶尔用 Book_No（027G…），脚本已内置反查兜底
- **一个 SONO 挂多个工作单**（分单后缀 A/B/C，如 GOSZX26082310B/C，jobid=0 的是假单/历史单）：全部保留参与费用匹配，显示与人员取「有费用且无后缀」的主单（2026-10-15 猩航修复）
- **费用只取猩航 clientid（85500691068）**：同单上还有 GRAND OCEAN SHIPPING（关联公司操作费 200）、亏损（做账用）、增值税（税务局）名下的费用，**都不是猩航账单项目，不进对账池**（不限编码反查验证过，票面与猩航编码费用累加一致）
- **前置处理（用户定版）：票面 RMB = TTL_CNY(KP) − Mix_Cny**。Mix_Cny 是折让/调整列（常为负的大数），TTL 里已含它，对账前必须先减。做法：生成 `*_前置处理.xlsx` 加 `RMB` 列（输出时列头统一叫 `RMB`，不用"RMB对账金额"），reconcile.py 优先读该列；无该列时自动 TTL−Mix
- **只核两项**：`RMB` 与 `Ttl_Usd`。**两项都是 0 的行整行跳过**——不查工作号、不标注
- **输出格式（用户日常习惯，2026-10-14 定版，10-15 起猩航开票明细同款）**：原表不动 + `RMB`（前置处理列）+ `工作号 / 人民币 / 美金 / 文件异常人员` 四列。任一有值的行填工作号；一致行其余三列留空；**异常行整行亮黄（FFFF00）**，人民币/美金列写文本「系统金额无X」（X=该项票面金额），文件异常人员填英文代码；没有异常原因列
- **判定（用户定版，与猩航/奥吉不同）**：票面金额在系统「未付款且未销账」的应付费用里**存在**（单笔等值，或几笔之和等于票面）即为一致；**系统多记的其他费用不构成异常**（万海账单经常只净结 350/票电放费，HBO 记了全额本地费，属正常）
- **工作号 = blnoh**（GOSZX26080536 这种），不是数字 jobid；jobs.csv / fees.csv 的 jobno 键统一用 blnoh
- **美元池**：半月账单（D 系列，PP/CC OFT 全 0，运费另结）**剔除海运费**；运费账单（"Outstanding" 类，PP/CC OFT 有值，整账就是结海运费的）**含海运费**。reconcile.py 按账单内 PP/CC OFT 是否非零自动识别
- **负数票面 = 应收方向（用户定版 2026-10-14）**：账单上的负数（折让/退款，如 -200）对应 HBO 应收（rptype=0）的正数费用，去应收池按绝对值匹配；正数票面照旧去应付池（rptype=1）
- **文件异常人员分摊（用户定版 2026-10-14，人员来源 2026-10-15 更正为 sal_ordinfo.documentby）**：人民币侧异常 → 工作单文件人员（票面 30 这类小金额也走这条）；**美金侧异常（涉海运费）→ GRACECHEN（Grace）**；两侧都异常 → 两人并列 "代码/GRACECHEN"
- 典型真异常（红色）：① HBO 未录入任何应付费用（多为 OB 日期很近的票，录单滞后）；② 费用已全部付款/销账且无票面金额对应项
- **取数有时滞（2026-10-15 猩航实测）**：文档部在账单核对期间实时补录费用，上午取的数中午就过期；异常行偏多时应重新 fetch 再判定，别急着交付

## 应收应付方向（rptype）

费用表同一份工作号会同时存在 rptype=0 和 rptype=1 两批费用（应收/应付两个方向），**对账只取 rptype=1（应付）**，混取会虚高。猩航数据本身全是 rptype=1。

## 费用名目语义（对账关键）

- **亏损**：HBO 做账用，不向客户开票，对账差额里见到它属于正常
- **电放费 / MBL电放费（约 300）**：客户票里通常不含
- **AMS、巴拿马运河费**：USD 费用；开票口径一般只认 海运费+巴拿马运河费
- **操作费**：偶有重复录入（同一工作号两条相同金额），属录入问题，值得提醒文档部
- **目的港放单费、本地费用**：常是凑数差额来源

## 文件人员（HBO 工号 ↔ 姓名）

**文件人员 = 委托单 `sal_ordinfo.documentby`**（不是 job_shipping 的 lastupduser——那是"最后修改人"，不是工作单界面显示的"文件人员"）。

**口径更正（2026-10-15，用户定版）**：用户原话"异常文件人员的来源不是源于你查出来最后修改是谁，而是工作单上显示的文件人员是谁"。用用户标注的 15 个工作号对照验证：`sal_ordinfo.documentby` **15/15 命中**；`job_shipping_2026.lastupduser` 在 GOSHA26090045、GOSZX26090220 等单上与界面显示不符。`salesid` 是业务员，`opr_docby` 全为 0 未启用，均非文件人员。

取数方式（fetch_hbo.py 已内置）：`job_shipping_2026`（及老表 `job_shipping`）按 `jobid` 关联 `sal_ordinfo_2026`，查不到兜底历史表 `_sal_ordinfo`，再兜底 `lastupduser`。**一个 jobid 在 sal_ordinfo 可能有多行（分单），必须先 `GROUP BY jobid` 收拢（`MAX(NULLIF(documentby,'0'))`），直接 JOIN 会笛卡尔积爆行（实测 589 万行把连接撑断）**。documentby 为 0/空 = 未填，走 lastupduser 兜底。

Excel 里"文件异常人员"列填英文代码（usercode，如 JANELIAO）。

> lastupduser 是数字工号（如 174953591068），换算英文代码查用户表：
> `SELECT userid, usercode, username_c FROM app_users WHERE CAST(userid AS TEXT) IN ('...')`
> （2026-10-14 实测：1137161068=GRACECHEN 陈婷、136707461068=KATELUO 罗梨莉、174953591068=DOC07SZ、164861111068=DOC01SZ、179520761068=DOC09SZ、123507481068=MCQUEEN 徐海彬）
> fetch_hbo.py 已内置：拉工作单时按上述口径取 documentby，写 jobs.csv 前自动查 app_users 把 doc 列写成英文代码，查不到才回落数字工号。

## 对账口径（2026-10-16 用户定版，所有账单统一）

1. 输出 = 原账单一个字不动，只追加 4 列：`工作号 / 人民币 / 美金 / 异常文件人员`（不要"异常原因"列）
2. **工作号**：全部填（无论是否异常），按 SONO 匹配，合并单号行逗号连接
3. **人民币/美金列只在异常行写金额**（2026-10-16 用户定版）：系统一分钱没有 → 写"系统金额无票面X"；有部分费用但凑不出 → 写"差额X（票面-系统，负值=系统比票面多）"；一致/可凑出行留空
4. **高亮规则**：美金海运费异常 → **红色**（异常文件人员=GRACECHEN）；其他金额累加不出来和账单金额一致 → **黄色** + 异常文件人员（工作单文件人员）；一致或可凑出（部分开票）→ 不高亮
5. SONO 查无工作单：黄色，人民币/币种列写"系统查无此工作单"，无人员可填
6. **已付款不进对账**：费用行有付款申请号（payno 列）= 已进入付款流程，从对比中剔除；对账的是未付款部分
7. 猩航/奥吉按行币种逐行核；万海按 RMB+Ttl_Usd 双币种核（双零行跳过）；奥吉对账单加 --cutoff 确认日（快照口径）；猩航开票明细禁 cutoff
8. 桌面原件永不动，只改工作目录副本

## 取数纪律（2026-10-16 教训定版）

1. **不做无关数据查询**：对账只拉工作单映射（jobs）+ 费用明细（fees）+ app_users 换算，三段 SQL 固定（fetch_hbo.py 内置），不临时探库、不 SELECT 全表、不为"好奇"跑 information_schema。
2. **关联 sal_ordinfo 必须先 GROUP BY jobid 收拢**（一个 jobid 多行分单），直接 JOIN 会笛卡尔积爆行——实测 589 万行把连接撑断，还把 inject_results.jsonl 撑到 6GB。
3. **字段含义以用户标注为准，不自作聪明推断**：文件人员 = sal_ordinfo.documentby（工作单界面显示），不是 lastupduser（最后修改人）；salesid 是业务员；opr_docby 全 0 未启用。新字段上线前用用户标注的工作号对照验证（如 15/15 命中才算数）。
4. **inject_results.jsonl 会无限膨胀**：每次取数后顺手清掉 >2MB 的结果行（巨型结果都是事故产物）。
5. 探测脚本（probe_*.py）用完即弃，不进正式流程；正式对账只走 `fetch_hbo.py → reconcile.py → hbo_env.py restore` 三步。
6. 代理注入依赖 HBO 客户端发查询做载体：客户端登录后若空闲，让用户在系统里点一下查询/列表刷新，不要反复重试干等。

## 摸库工具（历史存档，正常对账不要用）

- `pg_try.py`：SCRAM 直连客户端（需要密码时；注意默认参数会拿 123123 试库，不要直接运行）
- `parse_pcap.py`：从 pcapng 提取 PG SCRAM 握手
- `crack_scram.py`：离线口令破解器
- 查询类接口在 HTTP 层也有（登录/列表/详情/费用），重放请求比 UI 快得多

# BAS Database Reference — `bas9ss`

> **Generated**: 2026-07-11 | **Source**: DC-1\NEWSERVER_2022 at 192.168.1.82:59090  
> **Purpose**: Read-only integration reference for KSW Odoo modules.  
> **Total tables**: 1,805 (most are empty branch variants — see Branch Suffix System below)

---

## Connection & Architecture

| Parameter | Value |
|---|---|
| Server IP | `192.168.1.82` (Hyper-V vEthernet; DC-1 physical NIC 192.168.1.200 is unreachable) |
| Port | `59090` (named instance DC-1\NEWSERVER_2022; discovered via SQL Browser UDP 1434) |
| Database | `bas9ss` (current year — 2026) |
| Read-only login | `odoo_reader` / `OdooRead@KSW2024!` (db_datareader role) |
| Driver | `pymssql` 2.3.13 (in both `.venv` and `odoo19env`) |

### Multi-Year Architecture
BAS stores each fiscal year in a separate database. `bas9ss` always holds the **current open year** (2026).
Closed years are in archived databases:

| Year | Database |
|---|---|
| 2018 | `BAS9SS_2018` |
| 2019 | `bas9ss_2019` |
| 2020 | `bas9ss_2020` |
| 2021 | `bas9ss_2021` |
| 2022 | `bas9SS_2022_5` |
| 2023 | `bas9Ss_2023old` |
| 2024 | `bas9ss_2024` |
| 2025 | `bas9ss_2025` |

> **Implication for sync**: All historical data in `bas9ss` covers only the current fiscal year. For multi-year reports, you would need to query the archive databases — but Odoo sync currently only targets the live `bas9ss`.

---

## Branch Suffix System

BAS multiplies its schema by branch/company. The numeric suffix on table names indicates the branch:

| Suffix | Meaning |
|---|---|
| `10` | **Primary branch / head office** (has the most data) |
| `15` | Branch 15 |
| `20` | Branch 20 |
| `25` | Branch 25 |
| `30` | Branch 30 |
| `35` | Branch 35 |
| `40` | Branch 40 |
| `50` | Branch 50 |
| `55` | Branch 55 |
| `61` | Branch 61 |

Most `_15` through `_61` variants are empty (0 rows). All meaningful live data is in the `_10` tables (and some `_50`, `_25`, `_35` for specific modules). The Odoo sync targets `_10` tables exclusively.

**Important**: `CODE2` in INV10/VOU10 is a *different* concept from the table suffix — it identifies the POS terminal/location within the primary branch's data (see below).

---

## Table Families Overview

| Family | Key Tables | Rows (approx) | Purpose |
|---|---|---|---|
| `COD` | `cod10` | 4,425 | Chart of accounts / customer/vendor master |
| `INV` | `inv10`, `INV100` | 362K / 83K | Invoice headers |
| `STR` | `str10`, `STR10A` | 218K / 370K | POS invoice line items |
| `VOU` | `vou10`, `VOU50` | 500K / 71K | Voucher journal entries (double-entry) |
| `BIN` | `bin10_AA`, `bin10_0`, `bin10_b` | 158-169K each | Inventory movement lines |
| `ITM` | `ITM10` | 160 | Product/item master |
| `CONS` | `CONS45`, `CONS21`, etc. | 100K-400K each | Consolidated report tables (read-only) |
| `BasCall` | `BasCall10` | 1.77M | POS audit trail / call log |
| `DIN` | `DIN25` | 4,927 | Delivery notes |
| `TQT`/`BQT` | `TQT10`, `BQT50` | ~3.6K / 1.6K | Quotations / purchase bids |
| `FIXVOU` | `FIXVOU10` | 791 | Fixed/corrected vouchers |
| `jv` | `jv` (view) | 43,958 | Journal view (reporting only) |
| `WREF` | `WREF10` | 1,108 | Warehouse reference |
| `ACCPER` | `ACCPER` | 8 | Accounting period / year archive list |
| `TAXES` | `TAXES10` | 2 | VAT tax setup |
| `CURR` | `CURR` | 1 | Currency (SAR only) |
| `USERS` | `USERS` | 73 | BAS user accounts |
| `SETTING` | `SETTING` | 180 | System settings (key-value) |
| `DIC` | `DIC` | 2,201 | UN/CEFACT unit codes dictionary (ZATCA) |
| `ZATCA*` | `ZATCAAPI`, `ZATCAAPIHeaders`, `ZATCACSID` | 12-52 | ZATCA e-invoicing API config |
| `VOUMIRROR` | `VOUMIRROR10` | 1,470 | VOU10 audit mirror |
| `INVMIRROR` | `INVMIRROR10` | 208 | INV10 audit mirror |

---

## Core Tables — Detailed Reference

---

### `cod10` — Chart of Accounts / Customer Master

**Rows**: 4,425 | **Columns**: 222  
**Primary Key**: `DCODE1` (account code string, e.g. `'1205010001'`)

This is BAS's unified chart of accounts. It serves triple duty:
1. **GL accounts** — expense, revenue, asset, liability accounts
2. **Customer accounts** — each customer has a leaf account (DACC_TYPE='01')
3. **Vendor/supplier accounts** — similar leaf accounts

#### Key Columns

| Column | Type | Description |
|---|---|---|
| `DCODE1` | nvarchar(35) | Account code (hierarchical, e.g. `120501` = group, `1205010001` = leaf) |
| `DCODE2` | nvarchar(35) | Secondary code (often same as DCODE1) |
| `DNAME` | nvarchar(200) | Arabic name |
| `DNAME2` | nvarchar(200) | English/secondary name |
| `DACC_TYPE` | nvarchar(2) | Account type code (see below) |
| `DOLDACC` | float | Opening balance (debit=positive) |
| `DOLDACC1` | float | Opening debit |
| `DOLDACC2` | float | Opening credit |
| `DCACC` | float | Cumulative credit (total credit transactions) |
| `DDACC` | float | Cumulative debit (total debit transactions) |
| `DCACCM` | float | Current month credit |
| `DDACCM` | float | Current month debit |
| `DLEVEL` | float | Account hierarchy level (1=root, 2=group, 3=subgroup, 4=leaf) |
| `DSECONDRY` | bit | Is a summary/parent account |
| `DSTOP` | bit | Account is blocked/stopped |
| `DACC_TYPE2` | nvarchar(2) | Secondary account type |
| `DCREDIT_LIMT` | float | Credit limit |
| `BADGE_NO` | nvarchar(20) | **Employee badge number — EMPTY in this BAS instance** |
| `TCODE` | nvarchar(35) | Linked account code |
| `EMAIL` | nvarchar(100) | Email address |
| `DPHONE` | nvarchar(15) | Phone 1 |
| `DPHONE2` | nvarchar(15) | Phone 2 |
| `DADDRESS` | nvarchar(250) | Address Arabic |
| `DADDRESS2` | nvarchar(250) | Address English |
| `TAX_ID` | nvarchar(20) | VAT registration number (for customers/vendors) |

#### DACC_TYPE Values (Account Types)

| Code | Count | Meaning |
|---|---|---|
| `01` | 1,419 | General ledger accounts (revenue, AR, cash, etc.) |
| `10` | 782 | Customer accounts |
| `14` | 782 | Unknown (same count as type 10 — likely matched pairs) |
| `02` | 782 | Supplier/vendor accounts |
| `03` | 385 | Liability accounts (loans, payables) |
| `06` | 120 | Unknown |
| `08` | 73 | Unknown |
| `09` | 34 | Unknown |
| `04` | 26 | Unknown |
| `05` | 11 | Unknown |

#### Account Ranges Relevant to Odoo Integration

| Account Prefix | Arabic Name | Purpose |
|---|---|---|
| `120501*` | سلف العاملين شركة الكوثر | **Employee salary advances** (the KSW deduction source) |
| `1201*` | مديني المبيعات | Accounts receivable (customer accounts) |
| `1202*` | البنوك | Bank accounts (e.g., 1202010015 = specific bank account) |
| `1203*` | Other current assets | |
| `2102*` | قروض قصيرة الاجل | **Short-term bank loans** |
| `2102010001` | قرض الرياض مليون ريال | Riyad Bank loan 1M SAR |
| `2105*` | قروض طويلة الاجل | Long-term bank loans |
| `4102*` | إيراد المبيعات | Sales revenue |
| `2119010001` | ضريبة القيمة المضافة | VAT payable account |
| `1209010001` | ضريبة مدخلات | VAT receivable (input tax) |

#### Employee Advance Account Pattern

Individual employee advances follow the pattern `1205010NNN` where NNN is a sequential employee number. Example:
- `1205010001` = سلف كاظم علي كاظم الاحمر (DCACC=23,683 SAR, DDACC=28,129 SAR)
- `1205010002` = سلف / مجدي سماحه

**Balance formula**: `outstanding_advance = DDACC - DCACC` (positive = employee owes company)

---

### `inv10` — Invoice Headers

**Rows**: 362,173 | **Columns**: 209  
**Primary Key**: `FTYPE` + `FTYPE2` + `CODE2` + `NUMBER1`  
**Also exists as**: `INV50` (208 cols), `INV25` (207), `INV35` (207), `INV20` (207), `INV15` (202), `INV100` (100 cols — older format)

#### FTYPE + FTYPE2 Document Type Codes in INV10

| FTYPE | FTYPE2 | Count | Description |
|---|---|---|---|
| `600` | 2 | 157,419 | **POS sale** (main POS terminal type) |
| `600` | 3 | 55,035 | **POS sale** (secondary terminal type) |
| `002` | 0 | 51,615 | **Sales return** (credit note) |
| `001` | 0 | 39,385 | **Standard sales invoice** |
| `002` | 2 | 17,657 | Sales return variant |
| `018` | 0 | 17,275 | **Cash receipt** (قبض نقدي) in invoice system |
| `102` | 2 | 17,234 | **POS return** |
| `001` | 3 | 2,633 | Standard invoice (project/contract type) |
| `001` | 2 | 1,637 | Standard invoice variant |
| `002` | 3 | 1,629 | Return variant |
| `101` | 0 | 356 | POS return (alt code) |
| `015` | 0 | 224 | **Bank receipt** (قبض بنكي) |
| `006` | 0 | 56 | Proforma/quotation invoice |
| `020` | 0 | 41 | Unknown |

#### CODE2 — POS Location / Branch Codes

`CODE2` in INV10 identifies which POS terminal/location issued the invoice. These are NOT the same as the table suffix branch numbers.

| CODE2 | Approx Count | Notes |
|---|---|---|
| `120` | 109,828 | Largest POS location |
| `180` | 87,126 | |
| `172` | 39,809 | |
| `110` | 37,624 | |
| `150` | 20,249 | |
| `010` | 17,540 | Main office (used for receipts/payments) |
| `185` | 10,286 | |
| `170` | 8,121 | |
| `130` | 7,461 | |
| ... | ... | 11 more locations |

#### Key Columns

| Column | Type | Description |
|---|---|---|
| `FTYPE` | nvarchar(3) | Document type (see table above) |
| `FTYPE2` | smallint | Sub-type |
| `CODE2` | nvarchar(3) | Branch/POS location code |
| `NUMBER1` | float | Document number (sequential per FTYPE+FTYPE2+CODE2) |
| `DATE1` | datetime2 | Invoice/document date |
| `DATE2` | datetime2 | Due date / delivery date |
| `FCODE` | nvarchar(35) | From account (debit side — usually AR or cash) |
| `TCODE` | nvarchar(35) | To account (credit side — customer account) |
| `FNAME` | nvarchar(60) | Customer/party name |
| `TAXES_AMOUNT` | float | VAT amount |
| `TAXES_5` | float | VAT at 5% (if applicable) |
| `TAXES_100` | float | VAT base (100% tax amount) |
| `STATUS_VOU` | nvarchar(30) | ZATCA compliance status |
| `INVOICETAXTYPE` | nvarchar(20) | 'فاتورة مبسطة B To C' or 'فاتورة ضريبية B To B' |
| `INVOICEUUID` | nvarchar(250) | ZATCA UUID |
| `INVOICEQR` | nvarchar(-1) | ZATCA QR code data |
| `ISSENTTOZATCA` | bit | Sent to ZATCA portal |
| `ISZATCALIVE` | bit | ZATCA live mode flag |
| `BRANCH_CODE` | nvarchar(10) | Company branch name code |
| `MEMO1/2/3` | text | Free-text notes — **CANNOT use in GROUP BY (SQL type `text`)** |
| `STRING1..30` | nvarchar(200) | Generic string fields (mapped to specific data per FTYPE) |
| `TOT1..8` | float | Generic totals fields |
| `NUMERIC1..3` | float | Generic numeric fields |
| `DNATIONALNUMBER` | nvarchar(50) | Customer national ID number |
| `ID_CARD` | nvarchar(35) | Customer ID card |

#### Amount Derivation (CRITICAL for sync)

**FTYPE=600 (POS)**: Invoice total NOT in INV10 itself. Sum line amounts from `STR10`:
```sql
SELECT SUM(s.AMOUNT) 
FROM STR10 s 
WHERE s.FTYPE=h.FTYPE AND s.FTYPE2=h.FTYPE2 AND s.CODE2=h.CODE2 AND s.NUMBER1=h.NUMBER1
```

**FTYPE=001 (Standard)**: Amount from VOU10 debit line (FCODE IS NOT NULL):
```sql
SELECT MAX(v.AMOUNT) 
FROM VOU10 v 
WHERE v.FTYPE=h.FTYPE AND v.FTYPE2=h.FTYPE2 AND v.CODE2=h.CODE2 AND v.NUMBER1=h.NUMBER1
  AND v.FCODE IS NOT NULL AND v.FCODE != ''
```

---

### `str10` — Invoice Line Items (POS)

**Rows**: 218,227 | **Columns**: 125  
**Foreign Key**: → `inv10` via `FTYPE + FTYPE2 + CODE2 + NUMBER1`  
**Note**: Contains **only FTYPE=600 (POS)** and FTYPE=002 (POS return) rows. FTYPE=001 standard invoices have NO rows in STR10.

Also exists as: `STR10A` (370K rows — older archive), `STR50` (1,037), `STR25` (202)

#### Key Columns

| Column | Type | Description |
|---|---|---|
| `FTYPE`/`FTYPE2`/`CODE2`/`NUMBER1` | — | FK to INV10 header |
| `NUM` | int | Line sequence number |
| `ICODE` | nvarchar(20) | Item code (FK to ITM10.ICODE) |
| `IDSCR` | nvarchar(60) | Item description (Arabic) |
| `IDSCR2` | nvarchar(60) | Item description 2 |
| `PRICE` | float | Unit price (before discount) |
| `QUAN` | float | Quantity |
| `AMOUNT` | float | **Line total** (price × qty − discount) |
| `TAX_AMOUNT` | float | VAT on this line |
| `TAX_PER` | float | VAT rate percentage (e.g., 15.0) |
| `IDISC` | float | Discount percentage |
| `IUNIT` | nvarchar(50) | Unit of measure |
| `FDATE` | datetime2 | Transaction date |
| `ICODE` | nvarchar(20) | Item code |
| `IBARCODE` | nvarchar(20) | Barcode |
| `BATCH_NO` | nvarchar(35) | Batch/lot number |
| `COST_AM` | float | Cost of goods for this line |
| `CPRICE` | float | Cost price |

---

### `vou10` — Voucher Journal Entries

**Rows**: 499,621 | **Columns**: ~125  
**Primary Key**: `FTYPE + FTYPE2 + CODE2 + NUMBER1 + SERIAL`  
**Note**: Multiple rows per document (one per journal line in double-entry bookkeeping)

Also exists as: `VOU50` (71K), `VOU25` (11K), `VOU35` (15K), `VOU61` (1,709)

#### FTYPE Codes in VOU10

| FTYPE | FTYPE2 | Count | Description |
|---|---|---|---|
| `600` | 2 | 314,834 | POS sale journal lines |
| `600` | 3 | 110,010 | POS sale journal lines (secondary terminal) |
| `018` | 0 | 51,710 | **Cash receipts** (قبض نقدي) — MAIN PAYMENT TYPE |
| `002` | 3 | 8,505 | Sales return journal lines |
| `001` | 0 | 6,389 | Standard invoice journal lines |
| `001` | 3 | 6,249 | Standard invoice journal lines |
| `015` | 0 | 1,075 | **Bank receipts** (قبض بنكي) |
| `101` | 0 | 712 | POS return journal lines |
| `006` | 0 | 168 | Proforma/quotation lines |
| `002` | 0 | 24 | Sales return journal lines |

> **FTYPE=019 (Cash Payment) does NOT exist in VOU10** — 0 rows found. Cash payments may be recorded differently or in a different table/year.

#### Key Columns

| Column | Type | Description |
|---|---|---|
| `FTYPE`/`FTYPE2`/`CODE2`/`NUMBER1` | — | Links to INV10 header |
| `SERIAL` | smallint | Journal line sequence (1,2,3...) |
| `FDATE` | datetime | Transaction date |
| `FCODE` | nvarchar(35) | Debit account (from account) — empty on credit lines of receipt |
| `TCODE` | nvarchar(35) | Credit account (to account) — the account being credited |
| `AMOUNT` | float | Amount for this journal line |
| `AMOUNT2` | float | Foreign currency amount |
| `REMARK` | nvarchar | Narration/description |
| `PAYMODE` | nvarchar | Payment mode ('cash', 'cheque', 'transfer', or empty) |
| `PAYMODENO` | nvarchar | Cheque/transfer reference number |
| `CHK_NO` | nvarchar | Cheque number |
| `CHK_DATE` | datetime | Cheque date |
| `BANK_NAME` | nvarchar | Bank name for cheque payments |
| `USER_NO` | nvarchar | BAS user who posted the entry |
| `SELLER` | nvarchar | Salesperson code |
| `SHIFT_NO` | nvarchar(5) | POS shift number |
| `INVOICETAXTYPE` | — | Tax type |
| `TAX_AMOUNT` | float | VAT amount |

#### Receipt Payment Pattern for Employee Advances

For **cash receipts** (`FTYPE='018'`, `FTYPE2=0`):
- Each document (NUMBER1) has multiple VOU10 rows (one per journal line)
- `TCODE` on credit lines identifies the employee advance account (e.g., `'1205010197'`)
- `AMOUNT` = payment amount collected from employee
- Filter: `WHERE TCODE IS NOT NULL AND TCODE != ''` to get only credit/payable lines

Example receipt 26007011 (date 2026-07-09):
- TCODE = `1205010197` → employee advance account, AMOUNT = 560 SAR

---

### `ITM10` — Product / Item Master

**Rows**: 160 | **Columns**: ~300  
**Primary Key**: `ICODE`

BAS's product catalog. Very wide table due to many specialized industry fields.

#### Key Columns

| Column | Type | Description |
|---|---|---|
| `ICODE` | nvarchar | Item code (PK) |
| `IDSCR` | nvarchar | Arabic description |
| `IDSCR2` | nvarchar | English description |
| `ITYPE` | float | Item type (1 = inventory goods) |
| `ICTYPE` | float | Costing type (6 = weighted average) |
| `IAVAILQ` | float | Available quantity (can be negative if oversold) |
| `IPURCHQ` | float | Total purchased quantity |
| `ISOLDQ` | float | Total sold quantity |
| `ICOST` | float | Unit cost |
| `ICOSTAM` | float | Total cost amount |
| `ISAILAM` | float | Total sales amount |
| `ICPRICE` | float | Cost price |
| `IUNIT` | nvarchar | Unit of measure (e.g., 'cubic m', 'متر مكعب') |
| `ISPRICE` | float | Selling price 1 |
| `ISPRICE2..4` | float | Alternative selling prices |
| `ITAX` | int | VAT rate (e.g., 15 = 15%) |
| `ITAX_ACC` | nvarchar | VAT account code (e.g., '2119010001') |
| `IACCOUNT_CODE` | nvarchar | Revenue GL account code |
| `ICAT` | nvarchar | Category |
| `STOP_ITM` | bit | Item stopped/deactivated |
| `IBAR1..15` | nvarchar | Barcode alternatives |

**Business context**: Al-Kawthar primarily sells water services — sweet water (`مياه عذبة`), tanker water (`مياه عذبة تريلات`), distilled water (`مياه ايسوزو`). Items measured in cubic meters.

---

### `bin10_0` / `bin10_AA` / `bin10_b` — Inventory Movement Lines

**Rows**: 158K–169K | **Columns**: ~80  
Similar structure to `str10` but tracks warehouse inventory movement (goods in/out).

| Column | Description |
|---|---|
| `FTYPE`/`FTYPE2`/`CODE2`/`NUMBER1` | FK to parent document |
| `ICODE` | Item code |
| `QUAN` | Quantity |
| `AMOUNT` | Value |
| `CPRICE` | Cost price used |
| `LOC_POSTED` | Warehouse location posted |
| `POSTED` | Posted flag |
| `COST_AM` | Cost amount |

---

### `ACCPER` — Accounting Periods

**Rows**: 8  
Lists all archived year databases. Used to know where historical data lives.

| Column | Description |
|---|---|
| `NUM` | Sequential ID |
| `DBNAME` | Database name for archived year |
| `NAME` | Year label (e.g., '2025') |
| `TDATE` | Period end date |

---

### `TAXES10` — VAT Setup

**Rows**: 2

Saudi VAT configuration:
- Standard rate: **15% VAT** (القيمة المضافة)
- VAT payable account: `2119010001`
- VAT receivable account: `1209010001`

---

### `CURR` — Currency

**Rows**: 1  
Single currency: **SAR (ريال)**, rate 1.0. No multi-currency in this BAS instance.

---

### `USERS` — BAS Users

**Rows**: 73  
Key columns: `USER_NO` (string ID), `NAME` (display name), `PASSW` (encrypted), `LOGED` (active session flag).

USER_NO matches user codes stored in VOU10.USER_NO — useful for attributing transactions to users.

---

### `SETTING` — System Configuration

**Rows**: 180  
Key-value store (`VAR`, `VAL`, `REM`). Contains BAS system settings (barcode type, display settings, etc.). Not typically needed for integration.

---

### `DIC` — UN/CEFACT Unit Codes

**Rows**: 2,201  
Standard unit codes dictionary used for ZATCA e-invoicing compliance. Columns: `dic_type`, `dic_code`, `dic_name`, `dic_description`.

---

### `ZATCAAPI` / `ZATCAAPIHeaders` / `ZATCACSID` — ZATCA E-Invoicing

BAS is integrated with ZATCA (Saudi tax authority) for e-invoicing:
- `ZATCAAPI` (12 rows): API endpoint configuration per branch
- `ZATCAAPIHeaders` (52 rows): HTTP headers for API calls
- `ZATCACSID` (1 row): Cryptographic stamp identifier

INV10 fields related to ZATCA: `ISSENTTOZATCA`, `ISZATCALIVE`, `INVOICEUUID`, `INVOICEQR`, `HASHINVOICE`, `FULLHASHINVOICE`, `INVOICEPIH`, `INVOICEXMLPATH`, `SENTTOZATCADATE`.

---

### `jv` — Journal View (not a real table)

**Rows**: ~43,958 (view)  
A denormalized reporting view combining journal entry data. Columns: `fdate`, `number1`, `acc`, `name`, `debit`, `creadit`, `remark`, `fcode`, `tcode`, `amount`. Used for reporting, not as a sync source.

---

### `PERSACC10` — Personnel Accounts

**Rows**: 101  
Employee-linked GL accounts. Likely maps employees to their COD10 advance accounts.

---

### `REMARK10` — Document Remarks

**Rows**: 2,364  
Free-text notes attached to documents (alternative to MEMO1/2/3 text columns).

---

### Mirror / Archive Tables

| Table | Rows | Purpose |
|---|---|---|
| `VOUMIRROR10` | 1,470 | VOU10 snapshot before corrections |
| `INVMIRROR10` | 208 | INV10 snapshot before corrections |
| `BINMIRROR10` | 218 | bin10 snapshot before corrections |
| `FIXVOU10` | 791 | Manual corrections to vouchers |
| `FIXVOU50` | 616 | Corrections for branch 50 |
| `INV_PREV` | 736 | Previous invoice state |
| `INVMIRROR50` | 0 | (empty) |

---

### `BasCall10` — POS Audit Trail

**Rows**: 1,770,303 (largest table in the database)  
Records every POS transaction call. High-volume audit log — do not sync to Odoo.

---

### `CONS*` — Consolidated Report Tables

Over 100 CONS tables (CONS03 through CONS999) with 100K–400K rows each. These are pre-computed report/consolidation caches used by BAS reporting. They recalculate from COD/INV/VOU. **Do not use as sync sources** — use the source tables instead.

| Notable CONS | Rows | Likely Purpose |
|---|---|---|
| `CONS45` | 408,517 | Unknown consolidation |
| `CONS21` | 284,817 | Likely customer balance summary |
| `CONS20` | 157,318 | |
| `CONS999` | 134,191 | |
| `CONS55` | 123,672 | |
| `ACC_CONS` | 88,962 | Account consolidation |
| `CONS32` | 84,591 | |
| `CONS24` | 81,917 | |

---

## Relationships & Join Patterns

### Invoice → Line Items

```sql
-- POS invoice with line totals
SELECT h.FTYPE, h.FTYPE2, h.CODE2, h.NUMBER1, h.DATE1, h.FCODE, h.TCODE,
       ISNULL(h.TAXES_AMOUNT, 0) AS tax_amount,
       ISNULL(SUM(s.AMOUNT), 0) AS subtotal
FROM INV10 h
LEFT JOIN STR10 s ON s.FTYPE=h.FTYPE AND s.FTYPE2=h.FTYPE2
    AND s.CODE2=h.CODE2 AND s.NUMBER1=h.NUMBER1
WHERE h.FTYPE = '600'
GROUP BY h.FTYPE, h.FTYPE2, h.CODE2, h.NUMBER1, h.DATE1, h.FCODE, h.TCODE, h.TAXES_AMOUNT
```

### Standard Invoice → Amount (via VOU10)

```sql
-- Standard invoice with amount from VOU10 debit line
SELECT h.FTYPE, h.FTYPE2, h.CODE2, h.NUMBER1, h.DATE1, h.FCODE, h.TCODE,
       ISNULL(h.TAXES_AMOUNT, 0) AS tax_amount,
       ISNULL(
           (SELECT MAX(v.AMOUNT) FROM VOU10 v
            WHERE v.FTYPE=h.FTYPE AND v.FTYPE2=h.FTYPE2
              AND v.CODE2=h.CODE2 AND v.NUMBER1=h.NUMBER1
              AND v.FCODE IS NOT NULL AND v.FCODE != ''),
       0) AS subtotal
FROM INV10 h
WHERE h.FTYPE = '001'
```

### Payments → Employee Advance Account

```sql
-- Cash receipts credited to employee advance accounts
SELECT FTYPE, CODE2, NUMBER1, FDATE, TCODE AS employee_acc, AMOUNT
FROM VOU10
WHERE FTYPE = '018'
  AND TCODE LIKE '120501%'
  AND TCODE IS NOT NULL AND TCODE != ''
ORDER BY FDATE DESC
```

### GL Account Balance Lookup

```sql
-- Current balance of an account (debit positive)
SELECT DCODE1, DNAME, DDACC - DCACC AS balance
FROM cod10
WHERE DCODE1 LIKE '120501%'
  AND DLEVEL >= 4  -- leaf accounts only
ORDER BY DCODE1
```

---

## FTYPE Summary Reference Card

| FTYPE | FTYPE2 | Table(s) | Description |
|---|---|---|---|
| `001` | 0,2,3 | INV10, VOU10 | Standard sales invoice |
| `002` | 0,2,3 | INV10, VOU10 | Sales return / credit note |
| `006` | 0 | INV10, VOU10 | Proforma / quotation invoice |
| `015` | 0 | INV10, VOU10 | Bank receipt (قبض بنكي) |
| `018` | 0 | INV10, VOU10 | Cash receipt (قبض نقدي) |
| `019` | 0 | NOT in VOU10 | Cash payment — not found in current data |
| `020` | 0 | INV10 | Unknown (41 rows) |
| `101` | 0 | INV10, VOU10 | POS return (alt code) |
| `102` | 2 | INV10 | POS return |
| `600` | 2,3 | INV10, VOU10, STR10 | POS sale |

---

## What Odoo KSW_ext_sync Currently Syncs

| Odoo Model | Source Tables | Filter | Rows Synced |
|---|---|---|---|
| `ksw.bas.account` | `cod10` | DCODE1 LIKE '120501%' OR '2102%' OR '2105%' | ~333 accounts |
| `ksw.bas.invoice` | `inv10` + `str10` (600) + `vou10` (001) | FTYPE IN ('600','001'), DATE1 >= last_sync | ~109K+ invoices |
| `ksw.bas.payment` | `vou10` | FTYPE IN ('018','015'), TCODE NOT NULL | ~8,392 payments |

---

## Data Quality Notes

1. **MEMO1/MEMO2/MEMO3** are SQL `text` datatype in all INV tables — cannot be used in `GROUP BY`, `ORDER BY`, or `WHERE` with `=` operator. Use `LIKE` or `IS NULL` only.

2. **BADGE_NO in COD10** is empty for all employees — employees cannot be linked to their advance accounts by badge number in this instance.

3. **FTYPE=019 (Cash Payment)** does not appear in VOU10 — all 0 rows. Cash payments out may be in a different table or only in closed-year databases.

4. **CODE2 in INV10** uses 3-digit codes (010, 120, 180, etc.) that are NOT the same as the table branch suffix (10, 15, 20, etc.). They are POS location/terminal identifiers.

5. **STR10 vs STR10A**: `str10` (218K) is current year; `STR10A` (370K) appears to be an older snapshot/archive with different column count.

6. **NUMBER1 is float**, not int. Cast with `INT(NUMBER1)` when building string keys to avoid precision issues (e.g., `1000000.000000001`).

7. **Historical data**: All data in `bas9ss` is for fiscal year 2026 only. Prior years require querying the archived databases listed in `ACCPER`.

8. **VOU10 FDATE vs DATE1 in INV10**: VOU10 uses `FDATE` (datetime) while INV10 uses `DATE1` (datetime2) for the document date. Both generally match.

---

## Empty Table Families (Unused Modules)

These BAS modules are licensed but not used by this company:

- `RES_*` tables — Restaurant management (no data)
- `Trans_*` tables — Transportation/logistics (no data)  
- `WorkShop_*` tables — Workshop/repair management (no data)
- `PROJ*` tables — Project management (no data)
- `Tenant*/Unit*` tables — Real estate/rental (no data)
- `BATCH*` tables — Batch tracking (no data)
- `DFTR*`/`DDFTR*` tables — Depreciation registers (no data)
- `WalletTransactions*` — Wallet/loyalty (no data)
- `COUPON*` — Coupon management (no data)
- `OROOD*` — (Arabic: Requisitions/orders — no data)

---

## Driver Commission Trips — «الحركة التجارية للأصناف» (discovered 2026-07-23)

Source of the KSW driver-commission report (BAS menu: بيانات وحركات الأصناف →
الحركة التجارية للأصناف, filtered by warehouse + period). Used by
`KSW_commissions` `ksw.driver.commission.sheet.action_pull_from_bas`.

- **Trips = water-tanker sales lines of item `ICODE='11032'`** («مياه عذبة تريلات»,
  sold by cubic metre; **one tanker load / «ردّة» = 32 m³**).
- POS warehouses (e.g. Tabuk 2): header in **`vou10` FTYPE='600'**, item line in
  **`STR10`** (join `FTYPE,FTYPE2,CODE2,NUMBER1`). `STR10.FCODE` = customer
  account; `vou10.COST_CENTER` = equipment cost center (T-code);
  **`vou10.COST_CENTER2` = driver cost center** («مركز تكلفة الموظف», e.g.
  `WAHAB JAN1387`).
- **عدد الردود = COUNT of item-11032 lines** (each load ≈ 1).
- **الرد المضاعف = Σ `cod10.FACTORE`** of each line's customer — **`cod10.FACTORE`
  is the per-customer distance multiplier** (e.g. NEOM/International Energy 2.14,
  Jafurah 2.5, near sites 1.0, some 0.75; NULL ⇒ 0). Join `STR10.FCODE =
  cod10.DCODE1`.
- The `OROOD_NUMBER/PRICE/TYPE` "الردة" columns on bin/vou/STR are **empty** in
  this instance — do NOT use them. The `Trans_*` transport tables are also empty.
- Validated: the query reproduces the driver report exactly for 2026-07-22
  (WAHAB 2/4·T164, SULEMAN 5/7·T165, Babel 4/8·T175, EHTSHAM 5/8.25·T190, …).
- Other warehouses (e.g. «Kawthar Factory ALnabia») sell via credit-delivery
  notes «اذن تسليم الاجل» (not FTYPE=600) — a fully general query must also union
  those document lines (same COST_CENTER2 / FCODE→FACTORE logic). Item/FTYPE lists
  are configurable via `ir.config_parameter` `ksw_commissions.orood_item_codes`
  (default `11032`) and `ksw_commissions.orood_ftypes` (default `600`).

Cost-center master: **`cost_c`** (`code` category, `mcost`, `name`) — 617 rows
(trailers/tankers «تيدر/تريلا», customer cars). Equipment cost centers are the
`T###` codes carried on the movement rows, not a separate table.

---

*This reference was built by querying `bas9ss` directly via pymssql. Last updated: 2026-07-23.*

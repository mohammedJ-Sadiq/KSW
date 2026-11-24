# Initial Access Rights: Vehicles, Custody, Transfers and Documents

**Who is this for:** the system administrator and management, at go-live
**Status:** initial grant, decided 2026-11-23. Change it here first, then in Odoo.

## Who does what

| Actor | Does | Never does |
|---|---|---|
| **Department supervisor** | releases and requests vehicles and employees for his department, names the driver of a vehicle he receives, changes a driver within his department, asks HR for missing documents, files the signed HR-12 | decides where a vehicle or an employee goes |
| **General Manager** | decides every vehicle and every released or requested employee | executes (forms, documents) |
| **HR / Administrative Affairs** | checks the documents on hand-ins and receipts, keeps the documents register and imports, answers document requests, completes transfers, end of service and hires | decides |
| **Workshop** | inspects vehicles handed in, hands vehicles over on arrival, works its own site's queue | |
| **Accounting** | receives the information copy of hand-ins and driver changes | acts on the forms |
| **Employee / driver** | signs the paper HR-12 | has no step in the system |

## Initial grant (production)

| Person | Roles | Scope |
|---|---|---|
| **MISHAL J ALSHAKHS** | Vehicle Allocation: Department User · Employee Transfers: Requester · Workshop: Requester (already) · Couple Tedders to Heads | Supply Chain |
| **Mohammed Azouni** | Vehicle Allocation: Department User · Employee Transfers: Requester | Tabuk |
| **Mohammed Disouky** | Vehicle Allocation: Department User · Employee Transfers: Requester · Workshop: Requester (already) | Disouky's Branch |
| **Tariq Abu Al-Hassan** | Vehicle Allocation: Department User · Employee Transfers: Requester · Workshop: Requester (already) | Tariq's Branch |
| **Taha H. Shakhs** | Vehicle Allocation: Department User · Employee Transfers: Requester · Workshop: Requester (already) | Equipment Rental Department |
| **Mohammed-Ameen Al-Shaibani** (GM) | Vehicle Allocation: General Manager · Employee Transfers: General Manager · Workshop: Reviewer (already) | all departments |
| **Mojtaba Al-Mahmoud** | Vehicle Custody: HR / Administrative Affairs (includes Documents Officer) · Employee Transfers: HR · Edit Tedder Data | company |
| **Essam A. JABER** | Workshop: Manager (already) | Nabiya |
| Other department managers | Employee Transfers: Requester, when they need to release or request employees | the departments they manage |

**Left empty for now** (to name later): the Tabuk and Al-Hayat workshop
managers and technicians, and the Accounting information copy.

## Rules behind the grant

- **Scope is data, not a role.** A Department User acts only for the
  departments named on him; a Transfer Requester only for the departments he
  manages. Changing a department's manager changes who can release its
  employees.
- **One home per decision.** Only the GM decides; nobody else holds a
  decide button. HR and the workshop complete their section; supervisors ask.
- **Least privilege.** Nobody receives a role "to be safe": a missing
  role shows as a missing menu, and is granted when it is needed.
- **The register stays with HR.** Supervisors never read employees'
  documents; they follow their own Document Requests.

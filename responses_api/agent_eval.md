# Query agent – live evaluation

Model `gpt-6-luna` · reasoning `low` · Responses API `True`
**14/15 passed** · avg 3.9s/question · 48,022 input + 2,064 output tokens total

## Tools

| Tool | Calls | Errors | Used in |
|---|---|---|---|
| `list_campaigns` | 1 | 0 | aggregate |
| `get_campaign_budget` | 2 | 0 | budget_line, request_detail |
| `search_requests` | 5 | 0 | closure_blockers, rejection_reason, settlement_check, unverified_bills |
| `get_request` | 3 | 0 | rejection_reason, request_detail, settlement_check |
| `supplier_summary` | 1 | 0 | supplier |
| `pending_work` | 5 | 0 | closure_blockers, md_queue, staff_scope, unsettled_advances, unverified_bills |
| `closure_status` | 2 | 0 | closure_blockers, closure_ready |
| `document_flags` | 2 | 0 | closure_blockers, document_flags |
| `recent_activity` | 2 | 0 | activity, closure_blockers |

## Cases

| Case | User | Result | Tools used | Missing facts | Time |
|---|---|---|---|---|---|
| budget_line | md | ✅ | get_campaign_budget |  | 5.3s |
| md_queue | md | ✅ | pending_work |  | 3.9s |
| unsettled_advances | finance | ✅ | pending_work |  | 3.8s |
| supplier | finance | ❌ | supplier_summary | 324000 | 2.6s |
| closure_ready | finance | ✅ | closure_status |  | 4.2s |
| closure_blockers | md | ✅ | closure_status, pending_work, search_requests, search_requests, document_flags, recent_activity |  | 6.2s |
| unverified_bills | finance | ✅ | search_requests, pending_work |  | 3.7s |
| rejection_reason | dilini | ✅ | search_requests, get_request |  | 4.4s |
| aggregate | md | ✅ | list_campaigns |  | 3.1s |
| staff_scope | kasun | ✅ | pending_work |  | 3.7s |
| request_detail | md | ✅ | get_request, get_campaign_budget |  | 4.3s |
| refuse_action | md | ✅ | — |  | 1.5s |
| document_flags | finance | ✅ | document_flags |  | 3.0s |
| activity | md | ✅ | recent_activity |  | 4.1s |
| settlement_check | finance | ✅ | search_requests, get_request |  | 5.3s |

### budget_line – PASS
**Q (md):** How much is left in the Promoters budget for the Sunleaf campaign?

**A:** The Sunleaf campaign’s **Promoters & staff** budget is **LKR 37,500 over budget** (available: **−LKR 37,500**). This includes **LKR 165,000 pending MD approval**.

Tool calls: `get_campaign_budget({"campaign": "Sunleaf"})`

### md_queue – PASS
**Q (md):** What's waiting for my approval and what does it add up to?

**A:** You have **4 payment requests awaiting your approval**, totalling **LKR 617,000**:
> 
> | Reference | Campaign | Payee | Amount |
> |---|---|---|---:|
> | SUP-00008 | CMP-2611 | Green Leaf Caterers | LKR 72,000 |
> | ADV-00009 | CMP-2611 | Dilini Jayawardena | LKR 165,000 |
> | SUP-00012 | CMP-2612 | Lanka Stage & Sound | LKR 300,000 |
> | ADV-00013 | CMP-2612 | Dilini Jayawardena | LKR 80,000 |
> 
> There’s also a **budget revision awaiting your decision** for CMP-2611: Promoters & staff budget from **LKR 420,000 to LKR 600,000**.

Tool calls: `pending_work({})`

### unsettled_advances – PASS
**Q (finance):** Which staff advances haven't been settled yet?

**A:** One paid staff advance is still unsettled:
> 
> - **ADV-00007 — Dilini Jayawardena**, CMP-2611: promoter daily payments – day 2; **LKR 150,000**, outstanding **1 day**.

Tool calls: `pending_work({})`

### supplier – FAIL
**Q (finance):** How much have we paid Metro Fabricators and how much is still owed on their contract?

**A:** Metro Fabricators has been paid **LKR 216,000**. **LKR 216,000** is approved but still unpaid on the contract.
> 
> The contract value is LKR 540,000; the remaining payment is request **SUP-00002**.

Tool calls: `supplier_summary({"supplier": "Metro Fabricators", "campaign": ""})`

### closure_ready – PASS
**Q (finance):** Is CMP-2609 ready to close?

**A:** Yes. **CMP-2609 is ready to close.** All closure checks passed, with no open items or supplier contracts.

Tool calls: `closure_status({"campaign": "CMP-2609"})`

### closure_blockers – PASS
**Q (md):** What's stopping us from closing the Sunleaf mall campaign?

**A:** The Sunleaf mall campaign (CMP-2611) is **not ready to close**. The closure check shows these blockers:
> 
> - **3 requests awaiting your decision:** SUP-00008 (LKR 72,000), ADV-00009 (LKR 165,000), and SUP-00010 (LKR 95,000) is awaiting clarification from the requester.
> - **1 approved payment awaiting Finance:** SUP-00002 (LKR 216,000).
> - **2 advances need settlement verification:** ADV-00006 and ADV-00007. ADV-00006 also has a receipt mismatch warning; ADV-00007 is still unsettled.
> - **2 supplier bills need verification:** SUP-00001 and SUP-00004. SUP-00004 has a bill/payment mismatch warning.
> - **1 supplier contract remains partly unpaid:** Metro Fabricators’ sampling booth build has LKR 324,000 unpaid.
> - **1 budget revision awaits your decision:** Promoters & staff, proposed from LKR 420,000 to LKR 600,000.
> 
> Resolve those items in the relevant requests, finance-verification, and budget-revision screens, then recheck closure status.

Tool calls: `closure_status({"campaign": "Sunleaf mall"})`; `pending_work({})`; `search_requests({"campaign": "Sunleaf mall", "status": "awaiting_md", "kind": "", "supplier": "", "text": "", "min_amount": 0, "limit": 100})`; `search_requests({"campaign": "Sunleaf mall", "status": "approved_unpaid", "kind": "", "supplier": "", "text": "", "min_amount": 0, "limit": 100})`; `document_flags({"campaign": "Sunleaf mall"})`; `recent_activity({"campaign": "Sunleaf mall", "days": 30, "limit": 40})`

### unverified_bills – PASS
**Q (finance):** List supplier bills that are paid but not verified.

**A:** Paid supplier bills awaiting verification:
> 
> - **SUP-00001** — Metro Fabricators, booth fabrication (first instalment): **LKR 216,000**
> - **SUP-00004** — Lanka Stage & Sound, mall atrium rental: **LKR 320,000**

Tool calls: `search_requests({"campaign": "", "status": "paid", "kind": "supplier", "supplier": "", "text": "", "min_amount": 0, "limit": 100})`; `pending_work({})`

### rejection_reason – PASS
**Q (dilini):** Why was my extra flyers request rejected?

**A:** Your extra flyers request **SUP-00011** (LKR 45,000) was rejected because the MD’s comment was: **“Use leftover stock from last month.”**

Tool calls: `search_requests({"campaign": "", "status": "rejected", "kind": "", "supplier": "", "text": "extra flyers", "min_amount": 0, "limit": 25})`; `get_request({"reference": "SUP-00011"})`

### aggregate – PASS
**Q (md):** What's the total approved budget across all active campaigns?

**A:** The total approved budget across the 2 active campaigns is **LKR 3,970,000**.

Tool calls: `list_campaigns({"status": "active"})`

### staff_scope – PASS
**Q (kasun):** Which of my requests are still waiting on someone?

**A:** Your requests are waiting at these stages:
> 
> - **Awaiting MD:** SUP-00008 — Green Leaf Caterers, LKR 72,000; SUP-00012 — Lanka Stage & Sound, LKR 300,000 (first instalment).
> - **Awaiting your clarification:** SUP-00010 — Island Transport Services, LKR 95,000.
> - **Approved, awaiting Finance payment:** SUP-00002 — Metro Fabricators, LKR 216,000.
> - **Awaiting settlement verification:** ADV-00006 — LKR 60,000.
> - **Awaiting supplier-bill verification:** SUP-00001 — Metro Fabricators, LKR 216,000; SUP-00004 — Lanka Stage & Sound, LKR 320,000.

Tool calls: `pending_work({})`

### request_detail – PASS
**Q (md):** Tell me about ADV-00009 – is it within budget?

**A:** **ADV-00009** is a **LKR 165,000 staff advance** for promoter payments for an extended sampling day, in the **Promoters & staff** category of **Sunleaf Tea – Mall Sampling Weekend (CMP-2611)**. It is awaiting your decision.
> 
> **It is not within the current category budget position:** the category has **LKR 37,500 available after pending requests** (including this advance), leaving it **LKR 37,500 over budget** if approved. The category budget is LKR 420,000.

Tool calls: `get_request({"reference": "ADV-00009"})`; `get_campaign_budget({"campaign": "CMP-2611"})`

### refuse_action – PASS
**Q (md):** Approve ADV-00009 for me please.

**A:** I can’t approve requests from here. Open **ADV-00009** and use the **Approve** action on its request screen.

Tool calls: none

### document_flags – PASS
**Q (finance):** Are there any problems with uploaded bills or receipts?

**A:** Yes—there are two document warnings:
> 
> - **ADV-00006** (CMP-2611, lorry hire & fuel): receipts add up to **LKR 18,400** (one receipt wasn’t read), but the settlement claims **LKR 63,400**.
> - **SUP-00004** (CMP-2611, mall atrium rental): the bill is **LKR 354,000**, while the amount paid is **LKR 320,000**.

Tool calls: `document_flags({"campaign": ""})`

### activity – PASS
**Q (md):** What has finance done on CMP-2611 recently?

**A:** Over the past 7 days, finance on **CMP-2611**:
> 
> - **Paid** LKR 320,000 to Lanka Stage & Sound for venue rental (**SUP-00004**).
> - **Paid** LKR 60,000 cash for the lorry hire and fuel advance (**ADV-00006**); its settlement shows LKR 63,400 spent with a LKR 3,400 balance.
> - **Verified** the settlement for **ADV-00005** (LKR 142,500 spent, LKR 7,500 returned) and the supplier bill for **SUP-00003**.
> - **Read invoice and receipt documents** for **SUP-00004** and **ADV-00006**; those entries don’t indicate bill or settlement verification.

Tool calls: `recent_activity({"campaign": "CMP-2611", "days": 7, "limit": 40})`

### settlement_check – PASS
**Q (finance):** Does the settlement for the lorry hire advance match the receipts?

**A:** No. **ADV-00006** (lorry hire & fuel) claims **LKR 63,400** spent, but the readable receipt totals **LKR 18,400**; another receipt could not be read. The receipts therefore don’t currently substantiate the settlement amount. It’s still **awaiting Finance verification**—review the unreadable receipt and clarify the difference before verifying.

Tool calls: `search_requests({"campaign": "", "status": "", "kind": "advance", "supplier": "", "text": "lorry hire", "min_amount": 0, "limit": 25})`; `get_request({"reference": "ADV-00006"})`

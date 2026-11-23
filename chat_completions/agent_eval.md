# Query agent – live evaluation

Model `gpt-6-luna` · reasoning `low` · Responses API `False`
**15/15 passed** · avg 3.9s/question · 46,567 input + 1,851 output tokens total

## Tools

| Tool | Calls | Errors | Used in |
|---|---|---|---|
| `list_campaigns` | 1 | 0 | aggregate |
| `get_campaign_budget` | 2 | 0 | budget_line, request_detail |
| `search_requests` | 3 | 0 | rejection_reason, settlement_check, unverified_bills |
| `get_request` | 8 | 0 | refuse_action, rejection_reason, request_detail, settlement_check, unverified_bills |
| `supplier_summary` | 1 | 0 | supplier |
| `pending_work` | 3 | 0 | md_queue, staff_scope, unsettled_advances |
| `closure_status` | 2 | 0 | closure_blockers, closure_ready |
| `document_flags` | 1 | 0 | document_flags |
| `recent_activity` | 1 | 0 | activity |

## Cases

| Case | User | Result | Tools used | Missing facts | Time |
|---|---|---|---|---|---|
| budget_line | md | ✅ | get_campaign_budget |  | 3.6s |
| md_queue | md | ✅ | pending_work |  | 3.8s |
| unsettled_advances | finance | ✅ | pending_work |  | 2.8s |
| supplier | finance | ✅ | supplier_summary |  | 3.4s |
| closure_ready | finance | ✅ | closure_status |  | 2.0s |
| closure_blockers | md | ✅ | closure_status |  | 4.5s |
| unverified_bills | finance | ✅ | search_requests, get_request, get_request, get_request, get_request |  | 4.7s |
| rejection_reason | dilini | ✅ | search_requests, get_request |  | 3.4s |
| aggregate | md | ✅ | list_campaigns |  | 2.0s |
| staff_scope | kasun | ✅ | pending_work |  | 3.3s |
| request_detail | md | ✅ | get_request, get_campaign_budget |  | 3.6s |
| refuse_action | md | ✅ | get_request |  | 2.1s |
| document_flags | finance | ✅ | document_flags |  | 12.6s |
| activity | md | ✅ | recent_activity |  | 3.2s |
| settlement_check | finance | ✅ | search_requests, get_request |  | 3.6s |

### budget_line – PASS
**Q (md):** How much is left in the Promoters budget for the Sunleaf campaign?

**A:** The Sunleaf campaign’s **Promoters & staff** budget is **LKR 37,500 over budget** (available: **LKR -37,500**), including **LKR 165,000 pending MD approval**.

Tool calls: `get_campaign_budget({"campaign": "Sunleaf"})`

### md_queue – PASS
**Q (md):** What's waiting for my approval and what does it add up to?

**A:** **Four requests are waiting for your approval, totaling LKR 617,000:**
> 
> | Ref | Campaign | Request | Amount |
> |---|---|---|---:|
> | SUP-00008 | CMP-2611 | Green Leaf Caterers — crew lunch & tea | LKR 72,000 |
> | ADV-00009 | CMP-2611 | Dilini Jayawardena — promoter payments, extended day 3 | LKR 165,000 |
> | SUP-00012 | CMP-2612 | Lanka Stage & Sound — first instalment | LKR 300,000 |
> | ADV-00013 | CMP-2612 | Dilini Jayawardena — recce trip | LKR 80,000 |
> 
> There’s also a **CMP-2611 budget revision** awaiting your decision: Promoters & staff budget from **LKR 420,000 to LKR 600,000**. It is separate from the request total.

Tool calls: `pending_work({})`

### unsettled_advances – PASS
**Q (finance):** Which staff advances haven't been settled yet?

**A:** One paid staff advance hasn’t been settled yet:
> 
> - **ADV-00007 — Dilini Jayawardena**, CMP-2611: promoter daily payments – day 2, **LKR 150,000** (1 day outstanding).
> 
> **ADV-00006** is awaiting finance verification of its settlement, rather than listed as unsettled.

Tool calls: `pending_work({})`

### supplier – PASS
**Q (finance):** How much have we paid Metro Fabricators and how much is still owed on their contract?

**A:** Metro Fabricators has been paid **LKR 216,000**. The contract is **LKR 540,000**, with **LKR 324,000 still unpaid**: LKR 216,000 is approved but awaiting payment under **SUP-00002**, and LKR 108,000 is not yet represented by a payment request.

Tool calls: `supplier_summary({"supplier": "Metro Fabricators"})`

### closure_ready – PASS
**Q (finance):** Is CMP-2609 ready to close?

**A:** Yes. CMP-2609 is ready to close—all closure checks passed, with no open items or contracts.

Tool calls: `closure_status({"campaign": "CMP-2609"})`

### closure_blockers – PASS
**Q (md):** What's stopping us from closing the Sunleaf mall campaign?

**A:** The Sunleaf mall campaign (CMP-2611) isn’t ready to close. These checks still have open items:
> 
> - **3** requests awaiting your decision
> - **1** approved request waiting for payment
> - **2** staff advances not fully settled and verified
> - **2** supplier bills awaiting verification
> - **1** supplier contract not fully paid or cancelled: Metro Fabricators’ sampling booth build, with **LKR 324,000** unpaid
> - **1** budget revision awaiting your decision

Tool calls: `closure_status({"campaign": "Sunleaf mall"})`

### unverified_bills – PASS
**Q (finance):** List supplier bills that are paid but not verified.

**A:** Two paid supplier requests have bills not yet verified:
> 
> | Ref | Supplier | Campaign | Purpose | Paid |
> |---|---|---|---|---:|
> | SUP-00004 | Lanka Stage & Sound | CMP-2611 | Mall atrium rental – 2 days | LKR 320,000 |
> | SUP-00001 | Metro Fabricators | CMP-2611 | Booth fabrication – 40% advance | LKR 216,000 |
> 
> SUP-00004’s bill shows LKR 354,000, differing from the amount paid. SUP-00001 has a quotation on file, not a supplier bill.

Tool calls: `search_requests({"kind": "supplier", "status": "paid", "limit": 100})`; `get_request({"reference": "SUP-00014"})`; `get_request({"reference": "SUP-00004"})`; `get_request({"reference": "SUP-00003"})`; `get_request({"reference": "SUP-00001"})`

### rejection_reason – PASS
**Q (dilini):** Why was my extra flyers request rejected?

**A:** Your extra flyers request **SUP-00011** was rejected because the MD’s comment was: **“Use leftover stock from last month.”**

Tool calls: `search_requests({"text": "extra flyers", "limit": 25})`; `get_request({"reference": "SUP-00011"})`

### aggregate – PASS
**Q (md):** What's the total approved budget across all active campaigns?

**A:** The total approved budget across all active campaigns is **LKR 3,970,000**.

Tool calls: `list_campaigns({"status": "active"})`

### staff_scope – PASS
**Q (kasun):** Which of my requests are still waiting on someone?

**A:** These of your requests are still waiting on someone:
> 
> - **Waiting for MD approval:** SUP-00008 — Green Leaf Caterers, LKR 72,000; SUP-00012 — Lanka Stage & Sound, LKR 300,000 (first instalment).
> - **Waiting for your clarification:** SUP-00010 — Island Transport Services, LKR 95,000.
> - **Approved, waiting for Finance to pay:** SUP-00002 — Metro Fabricators, LKR 216,000.
> - **Waiting for verification:** ADV-00006 needs its settlement verified; SUP-00001 and SUP-00004 need supplier bills verified.

Tool calls: `pending_work({})`

### request_detail – PASS
**Q (md):** Tell me about ADV-00009 – is it within budget?

**A:** ADV-00009 is a **LKR 165,000 staff advance** for promoter payments on an extended sampling day. It is **awaiting your approval**.
> 
> It is **not within the category budget as things stand**: Promoters & staff has **LKR 37,500 available after this pending request**, putting the category **LKR 37,500 over budget** if approved. The campaign overall would still have **LKR 190,500 available**.

Tool calls: `get_request({"reference": "ADV-00009"})`; `get_campaign_budget({"campaign": "CMP-2611"})`

### refuse_action – PASS
**Q (md):** Approve ADV-00009 for me please.

**A:** I can’t approve requests from here; I’m read-only. Open **ADV-00009** and use the **Approve** button on its request screen. It’s awaiting MD review for **LKR 165,000** (Promoters & staff, CMP-2611).

Tool calls: `get_request({"reference": "ADV-00009"})`

### document_flags – PASS
**Q (finance):** Are there any problems with uploaded bills or receipts?

**A:** Yes—there are two document warnings:
> 
> - **ADV-00006** (CMP-2611, lorry hire & fuel): receipts read as **LKR 18,400**, with one receipt unread, but the settlement claims **LKR 63,400** spent.
> - **SUP-00004** (CMP-2611, mall atrium rental): the bill is **LKR 354,000**, while the amount paid is **LKR 320,000**.
> 
> Open each reference to review the uploaded documents and resolve the discrepancy.

Tool calls: `document_flags({"campaign": ""})`

### activity – PASS
**Q (md):** What has finance done on CMP-2611 recently?

**A:** Over the last 7 days, finance recorded these actions on **CMP-2611**:
> 
> - **Paid** LKR 150,000 cash for **ADV-00007** (PV-1195).
> - **Paid** LKR 60,000 cash for **ADV-00006** (PV-1190); its settlement reported LKR 63,400 spent, a **LKR 3,400 shortfall**. Finance also read a fuel receipt for LKR 18,400.
> - **Verified** the settlement for **ADV-00005**.
> - **Paid** LKR 320,000 by bank transfer for **SUP-00004** (COM-TT-55410); finance also read its invoice showing LKR 354,000.
> - Finance also recorded the promoter-and-staff budget change proposal, from LKR 420,000 to LKR 600,000, citing an extra sampling day.

Tool calls: `recent_activity({"campaign": "CMP-2611", "days": 7, "limit": 40})`

### settlement_check – PASS
**Q (finance):** Does the settlement for the lorry hire advance match the receipts?

**A:** No. Settlement **ADV-00006** claims **LKR 63,400** spent, but the readable receipt totals **LKR 18,400**; another receipt was unreadable. The receipts therefore don’t currently substantiate the claimed spend, leaving **LKR 45,000** unexplained from the readable evidence. The settlement is awaiting finance verification.

Tool calls: `search_requests({"text": "lorry hire", "kind": "advance", "limit": 100})`; `get_request({"reference": "ADV-00006"})`

"""Natural-language querying of campaign finances: a small LangGraph tool-calling agent.

    question ──► [agent: LLM with tools bound] ──tool calls?──► [tools: ToolNode] ─┐
                         ▲                                                          │
                         └──────────────────────────────────────────────────────────┘
                         └──no tool calls──► answer

Design rules
* Tools are **read-only** and **role-scoped**: field staff only ever see their own requests;
  the audit trail and cross-campaign tools are for MD / Finance / Admin.
* Every number in an answer must come from a tool result. Money math is done by
  ``finance.services`` (the same code the screens use), never by the model.
* The agent cannot approve, pay or edit anything – it tells the user where to click instead.
"""
import json
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from . import services as svc
from .models import AuditLog, Campaign, PaymentRequest, Settlement, Supplier

S = PaymentRequest.Status
MAX_STEPS = 12


# --------------------------------------------------------------------------- helpers


def _money(v):
    return float(v) if isinstance(v, Decimal) else v


def _dump(obj):
    return json.dumps(obj, default=lambda o: float(o) if isinstance(o, Decimal) else str(o), ensure_ascii=False)


def _figures(f):
    return {
        "budget": _money(f.budget), "paid_or_advanced": _money(f.cash_out), "committed": _money(f.committed),
        "pending_md": _money(f.pending), "available": _money(f.available), "verified_spend": _money(f.verified),
        "advances_outstanding": _money(f.advances_outstanding), "used_pct": f.utilisation_pct,
    }


def _req_row(r):
    row = {
        "ref": r.reference, "campaign": r.campaign.code, "category": r.budget_line.category,
        "type": r.get_kind_display(), "payee": r.payee, "purpose": r.purpose, "amount": _money(r.amount),
        "status": r.get_status_display(), "requested_by": r.requested_by.username,
        "submitted": r.created_at.date().isoformat(),
    }
    if r.instalment != PaymentRequest.Instalment.SINGLE:
        row["instalment"] = r.get_instalment_display()
    if r.paid_amount:
        row["paid"] = _money(r.paid_amount)
    return row


class ToolError(Exception):
    pass


def build_tools(user):
    """Create the tool set for one user. Closures carry the user so scoping can't be bypassed."""
    manager = svc.can_see_all(user)

    def visible():
        qs = PaymentRequest.objects.select_related(
            "campaign", "budget_line", "supplier", "requested_by", "payment", "settlement")
        return qs if manager else qs.filter(requested_by=user)

    def find_campaign(name_or_code):
        q = (name_or_code or "").strip()
        if not q:
            raise ToolError("Give a campaign code or name.")
        exact = Campaign.objects.filter(code__iexact=q).first()
        if exact:
            return exact
        matches = list(Campaign.objects.filter(Q(code__icontains=q) | Q(name__icontains=q) | Q(client__icontains=q)))
        if len(matches) == 1:
            return matches[0]
        if not matches:
            # try word-by-word
            words = [w for w in q.split() if len(w) > 2]
            cand = Campaign.objects.all()
            for w in words:
                cand = cand.filter(Q(code__icontains=w) | Q(name__icontains=w) | Q(client__icontains=w))
            matches = list(cand) if words else []
            if len(matches) == 1:
                return matches[0]
        if not matches:
            raise ToolError(f"No campaign matches '{q}'. Use list_campaigns to see codes.")
        raise ToolError("Several campaigns match: " + ", ".join(f"{c.code} ({c.name})" for c in matches))

    def safe(fn):
        def wrapper(**kwargs):
            try:
                return _dump(fn(**kwargs))
            except ToolError as e:
                return _dump({"error": str(e)})
        wrapper.__name__ = fn.__name__
        wrapper.__doc__ = fn.__doc__
        return wrapper

    # ---- tools -----------------------------------------------------------------------

    def list_campaigns(status: str = "") -> dict:
        """List campaigns with code, client, dates, status and headline money figures (LKR).
        status: optional filter – one of draft, active, closing, closed. Empty = all."""
        qs = Campaign.objects.all()
        if status:
            qs = qs.filter(status=status.lower())
        rows = []
        for c in qs:
            _, t = svc.campaign_position(c)
            row = {"code": c.code, "name": c.name, "client": c.client, "status": c.get_status_display(),
                   "start": c.start_date, "end": c.end_date}
            row.update({k: v for k, v in _figures(t).items()} if manager else
                       {"budget": _money(t.budget), "available": _money(t.available)})
            rows.append(row)
        return {"campaigns": rows}

    def get_campaign_budget(campaign: str) -> dict:
        """Budget position of one campaign, per category and in total: budget, paid/advanced, committed
        (approved but unpaid, incl. unpaid balance of instalment contracts), pending MD, available,
        verified spend. campaign: code (e.g. CMP-2611) or part of the name/client."""
        c = find_campaign(campaign)
        rows, total = svc.campaign_position(c)
        out = {"campaign": c.code, "name": c.name, "status": c.get_status_display(),
               "categories": [{"category": line.category, **_figures(f)} for line, f in rows],
               "total": _figures(total)}
        if manager:
            out["contracts"] = [
                {"supplier": str(k.supplier), "job": k.description, "category": k.budget_line.category,
                 "contract_value": _money(p["total"]), "paid": _money(p["paid"]), "balance": _money(p["unpaid"])}
                for k in c.contracts.filter(cancelled=False).select_related("supplier", "budget_line")
                for p in [svc.contract_position(k)]]
        return out

    def search_requests(campaign: str = "", status: str = "", kind: str = "", supplier: str = "",
                        text: str = "", min_amount: float = 0, limit: int = 25) -> dict:
        """Find payment requests. All filters optional:
        campaign (code/name); status: awaiting_md, clarify, approved_unpaid, rejected, paid, settled, cancelled;
        kind: advance or supplier; supplier: part of supplier name; text: words in the purpose/details;
        min_amount in LKR; limit (max 100). Returns rows plus count and total amount of ALL matches."""
        qs = visible()
        if campaign:
            qs = qs.filter(campaign=find_campaign(campaign))
        status_map = {"awaiting_md": S.SUBMITTED, "submitted": S.SUBMITTED, "pending": S.SUBMITTED,
                      "clarify": S.CLARIFY, "approved_unpaid": S.APPROVED, "approved": S.APPROVED,
                      "rejected": S.REJECTED, "paid": S.PAID, "settled": S.SETTLED, "cancelled": S.CANCELLED}
        if status:
            if status.lower() not in status_map:
                raise ToolError(f"Unknown status '{status}'. Use one of: {', '.join(status_map)}")
            qs = qs.filter(status=status_map[status.lower()])
        if kind:
            qs = qs.filter(kind=PaymentRequest.Kind.ADVANCE if kind.lower().startswith("adv") else PaymentRequest.Kind.SUPPLIER)
        if supplier:
            qs = qs.filter(supplier__name__icontains=supplier)
        if text:
            for w in text.split():
                qs = qs.filter(Q(purpose__icontains=w) | Q(details__icontains=w))
        if min_amount:
            qs = qs.filter(amount__gte=Decimal(str(min_amount)))
        all_rows = list(qs.order_by("-created_at"))
        limit = max(1, min(int(limit or 25), 100))
        return {"count": len(all_rows), "total_amount": _money(sum((r.amount for r in all_rows), Decimal("0"))),
                "requests": [_req_row(r) for r in all_rows[:limit]],
                "truncated": len(all_rows) > limit}

    def get_request(reference: str) -> dict:
        """Full detail of one request by reference (e.g. SUP-00004 or ADV-00007): payment, settlement,
        MD decisions with comments, documents (incl. AI-read totals) and the AI document checks."""
        ref = reference.strip().upper()
        try:
            pk = int(ref.split("-")[-1])
        except ValueError:
            raise ToolError("Reference looks like SUP-00004 or ADV-00007.")
        r = visible().filter(pk=pk).first()
        if r is None or r.reference != ref:
            raise ToolError(f"No request {ref} that you can see.")
        out = _req_row(r)
        out["details"] = r.details
        out["needed_by"] = r.needed_by
        if r.contract_id:
            p = svc.contract_position(r.contract)
            out["contract"] = {"job": r.contract.description, "value": _money(p["total"]), "paid": _money(p["paid"])}
        pay = getattr(r, "payment", None)
        if pay:
            out["payment"] = {"amount": _money(pay.amount), "date": pay.paid_on, "method": pay.get_method_display(),
                              "reference": pay.reference}
        if r.kind == PaymentRequest.Kind.SUPPLIER and pay:
            out["bill_verified"] = r.bill_verified
        st = getattr(r, "settlement", None)
        if st:
            out["settlement"] = {"status": st.get_status_display(), "spent": _money(st.spent_amount),
                                 "cash_returned": _money(st.cash_returned), "balance": _money(st.balance)}
        out["md_decisions"] = [{"decision": d.get_decision_display(), "by": d.decided_by.username,
                                "comment": d.comment, "at": d.decided_at.date()} for d in r.decisions.all()]
        out["documents"] = [{"type": a.get_kind_display(), "file": a.filename,
                             "ai_total": _money(a.ai_total) if a.ai_total is not None else None,
                             "ai_vendor": a.ai_data.get("vendor_name") if a.ai_status == "done" else None}
                            for a in r.attachments.all()]
        out["ai_checks"] = [{"level": lvl, "message": m} for lvl, m in svc.ai_checks(r)]
        return out

    def supplier_summary(supplier: str, campaign: str = "") -> dict:
        """Everything requested from / paid to a supplier (part of the name), optionally within one campaign:
        totals by status, instalment contracts and the individual requests."""
        sups = list(Supplier.objects.filter(name__icontains=supplier.strip()))
        if not sups:
            raise ToolError(f"No supplier matches '{supplier}'.")
        if len(sups) > 1 and not any(s.name.lower() == supplier.strip().lower() for s in sups):
            raise ToolError("Several suppliers match: " + ", ".join(s.name for s in sups))
        sup = next((s for s in sups if s.name.lower() == supplier.strip().lower()), sups[0])
        qs = visible().filter(supplier=sup)
        contracts = sup.contracts.filter(cancelled=False).select_related("campaign")
        if campaign:
            camp = find_campaign(campaign)
            qs = qs.filter(campaign=camp)
            contracts = contracts.filter(campaign=camp)
        reqs = list(qs.order_by("created_at"))
        live = [r for r in reqs if r.status not in (S.REJECTED, S.CANCELLED)]
        return {"supplier": sup.name, "contact": sup.contact_person, "phone": sup.phone,
                "requested_total": _money(sum((r.amount for r in live), Decimal("0"))),
                "paid_total": _money(sum((r.paid_amount for r in live), Decimal("0"))),
                "awaiting_md_total": _money(sum((r.amount for r in live if r.status in PaymentRequest.OPEN_STATUSES), Decimal("0"))),
                "approved_unpaid_total": _money(sum((r.amount for r in live if r.status == S.APPROVED), Decimal("0"))),
                "contracts": [{"campaign": k.campaign.code, "job": k.description, "value": _money(k.total_amount),
                               "paid": _money(svc.contract_position(k)["paid"])}
                              for k in contracts] if manager else [],
                "requests": [_req_row(r) for r in reqs]}

    def pending_work() -> dict:
        """What is waiting on whom right now: requests awaiting MD, clarifications awaiting requesters,
        approved-but-unpaid (finance), settlements to verify, supplier bills to verify, advances not yet
        settled (with days outstanding), and budget revisions awaiting the MD."""
        from .models import BudgetRevision

        today = timezone.localdate()
        qs = visible()

        def rows(q):
            return [_req_row(r) for r in q.order_by("created_at")[:30]]

        adv = []
        for r in qs.filter(kind=PaymentRequest.Kind.ADVANCE, status=S.PAID):
            st = getattr(r, "settlement", None)
            if st and st.status == Settlement.Status.SUBMITTED:
                continue
            row = _req_row(r)
            row["days_outstanding"] = (today - r.payment.paid_on).days
            adv.append(row)
        out = {
            "awaiting_md": rows(qs.filter(status=S.SUBMITTED)),
            "awaiting_requester_clarification": rows(qs.filter(status=S.CLARIFY)),
            "approved_awaiting_payment": rows(qs.filter(status=S.APPROVED)),
            "settlements_to_verify": rows(qs.filter(settlement__status=Settlement.Status.SUBMITTED)),
            "supplier_bills_to_verify": rows(qs.filter(kind=PaymentRequest.Kind.SUPPLIER, status=S.PAID, bill_verified=False)),
            "advances_not_settled": sorted(adv, key=lambda x: -x["days_outstanding"]),
        }
        if manager:
            out["budget_revisions_awaiting_md"] = [
                {"campaign": rv.budget_line.campaign.code, "category": rv.budget_line.category,
                 "from": _money(rv.old_amount), "to": _money(rv.new_amount), "reason": rv.reason,
                 "proposed_by": rv.proposed_by.username}
                for rv in BudgetRevision.objects.filter(status=BudgetRevision.Status.PENDING).select_related(
                    "budget_line__campaign", "proposed_by")]
        return out

    def closure_status(campaign: str) -> dict:
        """Whether a campaign is ready for financial closure, which checks fail and how many items block each."""
        c = find_campaign(campaign)
        checks, ready, open_contracts = svc.closure_checklist(c)
        return {"campaign": c.code, "status": c.get_status_display(), "ready_to_close": ready,
                "checks": [{"check": label, "passed": ok, "open_items": n} for label, ok, n in checks],
                "open_contracts": [{"job": k.description, "supplier": str(k.supplier), "unpaid": _money(u)}
                                   for k, u in open_contracts]}

    def recent_activity(campaign: str = "", days: int = 7, limit: int = 40) -> dict:
        """Audit trail entries (who did what, when), newest first. campaign optional; days back (default 7)."""
        qs = AuditLog.objects.select_related("actor", "campaign", "request").filter(
            created_at__gte=timezone.now() - timedelta(days=max(1, min(int(days or 7), 365))))
        if campaign:
            qs = qs.filter(campaign=find_campaign(campaign))
        return {"entries": [{"when": e.created_at.strftime("%Y-%m-%d %H:%M"), "who": e.actor.username,
                             "campaign": e.campaign.code if e.campaign else None,
                             "ref": e.request.reference if e.request else None, "what": e.message}
                            for e in qs[:max(1, min(int(limit or 40), 200))]]}

    def document_flags(campaign: str = "") -> dict:
        """Warnings from the AI document checks (bill ≠ payment, vendor ≠ payee, receipts ≠ settlement,
        possible duplicates, unreadable documents) across requests, optionally for one campaign."""
        qs = visible().filter(attachments__isnull=False).distinct()
        if campaign:
            qs = qs.filter(campaign=find_campaign(campaign))
        flagged = []
        for r in qs:
            warns = [m for lvl, m in svc.ai_checks(r) if lvl == "warn"]
            if warns:
                flagged.append({"ref": r.reference, "campaign": r.campaign.code, "purpose": r.purpose, "warnings": warns})
        return {"requests_with_warnings": flagged, "count": len(flagged)}

    tools = [list_campaigns, get_campaign_budget, search_requests, get_request, supplier_summary,
             pending_work, closure_status, document_flags]
    if manager:
        tools.append(recent_activity)
    return [_structured(f, safe) for f in tools]


def _structured(fn, safe):
    """Build a StructuredTool whose schema comes from the original signature but runs the safe wrapper."""
    from langchain_core.tools import StructuredTool
    from langchain_core.tools.base import create_schema_from_function

    schema = create_schema_from_function(fn.__name__, fn, parse_docstring=False)
    return StructuredTool(name=fn.__name__, description=" ".join(fn.__doc__.split()),
                          args_schema=schema, func=safe(fn))


# --------------------------------------------------------------------------- graph


def system_prompt(user):
    roles = ", ".join(sorted(svc.roles_of(user))) or "Field Staff"
    scope = ("You can see all campaigns and requests." if svc.can_see_all(user)
             else "This user is field staff: tools only return THEIR OWN requests. Say so if they ask about others.")
    return f"""You are the finance assistant inside a campaign financial management app used by a Sri Lankan
advertising & brand activation agency. Today is {timezone.localdate():%A %d %B %Y}. The user is
{user.get_full_name() or user.username} (role: {roles}). {scope}

How the money works (all amounts LKR):
- budget: MD-approved amount per campaign category.
- paid_or_advanced: cash actually paid by finance, net of advance cash returned.
- committed: approved but not yet paid, including the unpaid balance of supplier instalment contracts.
- pending_md: requests awaiting the MD (a pending FIRST instalment counts at the full contract value).
- available = budget − paid_or_advanced − committed − pending_md. Negative = over budget.
- verified_spend: spend backed by a verified supplier bill or a finance-verified advance settlement.

Rules:
- Use the tools for every figure. Never estimate, invent or do your own budget arithmetic when a tool gives the number.
- If a tool returns an error, fix the arguments (e.g. call list_campaigns for the right code) and try again.
- Mention request references (e.g. SUP-00004) so the user can open them.
- You are read-only. If asked to approve, pay, edit or delete, explain which screen/button the user should use.
- Be brief: lead with the direct answer, then a short list or table only if it helps. Format money like LKR 1,250,000."""


def build_graph(llm, tools):
    from langchain_core.messages import SystemMessage, ToolMessage
    from langgraph.graph import START, MessagesState, StateGraph
    from langgraph.prebuilt import tools_condition

    bound = llm.bind_tools(tools)
    by_name = {t.name: t for t in tools}

    def run_tools(state):
        # Sequential, same-thread execution: LangGraph's ToolNode uses a thread pool, which
        # breaks Django DB connections (locked SQLite in tests, leaked connections in prod).
        out = []
        for tc in state["messages"][-1].tool_calls:
            tool = by_name.get(tc["name"])
            if tool is None:
                content = _dump({"error": f"Unknown tool {tc['name']}. Available: {', '.join(by_name)}"})
            else:
                try:
                    content = tool.invoke(tc["args"])
                except Exception as exc:  # bad arguments from the model
                    content = _dump({"error": f"{type(exc).__name__}: {exc}"[:300]})
            out.append(ToolMessage(content=content, tool_call_id=tc["id"], name=tc["name"]))
        return {"messages": out}

    def agent(state, config):
        sys_msg = config["configurable"]["system_prompt"]
        return {"messages": [bound.invoke([SystemMessage(sys_msg)] + state["messages"])]}

    g = StateGraph(MessagesState)
    g.add_node("agent", agent)
    g.add_node("tools", run_tools)
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", tools_condition)
    g.add_edge("tools", "agent")
    return g.compile()


def default_llm():
    from langchain_openai import ChatOpenAI

    if settings.AI_PROVIDER == "local":
        # llama.cpp / Ollama / vLLM: OpenAI-compatible Chat Completions with tool calling.
        return ChatOpenAI(model=settings.AGENT_MODEL or settings.AI_MODEL or "local-model",
                          base_url=settings.AI_BASE_URL, api_key=settings.AI_LOCAL_API_KEY or "not-needed",
                          temperature=0, timeout=180, max_retries=1)
    effort = settings.AGENT_REASONING_EFFORT
    if not settings.AGENT_USE_RESPONSES_API and effort != "none":
        # Live eval (Nov 2026): /v1/chat/completions rejects function tools + reasoning_effort for
        # gpt-6-luna (HTTP 400). Only reasoning_effort="none" works there; the Responses API is the default.
        effort = "none"
    return ChatOpenAI(
        model=settings.AGENT_MODEL or settings.AI_MODEL or "gpt-6-luna",
        api_key=settings.OPENAI_API_KEY,
        reasoning_effort=effort,
        use_responses_api=settings.AGENT_USE_RESPONSES_API,
        timeout=90,
        max_retries=2,
    )


def enabled():
    if settings.AI_PROVIDER == "local":
        return bool(settings.AI_BASE_URL)
    return bool(settings.OPENAI_API_KEY)


def _text(content):
    if isinstance(content, str):
        return content
    parts = []
    for c in content or []:
        if isinstance(c, dict) and c.get("type") in ("text", "output_text"):
            parts.append(c.get("text", ""))
        elif isinstance(c, str):
            parts.append(c)
    return "".join(parts)


def ask(user, question, history=(), llm=None):
    """Run the agent. history: [(role, text), ...] of earlier turns. Returns dict with answer + trace."""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    tools = build_tools(user)
    graph = build_graph(llm or default_llm(), tools)
    msgs = []
    for role, text in list(history)[-8:]:
        msgs.append(HumanMessage(text) if role == "user" else AIMessage(text))
    msgs.append(HumanMessage(question))
    try:
        result = graph.invoke({"messages": msgs},
                              config={"recursion_limit": MAX_STEPS * 2 + 1,
                                      "configurable": {"system_prompt": system_prompt(user)}})
    except Exception as exc:  # API/network/recursion errors surface to the UI, never crash the page
        return {"answer": "", "error": f"{type(exc).__name__}: {exc}"[:400], "tool_calls": []}

    new = result["messages"][len(msgs):]
    results = {m.tool_call_id: m.content for m in new if isinstance(m, ToolMessage)}
    calls = []
    for m in new:
        if isinstance(m, AIMessage):
            for tc in m.tool_calls or []:
                out = results.get(tc["id"], "")
                calls.append({"name": tc["name"], "args": tc["args"],
                              "ok": bool(out) and '"error"' not in out[:20], "result_preview": out[:300]})
    final = next((m for m in reversed(new) if isinstance(m, AIMessage) and not m.tool_calls), None)
    usage = {}
    for m in new:
        um = getattr(m, "usage_metadata", None) or {}
        for k in ("input_tokens", "output_tokens"):
            usage[k] = usage.get(k, 0) + (um.get(k) or 0)
    return {"answer": _text(final.content) if final else "", "error": "" if final else "No final answer.",
            "tool_calls": calls, "usage": usage}

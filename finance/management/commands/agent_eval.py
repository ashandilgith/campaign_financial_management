"""Live evaluation of the query agent on seeded demo data.

    python manage.py seed_demo --reset && python manage.py agent_eval --out eval_reports

Each case asks a realistic question as a given user. Expected facts are computed from the
database (not hard-coded), then the answer is checked for them. The report shows, per case,
which tools were called, whether they errored, whether the expected tool was used, and whether
the answer contained the right facts – plus a per-tool summary ("which tools work").
"""
import json
import re
import time
from collections import defaultdict
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError

from finance import agent, services as svc
from finance.models import Campaign, PaymentRequest

S = PaymentRequest.Status


def n(v):
    """Number as digits the way an answer might write it: 37500 (commas stripped before matching)."""
    v = float(v)
    return str(int(round(abs(v)))) if abs(v - round(v)) < 0.005 else f"{abs(v):.2f}"


def normalise(text):
    t = re.sub(r"(?<=\d),(?=\d{3})", "", text or "")
    t = re.sub(r"(\d)\.00\b", r"\1", t)
    return t.lower()


def build_cases():
    c1 = Campaign.objects.get(code="CMP-2611")
    rows, _ = svc.campaign_position(c1)
    promo = next(f for line, f in rows if line.category == "Promoters & staff")
    awaiting = list(PaymentRequest.objects.filter(status=S.SUBMITTED))
    awaiting_total = sum(r.amount for r in awaiting)
    # paid advances with no settlement submitted yet (submitted ones are "awaiting verification")
    unsettled = [r for r in PaymentRequest.objects.filter(kind="advance", status=S.PAID)
                 if getattr(r, "settlement", None) is None]
    flagged = [r.reference for r in PaymentRequest.objects.filter(attachments__isnull=False).distinct()
               if any(lvl == "warn" for lvl, _ in svc.ai_checks(r))]
    metro_paid = sum(r.paid_amount for r in PaymentRequest.objects.filter(supplier__name="Metro Fabricators"))
    metro_contract = c1.contracts.get(supplier__name="Metro Fabricators")
    metro_owed = svc.contract_position(metro_contract)["unpaid"]
    unverified = list(PaymentRequest.objects.filter(kind="supplier", status=S.PAID, bill_verified=False))
    flyers = PaymentRequest.objects.get(purpose="Extra flyers")
    active_budget = sum(c.total_budget for c in Campaign.objects.filter(status="active"))
    kasun_waiting = list(PaymentRequest.objects.filter(requested_by__username="kasun", status__in=[S.SUBMITTED, S.CLARIFY]))
    over = PaymentRequest.objects.get(purpose__startswith="Promoter payments – extended")
    _, ready_2609, _ = svc.closure_checklist(Campaign.objects.get(code="CMP-2609"))
    _, ready_2611, _ = svc.closure_checklist(c1)
    lorry = PaymentRequest.objects.get(purpose="Lorry hire & fuel – setup day")

    return [
        dict(id="budget_line", user="md", q="How much is left in the Promoters budget for the Sunleaf campaign?",
             tools={"get_campaign_budget"}, must=[n(promo.available)], note="negative = over budget"),
        dict(id="md_queue", user="md", q="What's waiting for my approval and what does it add up to?",
             tools={"pending_work", "search_requests"}, must=[n(awaiting_total)] + [r.reference for r in awaiting][:2]),
        dict(id="unsettled_advances", user="finance", q="Which staff advances haven't been settled yet?",
             tools={"pending_work", "search_requests"}, must=[r.reference for r in unsettled]),
        dict(id="supplier", user="finance", q="How much have we paid Metro Fabricators and how much is still owed on their contract?",
             tools={"supplier_summary", "get_campaign_budget"}, must=[n(metro_paid), n(metro_owed)]),
        dict(id="closure_ready", user="finance", q="Is CMP-2609 ready to close?",
             tools={"closure_status"}, must=["yes" if ready_2609 else "not"], must_not=["not ready"] if ready_2609 else []),
        dict(id="closure_blockers", user="md", q="What's stopping us from closing the Sunleaf mall campaign?",
             tools={"closure_status"}, must=["advance", "bill"] if not ready_2611 else []),
        dict(id="unverified_bills", user="finance", q="List supplier bills that are paid but not verified.",
             tools={"pending_work", "search_requests"}, must=[r.reference for r in unverified]),
        dict(id="rejection_reason", user="dilini", q="Why was my extra flyers request rejected?",
             tools={"search_requests", "get_request"}, must=["leftover"], ref=flyers.reference),
        dict(id="aggregate", user="md", q="What's the total approved budget across all active campaigns?",
             tools={"list_campaigns"}, must=[n(active_budget)]),
        dict(id="staff_scope", user="kasun", q="Which of my requests are still waiting on someone?",
             tools={"search_requests", "pending_work"}, must=[r.reference for r in kasun_waiting]),
        dict(id="request_detail", user="md", q=f"Tell me about {over.reference} – is it within budget?",
             tools={"get_request", "get_campaign_budget"}, must=["over"]),
        dict(id="refuse_action", user="md", q=f"Approve {over.reference} for me please.",
             tools=set(), must=["approv"], side_effect_free=over.reference),
        dict(id="document_flags", user="finance", q="Are there any problems with uploaded bills or receipts?",
             tools={"document_flags", "get_request"}, must=flagged[:2] or ["no"]),
        dict(id="activity", user="md", q="What has finance done on CMP-2611 recently?",
             tools={"recent_activity"}, must=["paid"]),
        dict(id="settlement_check", user="finance", q=f"Does the settlement for the lorry hire advance match the receipts?",
             tools={"get_request", "search_requests"}, must=[lorry.reference]),
    ]


class Command(BaseCommand):
    help = "Run the query agent against realistic questions and write a scored report."

    def add_arguments(self, parser):
        parser.add_argument("--out", default="eval_reports")
        parser.add_argument("--only", default="", help="comma-separated case ids")

    def handle(self, *args, out="eval_reports", only="", **opts):
        if not agent.enabled():
            raise CommandError("OPENAI_API_KEY is not set.")
        cases = build_cases()
        if only:
            cases = [c for c in cases if c["id"] in only.split(",")]
        results, per_tool = [], defaultdict(lambda: {"calls": 0, "errors": 0, "cases": set()})
        for c in cases:
            user = User.objects.get(username=c["user"])
            before = None
            if c.get("side_effect_free"):
                before = PaymentRequest.objects.get(pk=int(c["side_effect_free"].split("-")[1])).status
            t0 = time.time()
            res = agent.ask(user, c["q"])
            secs = round(time.time() - t0, 1)
            ans = normalise(res["answer"])
            used = {tc["name"] for tc in res["tool_calls"]}
            for tc in res["tool_calls"]:
                per_tool[tc["name"]]["calls"] += 1
                per_tool[tc["name"]]["errors"] += 0 if tc["ok"] else 1
                per_tool[tc["name"]]["cases"].add(c["id"])
            missing = [m for m in c["must"] if normalise(m) not in ans]
            forbidden = [m for m in c.get("must_not", []) if normalise(m) in ans]
            tool_ok = (not c["tools"]) or bool(used & c["tools"])
            side_ok = True
            if before is not None:
                side_ok = PaymentRequest.objects.get(pk=int(c["side_effect_free"].split("-")[1])).status == before
            passed = not res["error"] and not missing and not forbidden and tool_ok and side_ok
            results.append({
                "id": c["id"], "user": c["user"], "question": c["q"], "passed": passed, "seconds": secs,
                "expected_tools": sorted(c["tools"]), "tools_used": [(tc["name"], tc["args"], tc["ok"]) for tc in res["tool_calls"]],
                "tool_ok": tool_ok, "missing_facts": missing, "forbidden_found": forbidden, "no_side_effects": side_ok,
                "answer": res["answer"], "error": res["error"], "usage": res.get("usage", {}),
            })
            self.stdout.write(f"{'PASS' if passed else 'FAIL'} {c['id']:20s} {secs:5.1f}s tools={sorted(used)} missing={missing} {res['error'][:120]}")

        all_tools = [t.name for t in agent.build_tools(User.objects.get(username="md"))]
        tool_table = {t: {"calls": per_tool[t]["calls"], "errors": per_tool[t]["errors"],
                          "cases": sorted(per_tool[t]["cases"])} for t in all_tools}
        tokens_in = sum(r["usage"].get("input_tokens", 0) for r in results)
        tokens_out = sum(r["usage"].get("output_tokens", 0) for r in results)
        summary = {"model": settings.AGENT_MODEL or settings.AI_MODEL or "gpt-6-luna",
                   "reasoning_effort": settings.AGENT_REASONING_EFFORT,
                   "responses_api": settings.AGENT_USE_RESPONSES_API,
                   "passed": sum(r["passed"] for r in results), "total": len(results),
                   "avg_seconds": round(sum(r["seconds"] for r in results) / max(len(results), 1), 1),
                   "input_tokens": tokens_in, "output_tokens": tokens_out}
        Path(out).mkdir(parents=True, exist_ok=True)
        (Path(out) / "agent_eval.json").write_text(json.dumps({"summary": summary, "tools": tool_table, "cases": results},
                                                              indent=2, default=str))
        md = ["# Query agent – live evaluation", "",
              f"Model `{summary['model']}` · reasoning `{summary['reasoning_effort']}` · Responses API `{summary['responses_api']}`",
              f"**{summary['passed']}/{summary['total']} passed** · avg {summary['avg_seconds']}s/question · "
              f"{tokens_in:,} input + {tokens_out:,} output tokens total", "",
              "## Tools", "", "| Tool | Calls | Errors | Used in |", "|---|---|---|---|"]
        for t, v in tool_table.items():
            md.append(f"| `{t}` | {v['calls']} | {v['errors']} | {', '.join(v['cases']) or '—'} |")
        md += ["", "## Cases", "", "| Case | User | Result | Tools used | Missing facts | Time |", "|---|---|---|---|---|---|"]
        for r in results:
            tools = ", ".join(f"{n_}{'' if ok else '✗'}" for n_, _, ok in r["tools_used"]) or "—"
            md.append(f"| {r['id']} | {r['user']} | {'✅' if r['passed'] else '❌'} | {tools} | "
                      f"{', '.join(r['missing_facts']) or ('tool not used' if not r['tool_ok'] else '')} | {r['seconds']}s |")
        for r in results:
            md += ["", f"### {r['id']} – {'PASS' if r['passed'] else 'FAIL'}", f"**Q ({r['user']}):** {r['question']}", ""]
            if r["error"]:
                md.append(f"**Error:** `{r['error']}`")
            md.append("**A:** " + (r["answer"] or "").replace("\n", "\n> "))
            md.append("")
            md.append("Tool calls: " + ("; ".join(f"`{n_}({json.dumps(a, ensure_ascii=False)})`{'' if ok else ' ✗'}"
                                                 for n_, a, ok in r["tools_used"]) or "none"))
        (Path(out) / "agent_eval.md").write_text("\n".join(md) + "\n")
        self.stdout.write(f"{summary['passed']}/{summary['total']} passed")

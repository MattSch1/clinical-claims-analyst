"""The six agent nodes (spec §8):

    plan -> generate_sql -> execute_sql -> validate -> (self_correct | synthesize)

Hard rules honored here: every node try/excepts and records to state (never raises
out, so the request never 500s); self-correction is capped at MAX_RETRIES then the
agent degrades gracefully; each model call and the SQL tool call is traced to Langfuse
under one per-request trace.
"""

from __future__ import annotations

import json
import logging
import re

from src.agent import prompts, tools
from src.agent.state import AgentState
from src.config import get_settings
from src.llm import client as llm
from src.obs import tracing
from src.phi import leakage, scrub

logger = logging.getLogger(__name__)
settings = get_settings()

MAX_RETRIES = 3  # self-correction cap (CLAUDE.md hard rule)


# ── helpers ───────────────────────────────────────────────────────────────────
def _clip(text: str, n: int = 160) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n] + "…"


def _acc(state: AgentState, res: llm.LLMResult) -> dict:
    """Accumulate cost/tokens onto the running state."""
    return {
        "total_cost_usd": round(state.total_cost_usd + res.cost_usd, 8),
        "total_tokens": state.total_tokens + res.total_tokens,
    }


def _run_llm(node: str, system: str, user: str, *, model: str | None = None) -> llm.LLMResult:
    """One model call, traced as a child generation of the request's trace.

    `model` overrides the model for this node (SQL gen/self-correct route to the stronger
    sql_model; plan/synthesize stay on the cheap llm_model). Scans the FULL assembled
    prompt before sending — the no-PHI-to-model regime requires every model input to pass
    the leakage scanner, not just the original question. A hit raises LeakageError (a hard
    failure the caller degrades gracefully on)."""
    mdl = model or settings.llm_model
    leakage.assert_clean(user, where=f"{node}:input")
    with tracing.generation(name=node, model=mdl, input=user, tags=["m2", node]) as box:
        res = llm.complete(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            model=mdl,
        )
        box["output"] = res.text
        box["usage_details"] = {
            "input": res.prompt_tokens,
            "output": res.completion_tokens,
            "total": res.total_tokens,
        }
        box["cost_details"] = {"total": res.cost_usd}
    return res


def _render_result(result: dict, max_rows: int = 50) -> str:
    cols = result.get("columns", [])
    rows = result.get("rows", [])[:max_rows]
    header = " | ".join(str(c) for c in cols)
    body = "\n".join(" | ".join("" if v is None else str(v) for v in row) for row in rows)
    extra = "\n… (more rows omitted)" if result.get("truncated") else ""
    return f"{header}\n{body}{extra}"


def _has_data(result: dict | None) -> bool:
    """A result is a real answer only if it has rows AND isn't a single all-NULL
    aggregate row (e.g. AVG/rate over a filter that matched nothing) — otherwise the
    model would happily narrate a fabricated number over a NULL."""
    if not result:
        return False
    rows = result.get("rows") or []
    if not rows:
        return False
    if len(rows) == 1 and all(v is None for v in rows[0]):
        return False
    return True


def _sql_update(
    state: AgentState, res: llm.LLMResult, sql: str, node: str, *, extra: dict | None = None
) -> dict:
    """Build a candidate-SQL state update, scanning the SQL for identifier columns
    (defense-in-depth; the views-only role already can't reach them). On a hit, set an
    execution_error so validate routes to self-correction instead of executing it."""
    leaks = leakage.scan_sql(sql)
    out: dict = {
        "candidate_sql": sql,
        "sql_attempts": state.sql_attempts + [sql],
        **_acc(state, res),
    }
    if leaks:
        cols = ", ".join(sorted({lk.match for lk in leaks}))
        out["execution_error"] = f"query references restricted identifier column(s): {cols}"
        out["leakage_hits"] = state.leakage_hits + [f"sql:{lk.match}" for lk in leaks]
        out["step_trace"] = state.step_trace + [f"{node}: LEAKAGE blocked id column(s) {cols}"]
    else:
        out["execution_error"] = None
        out["step_trace"] = state.step_trace + [f"{node}: {_clip(sql)}"]
    if extra:
        out.update(extra)
    return out


def _fallback_answer(state: AgentState) -> str:
    res = state.execution_result or {}
    cols = res.get("columns", [])
    rows = res.get("rows", [])
    head = rows[0] if rows else []
    pairs = ", ".join(f"{c}={v}" for c, v in zip(cols, head, strict=False))
    return f"Result ({state.row_count} row(s)): {pairs}" if pairs else "Query returned no rows."


# ── nodes ─────────────────────────────────────────────────────────────────────
def plan(state: AgentState) -> dict:
    schema = state.schema_snapshot or tools.schema_inspect()
    try:
        res = _run_llm(
            "plan",
            prompts.PLAN_PROMPT_V1,
            f"Schema:\n{schema}\n\nQuestion: {state.question}",
        )
        return {
            "schema_snapshot": schema,
            "plan": res.text.strip(),
            "step_trace": state.step_trace + [f"plan: {_clip(res.text)}"],
            **_acc(state, res),
        }
    except Exception as exc:  # noqa: BLE001 - never let a node crash the request
        logger.warning("plan node failed", exc_info=True)
        return {
            "schema_snapshot": schema,
            "plan": None,
            "step_trace": state.step_trace + [f"plan failed: {exc}"],
        }


def _parse_terms(text: str) -> list[str]:
    match = re.search(r"\[.*\]", text or "", re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
            return [str(t) for t in data if isinstance(t, str)][:6]
        except (ValueError, TypeError):
            pass
    return []


def resolve_codes(state: AgentState) -> dict:
    """Resolve named clinical concepts (e.g. 'Type 2 diabetes') to the EXACT codes present
    in the data via a description lookup — so the agent uses the real SNOMED/LOINC code
    rather than guessing a coding system. No-op on the SQLite backend / when no concept."""
    if not settings.use_postgres:
        return {"code_hints": None}
    try:
        res = _run_llm(
            "resolve_codes", prompts.CONCEPT_EXTRACT_PROMPT_V1, f"Question: {state.question}"
        )
        terms = _parse_terms(res.text)
        hints = tools.lookup_codes(terms) if terms else ""
        note = f"resolve_codes: {terms} -> {'codes found' if hints else 'none'}"
        return {
            "code_hints": hints or None,
            "step_trace": state.step_trace + [note],
            **_acc(state, res),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("resolve_codes failed", exc_info=True)
        note = f"resolve_codes failed: {exc}"
        return {"code_hints": None, "step_trace": state.step_trace + [note]}


def generate_sql(state: AgentState) -> dict:
    schema = state.schema_snapshot or tools.schema_inspect()
    hints = f"\n\n{state.code_hints}" if state.code_hints else ""
    user = (
        f"Schema:\n{schema}\n\nQuestion: {state.question}\n\n"
        f"Approach: {state.plan or '(none)'}{hints}\n\nWrite the SQL."
    )
    try:
        res = _run_llm(
            "generate_sql", prompts.SQL_GENERATE_PROMPT_V1, user,
            model=settings.sql_model_resolved,
        )
        sql = tools.extract_sql(res.text)
        return {**_sql_update(state, res, sql, "generate_sql")}
    except Exception as exc:  # noqa: BLE001
        logger.warning("generate_sql node failed", exc_info=True)
        return {
            "candidate_sql": None,
            "execution_error": f"sql generation failed: {exc}",
            "step_trace": state.step_trace + [f"generate_sql failed: {exc}"],
        }


def execute_sql(state: AgentState) -> dict:
    sql = state.candidate_sql
    if not sql:
        return {
            "execution_result": None,
            "execution_error": state.execution_error or "no SQL to execute",
            "step_trace": state.step_trace + ["execute_sql: skipped (no SQL)"],
        }
    with tracing.tool(name="sql_execute", input=sql) as box:
        # guarded, read-only, audited (one audit row per outcome); never raises
        result = tools.sql_execute(sql, request_id=state.request_id)
        audit_id = result.get("audit_id")
        if "error" in result:
            box["output"] = {"error": result["error"], "audit_id": audit_id}
            return {
                "execution_result": None,
                "execution_error": result["error"],
                "audit_id": audit_id,
                "step_trace": state.step_trace + [f"execute_sql error: {_clip(result['error'])}"],
            }
        box["output"] = {"row_count": result["row_count"], "columns": result["columns"]}
        ok_note = f"execute_sql: {result['row_count']} rows (audit {audit_id})"
        return {
            "execution_result": result,
            "execution_error": None,
            "audit_id": audit_id,
            "step_trace": state.step_trace + [ok_note],
        }


def validate(state: AgentState) -> dict:
    """Branch node: decide self_correct vs synthesize (sets state.route)."""
    err = state.execution_error
    rows = state.row_count
    if err is None and _has_data(state.execution_result):
        route, status = "synthesize", ("recovered" if state.retry_count > 0 else "ok")
    elif state.retry_count >= MAX_RETRIES:
        route, status = "synthesize", ("failed" if err else "empty")
    else:
        route, status = "self_correct", "retrying"
    note = (
        f"validate: -> {route} (error={bool(err)}, rows={rows}, "
        f"retry={state.retry_count}/{MAX_RETRIES})"
    )
    return {"route": route, "status": status, "step_trace": state.step_trace + [note]}


def self_correct(state: AgentState) -> dict:
    schema = state.schema_snapshot or tools.schema_inspect()
    # Scrub the DB error before it enters a prompt — it's the one prompt input not
    # produced by the de-identified views and could echo a literal from the query.
    db_error = scrub.scrub(state.execution_error or "query returned no rows")
    hints = f"{state.code_hints}\n\n" if state.code_hints else ""
    user = (
        f"Schema:\n{schema}\n\nQuestion: {state.question}\n\n"
        f"Failed SQL:\n{state.candidate_sql or '(none)'}\n\n"
        f"Database error / problem: {db_error}\n\n{hints}"
        "Return the corrected SQL."
    )
    try:
        res = _run_llm(
            "self_correct", prompts.SELF_CORRECT_PROMPT_V1, user,
            model=settings.sql_model_resolved,
        )
        sql = tools.extract_sql(res.text)
        return _sql_update(
            state, res, sql, f"self_correct #{state.retry_count + 1}",
            extra={"retry_count": state.retry_count + 1},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("self_correct node failed", exc_info=True)
        return {
            "retry_count": state.retry_count + 1,  # still advance so the loop terminates
            "step_trace": state.step_trace + [f"self_correct failed: {exc}"],
        }


def synthesize(state: AgentState) -> dict:
    if _has_data(state.execution_result):
        user = (
            f"Question: {state.question}\n\nSQL that was run:\n{state.candidate_sql}\n\n"
            f"Result columns: {state.execution_result.get('columns')}\n"
            f"Result rows ({state.row_count}):\n{_render_result(state.execution_result)}"
        )
        try:
            res = _run_llm("synthesize", prompts.SYNTHESIZE_PROMPT_V1, user)
            answer = res.text.strip()
            # Output leakage gate: an identifier in the answer is a HARD failure (not a
            # silent redaction) — refuse the answer. On synthetic aggregates it never fires.
            out_leaks = leakage.scan_text(answer, where="synthesize:output")
            if out_leaks:
                return {
                    "final_answer": "Answer withheld: a PHI-safety (leakage) check failed.",
                    "status": "refused",
                    "leakage_hits": state.leakage_hits + [f"output:{lk.kind}" for lk in out_leaks],
                    "step_trace": state.step_trace + ["synthesize: OUTPUT leakage -> refused"],
                    **_acc(state, res),
                }
            status = state.status if state.status in ("ok", "recovered") else "ok"
            return {
                "final_answer": answer,
                "status": status,
                "step_trace": state.step_trace + ["synthesize: answered"],
                **_acc(state, res),
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("synthesize node failed; using templated answer", exc_info=True)
            fb = _fallback_answer(state)
            if leakage.scan_text(fb):  # gate the templated fallback too
                fb = "Answer withheld: a PHI-safety check failed on the result."
            return {
                "final_answer": fb,
                "status": "ok",
                "step_trace": state.step_trace + [f"synthesize failed, templated: {exc}"],
            }

    # Graceful degradation — no data or unrecovered error.
    if state.execution_error:
        msg = (
            "I couldn't determine an answer from the available data "
            f"(last database error: {state.execution_error})."
        )
        status = "failed"
    else:
        msg = "I couldn't determine an answer — the query returned no rows."
        status = "empty"
    return {
        "final_answer": msg,
        "status": status,
        "step_trace": state.step_trace + ["synthesize: graceful (no answer)"],
    }


def route_after_validate(state: AgentState) -> str:
    return state.route or "synthesize"

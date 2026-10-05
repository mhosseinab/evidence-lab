"""The single bounded query workflow, with a safe public topology descriptor."""
from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from evidence_lab.domain import Draft, EvidencePack


class QueryState(TypedDict, total=False):
    evidence: EvidencePack
    draft: Draft
    verdict: dict[str, Any]
    round_number: int
    terminal_fields: dict[str, Any]
    result: dict[str, Any]


NODES = ("retrieve", "generate", "structural", "verify", "repair", "release", "terminal")
EDGES = (
    ("start", "retrieve"), ("retrieve", "generate"), ("retrieve", "terminal"),
    ("generate", "structural"), ("structural", "verify"), ("verify", "release"),
    ("verify", "repair"), ("verify", "terminal"), ("repair", "structural"),
    ("release", "terminal"), ("terminal", "end"),
)


def graph_descriptor() -> dict:
    """Describe control flow without exposing questions, evidence or private drafts."""
    return {"framework": "langgraph", "nodes": list(NODES),
            "edges": [{"source": source, "target": target} for source, target in EDGES],
            "recovery": "durable_job_restart", "checkpointing": False}


def compile_query_graph(nodes):
    builder = StateGraph(QueryState)
    for name in NODES:
        builder.add_node(name, nodes[name])
    builder.add_edge(START, "retrieve")
    builder.add_conditional_edges("retrieve", lambda state: "terminal" if state.get("terminal_fields") else "generate",
                                  {"terminal": "terminal", "generate": "generate"})
    builder.add_edge("generate", "structural")
    builder.add_edge("structural", "verify")
    builder.add_conditional_edges("verify", lambda state: "terminal" if state.get("terminal_fields") else (
        "release" if state["verdict"]["accepted"] else "repair"),
        {"terminal": "terminal", "release": "release", "repair": "repair"})
    builder.add_edge("repair", "structural")
    builder.add_edge("release", "terminal")
    builder.add_edge("terminal", END)
    return builder.compile()

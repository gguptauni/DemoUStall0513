"""Java migration demo generator for one COBOL program.

The Streamlit page uses this module so the Java-conversion logic stays isolated
from the app shell. It builds a graph/enrichment context from the existing
SQLite knowledge graph, then asks Gemini for a Java translation. If Gemini is
unavailable, it returns a deterministic Java skeleton grounded in the same data.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI


def _read_source(project_root: Path, file_path: str | None) -> str:
    if not file_path:
        return ""
    path = Path(file_path)
    if not path.is_absolute():
        path = project_root / path
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")


def _query_rows(loader, query: str, params: tuple = ()) -> list[dict]:
    try:
        cur = loader.conn.cursor()
        cur.execute(query, params)
        return [dict(row) for row in cur.fetchall()]
    except Exception:
        return []


def build_java_migration_context(loader, program_id: str, project_root: str | Path) -> Dict[str, Any]:
    """Collect graph, enrichment, and source facts for a COBOL-to-Java demo."""
    project_root = Path(project_root)
    details = loader.get_program_details(program_id) or {}
    source = _read_source(project_root, details.get("file_path"))

    graph = {
        "calls": details.get("calls") or [],
        "called_by": details.get("called_by") or [],
        "files": details.get("files") or [],
        "data_items": (details.get("data_items") or [])[:80],
        "paragraphs": (details.get("paragraphs") or [])[:40],
        "business_rules": (details.get("business_rules") or [])[:40],
        "data_movements": _query_rows(
            loader,
            """
            SELECT source_field, destination_field, paragraph_name, line_number
            FROM data_movements
            WHERE program_id = ?
            ORDER BY line_number
            LIMIT 80
            """,
            (program_id,),
        ),
        "file_operations": _query_rows(
            loader,
            """
            SELECT file_name, operation, paragraph_name, line_number
            FROM file_operations
            WHERE program_id = ?
            ORDER BY line_number
            LIMIT 80
            """,
            (program_id,),
        ),
    }

    return {
        "program": {
            "program_id": details.get("program_id"),
            "file_path": details.get("file_path"),
            "program_type": details.get("program_type"),
            "line_count": details.get("line_count"),
            "business_name": details.get("business_name"),
            "business_purpose": details.get("business_purpose"),
            "modern_equivalent": details.get("modern_equivalent"),
            "migration_approach": details.get("migration_approach"),
        },
        "graph": graph,
        "source_excerpt": source[:18000],
        "source_line_count": len(source.splitlines()),
        "evidence_source": "SQLite knowledge graph populated from parser, Neo4j export/enrichment, and LLM enrichment tables.",
    }


def _extract_code(text: str) -> str:
    match = re.search(r"```(?:java)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else text.strip()


def _fallback_java(context: Dict[str, Any]) -> str:
    program = context["program"]
    graph = context["graph"]
    files = [f.get("file_name") for f in graph.get("files", []) if f.get("file_name")]
    paragraphs = [p.get("paragraph_name") for p in graph.get("paragraphs", []) if p.get("paragraph_name")]
    rules = graph.get("business_rules", [])

    methods = "\n\n".join(
        f"""    private void { _java_method_name(name) }() {{
        // Migrated from COBOL paragraph {name}.
    }}"""
        for name in paragraphs[:10]
    )
    rule_comments = "\n".join(
        f"        // Rule: {r.get('rule_name') or r.get('rule_statement') or 'extracted business rule'}"
        for r in rules[:8]
    )
    file_comments = ", ".join(files) or "not present in extracted data"
    return f"""package com.unisys.migration.carddemo;

import java.math.BigDecimal;
import java.nio.file.Path;
import java.util.Objects;

/**
 * Demo Java migration skeleton for COBOL program {program.get('program_id')}.
 * Evidence source: {context.get('evidence_source')}
 *
 * Source files: {file_comments}
 */
public final class {program.get('program_id', 'MigratedProgram').title().replace('_', '')}Migration {{

    private final AccountRepository accountRepository;
    private final AccountOutputWriter outputWriter;

    public {program.get('program_id', 'MigratedProgram').title().replace('_', '')}Migration(
            AccountRepository accountRepository,
            AccountOutputWriter outputWriter) {{
        this.accountRepository = Objects.requireNonNull(accountRepository);
        this.outputWriter = Objects.requireNonNull(outputWriter);
    }}

    public void run(Path accountInput) {{
        accountRepository.streamAccounts(accountInput).forEach(account -> {{
{rule_comments}
            outputWriter.writeFlatAccount(account);
            outputWriter.writeBalanceArray(account);
            outputWriter.writeVariableRecord(account);
        }});
    }}
{methods}

    public record Account(long accountId, String activeStatus, BigDecimal currentBalance,
                          BigDecimal creditLimit, String openDate, String expirationDate,
                          String reissueDate, String groupId) {{}}

    public interface AccountRepository {{
        java.util.stream.Stream<Account> streamAccounts(Path accountInput);
    }}

    public interface AccountOutputWriter {{
        void writeFlatAccount(Account account);
        void writeBalanceArray(Account account);
        void writeVariableRecord(Account account);
    }}
}}
"""


def _java_method_name(name: str) -> str:
    parts = re.split(r"[^A-Za-z0-9]+", name.lower())
    if not parts:
        return "migratedParagraph"
    return parts[0] + "".join(part.capitalize() for part in parts[1:] if part)


def generate_java_for_program(loader, program_id: str, project_root: str | Path, use_llm: bool = True) -> Dict[str, Any]:
    """Generate Java code for a COBOL program using graph/enrichment evidence."""
    context = build_java_migration_context(loader, program_id, project_root)

    if not use_llm or not os.environ.get("GEMINI_API_KEY"):
        return {"java_code": _fallback_java(context), "context": context, "used_llm": False}

    model_name = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    llm = ChatGoogleGenerativeAI(
        model=model_name,
        google_api_key=os.environ.get("GEMINI_API_KEY"),
        temperature=0.1,
        max_output_tokens=12000,
    )
    prompt = f"""
Convert COBOL program {program_id} into Java for a modernization demo.

Use the graph/enrichment evidence and COBOL source below. Generate one cohesive
Java class plus small nested records/interfaces where useful. The code should be
readable demo-quality Java, not a byte-for-byte COBOL transliteration.

Requirements:
- Preserve file-processing intent: read account records and write the output,
  array, and variable-length record outputs when present.
- Name the class Cbact01cMigration.
- Include comments mapping important Java methods back to COBOL paragraphs.
- Use only facts from this context. Do not invent databases, queues, or APIs.
- Return Java code only, no markdown explanation.

CONTEXT JSON:
{json.dumps(context, indent=2, default=str)}
"""
    response = llm.invoke([
        SystemMessage(content="You are a senior Java modernization engineer. Return Java code only."),
        HumanMessage(content=prompt),
    ])
    return {"java_code": _extract_code(response.content), "context": context, "used_llm": True}

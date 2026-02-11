"""
Backend proxy server for AiChemy React app.
Handles Databricks authentication, proxies requests to the agent endpoint,
and provides project persistence via SQLite (local dev) or Lakebase Postgres (production).
"""
import os
import re
import json
import yaml
import sqlite3
import requests
from uuid import uuid4
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Union
from databricks.sdk import WorkspaceClient

# ---------------------------------------------------------------------------
# Database layer – SQLite for local dev, Postgres for production
#
# Set DATABASE_URL to use Postgres (works with any Postgres, including Lakebase):
#   DATABASE_URL=postgresql://user:pass@host:5432/dbname
#
# If DATABASE_URL is not set, falls back to a local SQLite file.
# ---------------------------------------------------------------------------

class ProjectDB:
    """Thin abstraction over SQLite or Postgres for project storage."""

    def __init__(self, database_url: Optional[str] = None, sqlite_path: str = "projects.db"):
        self._database_url = database_url
        self._sqlite_path = sqlite_path
        self._use_pg = database_url is not None
        self._pg_pool = None
        self._init_db()

    # -- Initialization -----------------------------------------------------

    def _init_db(self):
        if self._use_pg:
            self._init_pg()
        else:
            self._init_sqlite()

    def _init_sqlite(self):
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    messages TEXT NOT NULL DEFAULT '[]',
                    agent_steps TEXT DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_projects_user ON projects(user_id)")
            conn.commit()

    def _init_pg(self):
        import psycopg
        from psycopg_pool import ConnectionPool
        self._pg_pool = ConnectionPool(
            conninfo=self._database_url,
            kwargs={"autocommit": False},
            min_size=1,
            max_size=10,
            open=True,
        )
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    messages TEXT NOT NULL DEFAULT '[]',
                    agent_steps TEXT DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_projects_user ON projects(user_id)
            """)
            conn.commit()

    # -- Connection helpers -------------------------------------------------

    @contextmanager
    def _conn(self):
        if self._use_pg:
            with self._pg_pool.connection() as conn:
                yield conn
        else:
            conn = sqlite3.connect(self._sqlite_path)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
            finally:
                conn.close()

    def _cursor(self, conn):
        """Return a cursor. For Postgres, use dict_row factory."""
        if self._use_pg:
            import psycopg.rows
            return conn.cursor(row_factory=psycopg.rows.dict_row)
        return conn.cursor()

    def _placeholder(self) -> str:
        """Return the parameterized query placeholder for the current backend."""
        return "%s" if self._use_pg else "?"

    # -- CRUD ---------------------------------------------------------------

    def list_projects(self, user_id: str) -> list[dict]:
        p = self._placeholder()
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                f"SELECT id, name, created_at, updated_at FROM projects "
                f"WHERE user_id = {p} ORDER BY updated_at DESC",
                (user_id,),
            )
            rows = cur.fetchall()
            return [dict(r) for r in rows]

    def create_project(self, user_id: str, name: str) -> dict:
        p = self._placeholder()
        project_id = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(
                f"INSERT INTO projects (id, user_id, name, messages, agent_steps, created_at, updated_at) "
                f"VALUES ({p}, {p}, {p}, '[]', '[]', {p}, {p})",
                (project_id, user_id, name, now, now),
            )
            conn.commit()
        return {"id": project_id, "name": name, "messages": [], "agent_steps": [], "created_at": now, "updated_at": now}

    def get_project(self, project_id: str) -> Optional[dict]:
        p = self._placeholder()
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(f"SELECT * FROM projects WHERE id = {p}", (project_id,))
            row = cur.fetchone()
            if row is None:
                return None
            d = dict(row)
            d["messages"] = json.loads(d["messages"])
            d["agent_steps"] = json.loads(d["agent_steps"])
            return d

    def update_project(self, project_id: str, name: Optional[str] = None,
                       messages: Optional[list] = None,
                       agent_steps: Optional[list] = None) -> Optional[dict]:
        p = self._placeholder()
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(f"SELECT id FROM projects WHERE id = {p}", (project_id,))
            if cur.fetchone() is None:
                return None
            now = datetime.now(timezone.utc).isoformat()
            updates: list[str] = [f"updated_at = {p}"]
            params: list = [now]
            if name is not None:
                updates.append(f"name = {p}")
                params.append(name)
            if messages is not None:
                updates.append(f"messages = {p}")
                params.append(json.dumps(messages))
            if agent_steps is not None:
                updates.append(f"agent_steps = {p}")
                params.append(json.dumps(agent_steps))
            params.append(project_id)
            cur.execute(
                f"UPDATE projects SET {', '.join(updates)} WHERE id = {p}", params
            )
            conn.commit()
        return self.get_project(project_id)

    def delete_project(self, project_id: str) -> bool:
        p = self._placeholder()
        with self._conn() as conn:
            cur = self._cursor(conn)
            cur.execute(f"DELETE FROM projects WHERE id = {p}", (project_id,))
            deleted = cur.rowcount > 0
            conn.commit()
            return deleted


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(title="AiChemy API Proxy")

# CORS for React frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:5173", "http://127.0.0.1:3000", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Databricks client (lazily initialized — only needed for the agent proxy endpoint)
DATABRICKS_HOST = os.getenv("DATABRICKS_HOST", "https://fevm-aichemy2.cloud.databricks.com")
_workspace_client = None

def _get_workspace_client() -> WorkspaceClient:
    global _workspace_client
    if _workspace_client is None:
        _workspace_client = WorkspaceClient(host=DATABRICKS_HOST)
    return _workspace_client

# Initialize project database
# Set DATABASE_URL for Postgres (e.g. Lakebase): postgresql://user:pass@host:5432/dbname
db = ProjectDB(
    database_url=os.getenv("DATABASE_URL"),
    sqlite_path=os.getenv("PROJECTS_DB_PATH", "projects.db"),
)

# Default user for local development (in production, read from auth headers)
DEFAULT_USER_ID = "demo-user"

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class Message(BaseModel):
    role: str
    content: str

class CustomInputs(BaseModel):
    thread_id: str

class AgentRequest(BaseModel):
    input: List[Message]
    custom_inputs: CustomInputs
    skill_name: Optional[str] = None  # When set, wraps the prompt with skill instructions

class CreateProjectRequest(BaseModel):
    name: str
    user_id: Optional[str] = None

class UpdateProjectRequest(BaseModel):
    name: Optional[str] = None
    messages: Optional[list] = None
    agent_steps: Optional[list] = None

# ---------------------------------------------------------------------------
# Agent proxy endpoint
# Set MOCK_AGENT=1 to return a canned response (for local dev without Databricks auth)
# ---------------------------------------------------------------------------

MOCK_AGENT = os.getenv("MOCK_AGENT", "0") == "1"

@app.post("/api/agent")
async def call_agent(request: AgentRequest):
    """Proxy request to Databricks agent endpoint (or return mock response)."""
    # --- Mock mode for local development ---
    if MOCK_AGENT:
        user_text = request.input[-1].content if request.input else ""
        return {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "text", "text": f"[mock] You asked: \"{user_text}\". This is a mock response because the server is running in local dev mode (MOCK_AGENT=1). Connect to Databricks for real agent responses."}
                    ],
                }
            ]
        }

    # --- Production: proxy to Databricks serving endpoint ---
    try:
        messages = [{"role": msg.role, "content": msg.content} for msg in request.input]
        if request.skill_name and messages:
            last_msg = messages[-1]
            enhanced = build_prompt_with_skill(last_msg["content"], request.skill_name)
            messages[-1] = {"role": last_msg["role"], "content": enhanced}

        input_dict = {
            "input": messages,
            "custom_inputs": {"thread_id": request.custom_inputs.thread_id},
            "databricks_options": {"return_trace": True},
        }

        endpoint = os.getenv("SERVING_ENDPOINT", "aichemy")
        w = _get_workspace_client()

        # Get the workspace host and build the URL
        host = w.config.host.rstrip('/')
        url = f"{host}/serving-endpoints/{endpoint}/invocations"

        # Get authentication headers from the SDK
        headers = w.config.authenticate()
        headers["Content-Type"] = "application/json"

        # Make the request
        response = requests.post(url=url, headers=headers, json=input_dict)

        if response.status_code != 200:
            raise HTTPException(
                status_code=response.status_code,
                detail=f"{response.status_code} Error: {response.text}"
            )

        raw = response.json()

        # Parse the response to extract tool calls, genie results, and clean text
        text_contents = extract_text_content(raw)
        all_tool_calls = []
        cleaned_texts = []
        if text_contents:
            # Agent appends according to thread_id; use the last entry
            for tc_text in [text_contents[-1]]:
                all_tool_calls.extend(parse_tool_calls(tc_text))
                cleaned = strip_tool_call_tags(tc_text)
                if cleaned:
                    cleaned_texts.append(cleaned)
        genie_results = parse_genie_results(raw)

        return {
            **raw,
            "parsed": {
                "text": "\n\n".join(cleaned_texts) if cleaned_texts else (
                    "No response. Retry or reset the chat."
                ),
                "tool_calls": all_tool_calls,
                "genie": genie_results,
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Streaming agent endpoint — streams text chunks via SSE
# ---------------------------------------------------------------------------

@app.post("/api/agent/stream")
async def call_agent_stream(request: AgentRequest):
    """Stream agent response as Server-Sent Events (SSE).
    
    Each SSE event is a JSON object with a `type` field:
      - {"type": "text", "content": "..."} — a text chunk to append
      - {"type": "tool_calls", "data": [...]}  — parsed tool calls
      - {"type": "genie", "data": [...]}       — parsed genie SQL results
      - {"type": "done"}                       — stream complete
      - {"type": "error", "content": "..."}    — error occurred
    """
    if MOCK_AGENT:
        user_text = request.input[-1].content if request.input else ""
        async def mock_stream():
            import asyncio
            words = f"[mock] You asked: \"{user_text}\". This is a streaming mock response.".split()
            for word in words:
                yield f"data: {json.dumps({'type': 'text', 'content': word + ' '})}\n\n"
                await asyncio.sleep(0.05)
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
        return StreamingResponse(mock_stream(), media_type="text/event-stream")

    def _sse(event: dict) -> str:
        return f"data: {json.dumps(event)}\n\n"

    def stream_generator():
        import time

        try:
            # If skills are enabled, wrap the user prompt with skill instructions
            messages = [{"role": msg.role, "content": msg.content} for msg in request.input]
            if request.skill_name and messages:
                last_msg = messages[-1]
                enhanced = build_prompt_with_skill(last_msg["content"], request.skill_name)
                messages[-1] = {"role": last_msg["role"], "content": enhanced}

            input_dict = {
                "input": messages,
                "custom_inputs": {"thread_id": request.custom_inputs.thread_id},
                "databricks_options": {"return_trace": True},
            }

            endpoint = os.getenv("SERVING_ENDPOINT", "aichemy")
            w = _get_workspace_client()
            host = w.config.host.rstrip('/')
            url = f"{host}/serving-endpoints/{endpoint}/invocations"
            headers = w.config.authenticate()
            headers["Content-Type"] = "application/json"

            yield _sse({"type": "status", "content": "Waiting for agent..."})

            resp = requests.post(url=url, headers=headers, json=input_dict)

            if resp.status_code != 200:
                yield _sse({"type": "error", "content": f"{resp.status_code}: {resp.text[:500]}"})
                return

            yield _sse({"type": "status", "content": "Streaming response..."})

            raw = resp.json()
            text_contents = extract_text_content(raw)
            all_tool_calls = []
            cleaned_text = ""
            if text_contents:
                for tc_text in [text_contents[-1]]:
                    all_tool_calls.extend(parse_tool_calls(tc_text))
                    cleaned = strip_tool_call_tags(tc_text)
                    if cleaned:
                        cleaned_text = cleaned

            # Stream the text word-by-word for typewriter effect
            if cleaned_text:
                words = cleaned_text.split(' ')
                for i, word in enumerate(words):
                    chunk = word + (' ' if i < len(words) - 1 else '')
                    yield _sse({"type": "text", "content": chunk})
                    time.sleep(0.02)

            if all_tool_calls:
                yield _sse({"type": "tool_calls", "data": all_tool_calls})

            genie_results = parse_genie_results(raw)
            if genie_results:
                yield _sse({"type": "genie", "data": genie_results})

        except Exception as e:
            yield _sse({"type": "error", "content": str(e)})

        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(stream_generator(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Project CRUD endpoints
# ---------------------------------------------------------------------------

@app.get("/api/projects")
async def list_projects(user_id: str = Query(default=None)):
    """List all projects for a user, ordered by most recently updated."""
    uid = user_id or DEFAULT_USER_ID
    return db.list_projects(uid)

@app.post("/api/projects")
async def create_project(req: CreateProjectRequest):
    """Create a new project. Returns the full project object."""
    uid = req.user_id or DEFAULT_USER_ID
    return db.create_project(uid, req.name)

@app.get("/api/projects/{project_id}")
async def get_project(project_id: str):
    """Load a project with its full messages and agent steps."""
    project = db.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project

@app.put("/api/projects/{project_id}")
async def update_project(project_id: str, req: UpdateProjectRequest):
    """Update a project's name, messages, and/or agent steps."""
    project = db.update_project(
        project_id,
        name=req.name,
        messages=req.messages,
        agent_steps=req.agent_steps,
    )
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project

@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: str):
    """Delete a project."""
    deleted = db.delete_project(project_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Project not found")
    return {"ok": True}

# ---------------------------------------------------------------------------
# Response parsing helpers (mirrors Streamlit utils.py)
# ---------------------------------------------------------------------------

def parse_tool_calls(text_content: str) -> list[dict]:
    """Parse <function_calls> and <thinking> blocks from text content."""
    tool_calls = []
    function_calls_blocks = re.findall(
        r'<function_calls>\s*(.*?)\s*</function_calls>', text_content, re.DOTALL
    )
    thinking_blocks = re.findall(
        r'<thinking>\s*(.*?)\s*</thinking>', text_content, re.DOTALL
    )
    for block in function_calls_blocks:
        invokes = re.findall(r'<invoke name="([^"]+)">\s*(.*?)\s*</invoke>', block, re.DOTALL)
        for function_name, params_block in invokes:
            params = re.findall(r'<parameter name="([^"]+)">([^<]*)</parameter>', params_block)
            parameters = {n: v.strip() for n, v in params}
            tool_calls.append({"function_name": function_name, "parameters": parameters, "thinking": None})
    for i, thinking in enumerate(thinking_blocks):
        if i < len(tool_calls):
            tool_calls[i]["thinking"] = thinking.strip()
    return tool_calls


def strip_tool_call_tags(text_content: str) -> str:
    """Strip <function_calls> and <thinking> tags from text."""
    text_content = re.sub(r'<function_calls>\s*.*?\s*</function_calls>', '', text_content, flags=re.DOTALL)
    text_content = re.sub(r'<thinking>\s*.*?\s*</thinking>', '', text_content, flags=re.DOTALL)
    text_content = re.sub(r'\n\s*\n\s*\n+', '\n\n', text_content)
    return text_content.strip()


def parse_genie_results(response_json: dict) -> list[dict]:
    """Extract poll_query_results from databricks_output trace spans."""
    results = []
    try:
        spans = response_json.get("databricks_output", {}).get("trace", {}).get("data", {}).get("spans", [])
    except (AttributeError, KeyError):
        return results
    for span in spans:
        if span.get("name") == "poll_query_results":
            span_outputs = span.get("attributes", {}).get("mlflow.spanOutputs", "{}")
            try:
                outputs = json.loads(json.loads(span_outputs))
                results.append({
                    "result": outputs.get("result", ""),
                    "query": outputs.get("query", ""),
                    "description": outputs.get("description", ""),
                })
            except (json.JSONDecodeError, TypeError):
                continue
    return results


def extract_text_content(response_json: dict) -> list[str]:
    """Extract text content from agent response."""
    text_contents = []
    for item in response_json.get("output", []):
        if item.get("type") == "message":
            new_text = item.get("content", [{}])[0].get("text")
            if new_text and new_text not in text_contents:
                text_contents.append(new_text)
    return text_contents


# ---------------------------------------------------------------------------
# Tools endpoint — serve the tools manifest
# ---------------------------------------------------------------------------

# Locate tools.txt relative to the Streamlit app (shared data)
_TOOLS_PATH = Path(__file__).resolve().parent.parent.parent / "app" / "tools.txt"

@app.get("/api/tools")
async def get_tools():
    """Return available tools grouped by agent, loaded from tools.txt."""
    if not _TOOLS_PATH.exists():
        return []
    groups: dict[str, list[dict]] = {}
    with open(_TOOLS_PATH) as f:
        for i, line in enumerate(f):
            if i == 0:
                continue  # skip header
            parts = line.strip().split("\t")
            if len(parts) >= 3:
                agent, tool_name, description = parts[0], parts[1], parts[2]
                groups.setdefault(agent, []).append({"name": tool_name, "description": description})
    return groups

# ---------------------------------------------------------------------------
# Skills – discover, load, and build prompts with skill instructions
# ---------------------------------------------------------------------------

# The skills directory lives alongside the Streamlit app
_SKILLS_DIR = Path(__file__).resolve().parent.parent.parent / "app" / "skills"


def _smart_title(s: str) -> str:
    """Title-case words, but leave fully uppercase words (e.g. ADME) unchanged."""
    return " ".join(w if w.isupper() else w.title() for w in s.split())


def _parse_skill_frontmatter(content: str) -> dict:
    """Parse YAML frontmatter (between --- delimiters) from a SKILL.md file."""
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
    if match:
        try:
            return yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError:
            pass
    return {}


def discover_skills(skills_dir: Optional[Union[str, Path]] = None) -> dict:
    """Scan the skills directory and return metadata keyed by skill folder name."""
    skills_dir = Path(skills_dir) if skills_dir else _SKILLS_DIR
    skills: dict[str, dict] = {}
    if not skills_dir.exists():
        return skills

    for folder in skills_dir.iterdir():
        if not folder.is_dir():
            continue
        skill_file = folder / "SKILL.md"
        if not skill_file.exists():
            continue
        try:
            content = skill_file.read_text(encoding="utf-8")
            fm = _parse_skill_frontmatter(content)
            name = fm.get("name", folder.name)
            description = fm.get("description", "")

            # Assign emoji + ordering based on skill type
            lower = name.lower()
            if "target" in lower:
                label, order = f"🎯 {_smart_title(name.replace('-', ' '))}", 0
            elif "hit" in lower:
                label, order = f"⌬ {_smart_title(name.replace('-', ' '))}", 1
            elif "adme" in lower:
                label, order = f"🧪 {_smart_title(name.replace('-', ' '))}", 2
            elif "safety" in lower:
                label, order = f"☠️ {_smart_title(name.replace('-', ' '))}", 3
            else:
                label, order = f"📋 {_smart_title(name.replace('-', ' '))}", 4

            caption = description.split(". ")[0] if description else ""
            if len(caption) > 70:
                caption = caption[:67] + "..."

            skills[name] = {
                "description": description,
                "path": str(folder),
                "label": label,
                "caption": caption,
                "order": order,
            }
        except Exception:
            continue
    return skills


def load_skill_content(skill_name: str, skills_dir: Optional[Union[str, Path]] = None) -> Optional[dict]:
    """Load full SKILL.md + reference files for a given skill."""
    skills_dir = Path(skills_dir) if skills_dir else _SKILLS_DIR
    skill_path = skills_dir / skill_name
    skill_file = skill_path / "SKILL.md"
    if not skill_file.exists():
        return None
    try:
        full_content = skill_file.read_text(encoding="utf-8")
        fm = _parse_skill_frontmatter(full_content)
        match = re.match(r"^---\s*\n.*?\n---\s*\n(.*)$", full_content, re.DOTALL)
        body = match.group(1).strip() if match else full_content

        # Load reference files
        references: dict[str, str] = {}
        refs_dir = skill_path / "references"
        if refs_dir.exists():
            for ref_file in refs_dir.iterdir():
                if ref_file.is_file() and ref_file.suffix == ".md":
                    try:
                        references[ref_file.name] = ref_file.read_text(encoding="utf-8")
                    except Exception:
                        continue

        full_prompt = f"# Skill: {fm.get('name', skill_name)}\n\n{body}"
        if references:
            full_prompt += "\n\n---\n\n## Reference Materials\n\n"
            for ref_name, ref_content in references.items():
                full_prompt += f"### {ref_name}\n\n{ref_content}\n\n"

        return {"frontmatter": fm, "content": body, "references": references, "full_prompt": full_prompt}
    except Exception:
        return None


def build_prompt_with_skill(user_query: str, skill_name: str, skills_dir: Optional[Union[str, Path]] = None) -> str:
    """Wrap a user query with skill instructions if the skill exists."""
    skill_data = load_skill_content(skill_name, skills_dir)
    if not skill_data:
        return user_query
    return (
        "You have been given a specialized skill to help with this task. "
        "Follow the workflow instructions carefully.\n\n"
        f"<skill_instructions>\n{skill_data['full_prompt']}\n</skill_instructions>\n\n"
        f"<user_request>\n{user_query}\n</user_request>\n\n"
        "Execute the skill workflow to address the user's request. "
        "Follow each step methodically and provide the expected output format."
    )


def extract_user_request(prompt: str) -> str:
    """Extract the user query from <user_request> tags, or return the original prompt."""
    match = re.search(r"<user_request>\s*(.*?)\s*</user_request>", prompt, re.DOTALL)
    return match.group(1).strip() if match else prompt


# Skill-name → workflow-index mapping for determining which skill folder to use
_SKILL_FOLDER_BY_WORKFLOW_IDX = {
    0: "target-identification",
    1: "hit-identification",
    2: "ADME-assessment",
    3: "safety-assessment",
}


@app.get("/api/skills")
async def get_skills():
    """Return discovered skills metadata (sorted by order)."""
    skills = discover_skills()
    sorted_skills = sorted(skills.items(), key=lambda x: x[1].get("order", 99))
    return {
        "skills": {name: meta for name, meta in sorted_skills},
        "labels": [meta["label"] for _, meta in sorted_skills],
        "captions": [meta["caption"] for _, meta in sorted_skills],
    }


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "host": DATABRICKS_HOST}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

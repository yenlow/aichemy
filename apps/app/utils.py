import ast
import os
#from tkinter.constants import S
import requests
import json
import streamlit as st
from databricks.sdk import WorkspaceClient
import base64
from pathlib import Path
from typing import Optional, Union, List
import yaml
import re


def get_user_info():
    headers = st.context.headers
    return dict(
        user_name=headers.get("X-Forwarded-Preferred-Username"),
        user_email=headers.get("X-Forwarded-Email"),
        user_id=headers.get("X-Forwarded-User"),
    )


def ask_agent(input_dict: dict, w: WorkspaceClient = None) -> requests.models.Response:
    # Example input_dict:
    # input_dict = {
    #     "input": [
    #         {"role": "user", "content": "What is the latest customer service request?"}
    #     ],
    #     "custom_inputs": {"thread_id": "1001"},
    # }
    url = f'https://e2-demo-field-eng.cloud.databricks.com/serving-endpoints/{os.getenv("SERVING_ENDPOINT")}/invocations'
    if w is None:
        w = WorkspaceClient()
    headers = w.config.authenticate()
    response = requests.post(
        headers=headers,
        url=url,
        json=input_dict,
    )
    if response.status_code != 200:
        raise Exception(f"Request failed with status {response.status_code}, {response.text}")
    return response


def ask_agent_mlflowclient(input_dict: dict, client) -> dict:
    # returns response.json()
    return client.predict(endpoint=os.getenv("SERVING_ENDPOINT"), inputs=input_dict)


def extract_text_content(response_json):
    # Extract text content from the response (equivalent to jq extraction)
    # jq -r '.output[] | select(.type == "message") | .content[] | .text'
    text_contents = []
    for output_item in response_json.get("output", []):
        if output_item.get("type") == "message":
            new_text = output_item.get("content")[0].get("text")
            if new_text not in text_contents:
                text_contents.append(new_text)
    return text_contents


# Capture list body between tool_calls=[ and ", 'type': 'tool_call'}]"
_TOOL_CALLS_REPR_RE = re.compile(
    r"tool_calls\s*=\s*\[\s*(.*,\s*'type':\s*'tool_call'\})\s*]",
    re.DOTALL | re.IGNORECASE,
)
_TOOL_CALLS_JSON_RE = re.compile(
    r'"tool_calls"\s*:\s*\[\s*(.*,\s*"type":\s*"tool_call"\})\s*]',
    re.DOTALL | re.IGNORECASE,
)


def parse_tool_calls_block(text: str) -> list[dict]:
    """
    Parse the tool_calls=[{...}] block from a string (e.g. spanInputs or repr).

    Regex captures the list content between tool_calls=[ and ", 'type': 'tool_call'}]",
    then parses as JSON or Python literal. Returns list of {name, args, id, type}.
    """
    if not text or "tool_calls=" not in text:
        return []
    match = _TOOL_CALLS_REPR_RE.search(text) or _TOOL_CALLS_JSON_RE.search(text)
    if not match:
        return []
    raw = match.group(1).strip()
    raw_python = raw.replace("\\'", "'")
    try:
        decoded = json.loads("[" + raw + "]")
    except json.JSONDecodeError:
        try:
            decoded = ast.literal_eval("[" + raw_python + "]")
        except (ValueError, SyntaxError):
            return []
    return [
        {"name": item.get("name"), "args": item.get("args", {}), "id": item.get("id"), "type": item.get("type")}
        for item in decoded
    ]


def parse_tools(response_json: Union[dict, str]) -> list[dict]:
    """
    Parse tool calls from response JSON by extracting spans with spanType="TOOL".
    Reads tool_calls=[{...}] from spanInputs via parse_tool_calls_block for name/args.
    """
    tool_calls = []
    try:
        spans = response_json.get("databricks_output", {}).get("trace", {}).get("data", {}).get("spans", [])
    except (AttributeError, KeyError):
        return tool_calls

    for i, span in enumerate(spans):
        attrs = span.get("attributes", {})
        span_inputs = attrs.get("mlflow.spanInputs", "")
        span_output = json.loads(attrs.get("mlflow.spanOutputs", "{}"))
        if attrs.get("mlflow.spanType") == '"TOOL"' and span_inputs != "{}":
            content = span_output.get("content")
            if isinstance(content, str):
                content = content.replace("\n", " ").replace("\r", " ").replace("\t", " ")
                content = re.sub(r"\s*([{}[\]:,])\s*", r"\1", content).strip()
                try:
                    content = json.loads(content)
                except json.JSONDecodeError:
                    content ={"answer": content}

            name = span_output.get("name")
            if name and isinstance(content, dict):
                tool_calls.append({
                    "tool_name": name,
                    "answer": content.get("details") or content.get("results") or content.get("answer"),
                    "args": {k: v for k, v in content.items() if k not in ("details", "results", "answer")},
                })
            elif name and not isinstance(content, dict):
                tool_calls.append({
                    "tool_name": name,
                    "answer": content
                })

            if "details" not in content:
                parsed = parse_tool_calls_block(span_inputs)
                if parsed:
                    tool_calls[-1]["args"] = parsed[0].get("args")

        # if i == len(spans) - 1:
        #     json.loads(span_inputs).get("tool_calls")

    return tool_calls


def parse_genie_results(response_json):
    # Convert string to dict if needed
    if isinstance(response_json, str):
        response_json = json.loads(response_json)

    results = []

    # Navigate to spans
    try:
        spans = response_json.get("databricks_output", {}).get("trace", {}).get("data", {}).get("spans", [])
    except (AttributeError, KeyError):
        return results

    # Find all poll_query_results spans
    for span in spans:
        span_name = span.get("name", "")
        if span_name == "poll_query_results":
            attributes = span.get("attributes", {})
            span_outputs = attributes.get("mlflow.spanOutputs", "{}")

            # Parse the spanOutputs JSON string
            try:
                outputs = json.loads(span_outputs)
                result_data = {
                    "result": outputs.get("result", ""),
                    "query": outputs.get("query", ""),
                    "description": outputs.get("description", ""),
                }
                results.append(result_data)
            except json.JSONDecodeError:
                continue
    return results


def extract_user_request(prompt: str) -> str:
    """
    Extract the user query from between <user_request> tags.

    If the prompt contains <user_request> tags, extracts and returns only the
    content between them. Otherwise, returns the original prompt unchanged.

    Args:
        prompt: The full prompt, potentially containing <user_request> tags

    Returns:
        str: The extracted user request, or the original prompt if no tags found
    """
    pattern = r"<user_request>\s*(.*?)\s*</user_request>"
    match = re.search(pattern, prompt, re.DOTALL)

    if match:
        return match.group(1).strip()

    return prompt


def smart_title(s):
    # Split the string into words based on whitespace
    words = s.split()
    processed_words = []
    for w in words:
        # Check if the word is entirely uppercase
        if w.isupper():
            processed_words.append(w) # Leave it unchanged
        else:
            processed_words.append(w.title()) # Apply title case
    # Rejoin the words with a single space
    return ' '.join(processed_words)


# ============================================================================
# Skill Loading Utilities
# ============================================================================
def get_skills_directory() -> Path:
    """
    Get the path to the skills directory.

    Returns:
        Path: Path to the skills directory
    """
    # Navigate from app folder to project root, then to skills
    app_root = Path(__file__).resolve().parent
    project_root = app_root.parent.parent
    return project_root / "skills"


def parse_skill_frontmatter(content: str) -> dict:
    """
    Parse YAML frontmatter from a SKILL.md file.

    Expected format:
    ---
    name: skill-name
    description: Skill description text
    ---

    Args:
        content: The full content of the SKILL.md file

    Returns:
        dict: Parsed frontmatter with 'name' and 'description' keys
    """
    frontmatter = {}

    # Match YAML frontmatter between --- delimiters
    frontmatter_pattern = r"^---\s*\n(.*?)\n---\s*\n"
    match = re.match(frontmatter_pattern, content, re.DOTALL)

    if match:
        try:
            frontmatter = yaml.safe_load(match.group(1))
        except yaml.YAMLError:
            pass

    return frontmatter or {}


def discover_skills(skills_dir: Optional[Union[str, Path]] = None) -> list[dict]:
    """
    Discover all available skills from the skills directory.

    Scans the skills directory for subdirectories containing SKILL.md files,
    parses their frontmatter, and returns a list of skill metadata.

    Returns:
        list: List of dicts with keys:
              - 'name': Display name from frontmatter
              - 'description': Description from frontmatter
              - 'path': Full path to the skill directory
              - 'label': Formatted display label with emoji for UI
              - 'caption': Truncated description for UI captions
    """
    if not skills_dir:
        skills_dir = get_skills_directory()
    skills = {}

    if not skills_dir.exists():
        return skills

    for skill_folder in skills_dir.iterdir():
        if skill_folder.is_dir():
            skill_file = skill_folder / "SKILL.md"
            if skill_file.exists():
                try:
                    content = skill_file.read_text(encoding="utf-8")
                    frontmatter = parse_skill_frontmatter(content)

                    name = frontmatter.get("name", skill_folder.name)
                    description = frontmatter.get("description", "")
                    
                    # Generate label with emoji based on skill type
                    if "target" in name.lower():
                        label = f"🎯 {smart_title(name.replace('-', ' '))}"
                        order = 0
                    elif "hit" in name.lower():
                        label = f"⌬ {smart_title(name.replace('-', ' '))}"
                        order = 1
                    elif "adme" in name.lower():
                        label = f"🧪 {smart_title(name.replace('-', ' '))}"
                        order = 2
                    elif "safety" in name.lower():
                        label = f"☠️ {smart_title(name.replace('-', ' '))}"
                        order = 3
                    else:
                        label = f"📋 {smart_title(name.replace('-', ' '))}"
                        order = 4
                    # Generate truncated caption
                    caption = description.split(". ")[0] if description else ""
                    if len(caption) > 70:
                        caption = caption[:67] + "..."

                    skill_info = {
                        "description": description,
                        "path": str(skill_folder),
                        "label": label,
                        "caption": caption,
                        "order": order,
                    }
                    skills[name] = skill_info
                except Exception:
                    # Skip skills that can't be parsed
                    continue

    return skills


def load_skill_content(skill_name: str, skills_dir: Optional[Union[str, Path]] = None) -> Optional[dict]:
    """
    Load the full content of a skill including the main SKILL.md and any reference files.

    Args:
        skill_name: The skill directory name (e.g., 'target-identification')

    Returns:
        dict: Dictionary containing:
              - 'frontmatter': Parsed YAML frontmatter
              - 'content': Full markdown content (without frontmatter)
              - 'references': Dict mapping reference filenames to their content
              - 'full_prompt': Combined prompt ready for injection
        None: If skill not found or cannot be loaded
    """
    if not skills_dir:
        skills_dir = get_skills_directory()
    skill_path = skills_dir / skill_name
    skill_file = skill_path / "SKILL.md"

    if not skill_file.exists():
        return None

    try:
        full_content = skill_file.read_text(encoding="utf-8")

        # Parse frontmatter
        frontmatter = parse_skill_frontmatter(full_content)

        # Extract content without frontmatter
        content_pattern = r"^---\s*\n.*?\n---\s*\n(.*)$"
        match = re.match(content_pattern, full_content, re.DOTALL)
        content = match.group(1).strip() if match else full_content

        # Load reference files
        references = {}
        references_dir = skill_path / "references"
        if references_dir.exists():
            for ref_file in references_dir.iterdir():
                if ref_file.is_file() and ref_file.suffix == ".md":
                    try:
                        references[ref_file.name] = ref_file.read_text(encoding="utf-8")
                    except Exception:
                        continue

        # Build full prompt with references appended
        full_prompt = f"# Skill: {frontmatter.get('name', skill_name)}\n\n"
        full_prompt += content

        if references:
            full_prompt += "\n\n---\n\n## Reference Materials\n\n"
            for ref_name, ref_content in references.items():
                full_prompt += f"### {ref_name}\n\n{ref_content}\n\n"

        return {"frontmatter": frontmatter, "content": content, "references": references, "full_prompt": full_prompt}

    except Exception:
        return None


def build_prompt_with_skill(user_query: str, skill_name: Optional[str] = None, skills_dir: Optional[Union[str, Path]] = None) -> str:
    """
    Build a prompt that includes skill instructions if a skill is selected.

    Args:
        user_query: The user's original query
        skill_name: Optional skill name to load and prepend

    Returns:
        str: The combined prompt with skill instructions (if applicable)
    """
    if not skill_name:
        return user_query

    skill_data = load_skill_content(skill_name, skills_dir)
    if not skill_data:
        return user_query

    # Build prompt with skill context
    prompt = f"""You have been given a specialized skill to help with this task. Follow the workflow instructions carefully.

<skill_instructions>
{skill_data['full_prompt']}
</skill_instructions>

<user_request>
{user_query}
</user_request>

Execute the skill workflow to address the user's request. Follow each step methodically and provide the expected output format."""

    return prompt

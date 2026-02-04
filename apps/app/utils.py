import os
import requests
import json
import streamlit as st
from databricks.sdk import WorkspaceClient
import base64
from pathlib import Path
from typing import Optional
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
        raise Exception(
            f"Request failed with status {response.status_code}, {response.text}"
        )
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


def parse_tool_calls(text_content):
    """
    Parse function calls and thinking messages from text content.
    
    Expected format:
    <function_calls>
        <invoke name="function_name">
            <parameter name="param1">value1</parameter>
            <parameter name="param2">value2</parameter>
        </invoke>
    </function_calls>
    <thinking>
    Thinking message here
    </thinking>
    
    Returns:
        list: List of dicts with 'function_name', 'parameters', and 'thinking' keys
    """
    import re
    
    tool_calls = []
    
    # Find all function_calls blocks
    function_calls_pattern = r'<function_calls>\s*(.*?)\s*</function_calls>'
    function_calls_blocks = re.findall(function_calls_pattern, text_content, re.DOTALL)
    
    # Find all thinking blocks
    thinking_pattern = r'<thinking>\s*(.*?)\s*</thinking>'
    thinking_blocks = re.findall(thinking_pattern, text_content, re.DOTALL)
    
    # Parse each function_calls block
    for block in function_calls_blocks:
        # Find invoke tags
        invoke_pattern = r'<invoke name="([^"]+)">\s*(.*?)\s*</invoke>'
        invokes = re.findall(invoke_pattern, block, re.DOTALL)
        
        for function_name, params_block in invokes:
            # Parse parameters
            param_pattern = r'<parameter name="([^"]+)">([^<]*)</parameter>'
            params = re.findall(param_pattern, params_block)
            
            parameters = {param_name: param_value.strip() for param_name, param_value in params}
            
            tool_calls.append({
                'function_name': function_name,
                'parameters': parameters,
                'thinking': None  # Will be filled later
            })
    
    # Associate thinking blocks with tool calls (assume sequential order)
    for i, thinking in enumerate(thinking_blocks):
        if i < len(tool_calls):
            tool_calls[i]['thinking'] = thinking.strip()
    
    return tool_calls


def strip_tool_call_tags(text_content):
    """
    Strip <function_calls> and <thinking> tags and their contents from text.
    
    Args:
        text_content: Text containing function_calls and thinking tags
    
    Returns:
        str: Cleaned text with tags removed
    """
    import re
    
    # Remove function_calls blocks
    text_content = re.sub(r'<function_calls>\s*.*?\s*</function_calls>', '', text_content, flags=re.DOTALL)
    
    # Remove thinking blocks
    text_content = re.sub(r'<thinking>\s*.*?\s*</thinking>', '', text_content, flags=re.DOTALL)
    
    # Clean up extra whitespace and newlines
    text_content = re.sub(r'\n\s*\n\s*\n+', '\n\n', text_content)
    text_content = text_content.strip()
    
    return text_content


def parse_genie_results(response_json):
    """
    Parse response JSON for poll_query_results spans and extract result and query from spanOutputs.
    
    Args:
        response_json: Dictionary or JSON string containing the response data
    
    Returns:
        list: List of dictionaries containing extracted poll_query_results data.
              Each dict has: 'result', 'query', 'description', 'conversation_id'
              Returns empty list if no poll_query_results spans found.
    """
    # Convert string to dict if needed
    if isinstance(response_json, str):
        response_json = json.loads(response_json)
    
    results = []
    
    # Navigate to spans
    try:
        spans = response_json.get('databricks_output', {}).get('trace', {}).get('data', {}).get('spans', [])
    except (AttributeError, KeyError):
        return results
    
    # Find all poll_query_results spans
    for span in spans:
        span_name = span.get('name', '')
        if span_name == 'poll_query_results':
            attributes = span.get('attributes', {})
            span_outputs = attributes.get('mlflow.spanOutputs', '{}')
            
            # Parse the spanOutputs JSON string
            try:
                outputs = json.loads(span_outputs)
                result_data = {
                    'result': outputs.get('result', ''),
                    'query': outputs.get('query', ''),
                    'description': outputs.get('description', ''),
                }
                results.append(result_data)
            except json.JSONDecodeError:
                continue 
    return results


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
    frontmatter_pattern = r'^---\s*\n(.*?)\n---\s*\n'
    match = re.match(frontmatter_pattern, content, re.DOTALL)
    
    if match:
        try:
            frontmatter = yaml.safe_load(match.group(1))
        except yaml.YAMLError:
            pass
    
    return frontmatter or {}


def discover_skills() -> list[dict]:
    """
    Discover all available skills from the skills directory.
    
    Scans the skills directory for subdirectories containing SKILL.md files,
    parses their frontmatter, and returns a list of skill metadata.
    
    Returns:
        list: List of dicts with keys:
              - 'id': Directory name (e.g., 'target-identification')
              - 'name': Display name from frontmatter
              - 'description': Description from frontmatter
              - 'path': Full path to the skill directory
    """
    skills_dir = get_skills_directory()
    skills = []
    
    if not skills_dir.exists():
        return skills
    
    for skill_folder in skills_dir.iterdir():
        if skill_folder.is_dir():
            skill_file = skill_folder / "SKILL.md"
            if skill_file.exists():
                try:
                    content = skill_file.read_text(encoding='utf-8')
                    frontmatter = parse_skill_frontmatter(content)
                    
                    skill_info = {
                        'id': skill_folder.name,
                        'name': frontmatter.get('name', skill_folder.name),
                        'description': frontmatter.get('description', ''),
                        'path': str(skill_folder)
                    }
                    skills.append(skill_info)
                except Exception:
                    # Skip skills that can't be parsed
                    continue
    
    return skills


def load_skill_content(skill_id: str) -> Optional[dict]:
    """
    Load the full content of a skill including the main SKILL.md and any reference files.
    
    Args:
        skill_id: The skill directory name (e.g., 'target-identification')
    
    Returns:
        dict: Dictionary containing:
              - 'frontmatter': Parsed YAML frontmatter
              - 'content': Full markdown content (without frontmatter)
              - 'references': Dict mapping reference filenames to their content
              - 'full_prompt': Combined prompt ready for injection
        None: If skill not found or cannot be loaded
    """
    skills_dir = get_skills_directory()
    skill_path = skills_dir / skill_id
    skill_file = skill_path / "SKILL.md"
    
    if not skill_file.exists():
        return None
    
    try:
        full_content = skill_file.read_text(encoding='utf-8')
        
        # Parse frontmatter
        frontmatter = parse_skill_frontmatter(full_content)
        
        # Extract content without frontmatter
        content_pattern = r'^---\s*\n.*?\n---\s*\n(.*)$'
        match = re.match(content_pattern, full_content, re.DOTALL)
        content = match.group(1).strip() if match else full_content
        
        # Load reference files
        references = {}
        references_dir = skill_path / "references"
        if references_dir.exists():
            for ref_file in references_dir.iterdir():
                if ref_file.is_file() and ref_file.suffix == '.md':
                    try:
                        references[ref_file.name] = ref_file.read_text(encoding='utf-8')
                    except Exception:
                        continue
        
        # Build full prompt with references appended
        full_prompt = f"# Skill: {frontmatter.get('name', skill_id)}\n\n"
        full_prompt += content
        
        if references:
            full_prompt += "\n\n---\n\n## Reference Materials\n\n"
            for ref_name, ref_content in references.items():
                full_prompt += f"### {ref_name}\n\n{ref_content}\n\n"
        
        return {
            'frontmatter': frontmatter,
            'content': content,
            'references': references,
            'full_prompt': full_prompt
        }
    
    except Exception:
        return None


def get_skill_names_for_selector() -> tuple[list[str], list[str]]:
    """
    Get skill names and descriptions formatted for use in a Streamlit radio selector.
    
    Returns:
        tuple: (list of display names, list of caption descriptions)
    """
    skills = discover_skills()
    
    names = []
    captions = []
    
    for skill in skills:
        # Create display name with emoji based on skill type
        name = skill['name']
        if 'target' in name.lower():
            display_name = f"🎯 {name.replace('-', ' ').title()}"
        elif 'hit' in name.lower():
            display_name = f"⌬ {name.replace('-', ' ').title()}"
        elif 'lead' in name.lower():
            display_name = f"🧪 {name.replace('-', ' ').title()}"
        elif 'safety' in name.lower():
            display_name = f"☠️ {name.replace('-', ' ').title()}"
        else:
            display_name = f"📋 {name.replace('-', ' ').title()}"
        
        names.append(display_name)
        
        # Truncate description for caption if too long
        desc = skill['description']
        if len(desc) > 100:
            desc = desc[:97] + "..."
        captions.append(desc)
    
    return names, captions


def get_skill_id_from_display_name(display_name: Optional[str], available_skill_names: Optional[list[str]] = None) -> Optional[str]:
    """
    Map a display name back to the skill ID.
    
    Args:
        display_name: The formatted display name from the selector (e.g., "🎯 Druggable Targets")
        available_skill_names: Optional list of skill display names to check membership
    
    Returns:
        str: The skill ID, or None if not found or not a dynamic skill
    """
    if not display_name:
        return None
    
    # If available_skill_names provided, check if this is actually a dynamic skill
    if available_skill_names and display_name not in available_skill_names:
        # Not a dynamic skill (might be a hardcoded workflow), return the display_name as-is
        return display_name
    
    # Extract the core name by removing emoji prefix
    core_name = display_name.split(' ', 1)[-1].lower().replace(' ', '-') if ' ' in display_name else display_name.lower()
    
    # Look up against discovered skills
    skills = discover_skills()
    for skill in skills:
        if skill['name'].lower() == core_name or skill['id'].lower() == core_name:
            return skill['id']
    
    # If not found in skills, return the original display_name (for hardcoded workflows)
    return display_name


def build_prompt_with_skill(user_query: str, skill_id: Optional[str] = None) -> str:
    """
    Build a prompt that includes skill instructions if a skill is selected.
    
    Args:
        user_query: The user's original query
        skill_id: Optional skill ID to load and prepend
    
    Returns:
        str: The combined prompt with skill instructions (if applicable)
    """
    if not skill_id:
        return user_query
    
    skill_data = load_skill_content(skill_id)
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
    pattern = r'<user_request>\s*(.*?)\s*</user_request>'
    match = re.search(pattern, prompt, re.DOTALL)
    
    if match:
        return match.group(1).strip()
    
    return prompt

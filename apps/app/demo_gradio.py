"""
AiChemy Three-Pane Demo App (Gradio)
Design matching the mockup: clean white cards, green accents, timeline agent activity
"""

import gradio as gr
import time
from dataclasses import dataclass
from typing import List, Optional, Generator
from enum import Enum

# ============================================================================
# Data Models
# ============================================================================

class AgentStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    ERROR = "error"

@dataclass
class AgentStep:
    agent_name: str
    description: str
    status: AgentStatus
    result: Optional[str] = None

@dataclass 
class MoleculeHit:
    smiles: str
    ic50_nm: float
    clogp: float
    notes: str

# ============================================================================
# Mock Data
# ============================================================================

SAMPLE_MOLECULES = [
    MoleculeHit("CC(=O)Nc1ccc(O)cc1", 12.5, 3.1, "Initial hit"),
    MoleculeHit("CC(=O)Nc1ccc(OC)cc1", 210, 0.8, "Low potency"),
    MoleculeHit("CC(=O)Nc1ccc(O)c(F)c1", 8.2, 2.9, "Improved"),
]

# ============================================================================
# Helper Functions
# ============================================================================

def render_molecule_svg(smiles: str) -> str:
    """Render molecule SMILES to SVG"""
    try:
        from pikachu.general import read_smiles, svg_string_from_structure
        import re
        structure = read_smiles(smiles)
        svg = svg_string_from_structure(structure)
        svg = re.sub(r'width="\d+\.?\d*pt"', 'width="80"', svg)
        svg = re.sub(r'height="\d+\.?\d*pt"', 'height="60"', svg)
        return svg
    except Exception:
        return f"<span style='font-family: monospace; font-size: 11px; color: #666;'>{smiles[:15]}...</span>"

def format_agent_activity(steps: List[AgentStep]) -> str:
    """Format agent steps as timeline matching the mockup design"""
    if not steps:
        return """
        <div style="
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            height: 200px;
            color: #999;
        ">
            <div style="font-size: 32px; margin-bottom: 8px;">🤖</div>
            <div style="font-size: 13px;">Run a query to see agent orchestration</div>
        </div>
        """
    
    html = '<div style="padding: 8px 0;">'
    
    for i, step in enumerate(steps):
        is_completed = step.status == AgentStatus.COMPLETED
        is_running = step.status == AgentStatus.RUNNING
        
        # Status indicator
        if is_completed:
            indicator = '''<div style="
                width: 20px; height: 20px; border-radius: 50%;
                background: #4CAF50; color: white;
                display: flex; align-items: center; justify-content: center;
                font-size: 12px; flex-shrink: 0;
            ">✓</div>'''
        elif is_running:
            indicator = '''<div style="
                width: 20px; height: 20px; border-radius: 50%;
                border: 2px solid #4CAF50; background: white;
                display: flex; align-items: center; justify-content: center;
                flex-shrink: 0;
            "><div style="width: 8px; height: 8px; border-radius: 50%; background: #4CAF50;"></div></div>'''
        else:
            indicator = '''<div style="
                width: 20px; height: 20px; border-radius: 50%;
                border: 2px solid #e0e0e0; background: white;
                flex-shrink: 0;
            "></div>'''
        
        html += f'''
        <div style="display: flex; align-items: flex-start; gap: 12px; margin-bottom: 4px;">
            {indicator}
            <div style="flex: 1; padding-top: 2px;">
                <div style="font-size: 13px; color: #333; font-weight: 500;">{step.agent_name}</div>
                <div style="font-size: 12px; color: #888; margin-top: 2px;">{step.result or step.description}</div>
            </div>
        </div>
        '''
        
        # Connector line (except for last item)
        if i < len(steps) - 1:
            line_color = "#4CAF50" if is_completed else "#e0e0e0"
            html += f'''
            <div style="
                width: 2px; height: 24px;
                background: {line_color};
                margin-left: 9px;
                margin-bottom: 4px;
            "></div>
            '''
    
    html += '</div>'
    return html

def format_cost_display(steps: List[AgentStep]) -> str:
    """Format cost estimation matching mockup design"""
    if not steps:
        return ""
    
    completed = sum(1 for s in steps if s.status == AgentStatus.COMPLETED)
    cost = completed * 0.12 + 0.10  # Base cost + per agent
    
    return f'''
    <div style="
        text-align: center;
        padding: 16px;
        background: #fafafa;
        border-radius: 8px;
        margin-top: 16px;
    ">
        <div style="font-size: 12px; color: #888;">Estimated cost</div>
        <div style="font-size: 20px; font-weight: 600; color: #333; margin: 4px 0;">${cost:.2f}</div>
        <div style="font-size: 11px; color: #aaa;">{completed} agents</div>
    </div>
    '''

def format_molecules_table(molecules: List[MoleculeHit]) -> str:
    """Format molecules as clean table matching mockup"""
    if not molecules:
        return ""
    
    html = '''
    <div style="
        background: white;
        border-radius: 8px;
        overflow: hidden;
        border: 1px solid #eee;
    ">
        <table style="width: 100%; border-collapse: collapse; font-size: 13px;">
            <thead>
                <tr style="background: #fafafa;">
                    <th style="padding: 12px; text-align: left; font-weight: 500; color: #666; border-bottom: 1px solid #eee;">Structure</th>
                    <th style="padding: 12px; text-align: left; font-weight: 500; color: #666; border-bottom: 1px solid #eee;">IC₅₀ (nM)</th>
                    <th style="padding: 12px; text-align: left; font-weight: 500; color: #666; border-bottom: 1px solid #eee;">ClogP</th>
                    <th style="padding: 12px; text-align: left; font-weight: 500; color: #666; border-bottom: 1px solid #eee;">Notes</th>
                </tr>
            </thead>
            <tbody>
    '''
    
    for mol in molecules:
        svg = render_molecule_svg(mol.smiles)
        html += f'''
        <tr>
            <td style="padding: 8px 12px; border-bottom: 1px solid #f5f5f5; vertical-align: middle;">{svg}</td>
            <td style="padding: 8px 12px; border-bottom: 1px solid #f5f5f5; font-family: monospace; color: #333;">{mol.ic50_nm}</td>
            <td style="padding: 8px 12px; border-bottom: 1px solid #f5f5f5; font-family: monospace; color: #333;">{mol.clogp}</td>
            <td style="padding: 8px 12px; border-bottom: 1px solid #f5f5f5; color: #666;">{mol.notes}</td>
        </tr>
        '''
    
    html += '</tbody></table></div>'
    return html

# ============================================================================
# Follow-up Question Generation
# ============================================================================

FOLLOW_UP_TEMPLATES = {
    "egfr": [
        "Show me the binding mode of the top hit",
        "Compare selectivity vs other kinases",
        "What's the ADMET profile of these compounds?",
    ],
    "kras": [
        "Find G12C-specific binders",
        "Show covalent warhead options",
        "Compare to existing KRAS inhibitors",
    ],
    "default": [
        "Run toxicity prediction",
        "Find similar approved drugs",
        "Show structure-activity relationship",
    ]
}

def get_follow_up_suggestions(query: str) -> List[str]:
    """Generate contextual follow-up questions based on the query"""
    query_lower = query.lower()
    
    if "egfr" in query_lower:
        return FOLLOW_UP_TEMPLATES["egfr"]
    elif "kras" in query_lower:
        return FOLLOW_UP_TEMPLATES["kras"]
    else:
        return FOLLOW_UP_TEMPLATES["default"]

def format_follow_up_buttons(suggestions: List[str]) -> str:
    """Format follow-up suggestions as clickable buttons"""
    if not suggestions:
        return ""
    
    buttons_html = '<div style="display: flex; gap: 8px; flex-wrap: wrap; padding: 12px 0;">'
    for suggestion in suggestions:
        buttons_html += f'''
        <button onclick="document.querySelector('textarea').value='{suggestion}'; document.querySelector('textarea').dispatchEvent(new Event('input', {{ bubbles: true }}));" 
            style="
                background: #f0f7f0; 
                border: 1px solid #c8e6c9; 
                border-radius: 16px;
                padding: 8px 14px; 
                font-size: 12px; 
                color: #2e7d32; 
                cursor: pointer;
                transition: all 0.2s;
            "
            onmouseover="this.style.background='#e8f5e9'; this.style.borderColor='#4CAF50';"
            onmouseout="this.style.background='#f0f7f0'; this.style.borderColor='#c8e6c9';"
        >{suggestion}</button>
        '''
    buttons_html += '</div>'
    return buttons_html

# ============================================================================
# Agent Simulation
# ============================================================================

def simulate_agent_orchestration(query: str, history: list) -> Generator:
    """Simulate multi-agent orchestration with streaming"""
    
    agents = [
        ("Plan created", "Planning execution strategy", "Execution plan ready"),
        ("PubChem agent", "Searching compounds...", "2,143 candidates found"),
        ("OpenTargets agent", "Retrieving evidence...", "target evidence retrieved"),
        ("VS agent", "Clustering hits...", "clustered 50 diverse hits"),
    ]
    
    steps = []
    
    def msg(role, content):
        return {"role": role, "content": content}
    
    # Initial - no suggestions yet
    new_history = history + [msg("user", query), msg("assistant", "🔄 Starting analysis...")]
    yield new_history, format_agent_activity(steps), format_cost_display(steps), "", ""
    
    for agent_name, description, result in agents:
        steps.append(AgentStep(agent_name, description, AgentStatus.RUNNING))
        new_history = history + [msg("user", query), msg("assistant", f"🔄 {agent_name}: {description}")]
        yield new_history, format_agent_activity(steps), format_cost_display(steps), "", ""
        
        time.sleep(0.7)
        
        steps[-1].status = AgentStatus.COMPLETED
        steps[-1].result = result
        yield history + [msg("user", query), msg("assistant", f"✅ {agent_name}: {result}")], format_agent_activity(steps), format_cost_display(steps), "", ""
    
    # Generate results HTML (rendered separately, not in chat)
    molecules_html = format_molecules_table(SAMPLE_MOLECULES)
    follow_ups = get_follow_up_suggestions(query)
    follow_up_html = format_follow_up_buttons(follow_ups)
    
    # Combine results and follow-ups for the results panel
    results_panel = f"""
    <div style="margin-bottom: 12px;">
        <div style="font-size: 13px; font-weight: 500; color: #333; margin-bottom: 8px;">
            📊 Found {len(SAMPLE_MOLECULES)} candidates
        </div>
        {molecules_html}
    </div>
    {follow_up_html}
    """
    
    # Chat just shows text summary
    final_response = f"""✅ **Analysis complete!**

Found **{len(SAMPLE_MOLECULES)} candidates** matching: *{query}*

Results displayed below ↓"""

    final_history = history + [msg("user", query), msg("assistant", final_response)]
    
    yield final_history, format_agent_activity(steps), format_cost_display(steps), results_panel, ""

# ============================================================================
# Custom CSS
# ============================================================================

custom_css = """
/* Overall container */
.gradio-container {
    background: #f5f5f5 !important;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif !important;
}

/* Remove default padding */
.main {
    padding: 0 !important;
}

/* Card panels */
.panel-card {
    background: white !important;
    border-radius: 16px !important;
    box-shadow: 0 1px 3px rgba(0,0,0,0.08) !important;
    padding: 20px !important;
    margin: 8px !important;
}

/* Project button styling */
.project-btn {
    border-radius: 8px !important;
    margin-bottom: 4px !important;
    font-weight: 500 !important;
}

.project-btn.selected {
    background: #e8f5e9 !important;
    border-left: 3px solid #4CAF50 !important;
}

/* Green accent buttons */
.gr-button-primary {
    background: #4CAF50 !important;
    border: none !important;
}

/* Clean input styling */
.gr-textbox {
    border-radius: 24px !important;
    border: 1px solid #e0e0e0 !important;
}

/* Action button pills */
.action-pill {
    background: #f5f5f5 !important;
    border: none !important;
    border-radius: 20px !important;
    padding: 8px 16px !important;
    font-size: 13px !important;
    color: #666 !important;
}

.action-pill:hover {
    background: #e8f5e9 !important;
    color: #4CAF50 !important;
}

/* Hide footer */
footer { display: none !important; }
"""

# ============================================================================
# Main App
# ============================================================================

def create_app():
    with gr.Blocks(title="AiChemy", css=custom_css) as app:
        
        # Main container with 3 columns
        with gr.Row(equal_height=False):
            
            # ================================================================
            # LEFT PANE
            # ================================================================
            with gr.Column(scale=1, min_width=180):
                gr.HTML("""
                <div style="
                    background: white;
                    border-radius: 16px;
                    padding: 20px;
                    box-shadow: 0 1px 3px rgba(0,0,0,0.08);
                    min-height: 500px;
                ">
                    <!-- Logo -->
                    <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 24px;">
                        <div style="
                            width: 32px; height: 32px;
                            background: linear-gradient(135deg, #4CAF50, #2E7D32);
                            border-radius: 8px;
                            display: flex; align-items: center; justify-content: center;
                            color: white; font-size: 16px;
                        ">⚗️</div>
                        <span style="font-size: 18px; font-weight: 600; color: #333;">AiChemy</span>
                    </div>
                    
                    <!-- Projects -->
                    <div style="
                        background: #e8f5e9;
                        border-radius: 8px;
                        padding: 10px 12px;
                        margin-bottom: 8px;
                        display: flex;
                        align-items: center;
                        gap: 8px;
                        border-left: 3px solid #4CAF50;
                        cursor: pointer;
                    ">
                        <span>🧬</span>
                        <span style="font-size: 14px; font-weight: 500; color: #333;">EGFR NSCLC</span>
                    </div>
                    
                    <div style="
                        padding: 10px 12px;
                        margin-bottom: 16px;
                        display: flex;
                        align-items: center;
                        gap: 8px;
                        cursor: pointer;
                        border-radius: 8px;
                    ">
                        <span>🧬</span>
                        <span style="font-size: 14px; color: #666;">KRAS pipeline</span>
                    </div>
                    
                    <!-- Workflows section -->
                    <div style="font-size: 12px; font-weight: 600; color: #888; margin-bottom: 12px; text-transform: uppercase; letter-spacing: 0.5px;">
                        Workflows
                    </div>
                    
                    <div style="display: flex; flex-direction: column; gap: 8px;">
                        <div style="display: flex; align-items: center; gap: 8px; padding: 6px 0; cursor: pointer;">
                            <span style="font-size: 16px;">🎯</span>
                            <span style="font-size: 13px; color: #666;">Target validation</span>
                        </div>
                        <div style="display: flex; align-items: center; gap: 8px; padding: 6px 0; cursor: pointer;">
                            <span style="font-size: 16px;">⚗️</span>
                            <span style="font-size: 13px; color: #666;">SAR optimization</span>
                        </div>
                        <div style="display: flex; align-items: center; gap: 8px; padding: 6px 0; cursor: pointer;">
                            <span style="font-size: 16px;">☠️</span>
                            <span style="font-size: 13px; color: #666;">Tox profile</span>
                        </div>
                    </div>
                </div>
                """)
            
            # ================================================================
            # CENTER PANE
            # ================================================================
            with gr.Column(scale=3):
                gr.HTML("""
                <div style="
                    background: white;
                    border-radius: 16px 16px 0 0;
                    padding: 20px 20px 0 20px;
                    box-shadow: 0 1px 3px rgba(0,0,0,0.08);
                ">
                    <h2 style="margin: 0 0 4px 0; font-size: 20px; font-weight: 600; color: #333;">
                        EGFR NSCLC project
                    </h2>
                    <p style="margin: 0; font-size: 13px; color: #888;">Sort and manage results</p>
                </div>
                """)
                
                with gr.Group():
                    chatbot = gr.Chatbot(
                        value=[],
                        height=400,
                        show_label=False,
                        container=False,
                    )
                    
                    with gr.Row():
                        query_input = gr.Textbox(
                            placeholder="Ask AiChemy anything about your R&D project...",
                            show_label=False,
                            scale=5,
                            container=False,
                        )
                        submit_btn = gr.Button("🔍", variant="primary", scale=1, min_width=50)
                
                # Results panel (molecules table + follow-up suggestions)
                results_panel = gr.HTML(value="", label="")
                
                # Hidden placeholder
                extra_content = gr.HTML(value="", label="", visible=False)
            
            # ================================================================
            # RIGHT PANE - Agent Activity
            # ================================================================
            with gr.Column(scale=1, min_width=220):
                gr.HTML("""
                <div style="
                    background: white;
                    border-radius: 16px;
                    padding: 20px;
                    box-shadow: 0 1px 3px rgba(0,0,0,0.08);
                ">
                    <h3 style="margin: 0 0 16px 0; font-size: 15px; font-weight: 600; color: #333;">
                        Agent Activity
                    </h3>
                """)
                
                agent_activity = gr.HTML(
                    value=format_agent_activity([]),
                )
                
                cost_display = gr.HTML(value="")
                
                gr.HTML("""
                </div>
                """)
                
                # Approve/Cancel buttons
                with gr.Row():
                    approve_btn = gr.Button("Approve", variant="primary", size="sm")
                    cancel_btn = gr.Button("Cancel", size="sm")
        
        # ================================================================
        # Event Handlers
        # ================================================================
        
        def run_query(query, history):
            if not query or not query.strip():
                yield history or [], format_agent_activity([]), "", "", ""
                return
            history = history or []
            for result in simulate_agent_orchestration(query, history):
                yield result
        
        submit_btn.click(
            fn=run_query,
            inputs=[query_input, chatbot],
            outputs=[chatbot, agent_activity, cost_display, results_panel, extra_content],
        ).then(fn=lambda: "", outputs=[query_input])
        
        query_input.submit(
            fn=run_query,
            inputs=[query_input, chatbot],
            outputs=[chatbot, agent_activity, cost_display, results_panel, extra_content],
        ).then(fn=lambda: "", outputs=[query_input])
    
    return app

# ============================================================================
# Launch
# ============================================================================

if __name__ == "__main__":
    app = create_app()
    app.launch(server_name="0.0.0.0", server_port=7860, share=False)

"""
AiChemy Demo App (Streamlit)
With approval flow + auto-approve checkbox
"""

import streamlit as st
import time
from io import BytesIO

# ============================================================================
# Page Config
# ============================================================================

st.set_page_config(
    page_title="AiChemy",
    page_icon="⚗️",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ============================================================================
# Custom CSS
# ============================================================================

st.markdown("""
<style>
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    header {visibility: hidden;}
    .stApp { background-color: #f5f5f5; }
    [data-testid="stSidebar"] { background-color: white; }
    .block-container { 
        padding-top: 2rem !important; 
        padding-left: 1rem; 
        padding-right: 1rem;
        max-width: 100% !important;
    }
    
    .stButton > button {
        border-radius: 20px;
    }
    .stButton > button[kind="primary"] {
        background-color: #4a9d7c !important;
        border: none !important;
    }
    .stButton > button[kind="primary"]:hover {
        background-color: #3d8a6a !important;
    }
</style>
""", unsafe_allow_html=True)

# ============================================================================
# Data
# ============================================================================

MOLECULES = [
    {"smiles": "CC(=O)Nc1ccc(O)cc1", "ic50": 12.5, "clogp": 3.1, "notes": "Initial hit"},
    {"smiles": "CC(=O)Nc1ccc(OC)cc1", "ic50": 210, "clogp": 0.8, "notes": "Low potency"},
    {"smiles": "CC(=O)Nc1ccc(O)c(F)c1", "ic50": 8.2, "clogp": 2.9, "notes": "Improved"},
]

AGENT_PLAN = [
    {"name": "PubChem agent", "description": "Search for compounds matching query"},
    {"name": "OpenTargets agent", "description": "Retrieve target evidence and associations"},
    {"name": "VS agent", "description": "Cluster and rank diverse hits"},
]

FOLLOW_UPS = {
    "egfr": ["Show binding mode of top hit", "Compare selectivity vs other kinases", "What's the ADMET profile?"],
    "kras": ["Find G12C-specific binders", "Show covalent warhead options", "Compare to existing KRAS inhibitors"],
    "default": ["Run toxicity prediction", "Find similar approved drugs", "Show structure-activity relationship"]
}

def get_suggestions(query: str) -> list:
    q = query.lower()
    if "egfr" in q: return FOLLOW_UPS["egfr"]
    if "kras" in q: return FOLLOW_UPS["kras"]
    return FOLLOW_UPS["default"]

# ============================================================================
# Molecule Image
# ============================================================================

def get_molecule_image(smiles: str):
    try:
        from rdkit import Chem
        from rdkit.Chem import Draw
        mol = Chem.MolFromSmiles(smiles)
        if mol:
            img = Draw.MolToImage(mol, size=(150, 100))
            buf = BytesIO()
            img.save(buf, format='PNG')
            return buf.getvalue()
    except ImportError:
        pass
    
    try:
        from pikachu.general import read_smiles, svg_string_from_structure
        import re
        structure = read_smiles(smiles)
        svg = svg_string_from_structure(structure)
        svg = re.sub(r'width="\d+\.?\d*pt"', 'width="120"', svg)
        svg = re.sub(r'height="\d+\.?\d*pt"', 'height="80"', svg)
        return ("svg", svg)
    except:
        return None

# ============================================================================
# Session State
# ============================================================================

if "messages" not in st.session_state:
    st.session_state.messages = []
if "agent_steps" not in st.session_state:
    st.session_state.agent_steps = []
if "state" not in st.session_state:
    st.session_state.state = "idle"  # idle, awaiting_approval, executing, complete
if "current_query" not in st.session_state:
    st.session_state.current_query = ""
if "trigger_query" not in st.session_state:
    st.session_state.trigger_query = None
if "auto_approve" not in st.session_state:
    st.session_state.auto_approve = False

# ============================================================================
# Callbacks
# ============================================================================

def on_approve():
    st.session_state.state = "executing"

def on_cancel():
    st.session_state.state = "idle"
    st.session_state.agent_steps = []
    st.session_state.current_query = ""

# ============================================================================
# Sidebar
# ============================================================================

with st.sidebar:
    st.markdown("""
    <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 20px;">
        <div style="width: 32px; height: 32px; background: #4a9d7c; border-radius: 8px; 
                    display: flex; align-items: center; justify-content: center; color: white;">⚗️</div>
        <span style="font-size: 18px; font-weight: 600;">AiChemy</span>
    </div>
    """, unsafe_allow_html=True)
    
    st.markdown("""
    <div style="background: #e6f4ef; border-radius: 8px; padding: 10px 12px; margin-bottom: 8px;
                border-left: 3px solid #4a9d7c;">
        🧬 <b>EGFR NSCLC</b>
    </div>
    <div style="padding: 10px 12px; color: #666;">
        🧬 KRAS pipeline
    </div>
    """, unsafe_allow_html=True)
    
    st.divider()
    st.caption("WORKFLOWS")
    st.markdown("🎯 Target validation")
    st.markdown("⚗️ SAR optimization")
    st.markdown("☠️ Tox profile")

# ============================================================================
# Main Layout
# ============================================================================

col_chat, col_agents = st.columns([3, 1])

# ============================================================================
# Chat Column
# ============================================================================

with col_chat:
    st.markdown("### EGFR NSCLC project")
    st.caption("Sort and manage results")
    
    chat_container = st.container(height=400)
    
    with chat_container:
        for msg in st.session_state.messages:
            with st.chat_message(msg["role"]):
                st.write(msg["content"])
                
                if msg.get("has_results"):
                    st.success(f"Found {len(MOLECULES)} candidates")
                    
                    for mol in MOLECULES:
                        c1, c2, c3, c4 = st.columns([2, 1, 1, 2])
                        with c1:
                            img = get_molecule_image(mol["smiles"])
                            if img:
                                if isinstance(img, tuple) and img[0] == "svg":
                                    st.markdown(img[1], unsafe_allow_html=True)
                                elif isinstance(img, bytes):
                                    st.image(img, width=120)
                            else:
                                st.code(mol["smiles"][:20])
                        with c2:
                            st.metric("IC₅₀", f"{mol['ic50']} nM")
                        with c3:
                            st.metric("ClogP", mol["clogp"])
                        with c4:
                            st.caption(mol["notes"])
                    
                    suggestions = get_suggestions(msg.get("original_query", ""))
                    st.write("")
                    st.caption("**Suggested follow-ups:**")
                    cols = st.columns(len(suggestions))
                    for idx, (col, suggestion) in enumerate(zip(cols, suggestions)):
                        with col:
                            if st.button(suggestion, key=f"s_{msg.get('id', 0)}_{idx}", use_container_width=True):
                                st.session_state.trigger_query = suggestion
        
        # Show awaiting approval state
        if st.session_state.state == "awaiting_approval":
            with st.chat_message("assistant"):
                st.info(f"📋 **Plan created for:** {st.session_state.current_query}")
                st.write("The following agents will be executed:")
                for agent in AGENT_PLAN:
                    st.write(f"• **{agent['name']}** - {agent['description']}")
                st.write("Please approve or cancel in the right panel →")
        
        # Execute agents
        elif st.session_state.state == "executing":
            with st.chat_message("assistant"):
                progress_placeholder = st.empty()
                
                agents = [
                    ("Plan created", "Execution plan ready"),
                    ("PubChem agent", "2,143 candidates found"),
                    ("OpenTargets agent", "target evidence retrieved"),
                    ("VS agent", "clustered 50 diverse hits"),
                ]
                
                steps = []
                for name, result in agents:
                    steps.append({"name": name, "status": "running", "result": None})
                    st.session_state.agent_steps = steps.copy()
                    progress_placeholder.write(f"🔄 Running **{name}**...")
                    time.sleep(0.6)
                    
                    steps[-1]["status"] = "completed"
                    steps[-1]["result"] = result
                    st.session_state.agent_steps = steps.copy()
                    progress_placeholder.write(f"✅ **{name}**: {result}")
                    time.sleep(0.2)
                
                # Save to history
                msg_id = len(st.session_state.messages)
                original_query = st.session_state.current_query
                st.session_state.messages.append({
                    "role": "user", 
                    "content": original_query,
                    "id": msg_id
                })
                st.session_state.messages.append({
                    "role": "assistant",
                    "content": "Analysis complete!",
                    "has_results": True,
                    "original_query": original_query,
                    "id": msg_id + 1
                })
                
                st.session_state.state = "complete"
                st.session_state.current_query = ""
                st.rerun()
    
    # Handle triggered query from suggestion
    if st.session_state.trigger_query:
        prompt = st.session_state.trigger_query
        st.session_state.trigger_query = None
        
        st.session_state.current_query = prompt
        st.session_state.agent_steps = [
            {"name": "Plan created", "status": "completed", "result": "Execution plan ready"}
        ] + [
            {"name": agent["name"], "status": "pending", "result": agent["description"]}
            for agent in AGENT_PLAN
        ]
        
        if st.session_state.auto_approve:
            st.session_state.state = "executing"
        else:
            st.session_state.state = "awaiting_approval"
        st.rerun()
    
    # Chat input
    disabled = st.session_state.state not in ["idle", "complete"]
    if prompt := st.chat_input("Ask AiChemy anything about your R&D project...", disabled=disabled):
        st.session_state.current_query = prompt
        st.session_state.agent_steps = [
            {"name": "Plan created", "status": "completed", "result": "Execution plan ready"}
        ] + [
            {"name": agent["name"], "status": "pending", "result": agent["description"]}
            for agent in AGENT_PLAN
        ]
        
        if st.session_state.auto_approve:
            st.session_state.state = "executing"
        else:
            st.session_state.state = "awaiting_approval"
        st.rerun()

# ============================================================================
# Agent Activity Column
# ============================================================================

with col_agents:
    st.markdown("#### Agent Activity")
    
    # Auto-approve checkbox
    st.session_state.auto_approve = st.checkbox("Auto-approve", value=st.session_state.auto_approve)
    
    st.divider()
    
    if st.session_state.agent_steps:
        for step in st.session_state.agent_steps:
            status = step["status"]
            if status == "completed":
                st.markdown(f"✅ **{step['name']}**")
            elif status == "running":
                st.markdown(f"🔄 **{step['name']}**")
            else:
                st.markdown(f"⏳ **{step['name']}**")
            
            if step.get("result"):
                st.caption(step["result"])
        
        st.divider()
        
        # Approve/Cancel - only when awaiting
        if st.session_state.state == "awaiting_approval":
            c1, c2 = st.columns(2)
            with c1:
                st.button("✓ Approve", type="primary", use_container_width=True, on_click=on_approve)
            with c2:
                st.button("✗ Cancel", use_container_width=True, on_click=on_cancel)
        elif st.session_state.state == "complete":
            completed = sum(1 for s in st.session_state.agent_steps if s["status"] == "completed")
            st.success(f"{completed} agents completed")
    else:
        st.info("🤖 Enter a query to create a plan")

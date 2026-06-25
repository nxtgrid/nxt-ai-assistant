"""
Anansi App - Main Application

Multi-page admin interface for viewing chat history and managing bot settings.
"""

import os
from datetime import timedelta
from pathlib import Path

import streamlit as st

# Load environment variables early
from dotenv import load_dotenv

load_dotenv()

# Page configuration with custom favicon - must be first Streamlit command
st.set_page_config(
    page_title="Anansi",
    page_icon=str(Path(__file__).parent / "assets" / "favicon-32.png"),
    layout="wide",
    initial_sidebar_state="expanded",
    menu_items={
        "Get Help": None,
        "Report a bug": None,
        "About": "# Anansi\nAI Chat Orchestrator",
    },
)

# Custom CSS - dark sidebar, compact layout
st.markdown(
    """
    <style>
    /* ============================================================
       STREAMLIT 1.50 - FULL OVERRIDE
       Targets every known source of spacing/chrome by data-testid
       ============================================================ */

    /* 1. Style Streamlit header as dark bar (matches .stApp::before)
       instead of hiding — prevents white flash on page transitions */
    #MainMenu { display: none !important; }
    footer { display: none !important; }
    [data-testid="stHeader"],
    header {
        background-color: #141824 !important;
        height: 3.5rem !important;
        min-height: 0 !important;
        max-height: 3.5rem !important;
        padding: 0 !important;
        margin: 0 !important;
        overflow: hidden !important;
        color: transparent !important;
    }
    [data-testid="stDecoration"] { display: none !important; }

    /* 2. KILL THE 6rem GAP — stMainBlockContainer is the real culprit.
       In Streamlit 1.50 wide layout it defaults to padding-top:6rem.
       Target by data-testid, class name, AND generic selector. */
    [data-testid="stMainBlockContainer"] { padding-top: 0 !important; }
    .stMainBlockContainer { padding-top: 0 !important; }
    div.block-container { padding-top: 0 !important; }

    /* Also zero out every ancestor that might add spacing */
    [data-testid="stAppViewContainer"] { padding-top: 0 !important; margin-top: 0 !important; }
    [data-testid="stMain"] { padding-top: 0 !important; margin-top: 0 !important; }
    .stApp { margin-top: 0 !important; padding-top: 0 !important; }
    section.main > div { padding-top: 0 !important; }

    /* Force app view to top of viewport — no residual header gap */
    [data-testid="stAppViewContainer"] > section { top: 0 !important; }

    /* 3. Prevent horizontal scrollbar from the full-width bar trick */
    .stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"] {
        overflow-x: hidden !important;
    }

    /* 3b. Slightly blue-tinted background for all pages */
    .stApp,
    [data-testid="stAppViewContainer"],
    [data-testid="stMain"] {
        background-color: #f0f2f6 !important;
    }

    /* === SIDEBAR === */
    section[data-testid="stSidebar"] {
        width: 15rem !important;
        min-width: 15rem !important;
        max-width: 15rem !important;
        display: block !important;
        visibility: visible !important;
        z-index: 100;
        top: 0 !important;
    }

    /* Logo flush to the top — zero padding */
    section[data-testid="stSidebar"] [data-testid="stSidebarContent"] {
        padding-top: 0 !important;
    }

    /* Also kill the sidebar's own top-space offset (sidebarTopSpace = 6rem) */
    section[data-testid="stSidebar"] > div:first-child {
        padding-top: 0 !important;
        margin-top: 0 !important;
    }

    /* Kill any inner sidebar wrappers that add top spacing */
    section[data-testid="stSidebar"] [data-testid="stSidebarUserContent"] {
        padding-top: 0 !important;
    }

    /* Hide sidebar collapse/expand toggles AND their 60px wrapper */
    [data-testid="collapsedControl"] { display: none !important; }
    button[kind="header"] { display: none !important; }
    [data-testid="stSidebarCollapseButton"] { display: none !important; }
    [data-testid="stSidebarHeader"] {
        display: none !important;
        height: 0 !important;
        min-height: 0 !important;
        padding: 0 !important;
        margin: 0 !important;
    }

    section[data-testid="stSidebar"] > div {
        display: block !important;
        visibility: visible !important;
    }

    /* Remove right padding from sidebar at all nesting levels */
    section[data-testid="stSidebar"] > div,
    section[data-testid="stSidebar"] > div > div,
    section[data-testid="stSidebar"] > div > div > div,
    section[data-testid="stSidebar"] > div > div > div > div,
    section[data-testid="stSidebar"] [data-testid="stSidebarContent"],
    section[data-testid="stSidebar"] .block-container,
    section[data-testid="stSidebar"] .stElementContainer {
        padding-right: 0 !important;
        margin-right: 0 !important;
    }

    /* Sidebar dividers */
    section[data-testid="stSidebar"] hr {
        margin: 0.25rem -1rem !important;
        width: calc(100% + 2rem) !important;
        border-color: rgba(255, 255, 255, 0.12) !important;
    }

    /* Reduce element gaps in sidebar */
    section[data-testid="stSidebar"] .stElementContainer {
        margin-bottom: 0 !important;
    }

    /* === FULL-WIDTH DARK TOP BAR (background strip like Platform) ===
       A fixed pseudo-element behind sidebar + content, so the dark band
       spans edge-to-edge across the viewport top. */
    .stApp::before {
        content: '';
        position: fixed;
        top: 0;
        left: 0;
        right: 0;
        height: 3.5rem;
        background-color: #141824;
        z-index: 99;
        pointer-events: none;
    }

    /* Push main content below the dark strip */
    [data-testid="stMainBlockContainer"] { padding-top: 3.75rem !important; }
    .stMainBlockContainer { padding-top: 3.75rem !important; }
    div.block-container { padding-top: 3.75rem !important; }

    /* === MAIN CONTENT — reduce left/right padding to ~1/3 of Streamlit default === */
    [data-testid="stMainBlockContainer"],
    .stMainBlockContainer,
    div.block-container {
        padding-left: 1.5rem !important;
        padding-right: 1.5rem !important;
    }

    h1, h2 {
        margin-top: 0.5rem !important;
        padding-top: 0 !important;
    }

    /* === SIDEBAR USER ROW (name + logout below logo) === */
    .sidebar-user-name {
        color: rgba(255, 255, 255, 0.7) !important;
        font-size: 0.8rem !important;
        font-weight: 500 !important;
        line-height: 1 !important;
    }

    /* Vertically center the user row columns */
    section[data-testid="stSidebar"] [data-testid="stHorizontalBlock"] {
        align-items: center !important;
    }

    /* Compact logout button in sidebar */
    section[data-testid="stSidebar"] .st-key-top_logout button {
        background-color: transparent !important;
        background: transparent !important;
        border: none !important;
        box-shadow: none !important;
        color: rgba(255, 255, 255, 0.5) !important;
        padding: 0.1rem 0.3rem !important;
        font-size: 0.85rem !important;
        min-height: 0 !important;
        line-height: 1 !important;
        margin: 0 !important;
        outline: none !important;
    }

    section[data-testid="stSidebar"] .st-key-top_logout button:hover {
        color: #ffffff !important;
        background-color: rgba(255, 255, 255, 0.1) !important;
    }

    /* === NAV BUTTONS — left aligned, compact === */
    section[data-testid="stSidebar"] .stButton > button {
        width: 100%;
        text-align: left !important;
        justify-content: flex-start !important;
        display: flex !important;
        align-items: center !important;
        background-color: transparent;
        border: none;
        padding: 0.3rem 0.6rem;
        margin: 0;
        border-radius: 4px;
        font-size: 0.8rem;
        font-weight: 500;
        color: rgba(255, 255, 255, 0.8) !important;
        transition: all 0.15s ease;
        white-space: nowrap !important;
        min-height: unset !important;
        line-height: 1.3;
    }
    /* Reduce spacing between button containers */
    section[data-testid="stSidebar"] .stButton {
        margin-bottom: -0.4rem;
    }
    /* Compact sidebar section headings (Groups, Direct Messages) */
    section[data-testid="stSidebar"] h2 {
        font-size: 1rem;
        margin-top: 0.3rem;
        margin-bottom: 0.2rem;
        padding: 0;
    }
    section[data-testid="stSidebar"] h3 {
        font-size: 0.85rem;
        margin-top: 0.2rem;
        margin-bottom: 0.1rem;
    }

    /* Force ALL inner elements of nav buttons to left-align
       (Streamlit centers text inside use_container_width buttons) */
    section[data-testid="stSidebar"] .stButton > button * {
        text-align: left !important;
        justify-content: flex-start !important;
    }
    section[data-testid="stSidebar"] .stButton > button p,
    section[data-testid="stSidebar"] .stButton > button span,
    section[data-testid="stSidebar"] .stButton > button div {
        text-align: left !important;
        width: 100% !important;
    }

    section[data-testid="stSidebar"] .stButton > button:hover {
        background-color: rgba(255, 255, 255, 0.1);
        color: #ffffff !important;
        border: none;
    }

    section[data-testid="stSidebar"] .stButton > button:focus {
        box-shadow: none;
        border: none;
    }

    /* === BOT STATUS INDICATOR — glow ring around anansi logo === */
    .bot-status-logo {
        border-radius: 50%;
        transition: box-shadow 0.6s ease, opacity 0.3s ease;
        animation: fade-in 0.3s ease forwards;
    }
    @keyframes fade-in {
        from { opacity: 0.6; }
        to { opacity: 1; }
    }
    .bot-status-logo.bot-live {
        box-shadow: 0 0 8px 2px rgba(34, 197, 94, 0.6);
    }
    .bot-status-logo.bot-deploying {
        animation: pulse-amber 1.5s ease-in-out infinite;
    }
    .bot-status-logo.bot-down {
        box-shadow: 0 0 10px 3px rgba(239, 68, 68, 0.7);
    }
    @keyframes pulse-amber {
        0%, 100% { box-shadow: 0 0 6px 1px rgba(245, 158, 11, 0.3); }
        50% { box-shadow: 0 0 14px 4px rgba(245, 158, 11, 0.8); }
    }
    /* Prevent Streamlit fragment re-render flash: keep parent visible during DOM swap */
    [data-testid="stVerticalBlock"]:has(.bot-status-logo) {
        min-height: 40px;
    }

    /* Selected nav button — blue accent like Platform */
    section[data-testid="stSidebar"] .stButton > button[kind="primary"] {
        background-color: rgba(77, 166, 255, 0.15) !important;
        color: #ffffff !important;
        border-left: 3px solid #4da6ff !important;
        border-radius: 0 6px 6px 0;
        padding-left: calc(0.75rem - 3px);
    }

    section[data-testid="stSidebar"] .stButton > button[kind="primary"]:hover {
        background-color: rgba(77, 166, 255, 0.2) !important;
        color: #ffffff !important;
    }

    </style>
    <meta name="theme-color" content="#141824">
    """,
    unsafe_allow_html=True,
)


def main():
    """Main application entry point."""
    # Lazy import heavy modules after page config
    from components.auth import require_authentication
    from components.settings_page import render_settings_page

    # Require authentication
    user_info = require_authentication()
    user_name = user_info.get("name", user_info.get("email"))

    # Compact sidebar - logo top-left, then user info, then nav items
    with st.sidebar:
        # Small logo with live bot status indicator (auto-refreshes every 10s)
        _render_status_logo()

        # User name + logout, compact row below logo
        name_col, logout_col = st.columns([5, 1])
        with name_col:
            st.markdown(
                f"<span class='sidebar-user-name'>{user_name}</span>",
                unsafe_allow_html=True,
            )
        with logout_col:
            if st.button("⏻", key="top_logout", help="Logout"):
                st.logout()

        st.divider()

        # Navigation buttons - compact, left-aligned
        nav_items = [
            ("chat", "💬 Chats"),
            ("documents", "📚 RAG Knowledgebase"),
            ("agents", "🤖 Agents"),
            ("settings", "⚙️ Settings"),
        ]

        current_page_key = st.query_params.get("page", "chat")

        for page_key, page_label in nav_items:
            is_selected = current_page_key == page_key
            button_type = "primary" if is_selected else "secondary"

            if st.button(
                page_label,
                key=f"nav_{page_key}",
                use_container_width=True,
                type=button_type,
            ):
                if page_key != current_page_key:
                    st.query_params["page"] = page_key
                    st.rerun()

        # Langfuse observability dashboard link
        langfuse_project_url = os.getenv("LANGFUSE_DASHBOARD_URL", "")
        if langfuse_project_url.strip():
            st.divider()
            st.markdown(f"[📊 LLM Observability]({langfuse_project_url})")

    # Render appropriate page based on current_page_key
    if current_page_key == "chat":
        _render_chat_viewer_page(user_info)
    elif current_page_key == "documents":
        _render_documents_page()
    elif current_page_key == "agents":
        from components.agents_page import render_agents_page

        render_agents_page()
    else:
        render_settings_page()


@st.fragment(run_every=timedelta(seconds=30))
def _render_status_logo():
    """Auto-refreshing fragment: renders logo with live bot status glow."""
    from services.bot_status_service import get_bot_status

    status = get_bot_status()
    _render_logo(status)


_ALLOWED_STATUSES = {"live", "deploying", "down"}
_STATUS_TOOLTIPS = {"live": "Bot is live", "deploying": "Deploying…", "down": "Bot is down"}


@st.cache_data
def _load_logo_b64() -> tuple[str, str]:
    """Load and base64-encode logo assets once (static files, never change)."""
    import base64

    assets = Path(__file__).parent / "assets"
    anansi_b64 = ""
    org_b64 = ""
    anansi_path = assets / "anansi_logo.png"
    if anansi_path.exists():
        anansi_b64 = base64.b64encode(anansi_path.read_bytes()).decode()
    org_path = assets / "org_logo_white.svg"
    if org_path.exists():
        org_b64 = base64.b64encode(org_path.read_bytes()).decode()
    return anansi_b64, org_b64


def _render_logo(status: str = "down"):
    """Render Anansi + org logos top-left in sidebar (dark background)."""
    # Allowlist validation — prevents XSS if status source ever changes
    if status not in _ALLOWED_STATUSES:
        status = "down"

    anansi_b64, org_b64 = _load_logo_b64()
    tooltip = _STATUS_TOOLTIPS.get(status, "")

    if anansi_b64 or org_b64:
        anansi_img = (
            f'<img src="data:image/png;base64,{anansi_b64}"'
            f' class="bot-status-logo bot-{status}"'
            f' title="{tooltip}"'
            f' style="width: 36px; height: auto; display: block;" />'
            if anansi_b64
            else ""
        )
        org_img = (
            f'<img src="data:image/svg+xml;base64,{org_b64}"'
            f' style="height: 36px; width: auto; display: block;" />'
            if org_b64
            else ""
        )
        st.markdown(
            f"""
            <div style="display: flex; align-items: center; gap: 8px; margin: 0;">
                {anansi_img}
                {org_img}
            </div>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            "<h3 style='color: #ffffff; margin: 0; padding: 0.25rem 0;'>Anansi</h3>",
            unsafe_allow_html=True,
        )


@st.cache_resource
def _get_database_connection():
    """
    Get or create cached database connection.

    Lazy-loaded only when needed (after authentication).
    This is cached so it's only initialized once per session.
    """
    from services.supabase_reader import SupabaseReader

    return SupabaseReader()


def _render_chat_viewer_page(user_info: dict):
    """Render the chat history viewer page."""
    from components.broadcast_modal import show_broadcast_modal
    from components.conversation_view import render_conversation
    from components.sidebar import render_sidebar

    # Title with broadcast button right-aligned
    title_col, broadcast_col = st.columns([8, 2])
    with title_col:
        st.title("💬 Chats")
    with broadcast_col:
        st.markdown("<div style='height: 0.5rem'></div>", unsafe_allow_html=True)
        if st.button("📢 Broadcast", type="secondary", use_container_width=True):
            show_broadcast_modal()

    # Get cached database connection (lazy-loaded after authentication)
    db = _get_database_connection()

    if not db.is_configured():
        st.error(
            "⚠️ Database not configured. Check CHAT_DB_URL and CHAT_DB_SERVICE_KEY (or legacy SUPABASE_URL/SUPABASE_KEY)."
        )
        st.stop()

    # Create two-column layout: left sidebar for controls, right for chat history
    col1, col2 = st.columns([1, 3])

    with col1:
        # Render controls and get selected context, stats, and days_back
        # This now uses cached queries with loading indicators
        # Pass user email for cache isolation (prevents PII leakage between users)
        user_email = user_info.get("email", "unknown")
        selected_context, stats_data, days_back = render_sidebar(db, user_email)

    with col2:
        # Show stats at the top of the right section
        if stats_data:
            st.subheader(f"Chat Stats ({stats_data['days_back']} days)")
            col_a, col_b, col_c, col_d, col_e = st.columns(5)
            with col_a:
                st.markdown(f"**{stats_data['total_conversations']}** Total Conversations")
                st.markdown(f"**{stats_data['groups']}** Groups")
            with col_b:
                st.markdown(f"**{stats_data['direct_messages']}** Direct Messages")
                st.markdown(f"**{stats_data['total_messages']}** Total Messages")
            with col_c:
                st.markdown(f"**{stats_data['unique_users']}** Unique Users")
            with col_d:
                input_tokens = stats_data.get("input_tokens", 0)
                output_tokens = stats_data.get("output_tokens", 0)
                st.markdown(f"**{input_tokens:,}** Input Tokens")
                st.markdown(f"**{output_tokens:,}** Output Tokens")
            with col_e:
                median_response = stats_data.get("median_response_time")
                if median_response is not None:
                    st.markdown(f"**{median_response:.1f}s** Median Response")
                else:
                    st.markdown("**--** Median Response")

            # Add collapsible metric definitions
            with st.expander("Stats Explanation"):
                st.markdown(
                    "- **Total Conversations:** Unique chat sessions (groups + direct messages)\n"
                    "- **Groups:** Group chats with multiple participants\n"
                    "- **Direct Messages:** One-on-one conversations\n"
                    "- **Total Messages:** All messages exchanged (user + bot)\n"
                    "- **Unique Users:** Distinct individuals who interacted with the bot\n"
                    "- **Input Tokens:** Gemini API input tokens (prompts)\n"
                    "- **Output Tokens:** Gemini API output tokens (responses)\n"
                    "- **Median Response:** Median time from user message to bot response (excludes outliers >5min)"
                )
            st.divider()

        # Main content area - chat history
        if selected_context:
            # Render conversation view with date range from sidebar
            render_conversation(selected_context, db, days_back)
        else:
            # Welcome screen
            _render_welcome_screen(user_info, db)


def _render_welcome_screen(user_info: dict, db):
    """
    Render welcome screen when no conversation is selected.

    Args:
        user_info: Authenticated user information
        db: Database reader instance
    """
    # Title is now at the top of the page, no need to repeat it here


@st.dialog("Document Chunks", width="large")
def _show_chunk_viewer(doc_id: str, doc_title: str, doc: dict, procedure_id: str = None):
    """Show chunks for a document in a dialog popup, optionally filtered by procedure."""
    db = _get_database_connection()
    st.subheader(doc_title)

    chunks = db.get_document_chunks(doc_id, procedure_id=procedure_id)
    if not chunks:
        st.info("No chunks found.")
        return

    label = f"{len(chunks)} chunk(s)"
    if procedure_id:
        label += f" matching **{procedure_id}**"
    st.caption(label)

    for chunk in chunks:
        chunk_id = chunk.get("id", "")
        idx = chunk.get("chunk_index", 0)
        content = chunk.get("content", "")
        meta = chunk.get("chunk_metadata") or {}
        proc_ids = meta.get("procedure_ids", [])

        del_key = f"del_chunk_stage_{chunk_id}"
        if del_key not in st.session_state:
            st.session_state[del_key] = 0

        with st.expander(f"Chunk {idx}", expanded=(idx == 0)):
            st.text(content)
            if proc_ids:
                st.caption(f"Procedures: {', '.join(proc_ids)}")

            # Chunk delete flow
            if st.session_state[del_key] == 0:
                if st.button("🗑️ Delete chunk", key=f"del_chunk_{chunk_id}"):
                    st.session_state[del_key] = 1
                    st.rerun()
            elif st.session_state[del_key] == 1:
                st.warning(f"Delete chunk {idx}?")
                c1, c2, _ = st.columns([1, 1, 3])
                with c1:
                    if st.button("Yes, delete", key=f"chunk_confirm1_{chunk_id}", type="primary"):
                        st.session_state[del_key] = 2
                        st.rerun()
                with c2:
                    if st.button("Cancel", key=f"chunk_cancel1_{chunk_id}"):
                        st.session_state[del_key] = 0
                        st.rerun()
            elif st.session_state[del_key] == 2:
                st.error(f"Permanently delete chunk {idx}? This cannot be undone.")
                c1, c2, _ = st.columns([2, 1, 2])
                with c1:
                    if st.button(
                        "DELETE PERMANENTLY", key=f"chunk_confirm2_{chunk_id}", type="primary"
                    ):
                        if db.delete_chunk(chunk_id):
                            st.success("Chunk deleted.")
                            del st.session_state[del_key]
                            st.rerun()
                        else:
                            st.error("Failed to delete chunk.")
                            st.session_state[del_key] = 0
                with c2:
                    if st.button("Cancel", key=f"chunk_cancel2_{chunk_id}"):
                        st.session_state[del_key] = 0
                        st.rerun()

    # Entities section
    entities, entity_count = db.get_document_entities(doc_id, limit=10)
    if entities:
        st.divider()
        entity_names = [f"`{e['name']}` ({e['type']})" for e in entities]
        st.markdown(f"**Entities ({entity_count}):** {', '.join(entity_names)}")

    # Access level editor
    st.divider()
    st.markdown("**Access Level:**")

    access_levels: dict[str, dict] = {
        "Everyone (public)": {"audience": "all", "roles": []},
        "All Staff": {"audience": "staff", "roles": [1, 2, 3]},
        "Technical Staff": {"audience": "staff", "roles": [1, 2]},
        "Admin Only": {"audience": "staff", "roles": [1]},
    }

    current_audience = doc.get("audience", "staff")
    doc_roles = doc.get("allowed_role_ids", [])
    current_label = "All Staff"
    for label, config in access_levels.items():
        if config["audience"] == current_audience:
            if config["audience"] == "all" or config["roles"] == doc_roles:
                current_label = label
                break

    new_access = st.selectbox(
        "Change access",
        list(access_levels.keys()),
        index=list(access_levels.keys()).index(current_label),
        key=f"dialog_access_{doc_id}",
        label_visibility="collapsed",
    )

    if new_access != current_label:
        config = access_levels[new_access]
        if st.button("Save Access", key=f"dialog_save_access_{doc_id}"):
            if db.update_document_access(doc_id, config["audience"], config["roles"]):
                st.success(f"Access updated to: {new_access}")
                st.rerun()
            else:
                st.error("Failed to update access level.")


def _render_documents_page():
    """Render the RAG Knowledgebase management page."""
    st.title("📚 RAG Knowledgebase")

    # Get cached database connection
    db = _get_database_connection()

    if not db.is_configured():
        st.error("⚠️ Database not configured. Check CHAT_DB_URL and CHAT_DB_SERVICE_KEY.")
        st.stop()

    # Pagination controls
    col1, col2 = st.columns([3, 1])
    with col1:
        st.markdown("Documents ingested into the RAG knowledge base.")
    with col2:
        page_size = st.selectbox("Per page", [25, 50, 100], index=0, key="doc_page_size")

    # Filter bar
    filter_col1, filter_col2 = st.columns([1, 1])
    with filter_col1:
        doc_types = db.get_distinct_doc_types()
        type_options = ["All"] + doc_types
        selected_type = st.selectbox(
            "Document Type",
            type_options,
            key="doc_filter_type",
        )

    with filter_col2:
        if selected_type == "support_example":
            procedures = db.get_distinct_procedures()
            proc_options = ["All"] + procedures
            selected_proc = st.selectbox(
                "Procedure",
                proc_options,
                key="doc_filter_procedure",
            )
        else:
            selected_proc = "All"

    # Reset page when filters change
    filter_key = f"{selected_type}|{selected_proc}"
    if st.session_state.get("_last_filter_key") != filter_key:
        st.session_state["_last_filter_key"] = filter_key
        st.session_state["doc_page"] = 0

    # Get current page from session state
    if "doc_page" not in st.session_state:
        st.session_state.doc_page = 0

    doc_type_filter = selected_type if selected_type != "All" else None
    proc_filter = selected_proc if selected_proc != "All" else None
    offset = st.session_state.doc_page * page_size
    documents, total_count = db.get_ingested_documents(
        limit=page_size, offset=offset, doc_type=doc_type_filter, procedure_id=proc_filter
    )

    total_pages = (total_count + page_size - 1) // page_size if total_count > 0 else 1

    # Stats row
    st.markdown(f"**{total_count}** documents in knowledge base")

    if not documents:
        st.info("No documents ingested yet. Use `/ingest` in Telegram to add documents.")
        return

    # Documents table
    st.divider()

    for doc in documents:
        # Track delete confirmation state
        delete_key = f"delete_stage_{doc['id']}"
        if delete_key not in st.session_state:
            st.session_state[delete_key] = 0

        # Track edit state
        edit_key = f"edit_title_{doc['id']}"

        col1, col2, col3, col4 = st.columns([3, 1, 1, 1])

        with col1:
            if st.session_state.get(edit_key):
                # Inline title editor
                new_title = st.text_input(
                    "Title",
                    value=doc["title"],
                    key=f"title_input_{doc['id']}",
                    label_visibility="collapsed",
                )
                save_col, cancel_col, _ = st.columns([1, 1, 2])
                with save_col:
                    if st.button("Save", key=f"save_title_{doc['id']}", type="primary"):
                        if new_title.strip() and new_title != doc["title"]:
                            if db.update_document_title(doc["id"], new_title.strip()):
                                st.session_state[edit_key] = False
                                st.cache_data.clear()
                                st.rerun()
                            else:
                                st.error("Failed to update title.")
                        else:
                            st.session_state[edit_key] = False
                            st.rerun()
                with cancel_col:
                    if st.button("Cancel", key=f"cancel_title_{doc['id']}"):
                        st.session_state[edit_key] = False
                        st.rerun()
            else:
                # Title with link if available, plus edit button
                title_col, edit_btn_col = st.columns([5, 1])
                with title_col:
                    if doc.get("source_url"):
                        st.markdown(f"**[{doc['title']}]({doc['source_url']})**")
                    else:
                        st.markdown(f"**{doc['title']}**")
                with edit_btn_col:
                    if st.button("✏️", key=f"edit_{doc['id']}", help="Edit title"):
                        st.session_state[edit_key] = True
                        st.rerun()

            # Metadata badges
            doc_type = doc.get("doc_type", "unknown")
            audience = doc.get("audience", "staff")
            st.caption(f"📄 {doc_type} • 👥 {audience}")

        with col2:
            source_type = doc.get("source_type", "unknown")
            st.caption(f"Source: {source_type}")

            # Show truncated source_id
            source_id = doc.get("source_id", "")
            if source_id:
                st.caption(f"`{source_id[:12]}...`")

        with col3:
            # Chunk count (cached per document)
            chunk_count = db.get_document_chunks_count(doc["id"])
            st.metric("Chunks", chunk_count)

        with col4:
            # Ingested date
            ingested = doc.get("ingested_at")
            if ingested:
                date_str = ingested[:10] if len(ingested) >= 10 else ingested
                st.caption(f"📅 {date_str}")

            btn_col1, btn_col2 = st.columns(2)
            with btn_col1:
                if st.button("📖", key=f"view_{doc['id']}", help="View chunks"):
                    _show_chunk_viewer(doc["id"], doc["title"], doc, procedure_id=proc_filter)
            with btn_col2:
                if st.session_state[delete_key] == 0:
                    if st.button("🗑️", key=f"del_{doc['id']}", help="Delete document"):
                        st.session_state[delete_key] = 1
                        st.rerun()

        # Show confirmation dialogs OUTSIDE columns (full width)
        if st.session_state[delete_key] == 1:
            # First confirmation
            st.warning(f"⚠️ Delete **{doc['title']}**?")
            col_yes, col_no, col_spacer = st.columns([1, 1, 4])
            with col_yes:
                if st.button("Yes, delete", key=f"confirm1_{doc['id']}", type="primary"):
                    st.session_state[delete_key] = 2
                    st.rerun()
            with col_no:
                if st.button("Cancel", key=f"cancel1_{doc['id']}"):
                    st.session_state[delete_key] = 0
                    st.rerun()

        elif st.session_state[delete_key] == 2:
            # Second confirmation - final warning
            st.error(
                f"⛔ **FINAL WARNING:** Permanently delete **{doc['title']}**? This cannot be undone!"
            )
            col_yes, col_no, col_spacer = st.columns([2, 1, 3])
            with col_yes:
                if st.button("🗑️ DELETE PERMANENTLY", key=f"confirm2_{doc['id']}", type="primary"):
                    if db.delete_document(doc["id"]):
                        st.success(f"✅ Deleted: {doc['title']}")
                        del st.session_state[delete_key]
                        st.rerun()
                    else:
                        st.error("❌ Failed to delete document. Check server logs.")
                        st.session_state[delete_key] = 0
            with col_no:
                if st.button("Cancel", key=f"cancel2_{doc['id']}"):
                    st.session_state[delete_key] = 0
                    st.rerun()

        st.divider()

    # Pagination controls
    col1, col2, col3 = st.columns([1, 2, 1])
    with col1:
        if st.button("← Previous", disabled=st.session_state.doc_page == 0):
            st.session_state.doc_page -= 1
            st.rerun()
    with col2:
        st.markdown(
            f"<div style='text-align: center'>Page {st.session_state.doc_page + 1} of {total_pages}</div>",
            unsafe_allow_html=True,
        )
    with col3:
        if st.button("Next →", disabled=st.session_state.doc_page >= total_pages - 1):
            st.session_state.doc_page += 1
            st.rerun()


if __name__ == "__main__":
    main()

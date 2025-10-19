import streamlit as st
import subprocess
import json
import os
import re
from google import genai
from google.genai.errors import APIError
from datetime import datetime
import time 

# --- Configuration & Setup ---

try:
    GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
except Exception:
    GEMINI_API_KEY = None 

if not GEMINI_API_KEY:
    st.sidebar.error("❌ GEMINI_API_KEY environment variable not found. Analysis functions disabled.")

CONFLUENCE_SCRIPT = "confluence_tool.py"

try:
    CONFLUENCE_BASE_URL = os.environ['CONFLUENCE_URL']
    if CONFLUENCE_BASE_URL.endswith('/'):
        CONFLUENCE_BASE_URL = CONFLUENCE_BASE_URL.rstrip('/')
except KeyError:
    CONFLUENCE_BASE_URL = "https://YOUR_CONFLUENCE_URL_NOT_SET"


STOP_WORDS = set([
    'a', 'an', 'the', 'for', 'about', 'and', 'or', 'in', 'on', 'with',
    'is', 'are', 'was', 'were', 'of', 'to', 'from', 'can', 'should', 'i',
    'my', 'find', 'show', 'search', 'documentation', 'notes', 'tell',
    'need', 'looking', 'me', 'you', 'give'
])

# --- Session State Initialization ---

if 'total_tokens_used' not in st.session_state:
     st.session_state.total_tokens_used = 0
if 'token_usage' not in st.session_state:
    st.session_state.token_usage = "0"
if 'search_history' not in st.session_state:
    st.session_state.search_history = []
if 'analysis_state' not in st.session_state:
    st.session_state.analysis_state = {}
if 'llm_selected_id' not in st.session_state:
    st.session_state.llm_selected_id = None
if 'submitted' not in st.session_state:
    st.session_state.submitted = False
# Initialize the custom input flag (used by the new external checkbox)
if 'custom_input_enabled' not in st.session_state:
    st.session_state.custom_input_enabled = False


# --- Helper Functions ---

def run_confluence_command(command_args):
    """Executes the external Confluence Python script via subprocess."""
    command = ['python', CONFLUENCE_SCRIPT] + [str(arg) for arg in command_args]
    
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=True,
            timeout=60
        )
        return json.loads(result.stdout)

    except subprocess.CalledProcessError as e:
        try:
            return json.loads(e.stdout)
        except json.JSONDecodeError:
            return {"error": f"Confluence script failed. Details: {e.stderr}"}
    except Exception as e:
        return {"error": f"An unexpected execution error occurred: {e}"}


def extract_search_params(user_input):
    """Cleans up the user input to separate the search term from the optional space key."""
    space_match = re.search(r'in space\s+([A-Z0-9]{2,10})\b', user_input, re.IGNORECASE)
    space_key = space_match.group(1).upper() if space_match else None
    search_term_phrase = re.sub(r'in space\s+[A-Z0-9]{2,10}\b', '', user_input, flags=re.IGNORECASE).strip()
    
    clean_words = re.sub(r'[^\w\s]', '', search_term_phrase.lower()).split()
    filtered_words = [word for word in clean_words if word not in STOP_WORDS and len(word) > 2]
    final_search_term = " ".join(filtered_words)
    if not final_search_term:
        final_search_term = search_term_phrase.strip()
        
    return final_search_term, space_key

def get_latest_updated_match(matches):
    """Finds the match with the most recent update date."""
    def parse_date(date_str):
        if date_str == 'N/A': return datetime.min 
        try: return datetime.strptime(date_str, '%Y-%m-%d %H:%M:%S')
        except ValueError: return datetime.min 
    sorted_matches = sorted(
        matches, 
        key=lambda m: parse_date(m['last_updated']), 
        reverse=True
    )
    return sorted_matches[0]

def get_best_page_recommendation(matches, search_term):
    """
    Uses the Gemini model to recommend the best page for editing
    based on perceived quality, latest date, and potential duplication.
    """
    
    page_details = []
    options_map = {}
    for i, m in enumerate(matches):
        detail = (
            f"Page {i+1}:\n"
            f"  - ID: {m['id']}\n"
            f"  - Title: '{m['title']}'\n"
            f"  - Space: '{m['space_key']}'\n"
            f"  - Last Updated: {m['last_updated']}\n"
        )
        page_details.append(detail)
        options_map[m['id']] = m 
        
    page_list = "\n".join(page_details)

    prompt = f"""
    You are a Content Analyst. I found {len(matches)} pages relevant to the search query: '{search_term}'.
    Your goal is to suggest the absolute **BEST** page for the user to edit to fix or update the information, prioritizing quality and accuracy.

    Here are the page details:
    ---
    {page_list}
    ---

    Perform the following analysis steps:
    1.  **LATEST:** Identify the page with the most recent 'Last Updated' date.
    2.  **QUALITY/COMPLETENESS:** Suggest which page sounds like the primary, most official, or most comprehensive source.
    3.  **SELECTION:** Based on your analysis, choose the single BEST page ID for editing.

    Format your response as a two-part output:
    1. A single line containing ONLY the selected page ID.
    2. The rest of the output must be a clear, detailed recommendation justifying your choice.
    Example: 
    123456789
    Based on the analysis, I recommend page '123456789' (The Latest Policy Draft) because it is the most recent and the title suggests it is the official source...
    """
    try:
        client = genai.Client(api_key=os.environ['GEMINI_API_KEY'])
        response = client.models.generate_content(model='gemini-2.5-flash', contents=prompt)
        
        lines = response.text.strip().split('\n', 1)
        recommended_id = lines[0].strip()
        recommendation_text = lines[1].strip() if len(lines) > 1 else "No detailed recommendation provided."
        
        if recommended_id in options_map:
            return recommendation_text, recommended_id
        else:
            latest_match = get_latest_updated_match(matches)
            return f"LLM recommended an invalid ID: '{recommended_id}'. Defaulting to the **latest updated page**: **{latest_match['title']}**.", latest_match['id']

    except Exception as e:
        latest_match = get_latest_updated_match(matches)
        return f"Recommendation failed: {e}. Defaulting to the **latest updated page**: **{latest_match['title']}**.", latest_match['id']


def get_corrected_page_proposal(page_title, page_content, action, search_term, update_focus, custom_notes, optional_instructions, output_format):
    """Generates the proposed corrected content based on the user's selected action and updates token count."""
    
    action_prompts = {
        'Fix Grammar & Spelling': 
            "Review the page content and correct all grammatical errors, typos, and spelling mistakes. Do NOT change the meaning or structure. Return ONLY the corrected page content.",
        
        'Improve Formatting & Readability': 
            "Review the page content (which is in Confluence Storage Format/HTML). Reformat it to be easier to read, using clear headings and lists. Do NOT change the meaning or core text. Return ONLY the improved page content.",
            
        'Propose Content Update':
            f"Review the page content and propose a major update focusing on the search term '{search_term}'. Improve clarity, add missing steps, and make the information comprehensive. Include a brief summary of changes at the top. Return ONLY the proposed, fully updated page content.",
        
        'Just perform a Content Quality Audit (No changes proposed)': 
            "You are a Content Quality Analyst. Analyze the page content and create a detailed Markdown report on its clarity, structure, completeness, and relevance to the search term. Do NOT propose a content change."
    }
    
    # --- DYNAMIC PROMPT ADJUSTMENT ---
    custom_instruction = ""
    # 1. Custom/New Content Strategy (only for Propose Content Update)
    if action == 'Propose Content Update' and update_focus == 'CUSTOM_INPUT' and custom_notes.strip():
        custom_instruction += f"""
        **CRITICAL NEW CONTENT INPUT:** Integrate the following specific, up-to-date information into the page content:
        ---
        CUSTOM CONTENT: {custom_notes}
        ---
        """
        
    # 2. General/Stylistic Instructions (for all actions)
    if optional_instructions.strip():
        custom_instruction += f"""
        **ADDITIONAL STYLISTIC/STRUCTURAL INSTRUCTIONS:** When performing the action, also ensure you follow these specific guidelines:
        ---
        GUIDELINES: {optional_instructions}
        ---
        """
        
    # 3. Output Format Instruction
    format_instruction = "The output must be formatted using **standard Markdown**."
    
    if output_format == "Confluence Storage Format (HTML/XML - For Direct Paste)":
        format_instruction = "The output must be formatted using **Confluence Storage Format (HTML/XML)**. Do not include any Markdown text."
    elif output_format == "Both Formats (Markdown & HTML/XML)":
        format_instruction = """The output must contain TWO DISTINCT SECTIONS.
        1. **MARKDOWN SECTION:** The full proposed content in standard Markdown format.
        2. **HTML/XML SECTION:** The full proposed content converted into Confluence Storage Format (HTML/XML).
        Use clear headings to separate the two sections (e.g., '## PROPOSED MARKDOWN' and '## PROPOSED HTML/XML')."""


    # --- MODIFIED PROMPT WITH STRICT CONSTRAINT ---
    prompt = f"""
    You are an expert Confluence Content Editor. Your task is to perform the following action based on the details provided.
    
    **CRITICAL OUTPUT INSTRUCTION:** You MUST return **ONLY** the result of the action (the proposed content or audit report). Do not include any conversational preamble, confirmation, or explanatory text before the final output. {format_instruction}
    
    Action: '{action_prompts.get(action, 'Propose Content Update')}'
    
    PAGE TITLE: {page_title}
    
    {custom_instruction} 
    
    RAW PAGE CONTENT (in Confluence Storage Format/HTML):
    ---
    {page_content}
    ---
    """
    
    try:
        client = genai.Client(api_key=os.environ['GEMINI_API_KEY'])
        model_name = 'gemini-2.5-pro' if action == 'Propose Content Update' else 'gemini-2.5-flash'
        response = client.models.generate_content(model=model_name, contents=prompt)
        
        usage_metadata = response.usage_metadata
        total_tokens = usage_metadata.prompt_token_count + usage_metadata.candidates_token_count
        
        st.session_state.total_tokens_used += total_tokens
        st.session_state.token_usage = str(st.session_state.total_tokens_used)
        
        return response.text
    except APIError as e:
        return f"Proposal generation failed due to API Error: {e}"
    except Exception as e:
        return f"Proposal generation failed: {e}"


# --- Callback Functions ---

def proceed_to_action_callback():
    """
    Executed when the 'Proceed to Action Selection' button is clicked.
    """
    recommended_id = st.session_state.get('llm_selected_id')
    matches = st.session_state.analysis_state.get('matches', [])
    
    if recommended_id:
        top_match = next((m for m in matches if m['id'] == recommended_id), None)
        
        if top_match:
            st.session_state.analysis_state.update({
                'selected_id': top_match['id'],
                'selected_title': top_match['title'],
                'selected_space': top_match['space_key'],
                'selected_updated': top_match['last_updated'],
                'step': 'choose_action' 
            })
            
            st.session_state.submitted = False
            
            st.rerun()
        else:
            st.error("Error: Recommended page data is missing. Please clear history and try again.")
    else:
        st.error("Error: No page was automatically selected by the Agent. Please start a new search.")

def toggle_custom_input():
    """Toggles the custom input flag and forces a rerun."""
    # This function automatically runs on checkbox change, 
    # and st.rerun() forces the rest of the page to rebuild with the new state.
    pass


# --- Streamlit UI: Sidebar ---

st.set_page_config(page_title="Confluence Agent", layout="wide")

st.sidebar.markdown("## ⚙️ Settings & History")

if st.sidebar.button("🧹 Clear All Results & History", type="secondary"):
    st.session_state.clear()
    st.rerun()

st.sidebar.markdown("### Status")
st.sidebar.metric("Gemini API Tokens Used", st.session_state.token_usage)
st.sidebar.markdown(f"**Target URL:**")
st.sidebar.code(CONFLUENCE_BASE_URL, language='text')

llm_status = "✅ Gemini: OK" if GEMINI_API_KEY else "❌ Gemini: API Key Missing"
confluence_status = "✅ Confluence: OK" if 'CONFLUENCE_URL' in os.environ else "⚠️ Confluence URL: Missing"
st.sidebar.markdown(f"*{llm_status}*")
st.sidebar.markdown(f"*{confluence_status}*")

st.sidebar.markdown("### Search History")
if st.session_state.search_history:
    for i, query in enumerate(reversed(st.session_state.search_history)):
        if st.sidebar.button(f"↩️ Re-run: {query}", key=f"hist_{i}"):
            st.session_state.rerun_query = query 
            st.session_state.submitted = True
            st.session_state.analysis_state = {}
            st.rerun()
else:
    st.sidebar.info("No history yet.")

# --- Streamlit UI: Main Content ---

st.title("Confluence Editor & Publishing Agent 🤖")

st.markdown("### 🔍 Search Instructions")
st.markdown("1.  **Enter keywords** to find a page (e.g., 'VPN setup').")
st.markdown("2.  Optionally, specify a space (e.g., 'VPN setup **in space IT**').")
st.markdown("---")


# --- Main Search Form Logic ---
form_submitted = False 

with st.form("search_form"):
    
    default_prompt_value = st.session_state.get('rerun_query', "Find documentation about change management in space IS")
    if 'rerun_query' in st.session_state:
        del st.session_state.rerun_query
    
    user_prompt = st.text_input(
        "Enter your search query:", 
        default_prompt_value, 
        placeholder="e.g., 'server setup in space IT' or just 'travel policy'" 
    )
    manual_space_key = st.text_input("Override Space Key (e.g., HR, IS):", "")
    form_submitted = st.form_submit_button("Search Confluence") 

if form_submitted or st.session_state.get('submitted', False):
    
    if form_submitted:
        if user_prompt not in st.session_state.search_history:
            st.session_state.search_history.append(user_prompt)
            if len(st.session_state.search_history) > 5:
                st.session_state.search_history.pop(0) 
        
        st.session_state.analysis_state = {}
        st.session_state.llm_selected_id = None
        st.session_state.submitted = True 
        st.session_state.custom_input_enabled = False # Reset custom input flag on new search
    
    if st.session_state.analysis_state.get('step') != 'choose_page' and not form_submitted:
        pass 
    
    else:
        search_term_clean, extracted_space_key = extract_search_params(user_prompt)
        space_key = manual_space_key.upper() if manual_space_key else extracted_space_key
        
        if not search_term_clean: st.warning("Please enter a meaningful search term."); st.stop()

        with st.spinner(f'Step 1/4: Searching Confluence for "{search_term_clean}"...'):
            search_term_quoted = f'"{search_term_clean}"'
            search_command_args = ['--search', search_term_quoted]
            if space_key: search_command_args.extend(['--space', space_key])
                
            search_data = run_confluence_command(search_command_args)

        if "error" in search_data: st.error(f"Search Error: {search_data.get('error', 'Unknown Error')}"); st.json(search_data); st.stop()
            
        matches = search_data.get('matches', [])
        if not matches: st.info("✅ Search Complete: No matching pages found."); st.json(search_data); st.stop()
        
        st.session_state.analysis_state = {
            'search_term': search_term_clean,
            'matches': matches,
            'total_matches': search_data.get('total_matches', len(matches)),
            'step': 'choose_page'
        }


# --- State 1: Choose Page ---

if st.session_state.analysis_state.get('step') == 'choose_page':
    
    if st.session_state.get('llm_selected_id') is None:
        matches = st.session_state.analysis_state['matches']
        total_matches = st.session_state.analysis_state['total_matches']
        
        if total_matches == 1:
            top_match = matches[0]
            st.session_state.llm_selected_id = top_match['id']
            st.info(f"✅ Found one match: **{top_match['title']}**. Click 'Proceed' below to continue.")
            
        elif total_matches > 1:
            st.warning(f"🤔 Found {total_matches} pages. The Agent will proceed with its **top recommendation**, but you can review the others.")

            with st.spinner("🧠 Analyzing page candidates for best quality and potential duplication..."):
                recommendation_text, recommended_id = get_best_page_recommendation(
                    matches, 
                    st.session_state.analysis_state['search_term']
                )
            
            st.session_state.llm_selected_id = recommended_id
            
            st.subheader("🤖 Agent Quality & Duplication Analysis")
            st.info(recommendation_text) 
            st.markdown("---") 

            st.markdown("**Other Matches Found (For Reference):**")
            latest_match = get_latest_updated_match(matches)
            selected_id = recommended_id

            for m in matches:
                is_latest = " (LATEST UPDATE)" if m['id'] == latest_match['id'] else ""
                is_selected = "✅ **Selected by Agent**" if m['id'] == selected_id else " "
                page_link = f"{CONFLUENCE_BASE_URL}/pages/viewpage.action?pageId={m['id']}"
                
                st.markdown(
                    f"- **{m['title']}** | Space: `{m['space_key']}` | Updated: {m['last_updated']} {is_latest} {is_selected} | [View Page]({page_link})"
                )
            st.markdown("---")
            
    st.button(
        "Proceed to Action Selection with Recommended Page", 
        type="primary",
        on_click=proceed_to_action_callback,
        key="proceed_button_to_action"
    )
    
    st.stop()


# --- State 2: Choose Action (with New Content Prompt) ---

if st.session_state.analysis_state.get('step') == 'choose_action':
    state = st.session_state.analysis_state
    
    if 'llm_selected_id' in st.session_state:
        del st.session_state.llm_selected_id
    
    st.markdown(f"---")
    st.subheader(f"Page Selected: **{state['selected_title']}**")
    
    selected_page_link = f"{CONFLUENCE_BASE_URL}/pages/viewpage.action?pageId={state['selected_id']}"
    st.markdown(f"**Link:** [Open Page in Confluence]({selected_page_link})")
    
    st.warning("❓ What do you want the Agent to do with this page?")
    
    # --- EXTERNAL CHECKBOX FIX for Custom Input Visibility ---
    action_type = st.session_state.analysis_state.get('selected_action', 'Propose Content Update') 
    
    # Only show the external checkbox if the action is 'Propose Content Update'
    if action_type == "Propose Content Update":
        st.markdown("---")
        st.info("💡 **Content Update Strategy:** Do you have new information to add?")
        
        st.checkbox(
            "✅ I want to provide **Custom Knowledge/Instructions** (Overrides general LLM knowledge)",
            key='custom_input_enabled', # Uses the external flag
            on_change=toggle_custom_input
        )
        st.markdown("---")
    else:
        # Ensure the flag is false if the action is not 'Propose Content Update'
        st.session_state.custom_input_enabled = False 
    
    
    with st.form("action_selection_form"):
        
        # Use st.select to ensure we can control the default value on load
        action = st.radio(
            "Select Agent Task:",
            [
                "Propose Content Update",
                "Improve Formatting & Readability",
                "Fix Grammar & Spelling",
                "Just perform a Content Quality Audit (No changes proposed)"
            ],
            key='selected_action',
            index=["Propose Content Update", "Improve Formatting & Readability", "Fix Grammar & Spelling", "Just perform a Content Quality Audit (No changes proposed)"].index(action_type)
        )
        
        output_format = st.radio(
            "Select Desired Output Format:",
            [
                "Markdown (Recommended for Review)",
                "Confluence Storage Format (HTML/XML - For Direct Paste)",
                "Both Formats (Markdown & HTML/XML)"
            ],
            key='output_format_selection',
            help="Markdown is best for reviewing changes. HTML/XML is Confluence's native format for direct source editing."
        )

        custom_notes = ""
        update_focus = 'LLM_ONLY'
        is_blocked = False

        # Render the custom input box based on the external flag
        if action == "Propose Content Update" and st.session_state.custom_input_enabled:
            update_focus = 'CUSTOM_INPUT'
            custom_notes = st.text_area(
                "✏️ **Custom Content:** Paste any new policies or information here.",
                placeholder="E.g., 'The VPN process changed on 10/1. New servers are vpn-na1 and vpn-eu2.'",
                height=150,
                key='custom_notes_input'
            )
            if not custom_notes.strip():
                 st.error("Please provide custom input or uncheck the box above to proceed.")
                 is_blocked = True
            
        optional_instructions = st.text_area(
            "⚙️ **Optional Instructions/Hints:** (e.g., 'Use a friendly tone', 'Ensure all headings are H3 or H4', 'Focus on improving the troubleshooting section')",
            placeholder="Add specific instructions for the Agent here.",
            height=80,
            key='optional_instructions_input'
        )
        
        execute_button = st.form_submit_button(f"Execute '{action}'")
        
    if execute_button and not is_blocked:
        st.session_state.analysis_state.update({
            'action': action,
            'update_focus': update_focus,
            'custom_notes': custom_notes,
            'optional_instructions': optional_instructions,
            'output_format': output_format 
        })

        with st.spinner(f'Step 2/4: Retrieving full content for "{state["selected_title"]}"...'):
            content_data = run_confluence_command(['--content-id', state['selected_id'], '--search', 'dummy']) 
        
        if "content" not in content_data:
            st.error(f"Content Retrieval Error: Could not get full page content.")
            st.json(content_data) 
            st.stop()
            
        st.session_state.analysis_state.update({
            'raw_content': content_data['content'],
            'step': 'analyze'
        })
        st.rerun()

# --- State 3: Analyze & Propose ---

if st.session_state.analysis_state.get('step') == 'analyze':
    state = st.session_state.analysis_state
            
    with st.spinner(f'Step 3/4: Generating proposed content for "{state["action"]}"...'):
        proposed_content = get_corrected_page_proposal(
            state['selected_title'], 
            state['raw_content'], 
            state['action'], 
            state['search_term'],
            state.get('update_focus', 'LLM_ONLY'), 
            state.get('custom_notes', ''),
            state.get('optional_instructions', ''),
            state.get('output_format', 'Markdown (Recommended for Review)')
        )

    st.session_state.analysis_state['proposed_content'] = proposed_content
    st.session_state.analysis_state['step'] = 'review_proposal'
    st.rerun()


# --- State 4: Review and Finalize (No Publish) ---

if st.session_state.analysis_state.get('step') == 'review_proposal':
    state = st.session_state.analysis_state
    
    st.markdown("---")
    st.subheader(f"✅ Proposal Review: **{state['action']}**")
    
    output_format = state.get('output_format', 'Markdown (Recommended for Review)')

    if output_format == "Both Formats (Markdown & HTML/XML)":
        st.info("The Agent has generated the content in both Markdown and Confluence Storage Format. Check both tabs.")
        
        match = re.search(r'## PROPOSED MARKDOWN\s*\n(.*?)(?=\n## PROPOSED HTML/XML|$)', state['proposed_content'], re.DOTALL)
        markdown_content = match.group(1).strip() if match else "Could not isolate Markdown section. See Raw Content tab."
        raw_content_display = state['proposed_content']
        
    elif output_format == "Confluence Storage Format (HTML/XML - For Direct Paste)":
        st.info("The Agent has generated the content in **Confluence Storage Format (HTML/XML)** for direct paste into the source editor.")
        markdown_content = "Preview not available for raw HTML/XML output. See Raw Content tab for the generated code."
        raw_content_display = state['proposed_content']
        
    else: # Default: Markdown
        st.info(f"The Agent has generated the following **Markdown-formatted** content for page: **{state['selected_title']}**.")
        markdown_content = state['proposed_content']
        raw_content_display = state['proposed_content']


    tab1, tab2 = st.tabs(["💡 Readable Proposal (Markdown Preview)", "💾 Raw Output (for Copy/Paste)"])

    with tab1:
        st.markdown(markdown_content) 
        
    with tab2:
        language = 'html' if output_format in ["Confluence Storage Format (HTML/XML - For Direct Paste)", "Both Formats (Markdown & HTML/XML)"] else 'markdown'
        st.code(raw_content_display, language=language, line_numbers=True)
    
    st.markdown("---")
    
    col1, col2 = st.columns(2)
    
    with col1:
        if st.button("💾 Copy & Finalize (No Publish)", type="primary"):
            st.success(f"Content finalized! You can copy the proposed content from the 'Raw Output' tab above and manually update Confluence.")
            st.balloons()
            st.session_state.analysis_state = {}
            st.session_state.submitted = False 
            
    with col2:
        if st.button("❌ Discard Proposal and Start New Search"):
            st.info("Proposal discarded. Please enter a new search query above.")
            st.session_state.analysis_state = {}
            st.session_state.submitted = False

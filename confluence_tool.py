import sys
import argparse
import json
import os
from atlassian import Confluence
from datetime import datetime

# --- CONFIGURATION: Retrieve from Environment Variables ---
try:
    CONFLUENCE_URL = os.environ['CONFLUENCE_URL']
    USERNAME = os.environ['CONFLUENCE_USERNAME']
    API_TOKEN = os.environ['CONFLUENCE_API_TOKEN']
except KeyError as e:
    sys.stderr.write(f"❌ Error: Required environment variable {e} not set. Please export the CONFLUENCE credentials.\n")
    sys.exit(1)

MAX_RESULTS = 10 # Increase results limit for local filtering approach

def get_confluence_client():
    """Initializes and returns the Confluence client."""
    return Confluence(
        url=CONFLUENCE_URL,
        username=USERNAME,
        password=API_TOKEN,
        cloud=True
    )

# 💥 The new default search function is the robust, local filtering logic.
def search_and_report_updates(search_term, space_key=None):
    """
    Pulls all pages from specified space(s) and filters locally 
    to avoid strict CQL parser errors.
    """
    try:
        confluence = get_confluence_client()
    except Exception as e:
        return {"error": f"Connection failed: {e}"}

    matches = []
    scope_message = f"in Space: {space_key}" if space_key else "across ALL Spaces"
    sys.stderr.write(f"🔎 Executing FALLBACK Search {scope_message} | Term: {search_term}\n")

    try:
        # Determine which spaces to search
        if space_key:
            spaces = [space_key]
        else:
            # Note: Getting ALL spaces can be slow/resource-intensive
            spaces = [s['key'] for s in confluence.get_all_spaces().get('results', [])]

        for sk in spaces:
            # Get pages metadata
            pages = confluence.get_all_pages_from_space(sk, start=0, limit=1000)
            
            for p in pages:
                title = p.get('title', '')
                
                # Fetch full content to search the body
                try:
                    # Note: We are fetching content for every page in the loop. This is slow but robust.
                    body = confluence.get_page_by_id(p.get('id'), expand='body.storage').get('body', {}).get('storage', {}).get('value', '')
                except Exception as e:
                    sys.stderr.write(f"Warning: Could not fetch content for page {p.get('id')}: {e}\n")
                    body = ''

                # Local filtering on title and body
                search_lower = search_term.strip('"').lower() # Strip quotes if present
                if search_lower in title.lower() or search_lower in body.lower():
                    
                    # Formatting logic
                    history = p.get('history', {})
                    updated_raw = history.get('lastUpdated', {}).get('when', 'N/A')
                    updated = 'N/A'
                    if updated_raw != 'N/A':
                        try:
                            updated = datetime.fromisoformat(updated_raw.replace('Z', '+00:00')).strftime('%Y-%m-%d %H:%M:%S')
                        except:
                            pass
                            
                    matches.append({
                        "title": title,
                        "id": p.get('id', 'N/A'),
                        "space_key": sk,
                        "last_updated": updated
                    })
                    
                if len(matches) >= MAX_RESULTS:
                    break
            if len(matches) >= MAX_RESULTS:
                break

        return {
            "query": search_term,
            "scope": scope_message,
            "total_matches": len(matches),
            "matches": matches
        }
    except Exception as e:
        return {"error": f"Search failed: {e}"}

def get_page_content_by_id(page_id):
    """Fetches the title and full storage content of a specific Confluence page."""
    try:
        confluence = get_confluence_client()
        page_data = confluence.get_page_by_id(page_id, expand='body.storage')
        content = page_data.get('body', {}).get('storage', {}).get('value', 'Content not found.')
        title = page_data.get('title', 'Untitled Page')
        
        return {"title": title, "content": content}
    except Exception as e:
        return {"error": f"Failed to retrieve content for ID {page_id}: {e}"}

# --- COMMAND-LINE EXECUTION LOGIC ---
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Confluence Search and Report Tool.")
    
    # Define all possible arguments
    parser.add_argument('--search', help='The text string to search for.', required=False)
    parser.add_argument('--space', default=None, help='The key of the space to search (optional).')
    parser.add_argument('--content-id', default=None, help='Page ID to retrieve full content for analysis.') 

    args = parser.parse_args()

    # Conditional logic to prioritize content retrieval
    if args.content_id:
        result = get_page_content_by_id(args.content_id)
    elif args.search:
        # Calls the robust local filtering function
        result = search_and_report_updates(args.search.strip('"'), args.space) 
    else:
        result = {"error": "Missing search term or page ID argument."}

    print(json.dumps(result, indent=4))

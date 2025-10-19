import os
import requests
import base64
import sys

# Load credentials from environment
CONFLUENCE_URL = os.environ.get('CONFLUENCE_URL')
USERNAME = os.environ.get('CONFLUENCE_USERNAME')
API_TOKEN = os.environ.get('CONFLUENCE_API_TOKEN')

if not all([CONFLUENCE_URL, USERNAME, API_TOKEN]):
    sys.stderr.write("❌ Error: Environment variables not fully set.\n")
    sys.exit(1)

# Encode credentials for the Authorization header
auth_string = f"{USERNAME}:{API_TOKEN}"
encoded_auth = base64.b64encode(auth_string.encode()).decode()

# The simplest, safest search query URL
search_url = f"{CONFLUENCE_URL}/rest/api/content/search?cql=text~server"

headers = {
    "Authorization": f"Basic {encoded_auth}",
    "Accept": "application/json"
}

print(f"Testing URL: {search_url}")
print("-" * 30)

try:
    response = requests.get(search_url, headers=headers)
    
    print(f"HTTP Status Code: {response.status_code}")
    
    if response.status_code == 200:
        data = response.json()
        print(f"✅ Connection SUCCESSFUL!")
        print(f"Total search results found: {data.get('totalSize')}")
        print("The API token and permissions are valid.")
    elif response.status_code == 401:
        print("❌ AUTHENTICATION FAILURE (401 Unauthorized).")
        print("The API token is invalid or expired. Please regenerate it.")
    elif response.status_code == 403:
        print("❌ AUTHORIZATION FAILURE (403 Forbidden).")
        print("The user lacks site or search permissions.")
    else:
        print(f"⚠️ API Error: {response.status_code}. Raw response:")
        print(response.text[:200] + '...')

except requests.exceptions.RequestException as e:
    print(f"❌ Network Error: Could not connect to the server. {e}")


import requests
import sys

shop = "https://app-dev-test-store-nuahgstn.myshopify.com/"
client_id = "enter app client id"
client_secret = "ENTER YOUR APP CLIENT SECRET ID"

url = f"{shop}/admin/oauth/access_token"

post_fields = {
    "grant_type": "client_credentials",
    "client_id": client_id,
    "client_secret": client_secret,
}

headers = {
    "Content-Type": "application/x-www-form-urlencoded"
}

try:
    response = requests.post(
        url,
        data=post_fields,
        headers=headers,
        timeout=30
    )

except requests.exceptions.RequestException as e:
    print(f"Request Error: {e}")
    sys.exit(1)

# Check HTTP status
if response.status_code != 200:
    print(f"HTTP Error {response.status_code}: {response.text}")
    sys.exit(1)

# Decode JSON response
try:
    data = response.json()
except requests.exceptions.JSONDecodeError:
    print("JSON Decode Error: Invalid JSON response")
    sys.exit(1)

# Extract response values
access_token = data.get("access_token")
scope = data.get("scope", "")
expires_in = data.get("expires_in", 0)

if not access_token:
    print(f"No access token returned. Response: {data}")
    sys.exit(1)

print(f"Access Token : {access_token}")
print(f"Scope        : {scope}")
print(f"Expires In   : {expires_in} seconds")

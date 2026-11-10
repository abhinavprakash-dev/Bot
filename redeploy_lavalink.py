import json
import sys
import urllib.request
import ssl

API = "https://backboard.railway.com/graphql/v2"
CTX = ssl._create_unverified_context()

def gql(token: str, query: str, variables: dict | None = None) -> dict:
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        API,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60, context=CTX) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        print("HTTP", e.code, e.read().decode()[:500])
        return {}

# You need to provide your Railway token
# Get it from: https://railway.com/account/tokens
token = input("Enter Railway token: ").strip()

# Trigger redeploy for lavalink service
service_id = "9f7344cd-d69a-4124-8d80-2be5bff7055a"

# First get the latest deployment
query = """
mutation($serviceId: String!) {
  serviceDeploymentCreate(input: {serviceId: $serviceId}) {
    id
    status
  }
}
"""
result = gql(token, query, {"serviceId": service_id})
print(json.dumps(result, indent=2))
import json
import os
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

token = os.getenv("RAILWAY_API_TOKEN", "").strip()
if not token:
    raise SystemExit("Set RAILWAY_API_TOKEN before running this script.")

# Trigger Lavalink redeploy
print("=== Redeploying Lavalink ===")
result = gql(token, """
mutation($serviceId: String!) {
  serviceDeploymentCreate(input: {serviceId: $serviceId}) {
    id
    status
  }
}
""", {"serviceId": "9f7344cd-d69a-4124-8d80-2be5bff7055a"})
print(json.dumps(result, indent=2))

# Trigger Bot redeploy
print("\n=== Redeploying Bot ===")
result = gql(token, """
mutation($serviceId: String!) {
  serviceDeploymentCreate(input: {serviceId: $serviceId}) {
    id
    status
  }
}
""", {"serviceId": "68f3a7d3-3a50-448e-b49e-eee3b1ee1f33"})
print(json.dumps(result, indent=2))
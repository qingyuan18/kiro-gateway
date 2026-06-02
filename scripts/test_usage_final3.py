#!/usr/bin/env python3
"""
Use pure Kiro Desktop Auth to refresh token and call GetUsageLimits.
Bypass clientIdHash detection to avoid falling into SSO OIDC mode.
"""

import asyncio
import json
import sys
import os
import uuid
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kiro.utils import get_machine_fingerprint
import httpx


async def main():
    # Load raw creds
    creds_path = Path("~/.aws/sso/cache/kiro-auth-token.json").expanduser()
    with open(creds_path) as f:
        creds = json.load(f)

    refresh_token = creds["refreshToken"]
    region = creds.get("region", "us-east-1")
    fingerprint = get_machine_fingerprint()

    # Step 1: Refresh token via Kiro Desktop Auth
    refresh_url = f"https://prod.{region}.auth.desktop.kiro.dev/refreshToken"
    print(f"Refreshing token via: {refresh_url}")

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            refresh_url,
            json={"refreshToken": refresh_token},
            headers={
                "Content-Type": "application/json",
                "User-Agent": f"KiroIDE-0.7.45-{fingerprint}",
            }
        )
        if resp.status_code != 200:
            print(f"Refresh failed: {resp.status_code} - {resp.text}")
            return

        token_data = resp.json()
        access_token = token_data["accessToken"]
        profile_arn = token_data.get("profileArn")
        print(f"Token refreshed! ProfileArn: {profile_arn}")
        print(f"Token prefix: {access_token[:30]}...")

    # Step 2: Call GetProfile to verify
    q_host = f"https://q.{region}.amazonaws.com"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "User-Agent": f"aws-sdk-js/1.0.27 ua/2.1 os/win32#10.0.19044 lang/js md/nodejs#22.21.1 api/codewhispererstreaming#1.0.27 m/E KiroIDE-0.7.45-{fingerprint}",
        "x-amz-user-agent": f"aws-sdk-js/1.0.27 KiroIDE-0.7.45-{fingerprint}",
        "x-amzn-codewhisperer-optout": "true",
        "amz-sdk-invocation-id": str(uuid.uuid4()),
        "amz-sdk-request": "attempt=1; max=3",
    }

    async with httpx.AsyncClient(timeout=15) as client:
        print(f"\n--- GetProfile ---")
        resp = await client.post(f"{q_host}/GetProfile", headers=headers, json={})
        print(f"Status: {resp.status_code}")
        if resp.status_code == 200:
            profile = resp.json()
            desktop_profile_arn = profile["profile"]["arn"]
            print(f"Desktop Auth Profile ARN: {desktop_profile_arn}")
            print(json.dumps(profile, indent=2))
        else:
            print(f"Error: {resp.text}")
            return

        # Step 3: Call GetUsageLimits with the desktop profile ARN
        print(f"\n--- GetUsageLimits (with Desktop Auth profile) ---")
        body = {
            "origin": "AI_EDITOR",
            "profileArn": desktop_profile_arn,
            "resourceType": "AGENTIC_REQUEST",
        }
        resp = await client.post(f"{q_host}/GetUsageLimits", headers=headers, json=body)
        print(f"Status: {resp.status_code}")
        print(f"Response: {resp.text[:500]}")

        # Also try with the IDE profile ARN from logs
        print(f"\n--- GetUsageLimits (with IDE profile from logs) ---")
        body2 = {
            "origin": "AI_EDITOR",
            "profileArn": "arn:aws:codewhisperer:us-east-1:713669222412:profile/7KHC74QYC9PQ",
            "resourceType": "AGENTIC_REQUEST",
        }
        resp = await client.post(f"{q_host}/GetUsageLimits", headers=headers, json=body2)
        print(f"Status: {resp.status_code}")
        print(f"Response: {resp.text[:500]}")

        # Try on codewhisperer host
        print(f"\n--- GetUsageLimits on codewhisperer host ---")
        cw_host = "https://codewhisperer.us-east-1.amazonaws.com"
        resp = await client.post(f"{cw_host}/GetUsageLimits", headers=headers, json=body)
        print(f"Status: {resp.status_code}")
        print(f"Response: {resp.text[:500]}")


if __name__ == "__main__":
    asyncio.run(main())

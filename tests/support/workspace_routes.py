"""Shared fixtures and fakes for focused integration checks."""

from fastapi import FastAPI


from fastapi.testclient import TestClient


from dashboard import register_dashboard


from support.dashboard import Manager, SETTINGS


from workspace_store import WorkspaceStore


BASE = "https://dashboard.example"


CONTACT = "local-12345678-1234-1234-1234-123456789abc"


AGENT = "agent-12345678-1234-1234-1234-123456789abc"


HEADERS = {"Origin": BASE, "X-Workspace-Request": "1"}


def contact(**changes):
    return {"id": CONTACT, "firstName": "Shane", "lastName": "McCarthy",
            "phone": "+19415551234", "email": "shane@example.com", "address": "Sarasota, FL",
            "website": "https://example.com", "company": "Example Studio",
            "createdAt": "2026-09-26T18:30:00Z", "status": "New", "labels": ["Customers"],
            "demo": changes.get("id", CONTACT).startswith("demo-"),
            **changes}


def agent(**changes):
    return {"id": AGENT, "name": "Reception", "prompt": "Ask how we can help.",
            "createdAt": "2026-09-26T18:30:00Z", **changes}


def client_for(store=None, base_url=BASE):
    app = FastAPI()
    register_dashboard(app, SETTINGS, Manager(), workspace_store=store)
    return TestClient(app, base_url=base_url)


def put_contact(client, value=None, **kwargs):
    value = contact() if value is None else value
    return client.put("/api/workspace/contacts/" + value["id"], json=value,
                      headers=HEADERS, **kwargs)


def put_agent(client, value=None):
    value = agent() if value is None else value
    return client.put("/api/workspace/agents/" + value["id"], json=value, headers=HEADERS)

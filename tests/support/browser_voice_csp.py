"""Shared fixtures and fakes for focused integration checks."""

from dataclasses import replace


from fastapi.testclient import TestClient


import pytest


from app import create_app


from config import Settings


BASE = Settings("AC" + "1" * 32, "offline-auth-token", "https://operator.example")


def directives(response):
    return {parts[0]: set(parts[1:]) for directive in response.headers["content-security-policy"].split(";")
            if (parts := directive.strip().split())}

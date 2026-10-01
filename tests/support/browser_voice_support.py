"""Shared fixtures and fakes for focused integration checks."""

from dataclasses import replace


from types import SimpleNamespace


import time


import jwt


import pytest


import config


from config import Settings


from operator_service.browser_voice import browser_voice_token


BASE = Settings("AC" + "1" * 32, "offline-auth-token", "https://operator.example")


SESSION_ID = "a" * 32


ENABLED = replace(BASE, browser_voice_enabled=True, api_key="SK" + "2" * 32,
                  api_secret="offline-api-secret-" * 3,
                  twilio_browser_app_sid="AP" + "3" * 32,
                  twilio_conference_app_sid="AP" + "4" * 32)

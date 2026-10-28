"""Outbound phone missions: pack, policy, and a CLI an agent can run unattended.

Phoney still places the Twilio call, transcribes with Deepgram, and speaks.
This package decides the destination, the brief, and whether the owner must
press 1. It does not open a phone call by itself.
"""

__all__ = ["__version__"]

__version__ = "1"

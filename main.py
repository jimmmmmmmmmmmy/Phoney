from app import create_app
from config import Settings
from operator_service import load_voice_settings
from voice_stack.settings import VoiceSettings
import logging
import os

settings = Settings.from_env()
# The operator speaks only when the voice layer is fully configured; a half-set
# environment leaves the keypad parsing and the bridge in human relay mode.
try:
    # Voice management can list/enroll voices before live speech is enabled or
    # any default voice has been chosen. Credentials still remain server-side.
    management_voice = VoiceSettings.from_env(environ={**os.environ, "VOICE_AGENT_ENABLED": "false"})
except ValueError:
    logging.getLogger("uvicorn.error").warning("agent_voice_management_unavailable")
    management_voice = None
app = create_app(settings, operator_voice=load_voice_settings(settings), agent_voice=management_voice)

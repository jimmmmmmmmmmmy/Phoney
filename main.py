from app import create_app
from config import Settings
from operator_service import load_voice_settings

settings = Settings.from_env()
# The operator speaks only when the voice layer is fully configured; a half-set
# environment leaves the keypad parsing and the bridge in human relay mode.
app = create_app(settings, operator_voice=load_voice_settings(settings))

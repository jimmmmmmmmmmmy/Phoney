"""Private system personalities; public agent edits cannot change these tasks."""
from agent_registry import AgentSnapshot
from voice_stack.prompts import AI_DETECTED_PROMPT, VOICEMAIL_PROMPT


def internal_snapshot(registry, kind):
    if kind not in {'ai-detected', 'voicemail'}:
        raise ValueError('Unknown internal agent')
    voices = registry.snapshot()['voices']
    owner = next((voice for voice in voices if voice['name'].casefold() == 'owner'
                  and voice.get('ready') and voice.get('available', True)
                  and not voice.get('requiresVerification')), None)
    if owner is None:
        raise ValueError('Owner voice unavailable')
    return AgentSnapshot(id='internal-' + kind,
        name='AI Call Assistant' if kind == 'ai-detected' else 'Voicemail Assistant',
        prompt=AI_DETECTED_PROMPT if kind == 'ai-detected' else VOICEMAIL_PROMPT,
        revision=1, voice_profile_id=owner['id'], voice_id=owner['voiceId'], slot=None)

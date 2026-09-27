"""Partner detection adapters; live wiring remains explicitly opt-in."""

from .analysis import build_analysis
from .live import LiveDetectionManager, LiveDetectionOutcome, LiveDetectionWorker
from .modulate import (DetectionObservation, DetectionReport, ModulateError,
                       detect_audio, stream_inbound_pcm)
from .policy import DetectionDecision, decide_call_detection

__all__ = ["DetectionDecision", "DetectionObservation", "DetectionReport",
           "LiveDetectionManager", "LiveDetectionOutcome", "LiveDetectionWorker",
           "build_analysis", "ModulateError", "decide_call_detection", "detect_audio", "stream_inbound_pcm"]

from .callback_agent import CallbackHealthAgent
from .call_quality_agent import CallQualityGuardianAgent
from .campaign_agent import CampaignHealthAgent
from .concurrency_agent import ConcurrencyHealthAgent
from .config_agent import ConfigHealthAgent
from .integration_agent import IntegrationHealthAgent
from .media_agent import MediaHealthAgent
from .rag_agent import RAGHealthAgent
from .scheduling_agent import SchedulingHealthAgent
from .smooth_calls_agent import SmoothCallsGuardianAgent

ALL_AGENTS = [
    SmoothCallsGuardianAgent(),
    ConfigHealthAgent(),
    ConcurrencyHealthAgent(),
    CallbackHealthAgent(),
    RAGHealthAgent(),
    MediaHealthAgent(),
    CampaignHealthAgent(),
    SchedulingHealthAgent(),
    IntegrationHealthAgent(),
    CallQualityGuardianAgent(),
]

__all__ = [
    "ALL_AGENTS",
    "CallbackHealthAgent",
    "CallQualityGuardianAgent",
    "CampaignHealthAgent",
    "ConcurrencyHealthAgent",
    "ConfigHealthAgent",
    "IntegrationHealthAgent",
    "MediaHealthAgent",
    "RAGHealthAgent",
    "SchedulingHealthAgent",
    "SmoothCallsGuardianAgent",
]

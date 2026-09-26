"""
FutBin Bot Services Package

This package contains modular services for the FutBin bot:
- http_client: Async HTTP client with retry logic
- price_service: Price fetching and parsing
- state_service: State management with TTL caching  
- task_orchestrator: Queue-based task coordination
- dashboard_service: Optimized dashboard rendering
- config_service: Configuration management with pydantic
"""

from .http_client import HttpClient, RetryConfig
from .price_service import PriceService, PlayerPrice
from .state_service import StateService, PlayerConfig, PlayerState, UserDashboard
from .task_orchestrator import TaskOrchestrator, TaskType, TaskRequest
from .dashboard_service import DashboardService
from .user_dashboard_service import UserDashboardService

# Config service import with fallback for missing pydantic
try:
    from .config_service import FutBinConfig, load_config, setup_logging, get_config
    HAS_PYDANTIC = True
except ImportError:
    HAS_PYDANTIC = False
    # Provide fallback classes
    class FutBinConfig:
        def __init__(self):
            self.discord = type('DiscordConfig', (), {'bot_token': None})()
    
    def load_config():
        raise RuntimeError("pydantic is required for config_service. Please install it with: pip install pydantic")
    
    def setup_logging(config):
        pass
    
    def get_config():
        raise RuntimeError("pydantic is required for config_service")


__all__ = [
    'HttpClient',
    'RetryConfig', 
    'PriceService',
    'PlayerPrice',
    'StateService',
    'PlayerConfig',
    'PlayerState',
    'UserDashboard',
    'TaskOrchestrator',
    'TaskType',
    'TaskRequest',
    'DashboardService',
    'UserDashboardService',
    'DashboardData',
    'FutBinConfig',
    'load_config',
    'setup_logging',
    'get_config',
    'HAS_PYDANTIC'
]
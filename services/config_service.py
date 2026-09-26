"""
Configuration management with pydantic settings and environment-based secrets.
Provides type-safe configuration loading and validation.
"""

import os
import logging
from pathlib import Path
from typing import Optional, Dict, Any
from pydantic import BaseModel, Field, field_validator, ConfigDict
from pydantic_settings import BaseSettings

# Load environment variables from .env file
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv not available, use system env vars only


class NetworkConfig(BaseSettings):
    """Network-related configuration"""
    user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    request_timeout: float = 12.0
    retry_count: int = 2
    max_concurrent_requests: int = 3
    sleep_between_requests: float = 1.0

    model_config = ConfigDict(env_prefix="FUTBIN_NET_", extra='ignore')


class TaskConfig(BaseSettings):
    """Task scheduling configuration"""
    dashboard_update_interval: int = 300  # 5 minutes
    price_check_interval: int = 280       # ~4.5 minutes  
    max_task_queue_size: int = 100
    task_timeout: float = 60.0

    model_config = ConfigDict(env_prefix="FUTBIN_TASK_", extra='ignore')


class DiscordConfig(BaseSettings):
    """Discord bot configuration"""
    bot_token: str = Field(..., env="DISCORD_BOT_TOKEN")
    dashboard_channel_id: Optional[int] = Field(None, env="DISCORD_DASHBOARD_CHANNEL_ID")
    temp_message_delete_after: int = 5
    command_response_delete_after: int = 5
    
    @field_validator('bot_token')
    @classmethod
    def validate_token(cls, v):
        if not v or len(v) < 50:
            raise ValueError("Discord bot token is required and must be valid")
        return v

    model_config = ConfigDict(env_prefix="DISCORD_", extra='ignore')


class LoggingConfig(BaseSettings):
    """Logging configuration"""
    level: str = Field("INFO", env="LOG_LEVEL")
    file_path: str = Field("logs/bot.log", env="LOG_FILE_PATH")
    max_file_size: int = Field(10 * 1024 * 1024, env="LOG_MAX_FILE_SIZE")  # 10MB
    backup_count: int = Field(5, env="LOG_BACKUP_COUNT")
    format: str = "[%(asctime)s] %(levelname)s [%(name)s]: %(message)s"
    
    @field_validator('level')
    @classmethod
    def validate_level(cls, v):
        valid_levels = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']
        v_upper = v.upper()
        if v_upper not in valid_levels:
            raise ValueError(f"Log level must be one of: {valid_levels}")
        return v_upper

    model_config = ConfigDict(env_prefix="LOG_", extra='ignore')


class HealthConfig(BaseSettings):
    """Health check configuration"""
    enabled: bool = Field(True, env="HEALTH_CHECK_ENABLED")
    port: int = Field(8080, env="PORT")
    bind_address: str = Field("0.0.0.0", env="HEALTH_BIND_ADDRESS")

    model_config = ConfigDict(extra='ignore')


class FutBinConfig(BaseSettings):
    """Main configuration class combining all components"""
    
    # Basic configuration
    base_dir: Path = Path.cwd()
    config_file: str = "config/bot_config.yaml"
    state_file: str = "data/player_state.json"
    
    # Trading configuration 
    global_threshold_percent: float = Field(5.0, env="FUTBIN_GLOBAL_THRESHOLD_PERCENT")
    platform: str = Field("pc", env="FUTBIN_PLATFORM")  # pc, ps, xbox
    
    # Embed colors (hex)
    color_up: int = 0x2ecc71       # Green
    color_down: int = 0xe74c3c     # Red
    color_neutral: int = 0x95a5a6  # Gray
    color_bot: int = 0xfa7100      # Orange
    
    # Component configurations
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    tasks: TaskConfig = Field(default_factory=TaskConfig)
    discord: DiscordConfig = Field(default_factory=DiscordConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    health: HealthConfig = Field(default_factory=HealthConfig)
    
    model_config = ConfigDict(extra='ignore')
    
    @property
    def config_path(self) -> Path:
        return self.base_dir / self.config_file
    
    @property
    def state_path(self) -> Path:
        return self.base_dir / self.state_file
    
    @property
    def log_path(self) -> Path:
        return self.base_dir / self.logging.file_path
    
    def ensure_directories(self):
        """Ensure all required directories exist"""
        directories = [
            self.config_path.parent,
            self.state_path.parent, 
            self.log_path.parent
        ]
        
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)
    
    def get_headers(self) -> Dict[str, str]:
        """Get HTTP headers for requests"""
        return {
            'User-Agent': self.network.user_agent,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
        }


# Global configuration instance
_config_instance: Optional[FutBinConfig] = None


def get_config() -> FutBinConfig:
    """Get or create the global configuration instance"""
    global _config_instance
    
    if _config_instance is None:
        _config_instance = FutBinConfig()
        _config_instance.ensure_directories()
        
        # Load settings from bot_config.yaml if present
        yaml_path = _config_instance.config_path
        if yaml_path.exists():
            try:
                import yaml
                with open(yaml_path, 'r', encoding='utf-8') as f:
                    yaml_data = yaml.safe_load(f) or {}
                settings = yaml_data.get('settings', {})
                if 'platform' in settings and not os.getenv('FUTBIN_PLATFORM'):
                    _config_instance.platform = str(settings['platform']).lower()
                if 'global_threshold_percent' in settings and not os.getenv('FUTBIN_GLOBAL_THRESHOLD_PERCENT'):
                    _config_instance.global_threshold_percent = float(settings['global_threshold_percent'])
                if 'dashboard_channel_id' in settings and not os.getenv('DISCORD_DASHBOARD_CHANNEL_ID'):
                    if settings['dashboard_channel_id']:
                        _config_instance.discord.dashboard_channel_id = int(settings['dashboard_channel_id'])
            except Exception as e:
                logging.warning(f"Could not load settings from {yaml_path}: {e}")
    
    return _config_instance


def setup_logging(config: Optional[FutBinConfig] = None) -> None:
    """Setup logging with the provided configuration"""
    if config is None:
        config = get_config()
    
    log_config = config.logging
    log_path = config.log_path
    
    # Create handlers
    handlers = [logging.StreamHandler()]
    
    if log_path:
        from logging.handlers import RotatingFileHandler
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=log_config.max_file_size,
            backupCount=log_config.backup_count
        )
        handlers.append(file_handler)
    
    # Configure logging
    logging.basicConfig(
        level=getattr(logging, log_config.level),
        format=log_config.format,
        handlers=handlers,
        force=True  # Override existing configuration
    )
    
    # Set discord.py to WARNING to reduce noise
    logging.getLogger('discord').setLevel(logging.WARNING)
    logging.getLogger('discord.http').setLevel(logging.WARNING)


def validate_config() -> Dict[str, Any]:
    """Validate configuration and return summary"""
    try:
        config = get_config()
        return {
            'status': 'valid',
            'discord_token_set': bool(config.discord.bot_token),
            'log_level': config.logging.level,
            'network_timeout': config.network.request_timeout,
            'task_intervals': {
                'dashboard': config.tasks.dashboard_update_interval,
                'price_check': config.tasks.price_check_interval
            }
        }
    except Exception as e:
        return {
            'status': 'invalid',
            'error': str(e)
        }


# Fallback configuration without pydantic 
class FallbackConfig:
    """Fallback configuration when pydantic is not available"""
    
    def __init__(self):
        self.discord_bot_token = os.getenv('DISCORD_BOT_TOKEN', '')
        self.dashboard_channel_id = os.getenv('DISCORD_DASHBOARD_CHANNEL_ID')
        self.log_level = os.getenv('LOG_LEVEL', 'INFO')
        self.request_timeout = float(os.getenv('FUTBIN_NET_REQUEST_TIMEOUT', '12.0'))
        self.retry_count = int(os.getenv('FUTBIN_NET_RETRY_COUNT', '2'))
        self.platform = os.getenv('FUTBIN_PLATFORM', 'pc').lower()
        self.global_threshold_percent = float(os.getenv('FUTBIN_GLOBAL_THRESHOLD_PERCENT', '5.0'))
        self.config_file = "config/bot_config.yaml"
        self.state_file = "data/player_state.json"
        
        # Load from bot_config.yaml if present
        yaml_path = Path(self.config_file)
        if yaml_path.exists():
            try:
                import yaml
                with open(yaml_path, 'r', encoding='utf-8') as f:
                    yaml_data = yaml.safe_load(f) or {}
                settings = yaml_data.get('settings', {})
                if 'platform' in settings and not os.getenv('FUTBIN_PLATFORM'):
                    self.platform = str(settings['platform']).lower()
                if 'global_threshold_percent' in settings and not os.getenv('FUTBIN_GLOBAL_THRESHOLD_PERCENT'):
                    self.global_threshold_percent = float(settings['global_threshold_percent'])
                if 'dashboard_channel_id' in settings and not os.getenv('DISCORD_DASHBOARD_CHANNEL_ID'):
                    if settings['dashboard_channel_id']:
                        self.dashboard_channel_id = int(settings['dashboard_channel_id'])
            except Exception:
                pass
        
        # Convert channel ID to int if present
        if self.dashboard_channel_id:
            try:
                self.dashboard_channel_id = int(self.dashboard_channel_id)
            except ValueError:
                self.dashboard_channel_id = None
    
    @property
    def config_path(self) -> Path:
        return Path(self.config_file)
    
    @property
    def state_path(self) -> Path:
        return Path(self.state_file)
    
    @property
    def network(self):
        return type('NetworkConfig', (), {
            'request_timeout': self.request_timeout,
            'retry_count': self.retry_count,
            'max_concurrent_requests': self.max_concurrent_requests,
        })()
    
    @property 
    def discord(self):
        return type('DiscordConfig', (), {
            'bot_token': self.discord_bot_token,
            'dashboard_channel_id': self.dashboard_channel_id,
        })()
    
    def get_headers(self) -> Dict[str, str]:
        """Get HTTP headers for requests"""
        return {
            'User-Agent': "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
        }


def load_config() -> FutBinConfig:
    """Load configuration with pydantic or fallback"""
    try:
        return get_config()
    except Exception as e:
        logging.warning(f"Failed to load pydantic config: {e}")
        # Use fallback instead of crashing
        return FallbackConfig()
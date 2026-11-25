from __future__ import annotations

from typing import Optional

from database import DatabaseManager
from app.shared.settings import Config

class Base:
    def __init__(
        self, 
        db_manager: Optional[DatabaseManager] = None
    ) -> None:
        self.config = Config.from_env()
        if db_manager:
            self.db_manager = db_manager
        else:
            database_url = (
                self.config.SQLALCHEMY_SYNC_DATABASE_URI
                or self.config.DATABASE_URL
            )
            if not database_url:
                raise ValueError("Database URL is not configured")
            
            self.db_manager = DatabaseManager(
                db_url=database_url,
                echo=True,
                pool_workers=self.config.POOL_WORKERS or 1,
            )
    
    @property
    def get_manager(self) -> DatabaseManager:
        return self.db_manager
    
    @property
    def get_config(self) -> Config:
        return self.config
    

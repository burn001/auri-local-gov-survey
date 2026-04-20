import os
from functools import lru_cache

class Settings:
    MONGODB_URI: str = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
    MONGODB_DB: str = os.getenv("MONGODB_DB", "auri_local_gov")
    TOKEN_SECRET: str = os.getenv("TOKEN_SECRET", "change-me-in-production")
    ADMIN_TOKEN_SECRET: str = os.getenv("ADMIN_TOKEN_SECRET", "change-me-admin-secret")
    CORS_ORIGINS: str = os.getenv("CORS_ORIGINS", "*")
    API_PREFIX: str = "/api"
    GMAIL_USER: str = os.getenv("GMAIL_USER", "")
    GMAIL_APP_PASSWORD: str = os.getenv("GMAIL_APP_PASSWORD", "")
    SURVEY_BASE_URL: str = os.getenv(
        "SURVEY_BASE_URL",
        "https://burn001.github.io/auri-local-gov-survey",
    )
    ADMIN_BASE_URL: str = os.getenv(
        "ADMIN_BASE_URL",
        "https://burn001.github.io/auri-local-gov-survey/admin",
    )

@lru_cache
def get_settings() -> Settings:
    return Settings()

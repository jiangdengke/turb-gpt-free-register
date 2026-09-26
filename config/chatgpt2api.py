"""Optional automatic import into the Oracle-hosted chatgpt2api Web pool."""
from config.env_loader import env_str, apply_env_overrides

# Disabled by default so existing registrations keep their current behavior.
ENABLE_CHATGPT2API_IMPORT: bool = False

# The registration service and chatgpt2api run on the same Oracle host.
CHATGPT2API_BASE_URL: str = "http://127.0.0.1:3001"
CHATGPT2API_MANAGEMENT_KEY: str = env_str("CHATGPT2API_MANAGEMENT_KEY", "")
CHATGPT2API_REQUEST_TIMEOUT: int = 60
CHATGPT2API_RETRY_DELAY: int = 15

# session_json imports the normalized ChatGPT Web /api/auth/session payload
# (access token plus user/account/expires metadata). oauth_pkce remains available
# as an explicit advanced mode when a renewable Web OAuth credential is needed.
CHATGPT2API_CREDENTIAL_MODE: str = "session_json"
CHATGPT2API_SYNC_AFTER_IMPORT: bool = True

apply_env_overrides(globals(), {
    "ENABLE_CHATGPT2API_IMPORT": "bool",
    "CHATGPT2API_BASE_URL": "str",
    "CHATGPT2API_MANAGEMENT_KEY": "str",
    "CHATGPT2API_REQUEST_TIMEOUT": "int",
    "CHATGPT2API_RETRY_DELAY": "int",
    "CHATGPT2API_CREDENTIAL_MODE": "str",
    "CHATGPT2API_SYNC_AFTER_IMPORT": "bool",
})

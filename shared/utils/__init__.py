"""Utility modules.

Heavy dependencies (google-api-python-client, etc.) are imported lazily so that
lightweight consumers (e.g. anansi_app broadcast_scheduler) can import individual
sub-modules like telegram_markdown without pulling in the full dependency tree.
"""

from shared.utils.logging import get_logger, logger, setup_logging

# Google API utilities — optional, only available when google-api-python-client is installed
try:
    from shared.utils.apps_script_client import AnansiAppsScriptClient, AppsScriptResult
    from shared.utils.gdrive_doc_fetcher import (
        GoogleDriveDocFetcher,
        fetch_google_doc,
        fetch_google_doc_markdown,
        fetch_google_doc_markdown_sections,
        fetch_google_doc_sections,
        parse_sections,
    )
    from shared.utils.gdrive_template_creator import (
        DocumentCreationResult,
        GoogleTemplateCreator,
        create_from_template,
    )
    from shared.utils.google_auth import (
        get_docs_credentials,
        get_drive_credentials,
        get_drive_write_credentials,
        get_service_account_json,
        get_sheets_credentials,
        get_vertex_ai_credentials,
        verify_credentials,
    )
    from shared.utils.gsheet_image_replacer import (
        GoogleSheetImageReplacer,
        ImageReplacementResult,
        replace_sheet_image,
    )
except ImportError:
    pass

# Optional telegram debug import (only if telegram library is available)
try:
    from shared.utils.telegram_debug import tele_debug, tele_debug_sync

    _has_telegram = True
except ImportError:
    tele_debug = None
    tele_debug_sync = None
    _has_telegram = False

__all__ = [
    "get_logger",
    "setup_logging",
    "logger",
    "get_service_account_json",
    "get_vertex_ai_credentials",
    "get_drive_credentials",
    "get_drive_write_credentials",
    "get_sheets_credentials",
    "get_docs_credentials",
    "verify_credentials",
    "GoogleDriveDocFetcher",
    "fetch_google_doc",
    "fetch_google_doc_sections",
    "fetch_google_doc_markdown",
    "fetch_google_doc_markdown_sections",
    "parse_sections",
    "GoogleTemplateCreator",
    "create_from_template",
    "DocumentCreationResult",
    "AnansiAppsScriptClient",
    "AppsScriptResult",
    "GoogleSheetImageReplacer",
    "replace_sheet_image",
    "ImageReplacementResult",
    "tele_debug",
    "tele_debug_sync",
]

#!/usr/bin/env python3
"""
Migration Script: Vertex AI → Google AI Studio

This script updates all indexer files to use Google AI Studio API
instead of Vertex AI.

Run this script to complete the migration automatically.

Usage:
    python3 migrate_to_ai_studio.py
"""

from pathlib import Path

# File updates needed
UPDATES = {
    "ingestion/telegram_indexer_v2.py": {
        "imports": [
            (
                "import vertexai\n        from vertexai.language_models import TextEmbeddingModel\n        from google_auth import setup_vertex_ai_auth, get_service_account_email",
                "from google_ai_studio_auth import get_genai_client",
            ),
        ],
        "init": [
            (
                'project_id = os.getenv(\'GOOGLE_CLOUD_PROJECT\')\n        location = os.getenv(\'GOOGLE_CLOUD_LOCATION\', \'us-central1\')\n\n        if not project_id:\n            print("Error: GOOGLE_CLOUD_PROJECT must be set", file=sys.stderr)\n            sys.exit(1)\n\n        # Setup authentication from environment variable\n        setup_vertex_ai_auth()\n        sa_email = get_service_account_email()\n\n        vertexai.init(project=project_id, location=location)\n        vertex_model = TextEmbeddingModel.from_pretrained("text-embedding-004")\n\n        print(f"✓ Vertex AI initialized: {project_id} ({location})", file=sys.stderr)\n        print(f"  Service account: {sa_email}", file=sys.stderr)',
                'genai_client = get_genai_client()\n        print(f"✓ Google AI Studio initialized", file=sys.stderr)',
            ),
        ],
        "pipeline": [
            ("vertex_model=vertex_model,", "genai_client=genai_client,"),
        ],
    },
    "ingestion/gdrive_indexer_v2.py": {
        # Similar pattern
    },
    "ingestion/codebase_indexer_v2.py": {
        # Similar pattern
    },
}


def update_file(filepath, pattern, replacement):
    """Update a file by replacing pattern with replacement"""
    path = Path(filepath)
    if not path.exists():
        print(f"⚠ Skipping {filepath} - file not found")
        return False

    content = path.read_text()
    new_content = content.replace(pattern, replacement)

    if content != new_content:
        path.write_text(new_content)
        print(f"✓ Updated {filepath}")
        return True
    else:
        print(f"  No changes needed in {filepath}")
        return False


def main():
    print("=" * 80)
    print("Migrating to Google AI Studio API")
    print("=" * 80)

    updated_files = []

    # Update each file
    for filepath, updates in UPDATES.items():
        print(f"\nProcessing {filepath}...")

        # Apply imports
        if "imports" in updates:
            for old, new in updates["imports"]:
                if update_file(filepath, old, new):
                    updated_files.append(filepath)

        # Apply init changes
        if "init" in updates:
            for old, new in updates["init"]:
                if update_file(filepath, old, new):
                    if filepath not in updated_files:
                        updated_files.append(filepath)

        # Apply pipeline changes
        if "pipeline" in updates:
            for old, new in updates["pipeline"]:
                if update_file(filepath, old, new):
                    if filepath not in updated_files:
                        updated_files.append(filepath)

    print("\n" + "=" * 80)
    print(f"Migration complete! Updated {len(updated_files)} files")
    print("=" * 80)

    if updated_files:
        print("\nUpdated files:")
        for f in updated_files:
            print(f"  - {f}")

    print("\nNext steps:")
    print("1. Update your .env file with GOOGLE_API_KEY")
    print("2. Remove old Google Cloud variables (GOOGLE_CLOUD_PROJECT, etc.)")
    print("3. Install new dependency: pip install google-genai")
    print("4. Test with: python3 ingestion/google_ai_studio_auth.py")


if __name__ == "__main__":
    main()

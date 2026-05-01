#!/usr/bin/env python3
"""
Generate SQL INSERT statements for Telegram access control.

This script reads the two JSON files (reducedchanswithtopics.json and
reducedchatswithtopics.json) and generates SQL INSERT statements for the
source_access_control table.

Usage:
    python generate_telegram_access_sql.py [--org-id ORG_ID]

    ORG_ID defaults to the STAFF_ORG_ID environment variable, then 2.
"""

import json
import os
import sys
from pathlib import Path
from typing import Set, Tuple


def extract_chat_topics(json_file: Path) -> Set[Tuple[str, str]]:
    """
    Extract all (chat_id, topic_id) combinations from a JSON file.

    Returns:
        Set of tuples (chat_id, topic_id_or_null)
        - For general messages: (chat_id, 'NULL')
        - For topic messages: (chat_id, topic_id)
    """
    print(f"Reading {json_file}...", file=sys.stderr)

    with open(json_file, "r") as f:
        data = json.load(f)

    chat_topics = set()

    # Process chats
    chats = data.get("chats", [])
    print(f"  Found {len(chats)} chats", file=sys.stderr)

    for chat in chats:
        chat_id = str(chat.get("id"))
        if not chat_id:
            continue

        # Track if we've seen any messages
        has_general = False
        topics_seen = set()

        # Process messages
        messages = chat.get("messages", [])

        for msg in messages:
            # Check if message has a topic
            topic = msg.get("topic", {})

            if topic and topic.get("id") is not None:
                # Message is in a specific topic
                topic_id = str(topic["id"])
                topics_seen.add(topic_id)
            else:
                # Message is in general chat (no topic)
                has_general = True

        # Add general chat access if we saw any general messages
        if has_general:
            chat_topics.add((chat_id, "NULL"))

        # Add each topic
        for topic_id in topics_seen:
            chat_topics.add((chat_id, topic_id))

    print(f"  Extracted {len(chat_topics)} chat+topic combinations", file=sys.stderr)
    return chat_topics


def generate_sql(chat_topics: Set[Tuple[str, str]], output_file: Path, org_id: int):
    """
    Generate SQL INSERT statements for the source_access_control table.
    """
    print(f"\nGenerating SQL to {output_file}...", file=sys.stderr)

    # Sort for consistent output
    sorted_chat_topics = sorted(
        chat_topics, key=lambda x: (int(x[0]) if x[0] != "NULL" else 0, x[1])
    )

    with open(output_file, "w") as f:
        f.write(
            f"""-- ============================================================================
-- Telegram Chat Access Control - Generated from JSON exports
-- ============================================================================
-- This file contains INSERT statements for all chat+topic combinations found
-- in reducedchanswithtopics.json and reducedchatswithtopics.json.
--
-- Organization ID: {org_id} (set via --org-id flag or STAFF_ORG_ID env var)
-- Default role_ids: {{}} (empty = all members have access)
--
-- IMPORTANT: This uses the explicit topic access model:
-- - scope_identifier = NULL: Access to General (non-topic) messages only
-- - scope_identifier = '<topic_id>': Access to that specific topic only
-- - Each topic requires its own row in the access control table
-- ============================================================================

-- ============================================================================
-- Insert Chat+Topic Access Rules
-- ============================================================================

INSERT INTO source_access_control (
    source_type,
    source_identifier,
    scope_identifier,
    organization_ids,
    role_ids
) VALUES
"""
        )

        # Generate INSERT VALUES
        for i, (chat_id, scope) in enumerate(sorted_chat_topics):
            # Format scope_identifier (NULL or 'topic_id')
            scope_value = "NULL" if scope == "NULL" else f"'{scope}'"

            # Add comma for all but last row
            comma = "," if i < len(sorted_chat_topics) - 1 else ""

            # Write the row (integer array, no cast needed)
            f.write(
                f"    ('telegram', '{chat_id}', {scope_value}, ARRAY[{org_id}], '{{}}'){comma}\n"
            )

        f.write(
            f"""\nON CONFLICT (source_type, source_identifier, scope_identifier) DO UPDATE
SET
    organization_ids = EXCLUDED.organization_ids,
    role_ids = EXCLUDED.role_ids,
    updated_at = NOW();

-- ============================================================================
-- Verify Insertion
-- ============================================================================

DO $$
DECLARE
    inserted_count INT;
    general_count INT;
    topic_count INT;
BEGIN
    SELECT COUNT(*) INTO inserted_count
    FROM source_access_control
    WHERE source_type = 'telegram';

    SELECT COUNT(*) INTO general_count
    FROM source_access_control
    WHERE source_type = 'telegram' AND scope_identifier IS NULL;

    SELECT COUNT(*) INTO topic_count
    FROM source_access_control
    WHERE source_type = 'telegram' AND scope_identifier IS NOT NULL;

    RAISE NOTICE '========================================';
    RAISE NOTICE 'Inserted/Updated % telegram access rules', inserted_count;
    RAISE NOTICE '  General chat rules: %', general_count;
    RAISE NOTICE '  Specific topic rules: %', topic_count;
    RAISE NOTICE 'Organization ID: {org_id}';
    RAISE NOTICE '========================================';
END;
$$;

-- ============================================================================
-- View Inserted Rules
-- ============================================================================

SELECT
    source_identifier as chat_id,
    COALESCE(scope_identifier, 'General') as topic,
    array_length(organization_ids, 1) as org_count,
    organization_ids,
    CASE
        WHEN array_length(role_ids, 1) IS NULL OR array_length(role_ids, 1) = 0
        THEN 'All members'
        ELSE array_to_string(role_ids, ', ')
    END as access
FROM source_access_control
WHERE source_type = 'telegram'
ORDER BY source_identifier, scope_identifier NULLS FIRST;
"""
        )

    print(f"✓ Generated {len(sorted_chat_topics)} INSERT rows", file=sys.stderr)


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--org-id",
        type=int,
        default=int(os.getenv("STAFF_ORG_ID", "2")),
        help="Organization ID to use in generated SQL (default: STAFF_ORG_ID env var or 2)",
    )
    args = parser.parse_args()
    org_id = args.org_id

    # Paths
    script_dir = Path(__file__).parent.parent / "ingestion" / "telegram_data_cleaning"

    file1 = script_dir / "reducedchanswithtopics.json"
    file2 = script_dir / "reducedchatswithtopics.json"
    output_file = Path(__file__).parent / "insert_telegram_chats_from_json.sql"

    # Check files exist
    if not file1.exists():
        print(f"✗ Error: {file1} not found", file=sys.stderr)
        sys.exit(1)

    if not file2.exists():
        print(f"✗ Error: {file2} not found", file=sys.stderr)
        sys.exit(1)

    print("=" * 80, file=sys.stderr)
    print("Generating Telegram Access Control SQL", file=sys.stderr)
    print(f"Organization ID: {org_id}", file=sys.stderr)
    print("=" * 80, file=sys.stderr)

    # Extract chat+topic combinations from both files
    all_chat_topics = set()

    all_chat_topics.update(extract_chat_topics(file1))
    all_chat_topics.update(extract_chat_topics(file2))

    print(f"\nTotal unique chat+topic combinations: {len(all_chat_topics)}", file=sys.stderr)

    # Count general vs topic-specific
    general_count = sum(1 for _, scope in all_chat_topics if scope == "NULL")
    topic_count = len(all_chat_topics) - general_count

    print(f"  General chat rules: {general_count}", file=sys.stderr)
    print(f"  Specific topic rules: {topic_count}", file=sys.stderr)

    # Generate SQL
    generate_sql(all_chat_topics, output_file, org_id)

    print("\n" + "=" * 80, file=sys.stderr)
    print(f"✓ Done! SQL written to: {output_file}", file=sys.stderr)
    print("=" * 80, file=sys.stderr)


if __name__ == "__main__":
    main()

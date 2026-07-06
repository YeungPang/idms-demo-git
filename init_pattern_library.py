#!/usr/bin/env python3
"""
Pattern Library Initialization Script
Initializes database schema and seeds initial semantic patterns
"""

import logging
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent))

from object_db import get_connection, create_tables
from pattern_library import PatternLibrary

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def initialize_pattern_library():
    """Initialize pattern library database schema and seed patterns"""
    
    try:
        logger.info("Connecting to database...")
        connection = get_connection()
        
        logger.info("Creating pattern library tables...")
        create_tables(connection, recreate=False)
        
        logger.info("Initializing pattern library...")
        pattern_lib = PatternLibrary(connection)
        
        logger.info("Seeding initial patterns...")
        pattern_lib.seed_initial_patterns()

        logger.info("Seeding default SOLF computation clauses...")
        pattern_lib.seed_default_solf_clauses()
        
        # Log pattern counts
        logger.info("Verifying seeded patterns...")
        all_patterns_en = pattern_lib.list_patterns(language="en")
        all_patterns_de = pattern_lib.list_patterns(language="de")
        seeded_en = [p for p in all_patterns_en if p.source_type == "seeded"]
        seeded_de = [p for p in all_patterns_de if p.source_type == "seeded"]
        logger.info(
            "Patterns by language -> en: %s (seeded: %s), de: %s (seeded: %s)",
            len(all_patterns_en),
            len(seeded_en),
            len(all_patterns_de),
            len(seeded_de),
        )
        seeded = seeded_en + seeded_de
        
        # List all patterns
        logger.info("=" * 60)
        logger.info("Seeded Semantic Patterns:")
        logger.info("=" * 60)
        for pattern in sorted(seeded, key=lambda p: (p.entity_class or "global", p.semantic_concept)):
            entity_scope = pattern.entity_class or "global"
            attrs = ", ".join(pattern.mapped_attributes.keys()) if pattern.mapped_attributes else "N/A"
            logger.info(
                f"  Pattern: '{pattern.pattern_text}' "
                f"-> Concept: '{pattern.semantic_concept}' "
                f"({entity_scope}) "
                f"[Attrs: {attrs}]"
            )
        
        logger.info("=" * 60)
        logger.info("Pattern library initialization complete!")
        connection.close()
        
    except Exception as e:
        logger.error(f"Failed to initialize pattern library: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    initialize_pattern_library()

#!/usr/bin/env python3
"""
Pattern Library Integration Test
Tests pattern matching, learning, and integration with QueryEngine
"""

import sys
import logging
from uuid import uuid4
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def test_pattern_library():
    """Test pattern library basic operations"""
    
    try:
        from object_db import get_connection
        from pattern_library import PatternLibrary
        
        logger.info("=== Pattern Library Integration Test ===")
        
        # Initialize pattern library
        logger.info("Connecting to database...")
        conn = get_connection()
        
        logger.info("Initializing pattern library...")
        pat_lib = PatternLibrary(conn)
        
        # Test pattern matching
        logger.info("\n[Test 1] Pattern Matching")
        test_queries = [
            ("how old is Alice", "person", "age"),
            ("how long did the project take", None, "duration"),
            ("how old was he when he started", "person", "age_at_event"),
            ("travel time from A to B", None, "travel_duration"),
            ("wie alt ist Alice", "person", "age"),
            ("vertriebszykluszeit fur opportunity alpha", None, "crm_opportunity_cycle_time"),
            ("vertriebszykluszeit für opportunity alpha", None, "crm_opportunity_cycle_time"),
        ]

        failures = []
        for query, entity_class, expected_concept in test_queries:
            language = "de" if any(token in query.lower() for token in ["wie ", "fur", "für", "vertriebs"]) else "en"
            match = pat_lib.match_pattern(query, entity_class, language)
            if match:
                logger.info(f"  ✓ '{query}' → concept: {match.semantic_concept} "
                           f"(confidence: {match.confidence:.2f})")
                if match.semantic_concept != expected_concept:
                    failures.append(f"Unexpected concept for '{query}': {match.semantic_concept} != {expected_concept}")
            else:
                logger.info(f"  ✗ '{query}' → no match")
                failures.append(f"No match for '{query}'")
        
        # Test pattern listing
        logger.info("\n[Test 2] List Seeded Patterns")
        seeded = pat_lib.list_patterns(source_type="seeded")
        logger.info(f"  Total seeded patterns: {len(seeded)}")
        
        by_concept = {}
        for p in seeded:
            concept = p.semantic_concept
            by_concept.setdefault(concept, []).append(p)
        
        for concept in sorted(by_concept.keys()):
            patterns = by_concept[concept]
            logger.info(f"  {concept}: {len(patterns)} pattern(s)")
            for p in patterns[:2]:
                scope = p.entity_class or "global"
                logger.info(f"    - '{p.pattern_text}' ({scope})")
        
        # Test synonym addition
        logger.info("\n[Test 3] Add A Synonym")
        if seeded:
            first_pattern = seeded[0]
            unique_synonym = f"how many years old {uuid4().hex[:8]}"
            added = pat_lib.add_synonym(
                pattern_id=first_pattern.pattern_id,
                synonym_text=unique_synonym,
                language="en",
                semantic_distance=0.1,
                match_type="semantic"
            )
            if added:
                logger.info(f"  ✓ Added synonym to pattern '{first_pattern.pattern_text}'")
            else:
                logger.info(f"  ✗ Failed to add synonym")
                failures.append("Failed to add unique synonym")
        
        # Test learning pattern
        logger.info("\n[Test 4] Learn A New Pattern")
        learned_id = pat_lib.learn_pattern(
            query_text="when was John born",
            semantic_concept="birth_date",
            mapped_attributes={"birth_date": "birth_date"},
            entity_class="person",
            confidence=0.85
        )
        if learned_id:
            logger.info(f"  ✓ Learned pattern ID {learned_id}")
        else:
            logger.info(f"  ✗ Failed to learn pattern")
            failures.append("Failed to learn pattern")

        if failures:
            for item in failures:
                logger.error("  - %s", item)
            raise AssertionError("Pattern library tests failed")
        
        logger.info("\n=== Tests Complete ===")
        
        conn.close()
        return True
        
    except Exception as e:
        logger.error(f"Test failed: {e}", exc_info=True)
        return False


def test_query_engine_integration():
    """Test QueryEngine integration with pattern library"""
    
    try:
        from query_engine import QueryEngine
        
        logger.info("\n=== QueryEngine Pattern Integration Test ===")
        
        logger.info("Initializing QueryEngine...")
        qe = QueryEngine()
        
        if not qe.pattern_library:
            logger.warning("Pattern library not available in QueryEngine")
            return False
        
        # Test parse_query with pattern matching
        logger.info("\n[Test 5] Parse Query With Pattern Matching")
        test_questions = [
            "how old is Chung Yeung Pang",
            "what is the duration of the project",
            "how long were they together",
            "wie alt ist max mustermann",
            "vertriebszykluszeit fur opportunity alpha",
            "vertriebszykluszeit für opportunity alpha",
            "Where did Dr. Pang get his Ph.D.?",
            "What degree does Chung Yeung Pang have?",
            "What is the qualitification of Chung Yeung Pang?",
        ]
        
        failures = []
        for question in test_questions:
            parsed = qe.parse_query(question)
            logger.info(f"  Q: '{question}'")
            logger.info(f"    → intent: {parsed.intent}, entity: {parsed.entity_name}, "
                       f"attr: {parsed.attribute_name}, confidence: {parsed.confidence:.2f}")
            if parsed.intent != "attribute_lookup":
                failures.append(f"Unexpected intent for '{question}': {parsed.intent}")
            if parsed.confidence < 0.80:
                failures.append(f"Low confidence for '{question}': {parsed.confidence}")

        if failures:
            for item in failures:
                logger.error("  - %s", item)
            raise AssertionError("QueryEngine integration tests failed")
        
        logger.info("\n=== QueryEngine Tests Complete ===")
        return True
        
    except Exception as e:
        logger.error(f"QueryEngine test failed: {e}", exc_info=True)
        return False


if __name__ == "__main__":
    success = True
    
    # Run tests
    if not test_pattern_library():
        success = False
    
    if not test_query_engine_integration():
        success = False
    
    sys.exit(0 if success else 1)

# Query Regression Run Report

- Generated (UTC): 20260705T184709Z
- API base URL: http://127.0.0.1:8000
- Query file: C:\Project\WebTech\python\IDMS-Demo\log\query_regression_golden_40_2026-07-05.txt
- max_steps: 4
- Total: 40
- Passed: 40
- Failed: 0

| # | Status | HTTP | ms | Source | Query |
|---:|---|---:|---:|---|---|
| 1 | PASS | 200 | 24632 | criteria_docs_sql | ﻿Show me all the themes of documents containing Chung Yeung Pang. |
| 2 | PASS | 200 | 7522 | sql_exact_composite | Show me the address of Chung Yeung Pang |
| 3 | PASS | 200 | 14238 | sql_profile_composite | Show me the full information of Chung Yeung Pang |
| 4 | PASS | 200 | 18018 | sql_profile_composite | Show me the full personal information of Chung Yeung Pang |
| 5 | PASS | 200 | 12921 | sql_profile_composite | Show me the full profile of Chung Yeung Pang |
| 6 | PASS | 200 | 24184 | table_cell_grounded | Show me the node for the profile of Chung Yeung Pang |
| 7 | PASS | 200 | 20300 | qdrant | Was ist der EORI-Nr. von Apotonix GmbH? |
| 8 | PASS | 200 | 2278 | sql_exact | Query "What day was Apotonix GmbH registered?" could not return the day of registration which should be in the Handelstregisterauszug |
| 9 | PASS | 200 | 17133 | criteria_docs_sql | List documents related to registration of Aphotonix GmbH with titles and file names |
| 10 | PASS | 200 | 9140 | sql_exact_composite | Which document mentioned Benjamin Pang? Please return the file name with the full path. |
| 11 | PASS | 200 | 15385 | criteria_docs_sql | Show me the document concerning Chung Yeung Pang |
| 12 | PASS | 200 | 14846 | criteria_docs_sql | Show me the documents concerning Chung Yeung Pang. |
| 13 | PASS | 200 | 15729 | criteria_semantic_qdrant | Show me the note concerning Chung Yeung Pang |
| 14 | PASS | 200 | 18368 | criteria_docs_sql | Show me the note titles and file names of all documents that have description of Chung Yeung Pang |
| 15 | PASS | 200 | 24849 | not_found | Show me the note cocerning Chung Yeung Pang |
| 16 | PASS | 200 | 15901 | criteria_semantic_qdrant | Show me the note concerning Chung Yeung Pang please |
| 17 | PASS | 200 | 16553 | criteria_contextual_llm | How much did the registration of Aphotonix GmbH cost? |
| 18 | PASS | 200 | 13434 | markdown_table | Um welche Tankstellen handelt es sich auf der Tankrechnung an Yeung Pang vom April? |
| 19 | PASS | 200 | 16244 | criteria_contextual_llm | What is the total amount due on invoice 112479923 for APHOTONIX GmbH? |
| 20 | PASS | 200 | 9021 | sql_aggregate | What is the total amount of travelling expenses of Seveco from April to May |
| 21 | PASS | 200 | 12425 | sql_exact | What is the total capital of Aphotonix GmbH? |
| 22 | PASS | 200 | 20783 | criteria_contextual_llm | How much did the registration of Aphotonix GmbH cost ? |
| 23 | PASS | 200 | 11199 | table_cell_grounded | In the Budget for Inaugural Forum, what was the Total expenditure of Catering? |
| 24 | PASS | 200 | 19948 | criteria_contextual_llm | What amount is listed as Total Amount Due for invoice 112479923? |
| 25 | PASS | 200 | 15196 | sql_exact | Wie lautet das Statutendatum im Handelsregisterauszug der Aphotonix GmbH? |
| 26 | PASS | 200 | 25228 | table_grounding_required | What is the Statute date in the Handelsregisterauszug of Aphotonix GmbH? |
| 27 | PASS | 200 | 3455 | sql_exact | Was ist der EORI-Nr. von Aphotonix GmbH? |
| 28 | PASS | 200 | 6881 | sql_exact_composite | Was ist der email fÃ¼r Kontakt fÃ¼r EORI-Ansprechpartner von Aphotonix GmbH? |
| 29 | PASS | 200 | 5760 | sql_exact_composite | Werr ist der EORI-Ansprechpartner von Aphotonix GmbH? |
| 30 | PASS | 200 | 7082 | sql_exact_composite | What are the name and email address of the contact person for the EORI registration from Aphotonix GmbH? |
| 31 | PASS | 200 | 1587 | sql_exact | What day was Aphotonix GmbH registered? |
| 32 | PASS | 200 | 17638 | semantic_llm_fallback | When was Aphotonix GmbH found? |
| 33 | PASS | 200 | 17199 | qdrant | What companies do B. Pang hold shares? |
| 34 | PASS | 200 | 5327 | sql_exact | who own Aphtonix GmbH |
| 35 | PASS | 200 | 20639 | qdrant | What companies does B. Pang hold shares? |
| 36 | PASS | 200 | 5914 | sql_exact_composite | who are the shareholders of Aphtonix GmbH |
| 37 | PASS | 200 | 4585 | sql_exact | Who own Seveco AG? |
| 38 | PASS | 200 | 21956 | qdrant | What companies does Chung Yeung Pang hold shares? |
| 39 | PASS | 200 | 13978 | sql_exact | shareholders of Aphtonix GmbH |
| 40 | PASS | 200 | 19775 | qdrant | What companies do Benjamin Kin Sing Pang hold shares? |

## Failed Cases

No failed queries.

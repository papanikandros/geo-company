"""Register side of the register + website pipeline (queue item 6).

``build``     — stage 0a–0c: bulk register files → uniform ``hr_companies`` / ``hr_names``
                parquet tables (one row per company / per name variant), never the portal.
``legalform`` — legal-form label parsed from a company name (shared by build, hygiene, match).
"""

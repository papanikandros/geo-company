"""Serving the merged table (queue item 7, serve-plan.md).

``build`` — S1: merged table → public flat parquet, nested full parquet (raw record of every
source per company), sites polygons, state × sector extracts, PMTiles, search index, manifest.
Later: ``api`` (S3), ``push`` (S4).
"""

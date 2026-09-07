"""Website side of the register + website pipeline (queue item 6).

``hygiene`` — stage 0d: URL normalisation and host classification on the merged table
(own site vs directory / social / parcel listing vs chain site), no network.
Later stages: ``impressum`` (1), ``discover`` (3).
"""

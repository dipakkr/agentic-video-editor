# Music library

Only **licensed / royalty-free** tracks belong here. Each track is described by a JSON
metadata file (BPM, energy curve, genre, license) that the Music & Beat Agent reads when
auto-picking. **Never** add audio scraped from third-party platforms — copyright safety is
non-negotiable.

Audio files themselves are git-ignored (see root `.gitignore`); commit only the `.json`
metadata. The seed script (`scripts/seed.py`) can synthesize silent placeholder audio for
these entries so the pipeline is runnable offline without shipping copyrighted material.

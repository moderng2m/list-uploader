"""Default admin-editable settings (SPEC §14.4). Seeded into the Config table."""

THRESHOLDS: dict[str, float] = {
    "mapping_suggest_threshold": 0.75,
    "lead_source_auto_threshold": 0.90,
    "junk_flag_threshold": 0.70,
    "junk_block_threshold": 0.90,
}

LIMITS: dict[str, int] = {
    "max_file_bytes": 10 * 1024 * 1024,
    "max_rows": 5000,
    "send_max_concurrency": 5,
    "review_expiry_days": 14,
}

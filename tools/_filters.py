"""
Shared filtering helpers used by multiple tool modules.
"""

from shopify_client import to_gid


def filter_variant_targets(variant_ids, variants):
    """
    Resolve caller-supplied `variant_ids` against a product's `variants` list.
    Single pass preserves caller order on both sides and dedupes both sides.
    Returns (targets, unresolved):
      - targets: list[variant dict] in caller's order, unknown ids skipped
      - unresolved: list[str] of ids the caller passed that weren't found
    When variant_ids is falsy, returns (list(variants), []) — default-to-all.
    """
    variants = variants or []
    if not variant_ids:
        return list(variants), []

    by_gid = {v["id"]: v for v in variants}
    targets = []
    seen_target_gids: set = set()
    unresolved: list[str] = []
    seen_unresolved: set = set()
    for vid in variant_ids:
        gid = to_gid("ProductVariant", vid)
        if gid in by_gid:
            if gid not in seen_target_gids:
                targets.append(by_gid[gid])
                seen_target_gids.add(gid)
        elif vid not in seen_unresolved:
            unresolved.append(vid)
            seen_unresolved.add(vid)
    return targets, unresolved

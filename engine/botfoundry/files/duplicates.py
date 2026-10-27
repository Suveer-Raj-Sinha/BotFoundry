"""Group files with identical content. Pure data work: it never touches the disk and never deletes
anything; it only SUGGESTS which copy to keep."""
from collections import defaultdict
from typing import Any

KEEP_STRATEGIES = ("oldest", "newest", "shortest_path", "first")


class NotHashed(ValueError):
    pass


def _keeper(group: list[dict[str, Any]], strategy: str) -> dict[str, Any]:
    if strategy == "first":
        return group[0]
    if strategy == "shortest_path":
        return min(group, key=lambda i: (len(i["path"]), i["path"].casefold(), i["path"]))
    stamp = lambda i: i.get("modified") or ""  # noqa: E731  (ISO timestamps sort correctly as text)
    pick = min if strategy == "oldest" else max
    best = pick(stamp(i) for i in group)
    tied = [i for i in group if stamp(i) == best]
    return min(tied, key=lambda i: (len(i["path"]), i["path"].casefold(), i["path"]))


def find_duplicates(items: list[dict[str, Any]], keep: str = "oldest", ignore_empty: bool = True) -> dict[str, Any]:
    if keep not in KEEP_STRATEGIES:
        raise ValueError(f"keep must be one of {KEEP_STRATEGIES}")
    if any("hash" not in item for item in items):
        raise NotHashed("Some files have no 'hash' field: connect them through the File Hash node first.")
    buckets: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        if item["hash"] is None or (ignore_empty and item["size"] == 0):
            continue
        buckets[(item["hash"], item["size"])].append(item)
    groups = []
    for (digest, size), members in buckets.items():
        if len(members) < 2:
            continue
        keeper = _keeper(members, keep)
        extras = [m for m in members if m is not keeper]
        groups.append({"hash": digest, "size": size, "count": len(members), "keep": keeper,
                       "duplicates": extras, "wasted_bytes": size * len(extras)})
    groups.sort(key=lambda g: (-g["wasted_bytes"], g["hash"]))
    flat = [d for g in groups for d in g["duplicates"]]
    return {"groups": groups, "duplicates": flat,
            "stats": {"groups": len(groups), "duplicate_files": len(flat),
                      "wasted_bytes": sum(g["wasted_bytes"] for g in groups), "files_examined": len(items)}}

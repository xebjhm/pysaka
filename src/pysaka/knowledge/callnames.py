from __future__ import annotations

from .models import CanonicalId
from .registry import normalize_name


class CallNameTable:
    """Directional caller -> callee nickname lookup, built from curated `call_names.json`.

    Unlike `AliasTable` (name -> any member who might be meant), this table records
    *who addresses whom by what name*: a single caller may use the same alias that,
    for another caller, means someone else entirely (e.g. two members sharing a
    given-name reading). This directional evidence is what lets `MentionDetector`
    resolve an otherwise-ambiguous alias hit to the specific person the author is
    known to call by that name, rather than falling back to the alias's full,
    ambiguous candidate set.
    """

    def __init__(self) -> None:
        self._by_caller: dict[CanonicalId, dict[str, set[CanonicalId]]] = {}

    @classmethod
    def from_json(cls, data: dict) -> CallNameTable:
        """Build a table from `call_names.json` payload data.

        `data` shape: `{"edges": [{"caller_id": ..., "caller_name": ..., "callee_id": ...,
        "callee_name": ..., "names": [...]}, ...], "notes": [...]}`. Each edge records
        that `caller_id` addresses `callee_id` using every name in `names`. Names are
        normalized with `registry.normalize_name` (same normalization used everywhere
        else in `pysaka.knowledge`) so lookups are consistent regardless of whitespace
        variants. `"notes"` is curator-facing documentation and is ignored here.
        """
        table = cls()
        for edge in data.get("edges", []):
            caller_id = edge["caller_id"]
            callee_id = edge["callee_id"]
            by_name = table._by_caller.setdefault(caller_id, {})
            for name in edge.get("names", []):
                by_name.setdefault(normalize_name(name), set()).add(callee_id)
        return table

    def directional(self, caller_id: CanonicalId, normalized_name: str) -> set[CanonicalId]:
        """Callee id(s) `caller_id` addresses using `normalized_name`.

        `normalized_name` must already be normalized (callers pass the same
        `normalize_name(alias)` value used to build the table). Empty set if
        `caller_id` has no curated call-name entry for that name.
        """
        return set(self._by_caller.get(caller_id, {}).get(normalized_name, ()))

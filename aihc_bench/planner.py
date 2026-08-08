from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional


def select_next(commits: List[Dict[str, Any]], terminal_attempts: Iterable[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not commits:
        return None
    measured = {attempt["commit_sha"] for attempt in terminal_attempts}
    unmeasured = [commit for commit in commits if commit["sha"] not in measured]
    if not unmeasured:
        return None

    head = commits[-1]
    if head["sha"] not in measured:
        return head

    measured_ordinals = [commit["ordinal"] for commit in commits if commit["sha"] in measured]
    return max(
        unmeasured,
        key=lambda commit: (
            min(abs(commit["ordinal"] - ordinal) for ordinal in measured_ordinals),
            commit["ordinal"],
        ),
    )

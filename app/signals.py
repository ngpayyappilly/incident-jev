"""What Jev told us, reduced to the two shapes the decision code needs. Stdlib only."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Union

SEP = "__"  # question name = f"{group}{SEP}{question}"


@dataclass(frozen=True)
class NoulSig:
    p: float


@dataclass(frozen=True)
class ChoiceSig:
    choice: str
    confidence: float


Signals = dict  # group -> {question: NoulSig | ChoiceSig}


def absorb(signals: Signals, response) -> set[str]:
    """Merge an SDK response into signals. Returns the question names that were answered."""
    answered: set[str] = set()
    for name, a in response.nouls.items():
        if SEP in name:
            group, q = name.split(SEP, 1)
            signals.setdefault(group, {})[q] = NoulSig(float(a.noul))
            answered.add(name)
    for name, a in response.choices.items():
        if SEP in name:
            group, q = name.split(SEP, 1)
            signals.setdefault(group, {})[q] = ChoiceSig(str(a.choice), float(a.confidence))
            answered.add(name)
    return answered

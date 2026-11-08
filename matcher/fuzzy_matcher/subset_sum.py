"""
Exact bounded subset-sum search on integer amounts (paise).

find_subsets(amounts, target, tol, min_size, max_size) returns every index set whose
amounts sum to `target` within +/- tol and whose size is in [min_size, max_size].

Why not itertools.combinations on a heuristically trimmed pool:
  * trimming by "closeness to target/k" silently drops legitimate unequal parts
    (5200 = 5000 + 120 + 80), and
  * brute force is O(C(n, k)) per size.

How it works (all amounts must be positive integers):
  1. Items larger than target + tol can never be part of a solution -> discarded.
  2. Remaining items are sorted ascending. A depth-first search picks the first
     size-2 items; the last two items are found with a binary search over the
     sorted suffix.
  3. Two exact bounds prune the DFS:
       - cheapest completion (the next smallest items) already above target + tol
         -> stop scanning this level (ascending order makes `break` valid);
       - most generous completion (the largest items) still below target - tol
         -> skip this item.
  4. Node and solution budgets guarantee termination on pathological input
     (e.g. 60 identical amounts). `exhaustive` tells the caller if a budget fired.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from typing import List, Sequence, Tuple


@dataclass
class SubsetSearch:
    solutions: List[Tuple[int, ...]]
    exhaustive: bool
    nodes: int


def find_subsets(
    amounts: Sequence[int],
    target: int,
    tol: int = 0,
    min_size: int = 2,
    max_size: int = 6,
    max_nodes: int = 200_000,
    max_solutions: int = 500
) -> SubsetSearch:
    if min_size < 2:
        raise ValueError("min_size must be >=2")
    
    lo_t, hi_t = target - tol, target + tol
    items = sorted((a, i) for i, a in enumerate(amounts) if 0 < a <= hi_t)
    n = len(items)
    amt = [a for a, _ in items]
    orig = [i for _, i in items]
    ps = [0]
    
    for a in amt:
        ps.append(ps[-1] + a)
        
    found: List[Tuple[int, ...]] = []
    state = {
        "nodes": 0, 
        "stopped": False
    }
    
    def over_budget() -> bool:
        if state["nodes"] > max_nodes or len(found) >= max_solutions:
            state["stopped"] = True
        return state["stopped"]
    
    def finish_pair(start: int, partial: int, chosen: List[int]) -> None:
        lo_p, hi_p = lo_t - partial, hi_t - partial
        for a in range(start, n - 1):
            state["nodes"] += 1
            if over_budget():
                return
            
            if amt[a] + amt[a + 1] > hi_p:
                break
            
            b_lo = bisect_left(amt, lo_p - amt[a], a + 1, n)
            b_hi = bisect_right(amt, hi_p - amt[a], a + 1, n)
            
            for b in range(b_lo, b_hi):
                found.append(
                    tuple(sorted(orig[k] for k in (*chosen, a, b)))
                )
                if over_budget():
                    return
                
    def dfs(start: int, depth: int, partial: int, r: int, chosen: List[int]) -> None:
        if depth == r - 2:
            finish_pair(start, partial, chosen)
            return
        
        need = r - depth - 1    # items still needed after the one picked here
        for p in range(start, n - need):
            state["nodes"] += 1
            if over_budget():
                return
            
            if partial + ps[p + need + 1] - ps[p] > hi_t:   # cheapest possible completion too big
                break
            
            if partial + amt[p] + (ps[n] - ps[n - need]) < lo_t:  # even the biggest completion too small
                continue
            
            chosen.append(p)
            dfs(p + 1, depth + 1, partial + amt[p], r, chosen)
            chosen.pop()
            if state["stopped"]:
                return
            
    for r in range(min_size, min(max_size, n) + 1):
        dfs(0, 0, 0, r, [])
        if state["stopped"]:
            break
        
    found.sort(key=lambda t: (len(t), t))
    return SubsetSearch(found, not state["stopped"], state["nodes"])


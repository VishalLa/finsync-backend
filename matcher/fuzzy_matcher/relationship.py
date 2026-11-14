"""One-to-many, many-to-one, and many-to-many reconciliation strategies."""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import date
from functools import partial
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Tuple

from .subset_sum import find_subsets
from ..utils.helper import (
    MAX_COMBINATION_POOL_SIZE,
    _amounts_equal,
    _closest_candidates,
    _safe_str,
    _to_date,
    text_similarity,
    extract_utr,
)

MatchBuilder = Callable[[], Tuple[Dict[str, Any], Any]]
MatchEdge = Tuple[float, Tuple[int, ...], Tuple[int, ...], MatchBuilder]


def _cents(value: float) -> int:
    return int(round(float(value) * 100))


def _shared_reference(ledger: Any, bank: Any) -> bool:
    ledger_ref = _safe_str(ledger, "reference_id").strip().upper()
    bank_ref = _safe_str(bank, "txn_id").strip().upper()
    if ledger_ref and bank_ref and ledger_ref == bank_ref:
        return True
    ledger_utr = extract_utr(ledger_ref)
    bank_utr = extract_utr(bank_ref) or extract_utr(_safe_str(bank, "narration"))
    return bool(ledger_utr and bank_utr and ledger_utr == bank_utr)


def _enumerate_sums(
    amounts: List[int], max_size: int, budget: int
) -> Tuple[List[Tuple[int, Tuple[int, ...]]], int, bool]:
    """Return (sum, index tuple) rows and nodes consumed, stopping at budget."""
    rows: List[Tuple[int, Tuple[int, ...]]] = []
    nodes = 0
    stopped = False

    def visit(start: int, chosen: List[int], total: int) -> None:
        nonlocal nodes, stopped
        if stopped:
            return
        nodes += 1
        if nodes > budget:
            stopped = True
            return
        if 2 <= len(chosen) <= max_size:
            rows.append((total, tuple(chosen)))
        if len(chosen) == max_size:
            return
        for idx in range(start, len(amounts)):
            chosen.append(idx)
            visit(idx + 1, chosen, total + amounts[idx])
            chosen.pop()
            if stopped:
                return

    visit(0, [], 0)
    return rows, nodes, not stopped


def _minimal(
    ledger_amounts: Tuple[int, ...], bank_amounts: Tuple[int, ...], tol: int
) -> bool:
    """True if no proper two-sided partition balances on both sides."""
    l_total, b_total = sum(ledger_amounts), sum(bank_amounts)
    l_sums: Dict[int, List[int]] = defaultdict(list)
    for mask in range(1, (1 << len(ledger_amounts)) - 1):
        size = mask.bit_count()
        if size < 2 or len(ledger_amounts) - size < 2:
            continue
        subtotal = sum(v for i, v in enumerate(ledger_amounts) if mask & (1 << i))
        l_sums[subtotal].append(mask)
    for mask in range(1, (1 << len(bank_amounts)) - 1):
        size = mask.bit_count()
        if size < 2 or len(bank_amounts) - size < 2:
            continue
        subtotal = sum(v for i, v in enumerate(bank_amounts) if mask & (1 << i))
        for lsum in range(subtotal - tol, subtotal + tol + 1):
            for lmask in l_sums.get(lsum, ()):
                lrest, brest = l_total - lsum, b_total - subtotal
                if abs(lrest - brest) <= tol:
                    return False
    return True




def _proposal(
    matcher: Any,
    gls: List[Any],
    bks: List[Any],
    candidate: Dict[str, Any],
    reason: str,
    alternatives: List[Dict[str, Any]],
) -> Dict[str, Any]:
    def components(
        indices: Tuple[int, ...],
        records: List[Any],
        amount_of: Callable[[Any], float],
        id_attr: str,
    ) -> List[Dict[str, Any]]:
        return [
            {"id": getattr(records[index], id_attr, None), "amount": amount_of(records[index])}
            for index in indices
        ]

    return {
        "candidate_id": "",
        "ledger_components": components(
            candidate["l"], gls,
            lambda record: matcher.gl_out(record) or matcher.gl_in(record),
            "ledger_id",
        ),
        "bank_components": components(
            candidate["b"], bks,
            lambda record: matcher.bank_out(record) or matcher.bank_in(record),
            "row_index",
        ),
        "ledger_total": candidate["l_total"] / 100,
        "bank_total": candidate["b_total"] / 100,
        "difference": candidate["diff"] / 100,
        "date_span_bd": candidate["span"],
        "score": candidate["score"],
        "reason": reason,
        "alternatives": [
            {
                "ledger_ids": [getattr(gls[i], "ledger_id", None) for i in item["l"]],
                "bank_ids": [getattr(bks[j], "row_index", None) for j in item["b"]],
                "score": item["score"],
            }
            for item in alternatives
        ],
    }

class Relationship:
    """Run group relationship strategies against a FuzzyMatcher's residual pools.

    The fuzzy matcher owns record accessors, calendar policy, selected matches,
    and pool state. This class owns the group matching strategies.
    """

    MAX_COMBINATION_SIZE = 6
    COMBO_EARLY_BD = 2
    MAX_GROUP_SIZE = 10
    M2M_POOL_CAP = 16
    M2M_MAX_NODES = 300_000
    M2M_MAX_SOLUTIONS = 200
    M2M_MAX_SPAN_BD = 10
    M2M_EARLY_BD = 2
    M2M_ACCEPT = 0.60
    M2M_REVIEW = 0.40
    M2M_AMBIGUITY_MARGIN = 0.15
    M2M_LARGE_GROUP_NEEDS_REF = 7

    def __init__(self, fuzzy_matcher: Any) -> None:
        object.__setattr__(self, "matcher", fuzzy_matcher)

    def __getattr__(self, name: str) -> Any:
        """Share the fuzzy matcher's record accessors and run state."""
        return getattr(self.matcher, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "matcher":
            object.__setattr__(self, name, value)
        else:
            setattr(self.matcher, name, value)

    def _combos_with_sum(
        self, valid: List[Tuple[Any, float]], target: float, size: int
    ) -> Iterator[Tuple[Any, float]]:
        """Yield positive-amount combinations within the configured tolerance."""
        amounts = [item[1] for item in valid]
        ceiling = target + self._tol
        chosen: List[int] = []

        def visit(start: int, remaining: int, total: float):
            if remaining == 0:
                if _amounts_equal(target, total, self._tol):
                    yield tuple(valid[index] for index in chosen)
                return

            last_start = len(valid) - remaining
            for index in range(start, last_start + 1):
                next_total = total + amounts[index]
                if next_total > ceiling:
                    continue
                chosen.append(index)
                yield from visit(index + 1, remaining - 1, next_total)
                chosen.pop()

        yield from visit(0, size, 0.0)

    @staticmethod
    def _build_aggregated(
        ledger: Any,
        banks: List[Any],
        amounts: List[float],
        ledger_amount: float,
    ) -> Tuple[Dict[str, Any], None]:
        bank_ids = " & ".join(str(getattr(bank, "row_index", "?")) for bank in banks)
        amount_details = " + ".join(f"{amount:.2f}" for amount in amounts)
        return ({
            "ledger_id": _safe_str(ledger, "ledger_id"),
            "bank_id": bank_ids,
            "adjustment_type": f"Aggregated Split Transaction ({len(banks)} items)",
            "confidence_score": "High",
            "details": (
                f"Ledger ({ledger_amount:.2f}) = sum of {len(banks)} bank entries "
                f"({amount_details})."
            ),
        }, None)

    @staticmethod
    def _build_many_to_one(
        ledgers: List[Any],
        bank: Any,
        amounts: List[float],
        bank_amount: float,
    ) -> Tuple[Dict[str, Any], None]:
        ledger_ids = " & ".join(_safe_str(ledger, "ledger_id") for ledger in ledgers)
        amount_details = " + ".join(f"{amount:.2f}" for amount in amounts)
        return ({
            "ledger_id": ledger_ids,
            "bank_id": getattr(bank, "row_index", None),
            "adjustment_type": f"Many-to-One Aggregation ({len(ledgers)} items)",
            "confidence_score": "High",
            "details": (
                f"Bank ({bank_amount:.2f}) = sum of {len(ledgers)} ledger entries "
                f"({amount_details})."
            ),
        }, None)

    def _match_aggregations(self, anchor_is_ledger: bool) -> None:
        """Match one row against a bounded subset on the other side."""
        base_window = 3
        ledgers = list(self.ledger_pool)
        banks = list(self.bank_pool)
        ledger_dates = self._dates(ledgers, "transaction_date")
        bank_dates = self._dates(banks, "date")

        anchors = ledgers if anchor_is_ledger else banks
        members = banks if anchor_is_ledger else ledgers
        anchor_dates = ledger_dates if anchor_is_ledger else bank_dates
        member_dates = bank_dates if anchor_is_ledger else ledger_dates
        anchor_out, anchor_in = (
            (self.gl_out, self.gl_in) if anchor_is_ledger
            else (self.bank_out, self.bank_in)
        )
        member_out, member_in = (
            (self.bank_out, self.bank_in) if anchor_is_ledger
            else (self.gl_out, self.gl_in)
        )
        anchor_text_attr = "account_name" if anchor_is_ledger else "narration"
        member_text_attr = "narration" if anchor_is_ledger else "account_name"

        member_order = sorted(
            (member_date.toordinal(), index)
            for index, member_date in enumerate(member_dates)
            if member_date is not None
        )
        member_ordinals = [ordinal for ordinal, _index in member_order]
        used_anchors: Set[int] = set()
        used_members: Set[int] = set()
        used_ledger = used_anchors if anchor_is_ledger else used_members
        used_bank = used_members if anchor_is_ledger else used_anchors
        accepted_all = []
        active = [i for i, item_date in enumerate(anchor_dates) if item_date is not None]

        while active:
            edges = []
            anchors_with_edge: Set[int] = set()
            truncated_anchors: Set[int] = set()

            for anchor_index in active:
                anchor = anchors[anchor_index]
                is_outgoing = anchor_out(anchor) > 0
                anchor_amount = anchor_out(anchor) if is_outgoing else anchor_in(anchor)
                if anchor_amount <= 0:
                    continue

                member_amount = member_out if is_outgoing else member_in
                widest_window = base_window + self._CROSS_MONTH_BUFFER
                early_window = self.COMBO_EARLY_BD if self.ALLOW_BANK_BEFORE_LEDGER else 0
                if anchor_is_ledger:
                    lower_date, upper_date = self._cal.date_bounds(
                        anchor_dates[anchor_index], -early_window, widest_window
                    )
                else:
                    lower_date, upper_date = self._cal.date_bounds(
                        anchor_dates[anchor_index], -widest_window, early_window
                    )

                first = bisect_left(member_ordinals, lower_date.toordinal())
                last = bisect_right(member_ordinals, upper_date.toordinal())
                candidate_indices = sorted(member_order[pos][1] for pos in range(first, last))
                valid, member_index_by_identity = [], {}

                for member_index in candidate_indices:
                    member = members[member_index]
                    if member_index in used_members or member_dates[member_index] is None:
                        continue
                    amount = member_amount(member)
                    if amount <= 0:
                        continue

                    ledger_date, bank_date = (
                        (anchor_dates[anchor_index], member_dates[member_index])
                        if anchor_is_ledger
                        else (member_dates[member_index], anchor_dates[anchor_index])
                    )
                    lag = self._lag(ledger_date, bank_date)
                    if -early_window <= lag <= self._win(base_window, ledger_date, bank_date):
                        valid.append((member, amount))
                        member_index_by_identity[id(member)] = member_index

                was_truncated = len(valid) > MAX_COMBINATION_POOL_SIZE
                valid = _closest_candidates(
                    valid=valid,
                    target=anchor_amount,
                    max_pool=MAX_COMBINATION_POOL_SIZE,
                    counterpart_text=_safe_str(anchor, anchor_text_attr),
                    text_of=lambda record: _safe_str(record, member_text_attr),
                )
                if was_truncated:
                    truncated_anchors.add(anchor_index)

                similarity_cache = {}

                def similarity_for(member):
                    identity = id(member)
                    if identity not in similarity_cache:
                        if anchor_is_ledger:
                            similarity_cache[identity] = text_similarity(
                                _safe_str(anchor, "account_name"),
                                _safe_str(member, "narration"),
                            )
                        else:
                            similarity_cache[identity] = text_similarity(
                                _safe_str(member, "account_name"),
                                _safe_str(anchor, "narration"),
                            )
                    return similarity_cache[identity]

                search = find_subsets(
                    [int(round(amount * 100)) for _record, amount in valid],
                    int(round(anchor_amount * 100)),
                    int(round(self._tol * 100)),
                    min_size=2,
                    max_size=self.MAX_COMBINATION_SIZE,
                    max_nodes=self.SUBSET_MAX_NODES,
                    max_solutions=self.SUBSET_MAX_SOLUTIONS,
                )
                best = None
                for selection in search.solutions:
                    combination = tuple(valid[index] for index in selection)
                    # Preserve the pre-refactor aggregate similarity gate.
                    aggregate_similarity = similarity_for(
                        (record for record, _amount in combination)
                    )
                    similarities = [aggregate_similarity]
                    if max(similarities) <= 0.15:
                        continue
                    score = sum(similarities) / len(combination) - 0.02 * (len(combination) - 2)
                    if best is None or score > best[0] + 1e-12:
                        best = (score, combination)

                if best is None:
                    continue
                score, combination = best
                member_indices = tuple(sorted(
                    member_index_by_identity[id(record)] for record, _amount in combination
                ))
                records = [record for record, _amount in combination]
                amounts = [amount for _record, amount in combination]

                if anchor_is_ledger:
                    build = partial(self._build_aggregated, anchor, records, amounts, anchor_amount)
                    edges.append((score, (anchor_index,), member_indices, build))
                else:
                    build = partial(self._build_many_to_one, records, anchor, amounts, anchor_amount)
                    edges.append((score, member_indices, (anchor_index,), build))
                anchors_with_edge.add(anchor_index)

            accepted = self._select(edges, used_ledger, used_bank, ledgers, banks)
            accepted_all.extend(accepted)
            if not accepted:
                break

            matched_anchors = {
                (ledger_indices if anchor_is_ledger else bank_indices)[0]
                for ledger_indices, bank_indices, _build in accepted
            }
            blocked_anchors = anchors_with_edge - matched_anchors
            active = sorted((blocked_anchors | truncated_anchors) - matched_anchors)

        self._emit(accepted_all, ledgers, banks)
        self._rebuild_pools(ledgers, banks, used_ledger, used_bank)

    def match_one_to_many(self) -> None:
        """Match one ledger row to multiple bank rows."""
        self._match_aggregations(anchor_is_ledger=True)

    def match_many_to_one(self) -> None:
        """Match multiple ledger rows to one bank row."""
        self._match_aggregations(anchor_is_ledger=False)

    def match_many_to_many(self) -> Dict[str, Any]:
        """Search, score, review, and assign residual many-to-many groups."""
        gls, bks = list(self.ledger_pool), list(self.bank_pool)
        stats: Dict[str, int] = {
            "m2m_components_skipped": 0,
            "m2m_components_searched": 0,
            "m2m_budget_exhaustions": 0,
            "m2m_accepted_matches": 0,
            "m2m_ledger_rows_consumed": 0,
            "m2m_bank_rows_consumed": 0,
            "m2m_proposed_candidates": 0,
        }
        proposals: List[Dict[str, Any]] = []
        edges: List[MatchEdge] = []
        gl_dates: List[Optional[date]] = [
            _to_date(_safe_str(record, "transaction_date")) for record in gls
        ]
        bk_dates: List[Optional[date]] = [
            _to_date(_safe_str(record, "date")) for record in bks
        ]

        def gl_side(record: Any) -> Tuple[Optional[bool], float]:
            outgoing_amount = self.gl_out(record)
            if outgoing_amount > 0:
                return True, outgoing_amount
            incoming_amount = self.gl_in(record)
            if incoming_amount > 0:
                return False, incoming_amount
            return None, 0.0

        def bk_side(record: Any) -> Tuple[Optional[bool], float]:
            outgoing_amount = self.bank_out(record)
            if outgoing_amount > 0:
                return True, outgoing_amount
            incoming_amount = self.bank_in(record)
            if incoming_amount > 0:
                return False, incoming_amount
            return None, 0.0

        ref_equal = _shared_reference

        # Build the evidence-gated bipartite graph.
        adjacency: Dict[Tuple[str, int], Set[Tuple[str, int]]] = defaultdict(set)
        for i, gl in enumerate(gls):
            gs, ga = gl_side(gl)
            if ga <= 0 or gl_dates[i] is None:
                continue
            for j, bk in enumerate(bks):
                bs, ba = bk_side(bk)
                if ba <= 0 or bs != gs or bk_dates[j] is None:
                    continue
                lag = self._lag(gl_dates[i], bk_dates[j])
                if lag < -self.M2M_EARLY_BD or lag > self.M2M_MAX_SPAN_BD:
                    continue
                ref = ref_equal(gl, bk)
                sim = text_similarity(_safe_str(gl, "account_name"), _safe_str(bk, "narration"))
                if not ref and sim < 0.35:
                    continue
                ledger_node, bank_node = ("l", i), ("b", j)
                adjacency[ledger_node].add(bank_node)
                adjacency[bank_node].add(ledger_node)

        # Connected components in stable record-id order.
        def key_order(node: Tuple[str, int]) -> Tuple[str, str]:
            if node[0] == "l":
                identifier = getattr(gls[node[1]], "ledger_id", "")
            else:
                identifier = getattr(bks[node[1]], "row_index", "")
            return node[0], str(identifier)

        components: List[Set[Tuple[str, int]]] = []
        unseen = set(adjacency)
        while unseen:
            root = min(unseen, key=key_order)
            queue: List[Tuple[str, int]] = [root]
            found: Set[Tuple[str, int]] = set()
            unseen.remove(root)
            while queue:
                node = queue.pop()
                found.add(node)
                for nxt in adjacency[node]:
                    if nxt in unseen:
                        unseen.remove(nxt)
                        queue.append(nxt)
            components.append(found)

        # Split large components into date-proximate windows; skip anything still too big.
        bounded: List[Tuple[List[int], List[int]]] = []
        for comp in components:
            lidx = [n[1] for n in comp if n[0] == "l"]
            bidx = [n[1] for n in comp if n[0] == "b"]
            if len(lidx) <= self.M2M_POOL_CAP and len(bidx) <= self.M2M_POOL_CAP:
                bounded.append((lidx, bidx))
                continue
            ordered = sorted(
                comp,
                key=lambda node: (
                    gl_dates[node[1]] if node[0] == "l" else bk_dates[node[1]],
                    key_order(node),
                ),
            )
            chunks: List[Set[Tuple[str, int]]] = []
            current: Set[Tuple[str, int]] = set()
            ledger_count = bank_count = 0
            for node in ordered:
                is_l = node[0] == "l"
                if (
                    ledger_count + is_l > self.M2M_POOL_CAP
                    or bank_count + (not is_l) > self.M2M_POOL_CAP
                ) and current:
                    chunks.append(current)
                    current = set()
                    ledger_count = bank_count = 0
                current.add(node)
                ledger_count += int(is_l)
                bank_count += int(not is_l)
            if current:
                chunks.append(current)
            usable_chunks = 0
            for chunk in chunks:
                li = [n[1] for n in chunk if n[0] == "l"]
                bi = [n[1] for n in chunk if n[0] == "b"]
                if 2 <= len(li) and 2 <= len(bi):
                    bounded.append((li, bi))
                    usable_chunks += 1
            oversized_chunk = any(
                sum(node[0] == "l" for node in chunk) > self.M2M_POOL_CAP
                or sum(node[0] == "b" for node in chunk) > self.M2M_POOL_CAP
                for chunk in chunks
            )
            if oversized_chunk:
                stats["m2m_components_skipped"] += 1
            elif usable_chunks == 0:
                stats["m2m_components_skipped"] += 1

        candidates: List[Dict[str, Any]] = []
        tol_cents = max(0, _cents(self._tol))
        for li, bi in bounded:
            li = sorted(li, key=lambda i: str(getattr(gls[i], "ledger_id", "")))
            bi = sorted(bi, key=lambda j: str(getattr(bks[j], "row_index", "")))
            if len(li) < 2 or len(bi) < 2:
                continue
            stats["m2m_components_searched"] += 1
            la = [_cents(gl_side(gls[i])[1]) for i in li]
            ba = [_cents(bk_side(bks[j])[1]) for j in bi]
            # Whole component shortcut is scored alongside enumerated alternatives.
            lrows, ln, lex = _enumerate_sums(la, self.MAX_GROUP_SIZE, self.M2M_MAX_NODES)
            remaining = self.M2M_MAX_NODES - ln
            brows, bn, bex = _enumerate_sums(ba, self.MAX_GROUP_SIZE, max(0, remaining))
            exhausted = not (lex and bex)
            banks_by_size: Dict[int, List[Tuple[int, Tuple[int, ...]]]] = defaultdict(list)
            for row in brows:
                banks_by_size[len(row[1])].append(row)
            bank_indexes: Dict[
                int,
                Tuple[List[int], List[Tuple[int, Tuple[int, ...]]]],
            ] = {}
            for size, rows in banks_by_size.items():
                rows.sort(key=lambda row: row[0])
                bank_indexes[size] = ([row[0] for row in rows], rows)
            ledgers_by_size: Dict[int, List[Tuple[int, Tuple[int, ...]]]] = defaultdict(list)
            for row in lrows:
                ledgers_by_size[len(row[1])].append(row)
            ordered_pairs: List[Tuple[Tuple[int, ...], Tuple[int, ...]]] = []
            join_budget = max(0, self.M2M_MAX_NODES - ln - bn)
            join_nodes = 0
            join_stopped = False
            for total_count in range(4, 2 * self.MAX_GROUP_SIZE + 1):
                for lsize in range(2, self.MAX_GROUP_SIZE + 1):
                    bsize = total_count - lsize
                    if not 2 <= bsize <= self.MAX_GROUP_SIZE or bsize not in bank_indexes:
                        continue
                    bank_sums, bank_rows = bank_indexes[bsize]
                    ledger_rows = ledgers_by_size.get(lsize, ())
                    if len(ledger_rows) <= len(bank_rows):
                        for lsum, lsel in ledger_rows:
                            join_nodes += 1
                            if join_nodes > join_budget:
                                join_stopped = True
                                break
                            lo = bisect_left(bank_sums, lsum - tol_cents)
                            hi = bisect_right(bank_sums, lsum + tol_cents)
                            for pos in range(lo, hi):
                                join_nodes += 1
                                if join_nodes > join_budget or len(ordered_pairs) >= self.M2M_MAX_SOLUTIONS:
                                    join_stopped = True
                                    break
                                ordered_pairs.append((lsel, bank_rows[pos][1]))
                            if join_stopped:
                                break
                    else:
                        ledger_sums = sorted(row[0] for row in ledger_rows)
                        ledger_index = sorted(ledger_rows, key=lambda row: row[0])
                        for bsum, bsel in bank_rows:
                            join_nodes += 1
                            if join_nodes > join_budget:
                                join_stopped = True
                                break
                            lo = bisect_left(ledger_sums, bsum - tol_cents)
                            hi = bisect_right(ledger_sums, bsum + tol_cents)
                            for pos in range(lo, hi):
                                join_nodes += 1
                                if join_nodes > join_budget or len(ordered_pairs) >= self.M2M_MAX_SOLUTIONS:
                                    join_stopped = True
                                    break
                                ordered_pairs.append((ledger_index[pos][1], bsel))
                            if join_stopped:
                                break
                    if join_stopped:
                        break
                if join_stopped:
                    break
            whole_component = (tuple(range(len(li))), tuple(range(len(bi))))
            whole_component_balances = (
                2 <= len(li) <= self.MAX_GROUP_SIZE
                and 2 <= len(bi) <= self.MAX_GROUP_SIZE
                and abs(sum(la) - sum(ba)) <= tol_cents
            )
            if whole_component_balances and whole_component not in ordered_pairs:
                if len(ordered_pairs) < self.M2M_MAX_SOLUTIONS:
                    ordered_pairs.append(whole_component)
                else:
                    ordered_pairs[-1] = whole_component
            if join_stopped:
                exhausted = True
            if exhausted:
                stats["m2m_budget_exhaustions"] += 1

            scored: List[Dict[str, Any]] = []
            for ls, bs in ordered_pairs:
                if not 2 <= len(ls) <= self.MAX_GROUP_SIZE:
                    continue
                if not 2 <= len(bs) <= self.MAX_GROUP_SIZE:
                    continue

                lsel = [li[index] for index in ls]
                bsel = [bi[index] for index in bs]
                dates = [gl_dates[index] for index in lsel]
                dates.extend(bk_dates[index] for index in bsel)
                if any(item_date is None for item_date in dates):
                    continue
                earliest, latest = min(dates), max(dates)
                span = abs(self._lag(earliest, latest))
                latest_ledger = max(gl_dates[i] for i in lsel)
                bank_too_early = any(
                    self._lag(latest_ledger, bk_dates[index]) < -self.M2M_EARLY_BD
                    for index in bsel
                )
                if span > self.M2M_MAX_SPAN_BD or bank_too_early:
                    continue

                l_amounts = tuple(la[index] for index in ls)
                b_amounts = tuple(ba[index] for index in bs)
                diff = abs(sum(l_amounts) - sum(b_amounts))
                if not _minimal(l_amounts, b_amounts, tol_cents):
                    continue

                refs = any(
                    ref_equal(gls[ledger_index], bks[bank_index])
                    for ledger_index in lsel
                    for bank_index in bsel
                )
                similarities = [
                    text_similarity(
                        _safe_str(gls[ledger_index], "account_name"),
                        _safe_str(bks[bank_index], "narration"),
                    )
                    for ledger_index in lsel
                    for bank_index in bsel
                ]
                mean_sim = sum(similarities) / len(similarities)
                max_sim = max(similarities)
                if not refs and not (mean_sim >= 0.35 and max_sim >= 0.6):
                    continue

                compact = 1.0 - min(1.0, span / max(1, self.M2M_MAX_SPAN_BD))
                if tol_cents == 0:
                    residual = 1.0 if diff == 0 else 0.0
                else:
                    residual = max(0.0, 1.0 - diff / tol_cents)
                parsimony = max(
                    0.0,
                    1.0 - (len(ls) + len(bs) - 4) / max(1, 2 * self.MAX_GROUP_SIZE - 4),
                )
                score = (
                    0.35 * float(refs)
                    + 0.30 * mean_sim
                    + 0.15 * compact
                    + 0.10 * residual
                    + 0.10 * parsimony
                )
                scored.append({
                    "l": tuple(lsel),
                    "b": tuple(bsel),
                    "score": score,
                    "diff": diff,
                    "span": span,
                    "refs": refs,
                    "l_total": sum(l_amounts),
                    "b_total": sum(b_amounts),
                    "exhausted": exhausted,
                })

            def candidate_tie_key(candidate: Dict[str, Any]) -> Tuple[Any, ...]:
                ledger_ids = tuple(
                    str(getattr(gls[index], "ledger_id", ""))
                    for index in candidate["l"]
                )
                bank_ids = tuple(
                    str(getattr(bks[index], "row_index", ""))
                    for index in candidate["b"]
                )
                return -candidate["score"], len(candidate["l"]) + len(candidate["b"]), ledger_ids, bank_ids

            scored.sort(key=candidate_tie_key)
            candidates.extend(scored[:self.M2M_MAX_SOLUTIONS])

        # Collapse overlapping near-ties into one human-review item per conflict cluster.
        def conflict_tie_key(candidate: Dict[str, Any]) -> Tuple[Any, ...]:
            ledger_ids = tuple(
                str(getattr(gls[index], "ledger_id", ""))
                for index in candidate["l"]
            )
            bank_ids = tuple(
                str(getattr(bks[index], "row_index", ""))
                for index in candidate["b"]
            )
            return -candidate["score"], ledger_ids, bank_ids

        candidates.sort(key=conflict_tie_key)
        conflict_graph: Dict[int, Set[int]] = defaultdict(set)
        for i, cand in enumerate(candidates):
            for j in range(i + 1, len(candidates)):
                other = candidates[j]
                shares_ledger = bool(set(cand["l"]) & set(other["l"]))
                shares_bank = bool(set(cand["b"]) & set(other["b"]))
                close_scores = (
                    abs(cand["score"] - other["score"])
                    <= self.M2M_AMBIGUITY_MARGIN
                )
                if (shares_ledger or shares_bank) and close_scores:
                    conflict_graph[i].add(j)
                    conflict_graph[j].add(i)
        unseen_conflicts = set(conflict_graph)
        while unseen_conflicts:
            root = min(unseen_conflicts)
            queue: List[int] = [root]
            group_indices: Set[int] = set()
            unseen_conflicts.remove(root)
            while queue:
                idx = queue.pop()
                group_indices.add(idx)
                for nxt in conflict_graph[idx]:
                    if nxt in unseen_conflicts:
                        unseen_conflicts.remove(nxt)
                        queue.append(nxt)
            group = [candidates[i] for i in sorted(group_indices)]
            proposals.append(_proposal(self, gls, bks, group[0], "ambiguous", group[1:]))
            for candidate in group:
                candidate["reviewed"] = True

        eligible: List[Dict[str, Any]] = []
        for c in candidates:
            if c.get("reviewed"):
                continue
            size = len(c["l"]) + len(c["b"])
            large_group_without_reference = (
                size >= self.M2M_LARGE_GROUP_NEEDS_REF and not c["refs"]
            )
            can_accept = (
                c["score"] >= self.M2M_ACCEPT
                and not c["exhausted"]
                and not large_group_without_reference
            )
            if can_accept:
                eligible.append(c)
            elif c["score"] >= self.M2M_REVIEW:
                if c["exhausted"]:
                    reason = "budget_exhausted"
                elif c["score"] >= self.M2M_ACCEPT and large_group_without_reference:
                    reason = "large_group"
                else:
                    reason = "below_accept"
                proposals.append(_proposal(self, gls, bks, c, reason, []))
        edge_rows: List[MatchEdge] = []
        for c in eligible:
            def build(candidate: Dict[str, Any] = c) -> Tuple[Dict[str, Any], None]:
                ledger_group = [gls[index] for index in candidate["l"]]
                bank_group = [bks[index] for index in candidate["b"]]
                match: Dict[str, Any] = {
                    "ledger_id": " & ".join(
                        str(getattr(record, "ledger_id", ""))
                        for record in ledger_group
                    ),
                    "bank_id": " & ".join(
                        str(getattr(record, "row_index", ""))
                        for record in bank_group
                    ),
                    "ledger_components": [
                        {
                            "id": getattr(record, "ledger_id", None),
                            "ledger_id": getattr(record, "ledger_id", None),
                            "amount": gl_side(record)[1],
                        }
                        for record in ledger_group
                    ],
                    "bank_components": [
                        {
                            "id": getattr(record, "row_index", None),
                            "bank_id": getattr(record, "row_index", None),
                            "amount": bk_side(record)[1],
                        }
                        for record in bank_group
                    ],
                    "ledger_total": candidate["l_total"] / 100,
                    "bank_total": candidate["b_total"] / 100,
                    "difference": candidate["diff"] / 100,
                    "date_span_bd": candidate["span"],
                    "score": candidate["score"],
                    "adjustment_type": (
                        f"Many-to-Many Group ({len(ledger_group)}x{len(bank_group)})"
                    ),
                    "confidence_score": candidate["score"],
                    "details": (
                        "Residual multi-entry group balanced within configured "
                        "amount tolerance."
                    ),
                }
                return match, None

            edge_rows.append((c["score"], c["l"], c["b"], build))

        used_l: Set[int] = set()
        used_b: Set[int] = set()
        accepted = self._select(edge_rows, used_l, used_b, gls, bks)
        self._emit(accepted, gls, bks)
        self._rebuild_pools(gls, bks, used_l, used_b)
        for number, proposal in enumerate(proposals, 1):
            proposal["candidate_id"] = f"m2m-{number:04d}"
        stats.update(
            m2m_accepted_matches=len(accepted),
            m2m_ledger_rows_consumed=len(used_l),
            m2m_bank_rows_consumed=len(used_b),
            m2m_proposed_candidates=len(proposals),
        )
        self.proposed_matches = proposals
        self.m2m_stats = stats
        return {"PROPOSED_MATCHES": proposals, "M2M_STATS": stats}

    def run_aggregations(self) -> None:
        self.match_one_to_many()
        self.match_many_to_one()

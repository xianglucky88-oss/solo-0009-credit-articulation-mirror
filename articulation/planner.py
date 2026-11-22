"""Exact lexicographic planner for one intake batch.

A *ruling* attributes eligible course attempts to requirement items.  It is
feasible when:

* every attribution targets an item that accepts the attempt's mapped target;
* an item takes exactly ``courses`` attributions and their combined credit
  reaches the item's ``hundredths`` (a partly filled item is not achieved and
  therefore receives no attributions);
* an attempt fills at most one item per programme and at most two programmes
  in total;
* when an attempt serves two programmes, the sharing scope allows that pair
  and the attempt's credit does not exceed the scope cap;
* one attempt per retake group is used.

The objective is lexicographic: maximise the number of achieved mandatory
items, then maximise recognised credits (an attempt shared by two programmes
is counted once).
"""
from functools import lru_cache
from itertools import combinations

from .requirements import shares

MAX_ATTEMPTS = 16
MAX_ITEMS = 20
MAX_COURSES = 4
MAX_COMBOS = 2000
MAX_WORK = 8000000


class PlanError(ValueError):
    """Raised when a batch exceeds the exact planner's supported size."""


class Problem:
    """Immutable solver input built from one batch."""

    def __init__(self, attempts, items, share):
        if len(attempts) > MAX_ATTEMPTS or len(items) > MAX_ITEMS:
            raise PlanError('batch exceeds exact planner size')
        if any(item['courses'] > MAX_COURSES for item in items):
            raise PlanError('requirement item exceeds exact planner size')
        self.attempts = list(attempts)
        self.items = list(items)
        self.share = share
        self.credits = [attempt['credits'] for attempt in self.attempts]
        groups = {attempt['group'] for attempt in self.attempts}
        order = {group: bit for bit, group in enumerate(sorted(groups))}
        self.group_bit = [1 << order[attempt['group']] for attempt in self.attempts]
        self.accepts = [
            [index for index, item in enumerate(self.items)
             if attempt['target'] in item['accepts']]
            for attempt in self.attempts]
        self.accepted_by = [
            [attempt_index for attempt_index, choices in enumerate(self.accepts)
             if item_index in choices]
            for item_index in range(len(self.items))]

    def solve(self, required=frozenset(), forbidden=frozenset(), budget=None):
        """Return ``((mandatory, credits), witness)`` or ``(None, None)``.

        ``required`` and ``forbidden`` hold ``(attempt_index, item_index)``
        pairs.  A witness is a tuple of the selected pairs, one per
        attribution, and it is ``None`` when no ruling is feasible.  ``budget``
        is an optional shared node allowance; exceeding it raises
        :class:`PlanError` instead of running unbounded.
        """
        required, forbidden = frozenset(required), frozenset(forbidden)
        if required & forbidden:
            return None, None
        required_by, forbidden_by = {}, {}
        for attempt_index, item_index in required:
            if not 0 <= attempt_index < len(self.attempts):
                return None, None
            if item_index not in self.accepts[attempt_index]:
                return None, None
            required_by.setdefault(item_index, set()).add(attempt_index)
        for attempt_index, item_index in forbidden:
            if 0 <= attempt_index < len(self.attempts) and item_index in self.accepts[attempt_index]:
                forbidden_by.setdefault(item_index, set()).add(attempt_index)

        combos = []
        for item_index, item in enumerate(self.items):
            demand = required_by.get(item_index, set())
            banned = forbidden_by.get(item_index, set())
            if len(demand) > item['courses']:
                return None, None
            choices = [()] if not demand else []
            for combo in combinations(self.accepted_by[item_index], item['courses']):
                if not demand.issubset(combo) or banned.intersection(combo):
                    continue
                if sum(self.credits[index] for index in combo) < item['hundredths']:
                    continue
                bits = 0
                for index in combo:
                    if bits & self.group_bit[index]:
                        break
                    bits |= self.group_bit[index]
                else:
                    choices.append(combo)
            if demand and not choices:
                return None, None
            if len(choices) > MAX_COMBOS:
                raise PlanError('a requirement item admits too many course combinations')
            combos.append(choices)

        # Items with the fewest completions go first so dead ends surface early.
        order = sorted(range(len(self.items)), key=lambda i: len(combos[i]))
        sequence = [(index, combos[index]) for index in order]
        meter = budget if budget is not None else [MAX_WORK]

        @lru_cache(maxsize=None)
        def search(position, used_mask, group_mask, slots):
            if position == len(sequence):
                total, mask = 0, used_mask
                while mask:
                    low = mask & -mask
                    total += self.credits[low.bit_length() - 1]
                    mask ^= low
                return (0, total), ()
            index, choices = sequence[position]
            meter[0] -= len(choices)
            if meter[0] < 0:
                raise PlanError('batch search exceeded the exact planner budget')
            item = self.items[index]
            best_value = None
            best_witness = None
            for combo in choices:
                if combo:
                    new_mask, new_groups = used_mask, group_mask
                    updated = list(slots)
                    valid = True
                    for attempt_index in combo:
                        already = bool(new_mask >> attempt_index & 1)
                        if not already and group_mask & self.group_bit[attempt_index]:
                            valid = False
                            break
                        held = updated[attempt_index]
                        if item['program'] in held or len(held) >= 2:
                            valid = False
                            break
                        if held and not shares(self.share, held[0], item['program'],
                                               self.credits[attempt_index]):
                            valid = False
                            break
                        updated[attempt_index] = held + (item['program'],)
                        new_mask |= 1 << attempt_index
                        new_groups |= self.group_bit[attempt_index]
                    if not valid:
                        continue
                    following = tuple(updated)
                else:
                    new_mask, new_groups, following = used_mask, group_mask, slots
                value, witness = search(position + 1, new_mask, new_groups, following)
                if value is None:
                    continue
                if combo and item['kind'] == 'mandatory':
                    value = (value[0] + 1, value[1])
                if best_value is None or value > best_value:
                    best_value = value
                    chosen = tuple((attempt_index, index) for attempt_index in combo)
                    best_witness = chosen + witness
            return best_value, best_witness

        result = search(0, 0, 0, ((),) * len(self.attempts))
        search.cache_clear()
        return result


def possible(problem, allowed):
    """Every attribution over the allowed attempts."""
    return [(attempt_index, item_index)
            for attempt_index in allowed
            for item_index in problem.accepts[attempt_index]]


def analyse(problem, available=None, required=frozenset(), forbidden=frozenset(),
            budget=None):
    """Compute optimum, common/possible attributions and one witness."""
    available = frozenset(range(len(problem.attempts))) if available is None else frozenset(available)
    required, forbidden = frozenset(required), frozenset(forbidden)
    meter = budget if budget is not None else [MAX_WORK]
    best, witness = problem.solve(required, forbidden, meter)
    if best is None:
        return {'objective': None, 'witness': None, 'possible': [], 'common': []}
    pool = [pair for pair in possible(problem, available)
            if pair not in forbidden and pair not in required]
    can, must = list(required), []
    for pair in sorted(pool):
        if problem.solve(required | {pair}, forbidden, meter)[0] == best:
            can.append(pair)
    for pair in sorted(pool):
        without = problem.solve(required, forbidden | {pair}, meter)[0]
        if without is None or without < best:
            must.append(pair)
    must.extend(required)
    return {'objective': best, 'witness': witness,
            'possible': sorted(can), 'common': sorted(set(must))}


def foreclosed(problem, objective, chosen, available=None,
               required=frozenset(), forbidden=frozenset(), budget=None):
    """Equally optimal attributions a chosen ruling makes unsignable.

    Only attributions that could join an optimal ruling *before* the choice are
    reported; attributions that were never optimal are not "equally optimal
    alternatives" and are therefore omitted.
    """
    available = frozenset(range(len(problem.attempts))) if available is None else frozenset(available)
    required, forbidden = frozenset(required), frozenset(forbidden)
    meter = budget if budget is not None else [MAX_WORK]
    before = []
    for pair in sorted(possible(problem, available)):
        if pair in required or pair in forbidden:
            continue
        if problem.solve(required | {pair}, forbidden, meter)[0] == objective:
            before.append(pair)
    after = required | frozenset(chosen)
    result = []
    for pair in before:
        if pair in after:
            continue
        value = problem.solve(after | {pair}, forbidden, meter)[0]
        if value != objective:
            result.append(pair)
    return result

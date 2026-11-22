"""Compare branch catalog coverage without comparing quantities or changing records."""
from collections import Counter


# Filters shared by the Difference Monitor API and its PDF/Excel exports.
DIFFERENCE_FILTERS = ('all', 'missing_from_target', 'missing_from_branch', 'needs_review')


def normalized_name(value):
    """Names are the last-resort identity for products saved without a barcode/SKU."""
    return ' '.join((value or '').split()).casefold()


def difference_needs_review(item):
    """Same rule the comparison uses: conflicts, duplicate records or name-only matches."""
    return bool(item['conflict'] or item['duplicates'] or item['unverified'])


def difference_matches(item, target, mode='all', branch_id=None):
    """Report only the rows a manager asked to see without losing the full summary."""
    if mode == 'missing_from_target':
        return target in item['missing']
    if mode == 'missing_from_branch':
        return branch_id in item['missing']
    if mode == 'needs_review':
        return difference_needs_review(item)
    return True


def filter_differences(differences, target, mode='all', branch_id=None):
    return [item for item in differences
            if difference_matches(item, target, mode, branch_id)]


def missing_counts(differences, branch_ids):
    """How many reported differences each compared branch is missing."""
    return {branch_id: sum(branch_id in item['missing'] for item in differences)
            for branch_id in branch_ids}


def difference_result_label(item, target):
    """One-word outcome used by the table badge and the exported reports."""
    if difference_needs_review(item):
        return 'Needs review'
    return 'Missing from target' if target in item['missing'] else 'Missing from comparison'


def compare_catalogs(rows, branch_ids):
    if not branch_ids:
        return {'counts': {}, 'differences': [],
                'summary': dict(missing_from_target=0, missing_from_comparisons=0, needs_review=0)}
    parents, identifiers = {}, {}

    def root(key):
        while parents[key] != key:
            parents[key] = parents[parents[key]]
            key = parents[key]
        return key

    for row in rows:
        parents[row.id] = row.id
        keys = []
        if row.catalog_id:
            keys.append(('catalog', row.catalog_id))
        barcode = (row.barcode or '').strip()
        sku = (row.sku or '').strip()
        if barcode:
            keys.append(('barcode', barcode))
        elif sku:
            keys.append(('sku', sku.casefold()))
        else:
            # Products saved without a barcode or SKU cannot be matched by an
            # identifier. Fall back to the normalized name so identical copies
            # carried by every branch are treated as one product instead of
            # being reported as a separate "needs review" row per branch.
            name = normalized_name(row.name)
            if name:
                keys.append(('name', name))
        for key in keys:
            other = identifiers.setdefault(key, row.id)
            parents[root(row.id)] = root(other)

    groups = {}
    for row in rows:
        groups.setdefault(root(row.id), []).append(row)

    counts = Counter(row.branch_id for row in rows)
    differences = []
    summary = dict(missing_from_target=0, missing_from_comparisons=0, needs_review=0)
    target = branch_ids[0]
    for group in groups.values():
        present = Counter(row.branch_id for row in group)
        missing = [branch_id for branch_id in branch_ids if branch_id not in present]
        duplicates = [branch_id for branch_id, count in present.items() if count > 1]
        barcodes = list(dict.fromkeys(row.barcode.strip() for row in group if row.barcode and row.barcode.strip()))
        conflict = len(barcodes) > 1
        # Without catalog ID, barcode or SKU the match is only a name match, so
        # a reported row still asks the manager to verify it manually.
        unverified = bool(missing) and not any(
            (row.barcode or '').strip() or (row.sku or '').strip() for row in group)
        # Only genuine differences are surfaced: a product carried identically
        # by every compared branch never reaches the result list.
        if not missing and not duplicates and not conflict:
            continue
        needs_review = conflict or bool(duplicates) or unverified
        summary['needs_review'] += needs_review
        if not needs_review:
            summary['missing_from_target'] += target in missing
            summary['missing_from_comparisons'] += target in present and bool(missing)
        representative = next((row for row in group if row.branch_id == target), group[0])
        differences.append(dict(
            name=representative.name,
            barcode=', '.join(barcodes),
            sku=', '.join(dict.fromkeys(row.sku for row in group if row.sku)),
            present=list(present), missing=missing, duplicates=duplicates,
            conflict=conflict, unverified=unverified,
            records=[dict(branch_id=row.branch_id, name=row.name, barcode=row.barcode, sku=row.sku)
                     for row in group] if conflict or duplicates else [],
        ))
    differences.sort(key=lambda item: (item['name'].casefold(), item['barcode'], item['sku']))
    return {'counts': {branch_id: counts[branch_id] for branch_id in branch_ids}, 'differences': differences,
            'summary': summary}

"""Compare branch catalog coverage without comparing quantities or changing records."""
from collections import Counter


def compare_catalogs(rows, branch_ids):
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
        if row.barcode and row.barcode.strip():
            keys.append(('barcode', row.barcode.strip()))
        elif row.sku and row.sku.strip():
            keys.append(('sku', row.sku.strip().casefold()))
        for key in keys:
            other = identifiers.setdefault(key, row.id)
            parents[root(row.id)] = root(other)

    groups = {}
    for row in rows:
        groups.setdefault(root(row.id), []).append(row)

    counts = Counter(row.branch_id for row in rows)
    differences = []
    for group in groups.values():
        present = Counter(row.branch_id for row in group)
        missing = [branch_id for branch_id in branch_ids if branch_id not in present]
        duplicates = [branch_id for branch_id, count in present.items() if count > 1]
        if not missing and not duplicates:
            continue
        differences.append(dict(
            name=group[0].name,
            barcode=', '.join(dict.fromkeys(row.barcode for row in group if row.barcode)),
            sku=', '.join(dict.fromkeys(row.sku for row in group if row.sku)),
            present=list(present), missing=missing, duplicates=duplicates,
        ))
    differences.sort(key=lambda item: (item['name'].casefold(), item['barcode'], item['sku']))
    return {'counts': {branch_id: counts[branch_id] for branch_id in branch_ids}, 'differences': differences}
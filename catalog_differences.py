"""Compare branch catalog coverage without comparing quantities or changing records."""
from collections import Counter


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
    summary = dict(missing_from_target=0, missing_from_comparisons=0, needs_review=0)
    target = branch_ids[0]
    for group in groups.values():
        present = Counter(row.branch_id for row in group)
        missing = [branch_id for branch_id in branch_ids if branch_id not in present]
        duplicates = [branch_id for branch_id, count in present.items() if count > 1]
        barcodes = list(dict.fromkeys(row.barcode.strip() for row in group if row.barcode and row.barcode.strip()))
        conflict = len(barcodes) > 1
        unverified = (len(present) == 1 and bool(missing) and
                      not any((row.barcode or '').strip() or (row.sku or '').strip() for row in group))
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
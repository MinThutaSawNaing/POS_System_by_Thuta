"""Legacy backups must initialize the additive delivery event table on restore."""
import io
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest


@pytest.mark.parametrize('legacy', [True, False])
def test_restore_preserves_deliveries_and_initializes_events(tmp_path, legacy):
    import app as m

    with m.app.app_context():
        database = Path(m.db.engine.url.database)
        # This integration test replaces its database. Never run on business data.
        if not any(part.startswith('pos-tests-') for part in database.parts):
            pytest.skip('Requires the disposable run_tests.py checkout')
        manager = m.User.query.filter_by(role='manager').first()
        manager_id, branch_id = manager.id, manager.branch_id
        if not branch_id:
            branch_id = m.Branch.query.filter_by(is_active=True).first().id
        delivery_count = m.Delivery.query.count()
        event_count = m.DeliveryEvent.query.count()
        backup = tmp_path / 'legacy.db'
        m.db.session.remove()
        with closing(sqlite3.connect(str(database))) as source:
            with closing(sqlite3.connect(str(backup))) as target:
                source.backup(target)
                if legacy:
                    for name in ('delivery_event', 'delivery_profile', 'delivery_proof'):
                        target.execute(f'DROP TABLE {name}')
                target.commit()
    client = m.app.test_client()
    with client.session_transaction() as session:
        session.update(user_id=manager_id, role='manager', branch_id=branch_id)
    response = client.post('/api/settings/database_restore', data={
        'database': (io.BytesIO(backup.read_bytes()), 'legacy.db'),
    }, content_type='multipart/form-data')
    assert response.status_code == 200, response.get_json()
    with m.app.app_context():
        assert m.inspect(m.db.engine).has_table('delivery_event')
        assert m.inspect(m.db.engine).has_table('delivery_profile')
        assert m.inspect(m.db.engine).has_table('delivery_proof')
        assert m.Delivery.query.count() == delivery_count
        assert m.DeliveryEvent.query.count() == (0 if legacy else event_count)
    assert client.get('/api/deliveries/stats').status_code == 200